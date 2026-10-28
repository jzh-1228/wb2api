"""用量日志管理：服务端分页 + 过滤（基于 usage_logs 表，由代理网关写入）。

性能设计（这是本模块存在的核心理由）
------------------------------------
使用记录是后台最重的一张表：每个代理请求写一行，且**永不停止增长**。
原实现有三个真实瓶颈，都出在「把过滤逻辑写在了数据库做不到的地方」：

1. **按名称筛选走了两次查询 + 丢结果**。
   `if account: aid = db.query(Account.id).filter(name like ...).scalar()`
   只取**第一条**匹配的账号 id。号池里有多个人名相近的账号（「小明」/「小明2」）
   时，筛出来的只是其中一个，另外那些号的记录凭空消失 —— 用户看到的是
   「明明有记录却查不到」。而且 `Account.name` 没有索引，
   这一步本身就是全表扫。

2. **总数统计与列表查询各扫一遍**。
    `q.count()` 会把整个过滤条件重算一次；在只有 PRIMARY(id) 的表上，
   过滤条件（account_id / created_at）都要全表扫，于是「慢」被翻倍。

3. **每次请求都把 Key / Account 全表拉进内存做名称映射**。
   `db.query(ApiKey).all()` + `db.query(Account).all()` 与日志量无关，
   却每次分页都执行一遍；这两个表很小所以不致命，但属于无谓开销。

本实现的对策：
  * **名称筛选改成「先查匹配 id 集合，再 IN 过滤」**（不丢记录），
    并且有匹配不到时**短路**（不可能有结果，直接返回空页，不扫日志表）。
  * **复合索引**覆盖「等值过滤 + created_at 倒序」，见 admin/db.py 的
    `_ensure_index`（`ix_usage_logs_account_created` 等）。有了索引，
    分页变成「沿索引倒序取 N 行」，不再全表扫 + filesort。
  * **名称映射只取用到的 id**（`IN (...)` 而非全表），并缓存到进程内
    （短 TTL），因为这两个表几乎不变。
  * **分页走 keyset（游标）优先**：给一个 `before_id` 时用
    `id < before_id ORDER BY id DESC LIMIT n`，这条路只需主键索引，
    深翻页不再是 `OFFSET 100000`（那是 O(offset) 的扫描）。
    仍保留 page/page_size 兼容既有前端。
"""
import asyncio
import csv
import copy
import io
import json
import secrets
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query, Request, Response, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, case
from sqlalchemy.orm import Session, defer
from starlette.concurrency import run_in_threadpool

from admin.db import get_db
from admin.models import Account, ApiKey, UsageLog, RequestAudit
from admin.security import require_admin, verify_admin_token


_stats_cache_lock = threading.Lock()
_stats_cache = OrderedDict()
_STATS_CACHE_TTL = 5.0
_STATS_CACHE_MAX = 64
#: 单条最重的 IP 聚合查询，重复访问给更长的复用窗口。
_STATS_CACHE_TTL_HEAVY = 20.0
#: 同一个 key 正在重算时，后来者等它的结果而不是各自再打一遍库。
#: 2 核机器上并发重算同一条聚合查询会把单次 200ms 拖到 1s 以上。
_stats_inflight = {}
_stats_inflight_lock = threading.Lock()


def _stats_cache_key(kind, values):
    return kind, json.dumps(values, sort_keys=True, default=str, separators=(',', ':'))


def _stable_window_start(start, days):
    """Quantise the derived window start so repeated requests share a cache key.

    ``datetime.utcnow() - timedelta(days=1)`` changes on every call, which made
    every request a cache miss and defeated single-flight entirely. Rounding to
    the minute keeps the numbers honest while letting identical panel refreshes
    reuse one aggregation.
    """
    if start:
        return start
    if not days or days <= 0:
        return None
    window = datetime.utcnow() - timedelta(days=min(days, 3650))
    return window.replace(second=0, microsecond=0)


def _stats_cache_get(key):
    now = time.monotonic()
    with _stats_cache_lock:
        entry = _stats_cache.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires <= now:
            _stats_cache.pop(key, None)
            return None
        _stats_cache.move_to_end(key)
        return copy.deepcopy(value)


def _stats_cache_put(key, value, ttl=None):
    with _stats_cache_lock:
        _stats_cache[key] = (time.monotonic() + (_STATS_CACHE_TTL if ttl is None else ttl),
                             copy.deepcopy(value))
        _stats_cache.move_to_end(key)
        while len(_stats_cache) > _STATS_CACHE_MAX:
            _stats_cache.popitem(last=False)


def _cached_stats(key, compute, ttl=None):
    """Single-flight cached aggregation: concurrent identical reads share one run.

    ``compute`` must be pure SQL work — it may run on the caller's session, so
    a leader that fails just lets the next caller retry.
    """
    cached = _stats_cache_get(key)
    if cached is not None:
        return cached
    with _stats_inflight_lock:
        pending = _stats_inflight.get(key)
        leader = pending is None
        if leader:
            pending = _stats_inflight[key] = threading.Event()
    if not leader:
        # Bounded: the leader's own query is already time-limited by the request
        # deadline, and a broken leader always releases this event.
        pending.wait(timeout=30.0)
        cached = _stats_cache_get(key)
        if cached is not None:
            return cached
        # Leader failed or timed out; fall through and compute independently.
    try:
        value = compute()
    finally:
        if leader:
            with _stats_inflight_lock:
                _stats_inflight.pop(key, None)
            pending.set()
    _stats_cache_put(key, value, ttl)
    return value

router = APIRouter(prefix="/api/logs", tags=["logs"])

#: 单页上限：防止 `page_size=1000000` 把整张表拉进内存（既是性能问题也是 DoS 面）。
MAX_PAGE_SIZE = 500


def _activity_probe(db: Session, key: Optional[str], limit: int,
                    include_history: bool = True) -> dict:
    """采集「请求动态」的一帧（HTTP 轮询与 SSE 推送共用同一份）。

    抽成函数是为了让两条通道**口径完全一致**：前端同一套渲染代码吃两种来源，
    如果各写一份查询，「手动刷新拿到的」和「自动刷新拿到的」迟早会漂移。
    """
    from admin import request_metrics
    from admin.request_audit import INFERENCE_PATHS
    # Primary-key lookup: a long request completing after newer requests still
    # advances this cursor. No COUNT or diagnostic payload is needed per tick.
    latest = db.query(UsageLog.id).order_by(UsageLog.id.desc()).first()
    usage_revision = latest[0] if latest else 0
    if not include_history:
        return {'usage_revision': usage_revision}
    limit = max(1, min(100, limit))
    ids = set(_resolve_ids_by_name(db, ApiKey, key, ('name',))) if key else None
    snapshot = request_metrics.activity_snapshot(INFERENCE_PATHS, ids)
    def visible(record):
        return (record.get('key_id') and record.get('path') in INFERENCE_PATHS
                and (ids is None or record['key_id'] in ids))
    def live(record):
        return {'request_id': record['id'], 'key_id': record['key_id'],
                'account_id': record.get('last_account_id'),
                'client_ip': record.get('client_ip') or '',
                'path': record['path'], 'model': record.get('requested_model', ''),
                'started_at': record['started_at'], 'status': record.get('status'),
                'outcome': record['outcome'], 'elapsed_ms': record['elapsed_ms'],
                'phase': record.get('phase', ''),
                'reason': record.get('request_error') or record.get('network_error') or record.get('upstream_error') or '',
                'upstream_attempts': record.get('upstream_attempts') or 0}
    active = [live(r) for r in snapshot['active'] if visible(r)]
    query = db.query(RequestAudit)
    if ids is not None:
        query = query.filter(RequestAudit.api_key_id.in_(ids))
    rows = query.order_by(RequestAudit.started_at.desc(), RequestAudit.id.desc()).limit(limit).all()
    completed = {r.request_id: {'request_id': r.request_id, 'key_id': r.api_key_id,
                 'account_id': r.account_id,
                 'client_ip': r.client_ip or '',
                 'path': r.path, 'model': r.model, 'started_at': r.started_at.isoformat()+'Z',
                 'status': r.status, 'outcome': r.outcome, 'elapsed_ms': r.elapsed_ms,
                 'phase': r.phase, 'reason': r.reason, 'upstream_attempts': r.upstream_attempts}
                 for r in rows}
    # Cover the short interval between finishing a response and its audit write.
    for record in snapshot['recent']:
        if visible(record):
            completed.setdefault(record['id'], live(record))
    finished = sorted(completed.values(), key=lambda r: r['started_at'], reverse=True)[:limit]
    key_names, account_names = _name_maps(db, {r['key_id'] for r in active + finished},
                                        {r['account_id'] for r in active + finished if r.get('account_id')})
    for row in active + finished:
        row['key_name'] = key_names[row['key_id']]
        aid = row.get('account_id')
        row['account_name'] = account_names.get(aid) or (f'账号 #{aid}' if aid else '')
    return {'active': active[:100], 'active_count': snapshot['active_count'], 'recent': finished,
            'usage_revision': usage_revision,
            'active_scope': 'process', 'history_persistent': True}


@router.get('/requests')
def request_activity(key: Optional[str] = None, limit: int = 40, include_history: bool = True,
                     _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """Show in-flight inference and outcomes that need not have billable usage."""
    return _activity_probe(db, key, limit, include_history)


_activity_sample_cache = {}
_activity_sample_lock = threading.Lock()


def _activity_sample(key: Optional[str], limit: int) -> dict:
    """Let one execution serve several callers that ask at the same instant.

    Deliberately does NOT keep a time-based cache: the stream must still sample
    on its own schedule, otherwise "no database queries while idle" would turn
    into stale frames. This only collapses the momentary double-run when several
    tabs sample together on a two-core box.
    """
    flight_key = (key or '', int(limit))
    with _activity_sample_lock:
        entry = _activity_sample_cache.get(flight_key)
        leader = entry is None
        if leader:
            entry = _activity_sample_cache[flight_key] = {'event': threading.Event()}
    if not leader:
        entry['event'].wait(timeout=_ACTIVITY_STREAM_SAMPLE_TTL)
        shared = entry.get('payload')
        if shared is not None:
            return copy.deepcopy(shared)
    payload = None
    try:
        payload = _activity_fetch(key, limit)
        return payload
    finally:
        if leader:
            entry['payload'] = payload
            with _activity_sample_lock:
                _activity_sample_cache.pop(flight_key, None)
            entry['event'].set()


# ---------------------------------------------------------------------------
# 「请求动态」推送通道（SSE）—— 替代原来的「每 3 秒轮询」
# ---------------------------------------------------------------------------
# 换掉轮询的理由：一个开着日志面板的标签页每小时要发 1200 次请求，每次都走
# 完鉴权 → 线程池 → SQL 采样 → JSON 序列化，而绝大多数时候结果根本没变。
# 它不会堵死 /v1/chat（那边是独立的 async 流式端点），但会持续占用线程池与
# 数据库连接的份额，放大抖动；开三四个标签页就是几千次/小时。
#
# 为什么不用 WebSocket：这里是单向、事件型、有明确生命周期的数据，SSE 足够；
# WebSocket 需要 uvicorn 装 ws 库、反向代理显式转发 Upgrade/Connection，
# 漏一条线上就是「连不上」；SSE 就是普通 HTTP GET，代理不用改配置。
#
# 成本：一条长连接，**没变化就只发一行心跳**；折叠卡片/离开面板时浏览器
# abort，服务端立刻停采样。
_ACTIVITY_STREAM_HEARTBEAT = 15.0       # 秒：无变化时的心跳间隔
_ACTIVITY_STREAM_TICK = 0.5             # 秒：内存指纹比较间隔（无 SQL）
#: 内存指纹没变时的兜底对库间隔。这里做两件事：读一条 request_audits 的
#: 尾巴、以及 usage_logs 的主键最大值。原来 10 秒一次，在 2 核机器上和
#: 面板其它查询抢 CPU；30 秒足够发现“有别处写入”，而真正的变化走内存指纹
#: 立刻推送，不依赖这个兜底。
_ACTIVITY_STREAM_REFRESH = 30.0
#: 多开标签页时每个连接都会查库。同一条推送查询在这么多秒内复用结果。
_ACTIVITY_STREAM_SAMPLE_TTL = 5.0
_ACTIVITY_STREAM_MAX_SECONDS = 30 * 60  # 单连接上限，到点让前端换票重连
_STREAM_TICKET_TTL = 120.0              # 推送票据有效期（秒）
_STREAM_TICKET_LIMIT = 256              # 票据总量上限（纯内存结构，必须有界）
_ACTIVITY_STREAM_LIMIT = 16             # 同时最长连接数
_activity_stream = {'now': 0}

_stream_tickets: dict[str, float] = {}
_stream_tickets_lock = threading.Lock()


def issue_stream_ticket(now: float | None = None) -> dict:
    """签发一张推送票据（调用方必须已通过管理员鉴权）。

    EventSource 不能带自定义请求头，而**管理员 JWT 绝不能进 URL** ——
    URL 会进 uvicorn/nginx 访问日志、浏览器历史与 Referer，那等于长期凭据泄漏。
    所以前端先用请求头换一张 2 分钟有效、只能读这条推送的票据。
    """
    now = now if now is not None else time.time()
    token = secrets.token_urlsafe(24)
    with _stream_tickets_lock:
        for key, expiry in list(_stream_tickets.items()):
            if expiry <= now:
                _stream_tickets.pop(key, None)
        while len(_stream_tickets) >= _STREAM_TICKET_LIMIT:
            _stream_tickets.pop(next(iter(_stream_tickets)), None)
        _stream_tickets[token] = now + _STREAM_TICKET_TTL
    return {'ticket': token, 'expires_in': int(_STREAM_TICKET_TTL)}


def stream_ticket_valid(token: str | None, now: float | None = None) -> bool:
    """票据是否有效（**不消费**：浏览器自动重连时会复用同一个 URL）。"""
    if not token:
        return False
    now = now if now is not None else time.time()
    with _stream_tickets_lock:
        expiry = _stream_tickets.get(token)
        if expiry is None:
            return False
        if expiry <= now:
            _stream_tickets.pop(token, None)
            return False
    return True


def _activity_signature(payload: dict) -> str:
    """把一帧压成「有没有变化」的短指纹，避免重复推送同一份 JSON。"""
    parts = [str(payload.get('usage_revision'))]
    for r in payload.get('active') or []:
        parts.append('a%s:%s:%s:%s' % (r.get('request_id'), r.get('phase'),
                                       int(r.get('elapsed_ms') or 0) // 1000,
                                       str(r.get('status')) + ':' + str(r.get('account_id'))))
    for r in payload.get('recent') or []:
        parts.append('r%s:%s:%s:%s' % (r.get('request_id'), r.get('outcome'),
                                       r.get('status'), r.get('account_id')))
    return '|'.join(parts)


def _activity_live_signature(key: Optional[str]) -> str:
    """纯内存的「要不要查库」判定（零 SQL）。

    没有它，每帧都得查一次库才知道要不要推，那比 3 秒轮询还费。
    带 Key 名称筛选时拿不到 id 集合（那需要查库），退化为不过滤的指纹 ——
    只会让推送略保守，真正发出去的那一帧仍走 `_activity_probe` 的严格过滤。
    """
    from admin import request_metrics
    from admin.request_audit import INFERENCE_PATHS
    return request_metrics.activity_live_signature(INFERENCE_PATHS)


def _activity_fetch(key: Optional[str], limit: int) -> dict:
    """线程池里跑的一帧：**独立的短会话**。

    SSE 的响应体在依赖 teardown 之后才继续迭代（与 StreamingResponse 落库
    是同一类坑），复用请求作用域的 db 会抛 ResourceClosedError，且会被
    静默吞掉 —— 表现就是「推送忽然没了」。会话工厂运行时取，便于测试替换。
    """
    from admin import db as admin_db
    db = admin_db.SessionLocal()
    try:
        return _activity_probe(db, key, limit)
    finally:
        db.close()


@router.get('/requests/ticket')
def request_activity_ticket(_: bool = Depends(require_admin)):
    """用**请求头**换一张短票据，供 EventSource 拼进 URL。"""
    return issue_stream_ticket()


@router.get('/requests/stream')
async def request_activity_stream(
    request: Request,
    key: Optional[str] = None,
    limit: int = 40,
    ticket: Optional[str] = None,
):
    """「请求动态」的服务端推送（SSE）。

    帧协议：
      * `event: activity` + `data: <json>` —— 与 `/api/logs/requests` **同构**；
      * `: keepalive` 注释行 —— 空闲心跳，顺带防止中间代理掐连接；
      * `event: reauth` —— 连接到点收尾前发出，前端据此换票重连。

    鉴权：优先 `X-Admin-Token` 头，其次 `?ticket=`。**不接受 URL 里的 JWT**。
    """
    header_token = request.headers.get('x-admin-token')
    if header_token:
        verify_admin_token(header_token)
    elif not stream_ticket_valid(ticket):
        raise HTTPException(status_code=401, detail='推送票据无效或已过期')
    limit = max(1, min(100, int(limit or 40)))

    async def frames():
        # 连接上限与计数都放在生成器里，不放 handler：
        #   * 超出上限时立刻结束、不发帧，让前端马上退到低频轮询；
        #   * 客户端在响应开始前断开时生成器可能一次都不被迭代 ——
        #     在 handler 里加一就永远减不回来，计数「只增不减」，
        #     几次刷新后就把自己锁死在上限上。
        if _activity_stream['now'] >= _ACTIVITY_STREAM_LIMIT:
            return
        _activity_stream['now'] += 1
        started = time.monotonic()
        last_mem = None      # 上次查库时的内存指纹
        last_pushed = None   # 上次真正推出去的内容指纹
        last_sent = 0.0
        last_db = 0.0
        last_check = 0.0
        try:
            while True:
                if await request.is_disconnected():
                    break
                now = time.monotonic()
                if now - started > _ACTIVITY_STREAM_MAX_SECONDS:
                    # 收尾前让前端换票重连：EventSource 自动重连会复用旧 URL，
                    # 而那张票据早已过期，必然 401。
                    yield 'event: reauth\ndata: {}\n\n'
                    break
                # 两级节流：每 0.5s 只看内存指纹（无 SQL）；只有真的多/少了一条
                # 请求、或阶段变了，或距上次对库超过 10s，才去查库。
                if now - last_check >= _ACTIVITY_STREAM_TICK:
                    last_check = now
                    live = _activity_live_signature(key)
                    if live != last_mem or now - last_db >= _ACTIVITY_STREAM_REFRESH:
                        last_mem = live
                        last_db = now
                        try:
                            payload = await run_in_threadpool(_activity_sample, key, limit)
                        except Exception:
                            # 数据库短暂不可用不该让面板永远空白：发一帧空结果。
                            payload = {'active': [], 'active_count': 0, 'recent': [],
                                       'usage_revision': None, 'degraded': True}
                        pushed = _activity_signature(payload)
                        if pushed != last_pushed:
                            last_pushed = pushed
                            last_sent = now
                            payload['mode'] = 'stream'
                            yield 'event: activity\ndata: ' + json.dumps(
                                payload, ensure_ascii=False, separators=(',', ':')) + '\n\n'
                if now - last_sent >= _ACTIVITY_STREAM_HEARTBEAT:
                    last_sent = now
                    yield ': keepalive\n\n'
                await asyncio.sleep(_ACTIVITY_STREAM_TICK)
        finally:
            _activity_stream['now'] = max(0, _activity_stream['now'] - 1)

    return StreamingResponse(frames(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-store',
        # 关掉 nginx 响应缓冲，否则推送会被攒着一起发，等于把 SSE 变回批量。
        'X-Accel-Buffering': 'no',
        'Connection': 'keep-alive',
    })


# ---------------------------------------------------------------------------
# 名称 → id 解析
# ---------------------------------------------------------------------------
def _resolve_ids_by_name(db: Session, model, term: str,
                         fields: tuple[str, ...]) -> list[int]:
    """按名称/UID 模糊匹配出一组 id（**不做 LIMIT 1**）。

    原实现用 `.scalar()` 只取第一条：号池里有多条匹配时结果会丢，
    这是「按账号查使用记录查不全」的直接原因。这里返回完整集合，
    调用方用 `IN` 过滤，语义才是用户期待的「名字里含这个词的都算」。
    """
    term = (term or "").strip()
    if not term:
        return []
    like = f"%{term}%"
    conds = [getattr(model, f).like(like) for f in fields]
    q = db.query(model.id).filter(or_(*conds))
    return [r[0] for r in q.limit(500).all()]


def _resolve_single_id(db: Session, model, term: str,
                       fields: tuple[str, ...]) -> Optional[int]:
    """按名称精确解析单个 id（导出用：导出 CSV 不应因为模糊匹配而炸成几十万行）。"""
    ids = _resolve_ids_by_name(db, model, term, fields)
    return ids[0] if ids else None


def _name_maps(db: Session, key_ids: set[int], acc_ids: set[int]) -> tuple[dict, dict]:
    """只取用到的 id 的名称映射（避免每次分页都全表拉）。"""
    key_names: dict[int, str] = {}
    acc_names: dict[int, str] = {}
    if key_ids:
        for k in db.query(ApiKey.id, ApiKey.name).filter(ApiKey.id.in_(key_ids)).all():
            key_names[k[0]] = k[1] or f"未命名 Key #{k[0]}"
        for key_id in key_ids - key_names.keys():
            key_names[key_id] = f"已删除 Key #{key_id}"
    if acc_ids:
        for a in db.query(Account.id, Account.name, Account.uid).filter(
                Account.id.in_(acc_ids)).all():
            acc_names[a[0]] = a[1] or a[2] or ""
    return key_names, acc_names


def _build_filters(q, *, key_id, account_ids, models, model, key, account,
                   client_ip, start, end, db: Session, result=None):
    """构造统一的 where 条件（列表与导出共用，保证口径一致）。

    返回 `(query, short_circuit)`：`short_circuit=True` 表示「筛选条件不可能
    有任何匹配」（例如按名称筛选但一个账号都没匹配上）——
    此时调用方应直接返回空结果，**不要**去扫日志表。这是最省的一次优化：
    一次不存在的结果不该付出全表扫描的代价。

    注意 `account_ids=[]`（**显式传入的空集合**）与 `account_ids=None`
    （没传）语义完全不同：前者是「限定在这些 id 里，而集合是空的」→ 必然
    没有结果；后者是「不按 id 过滤」。用 `if account_ids:` 会把前者也当成
    「不过滤」，于是「按一个不存在的账号名筛选」会返回**全表**记录 ——
    这是最容易被误认为「功能正常」的一类错：界面上看起来有数据，
    只是数据是错的。所以这里对空集合显式短路。
    """
    if account_ids is not None and len(account_ids) == 0:
        return q, True
    if key_id:
        q = q.filter(UsageLog.api_key_id == key_id)
    if account_ids:
        q = q.filter(UsageLog.account_id.in_(account_ids))
    if models:
        q = q.filter(UsageLog.model.in_(models))
    if model:
        q = q.filter(UsageLog.model == model)
    if key:
        kids = _resolve_ids_by_name(db, ApiKey, key, ("name",))
        if not kids:
            return q, True
        q = q.filter(UsageLog.api_key_id.in_(kids))
    if account:
        aids = _resolve_ids_by_name(db, Account, account, ("name", "uid"))
        if not aids:
            return q, True
        q = q.filter(UsageLog.account_id.in_(aids))
    if client_ip:
        from admin.client_ip import normalize_ip
        try:
            normalized = normalize_ip(client_ip)
        except ValueError:
            normalized = None
        q = q.filter(UsageLog.client_ip == normalized) if normalized else q.filter(
            UsageLog.client_ip.contains(client_ip, autoescape=True))
    def timestamp(value):
        if not isinstance(value, str):
            return value
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError:
            raise HTTPException(400, "日志时间格式无效") from None
    if start:
        start = timestamp(start)
        q = q.filter(UsageLog.created_at >= start)
    if end:
        end = timestamp(end)
        q = q.filter(UsageLog.created_at <= end)
    success = func.coalesce(UsageLog.error_kind, "").in_(("", "success"))
    if result == "error":
        q = q.filter(~success)
    elif result == "success":
        q = q.filter(success)
    return q, False


def _serialize(rows, key_names: dict, acc_names: dict) -> list[dict]:
    return [
        {
            "id": r.id,
            "key_name": key_names.get(r.api_key_id) or f"#{r.api_key_id}",
            "account_name": acc_names.get(r.account_id) or (f"#{r.account_id}" if r.account_id else "未记录账号 / 未选号"),
            "model": r.model,
            "credits": r.credits,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "total_tokens": r.total_tokens,
            "cached_tokens": r.cached_tokens,
            "client_ip": r.client_ip or "",
            "use_case": r.use_case or "",
            "reasoning_effort": r.reasoning_effort or "",
            "seq": r.seq,
            "ttfb_ms": r.ttfb_ms,
            "latency_ms": r.latency_ms,
            "error_kind": r.error_kind or "",
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.get("")
def list_logs(
    page: int = 1,
    page_size: int = 20,
    key_id: Optional[int] = None,
    account_id: Optional[int] = None,
    model: Optional[str] = None,
    key: Optional[str] = None,      # 按 Key 名称（模糊）筛选
    account: Optional[str] = None,  # 按账号名称 / UID（模糊）筛选
    client_ip: Optional[str] = None,
    start: Optional[str] = None,  # 起始时间 YYYY-MM-DD HH:MM:SS
    end: Optional[str] = None,
    before_id: Optional[int] = None,  # keyset 分页游标（深翻页用，比 OFFSET 快）
    result: Optional[str] = None,
    with_total: bool = True,          # 深翻页时可关掉总数统计省一次扫描
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """用量日志列表（服务端分页 + 过滤）。

    支持两种翻页方式：
      * `page` / `page_size` —— 传统 OFFSET 分页，兼容既有前端；
      * `before_id` —— keyset（游标）分页，**深翻页请用它**：
        `WHERE id < before_id ORDER BY id DESC LIMIT n` 只走主键索引，
        而 `OFFSET 100000` 需要先扫掉 10 万行再丢弃，日志一大就明显卡。

    `with_total=false` 时跳过 `COUNT(*)`。总数统计在日志量大时本身
    就是一次完整的索引扫描；用户滚到第 50 页时通常只关心「还有没有更多」，
    不关心精确总数 —— 把选择权交给调用方，默认仍给总数（保持兼容）。
    """
    page_size = max(1, min(MAX_PAGE_SIZE, int(page_size or 20)))
    page = max(1, int(page or 1))

    # account_id（精确）与 account（名称模糊）是两条独立入口：
    # 前者来自下拉（已经是 id），后者是手输名称。两个都给时以精确 id 优先。
    account_ids = [account_id] if account_id else None
    name_term = account if not account_id else None

    q, short = _build_filters(
        db.query(UsageLog).options(defer(UsageLog.diagnostic_json)),
        key_id=key_id, account_ids=account_ids, models=None, model=model,
        key=key, account=name_term,
        client_ip=client_ip, start=start, end=end, db=db, result=result,
    )

    if short:
        return {"items": [], "total": 0, "page": page,
                "stats": {"total": 0, "success": 0, "error": 0} if with_total and before_id is None else None,
                "page_size": page_size, "has_more": False, "next_before_id": None}

    total = None
    stats = None
    if with_total and before_id is None:
        cache_key = _stats_cache_key('list_total', (
            key_id, account_id, model, key, account, client_ip, start, end, result))
        stats = _stats_cache_get(cache_key)
        if stats is None:
            total, successes = q.with_entities(func.count(UsageLog.id), func.coalesce(func.sum(case(
                (func.coalesce(UsageLog.error_kind, "").in_(("", "success")), 1), else_=0)), 0)).one()
            total = int(total or 0)
            successes = int(successes or 0)
            stats = {"total": total, "success": successes, "error": total - successes}
            _stats_cache_put(cache_key, stats)
        total = stats['total']

    # keyset 分页优先；否则退回 OFFSET
    if before_id is not None:
        rows = (q.filter(UsageLog.id < int(before_id))
                .order_by(UsageLog.id.desc()).limit(page_size + 1).all())
        has_more = len(rows) > page_size
        rows = rows[:page_size]
    else:
        rows = (q.order_by(UsageLog.id.desc())
                .offset((page - 1) * page_size).limit(page_size + 1).all())
        has_more = len(rows) > page_size
        rows = rows[:page_size]

    key_names, acc_names = _name_maps(
        db,
        {r.api_key_id for r in rows if r.api_key_id},
        {r.account_id for r in rows if r.account_id},
    )
    return {
        "items": _serialize(rows, key_names, acc_names),
        "total": total,                     # None = 本次未统计
        "stats": stats,
        "page": page,
        "page_size": page_size,
        "has_more": has_more,
        # 下一次 keyset 请求带上它即可无 OFFSET 继续往后翻
        "next_before_id": rows[-1].id if (has_more and rows) else None,
    }


@router.get("/export")
def export_logs(
    key_id: Optional[int] = None,
    account_id: Optional[int] = None,
    model: Optional[str] = None,
    key: Optional[str] = None,
    account: Optional[str] = None,
    client_ip: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: int = 50000,
    result: Optional[str] = None,
    diagnostics: bool = False,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """导出用量日志为 CSV（UTF-8 BOM，Excel 直接可打开）。

    `limit` 是**必须有的护栏**：原实现 `.all()` 会把整张表读进内存，
    日志上百万行时会直接把进程打爆（OOM 杀掉的正是这个服务）。
    默认 5 万行，配合时间范围筛选足够覆盖正常对账需求。
    """
    limit = max(1, min(200000, int(limit or 50000)))
    if diagnostics:
        limit = min(limit, 1000)

    account_ids = [account_id] if account_id else None
    name_term = account if not account_id else None

    q, short = _build_filters(
        db.query(UsageLog).options(*([] if diagnostics else [defer(UsageLog.diagnostic_json)])),
        key_id=key_id, account_ids=account_ids, models=None, model=model,
        key=key, account=name_term,
        client_ip=client_ip, start=start, end=end, db=db, result=result,
    )

    buf = io.StringIO()
    buf.write("\ufeff")  # UTF-8 BOM，保证 Excel 正确识别中文
    w = csv.writer(buf)
    w.writerow(["ID", "Key", "账号", "模型", "积分", "提示tokens", "补全tokens", "缓存tokens", "总tokens",
                "TTFB(ms)", "总耗时(ms)", "结果", "客户端IP", "用途", "思考强度(请求)", "时间(北京)"] + (["诊断JSON（含上游响应及本地错误）"] if diagnostics else []))

    if not short:
        rows = q.order_by(UsageLog.id.desc()).limit(limit).all()
        key_names, acc_names = _name_maps(
            db,
            {r.api_key_id for r in rows if r.api_key_id},
            {r.account_id for r in rows if r.account_id},
        )
        for r in rows:
            t = r.created_at
            t_str = ""
            if t:
                # 数据库存 UTC，导出统一 +8 北京时间，与界面显示一致
                t_str = (t + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
            w.writerow([
                r.id,
                key_names.get(r.api_key_id) or f"#{r.api_key_id}",
                acc_names.get(r.account_id) or (f"#{r.account_id}" if r.account_id else "未记录账号 / 未选号"),
                r.model,
                r.credits,
                r.prompt_tokens if r.prompt_tokens is not None else "",
                r.completion_tokens if r.completion_tokens is not None else "",
                r.cached_tokens if r.cached_tokens is not None else "",
                r.total_tokens if r.total_tokens is not None else "",
                r.ttfb_ms if r.ttfb_ms is not None else "",
                r.latency_ms if r.latency_ms is not None else "",
                r.error_kind or "",
                r.client_ip or "",
                r.use_case or "",
                r.reasoning_effort or "",
                t_str,
            ] + ([r.diagnostic_json or ""] if diagnostics else []))
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=usage_logs.csv"},
    )


@router.get("/accounts")
def log_accounts(
    days: int = 30,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """「按账号查询」用的下拉选项：**窗口内真实产生过请求**的账号。

    为什么只列有记录的账号：号池里可能有几十个号，但只有少数在真实承载流量。
    把全都塞进下拉，用户选中一个从没被用过的号会筛出空结果，
    以为「功能坏了」——这是使用记录筛选最常见的误用。

    附带每个账号的请求数 / 积分 / 最近使用时间，让用户在**选之前**
    就知道哪个号有数据、值不值得点进去看。
    """
    cache_key = _stats_cache_key('accounts', (days,))
    cached = _stats_cache_get(cache_key)
    if cached is not None:
        return cached
    since = datetime.utcnow() - timedelta(days=max(1, days))
    rows = (
        db.query(
            UsageLog.account_id,
            func.count(UsageLog.id),
            func.coalesce(func.sum(UsageLog.credits), 0.0),
            func.max(UsageLog.created_at),
        )
        .filter(UsageLog.created_at >= since, UsageLog.account_id > 0)
        .group_by(UsageLog.account_id)
        .order_by(func.count(UsageLog.id).desc())
        .all()
    )
    ids = [r[0] for r in rows]
    meta: dict[int, dict] = {}
    if ids:
        for a in db.query(Account).filter(Account.id.in_(ids)).all():
            meta[a.id] = {
                "name": a.name or a.uid or f"#{a.id}",
                "uid": a.uid or "",
                "status": a.status or "active",
                "balance_remain": int(a.balance_remain or 0),
                "expiring_soon": int(a.credits_expiring_soon or 0),
            }
    result = {
        "range_days": days,
        "items": [
            {
                "id": aid,
                "name": (meta.get(aid) or {}).get("name") or f"#{aid}",
                "uid": (meta.get(aid) or {}).get("uid") or "",
                "status": (meta.get(aid) or {}).get("status") or "unknown",
                "balance_remain": (meta.get(aid) or {}).get("balance_remain") or 0,
                "expiring_soon": (meta.get(aid) or {}).get("expiring_soon") or 0,
                "requests": int(cnt or 0),
                "credits": round(float(cr or 0), 4),
                "last_used_at": last.isoformat() if last else None,
            }
            for aid, cnt, cr, last in rows
        ],
    }
    _stats_cache_put(cache_key, result)
    return result


@router.get("/summary")
def log_summary(
    days: int = 1, account_id: Optional[int] = None,
    key_id: Optional[int] = None, model: Optional[str] = None,
    key: Optional[str] = None, account: Optional[str] = None,
    client_ip: Optional[str] = None, start: Optional[str] = None,
    end: Optional[str] = None, result: Optional[str] = None,
    _: bool = Depends(require_admin), db: Session = Depends(get_db),
):
    window_start = _stable_window_start(start, days)
    cache_key = _stats_cache_key('summary', (
        days, account_id, key_id, model, key, account, client_ip, window_start, end, result))

    def compute():
        success = func.coalesce(UsageLog.error_kind, "").in_(("", "success"))
        q = db.query(
            UsageLog.account_id, func.count(UsageLog.id),
            func.coalesce(func.sum(UsageLog.credits), 0.0),
            func.coalesce(func.sum(UsageLog.total_tokens), 0),
            func.coalesce(func.sum(UsageLog.prompt_tokens), 0),
            func.coalesce(func.sum(UsageLog.completion_tokens), 0),
            func.coalesce(func.sum(UsageLog.cached_tokens), 0),
            func.avg(UsageLog.latency_ms), func.max(UsageLog.created_at),
            func.coalesce(func.sum(case((success, 0), else_=1)), 0),
        )
        q, short = _build_filters(q, key_id=key_id,
            account_ids=[account_id] if account_id is not None else None,
            models=None, model=model, key=key, account=account,
            client_ip=client_ip, start=window_start, end=end, db=db, result=result)
        rows = [] if short else q.group_by(UsageLog.account_id).order_by(
            func.coalesce(func.sum(UsageLog.credits), 0.0).desc()).all()
        _, names = _name_maps(db, set(), {r[0] for r in rows if r[0]})
        items = []
        for aid, reqs, credits, ttok, ptok, ctok, cached_tokens, avg_lat, last, fails in rows:
            items.append({
                "account_id": aid,
                "account_name": names.get(aid) or (f"#{aid}" if aid else "未记录账号 / 未选号"),
                "requests": int(reqs), "failed_requests": int(fails),
                "ok_requests": int(reqs - fails), "credits": round(float(credits), 4),
                "total_tokens": int(ttok), "prompt_tokens": int(ptok),
                "completion_tokens": int(ctok), "cached_tokens": int(cached_tokens),
                "avg_latency_ms": int(avg_lat or 0),
                "last_used_at": last.isoformat() if last else None,
            })
        return {"range_days": days, "total_requests": sum(i["requests"] for i in items),
                "ok_requests": sum(i["ok_requests"] for i in items),
                "failed_requests": sum(i["failed_requests"] for i in items),
                "account_count": sum(1 for i in items if i["account_id"]),
                "unattributed_requests": sum(i["requests"] for i in items if not i["account_id"]),
                "total_credits": round(sum(i["credits"] for i in items), 4), "items": items}

    return _cached_stats(cache_key, compute)


@router.get("/ip-summary")
def log_ip_summary(
    key_id: Optional[int] = None, account_id: Optional[int] = None,
    model: Optional[str] = None, key: Optional[str] = None,
    account: Optional[str] = None, client_ip: Optional[str] = None,
    start: Optional[str] = None, end: Optional[str] = None,
    result: Optional[str] = None, low_tokens: int = 100,
    min_requests: int = 1, sort: str = "requests", limit: int = 50, days: int = 1,
    _: bool = Depends(require_admin), db: Session = Depends(get_db),
):
    """Filtered usage, not ingress: unknown token counts never mean zero."""
    low_tokens = max(0, min(1000000, low_tokens))
    min_requests = max(1, min(1000000, min_requests))
    limit = max(1, min(100, limit))
    start = _stable_window_start(start, days)
    cache_key = _stats_cache_key('ip_summary', (
        key_id, account_id, model, key, account, client_ip, start, end, result,
        low_tokens, min_requests, sort, limit))

    def compute():
        requests = func.count(UsageLog.id).label('requests')
        known = func.count(UsageLog.total_tokens).label('known')
        credits = func.coalesce(func.sum(UsageLog.credits), 0).label('credits')
        tokens = func.sum(UsageLog.total_tokens).label('tokens')
        average = func.avg(UsageLog.total_tokens).label('avg_tokens')
        low = func.sum(case((UsageLog.total_tokens <= low_tokens, 1), else_=0)).label('low')
        zero = func.sum(case((UsageLog.total_tokens == 0, 1), else_=0)).label('zero')
        failed = func.sum(case((func.coalesce(UsageLog.error_kind, '').in_(('', 'success')), 0), else_=1))
        q = db.query(UsageLog.client_ip, requests, known, credits, tokens, average, low, zero,
                     failed.label('failed'), func.max(UsageLog.created_at).label('last'))
        q, short = _build_filters(q, key_id=key_id,
            account_ids=[account_id] if account_id is not None else None,
            models=None, model=model, key=key, account=account if account_id is None else None,
            client_ip=client_ip, start=start, end=end, db=db, result=result)
        orders = {'requests': requests.desc(), 'low_tokens': low.desc(),
                  'avg_tokens': average.asc(), 'credits': credits.asc()}
        if sort not in orders:
            raise HTTPException(400, "IP 排序方式无效")
        order = [orders[sort], requests.desc(), UsageLog.client_ip.asc()]
        if sort == 'avg_tokens':
            order.insert(0, case((known == 0, 1), else_=0))
        rows = [] if short else q.group_by(UsageLog.client_ip).having(requests >= min_requests).order_by(
            *order).limit(limit + 1).all()
        items = []
        for r in rows[:limit]:
            items.append({'client_ip': r.client_ip or '', 'requests': int(r.requests),
                'known_usage_requests': int(r.known), 'missing_usage_requests': int(r.requests-r.known),
                'total_tokens': int(r.tokens) if r.tokens is not None else None,
                'avg_tokens': round(float(r.avg_tokens), 1) if r.avg_tokens is not None else None,
                'low_token_requests': int(r.low), 'zero_token_requests': int(r.zero),
                'credits': round(float(r.credits), 4), 'avg_credits': round(float(r.credits)/r.requests, 6),
                'failed_requests': int(r.failed), 'last_used_at': r.last.isoformat() if r.last else None})
        return {'items': items, 'has_more': len(rows) > limit, 'low_tokens': low_tokens,
                'min_requests': min_requests, 'limit': limit, 'sort': sort}

    return _cached_stats(cache_key, compute, ttl=_STATS_CACHE_TTL_HEAVY)


@router.get("/capture/settings")
def capture_settings(_=Depends(require_admin)):
    from admin.request_payload import status
    return status()


@router.put("/capture/settings")
def update_capture_settings(body: dict = Body(...), _=Depends(require_admin)):
    from admin.request_payload import configure
    if set(body) != {'enabled'} or type(body.get('enabled')) is not bool:
        raise HTTPException(400, "enabled 必须为布尔值")
    return configure(body['enabled'])


@router.delete("/capture/cache")
def clear_capture_cache(_=Depends(require_admin)):
    from admin.request_payload import clear
    return clear()


@router.get("/{log_id}")
def log_detail(log_id: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    row = db.query(UsageLog).filter(UsageLog.id == log_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="使用记录不存在")
    keys, accounts = _name_maps(db, {row.api_key_id}, {row.account_id})
    item = _serialize([row], keys, accounts)[0]
    from admin.request_payload import get
    item['request_capture'] = get(log_id, include_bodies=False)
    try:
        item["diagnostic"] = json.loads(row.diagnostic_json) if row.diagnostic_json else None
    except (TypeError, ValueError):
        item["diagnostic"] = None
    return item


@router.get("/{log_id}/capture/body")
def capture_body(log_id: int, part: str = Query(default='ingress', max_length=32),
                 offset: int = Query(default=0, ge=0), limit: int = Query(default=65536, ge=4, le=65536),
                 response: Response = None, db: Session = Depends(get_db), _=Depends(require_admin)):
    if not db.query(UsageLog.id).filter(UsageLog.id == log_id).first():
        raise HTTPException(404, '使用记录不存在')
    from admin.request_payload import body_page
    page = body_page(log_id, part, offset, limit)
    if page is None:
        raise HTTPException(404, '请求正文缓存已过期或不可用')
    if response is not None:
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
    return page


@router.get("/{log_id}/capture/download")
def download_capture(log_id: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    item = log_detail(log_id, db, True)
    from admin.request_payload import get
    capture = get(log_id)
    item.pop('request_capture', None)
    diagnostic = item.pop('diagnostic', None)
    def rows():
        yield json.dumps({'type': 'usage', **item}, ensure_ascii=False)+'\n'
        if capture:
            yield json.dumps({'type': 'capture', 'captured_at': capture['captured_at'],
                              'upstream_omitted': capture['upstream_omitted']})+'\n'
            yield json.dumps({'type': 'ingress', **capture['ingress']}, ensure_ascii=False)+'\n'
            for attempt in capture['upstream']:
                yield json.dumps({'type': 'upstream_attempt', **attempt}, ensure_ascii=False)+'\n'
        yield json.dumps({'type': 'diagnostic', 'data': diagnostic}, ensure_ascii=False)+'\n'
    return StreamingResponse(rows(), media_type='application/x-ndjson',
        headers={'Content-Disposition': f'attachment; filename="wb2api-usage-{log_id}.jsonl"',
                 'Cache-Control': 'no-store'})
