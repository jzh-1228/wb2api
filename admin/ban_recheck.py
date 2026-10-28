"""风控封号（11140 等 account_fault）自动复核与解封。

为什么需要这个模块
------------------
1. ``_classify_error`` 把 11140/14017/14015/14016 归为 ``account_fault``，
   处置是**冷却 30 分钟**，冷却到期后账号自然回到选号池 —— 这条路本身
   会自动恢复。但两条相邻路径不会：
     * 风控封号常常连 token 刷新一起拒（401/403 / invalid_grant），
       于是账号被 ``session_dead`` **永久禁用**（status=disabled），
       而保活任务会跳过非 active 账号 —— 没有任何东西再碰它，
       唯一的出路是人工逐个点启用。这就是「自动解封还没做进去」。
     * 现有探测（保活的 fetch_models、登录校验的 fetch_balance）打的都是
       余额/模型列表接口 —— 这些接口对风控号**常常照样放行**，探测「成功」
       证明不了「还能用模型」。
2. 所以复核先真调余额/账号信息这两条已认证端点；任一成功就证明凭证仍
   有效并自动恢复。只有两条都明确 401/403 才记录为登录态失效。可信探测
   遇到网关/网络异常时，才退回一次最小模型请求作补充证据。

做什么
------
``run_ban_recheck``（由调度器每 30 分钟跑一次，也可在后台手动触发）：

* 候选 = 已禁用（人工停用除外）+ 冷却到期的 account_fault 号；
* 逐号先探测余额/账号信息，必要时再做真实模型探测（只带账号凭据和明确的会话标识）；
  - 通过 → 自动解封：恢复 active、清全部失败计数与冷却；
  - 仍 11140/授权类 → 维持禁用/重新冷却，复核间隔指数退避（封顶可配）；
  - 登录态死亡 → 维持禁用，等人工重登（这不是风控，探测无意义）；
  - 网络抖动 → 状态不动，本轮白跑，不算失败次数。

防滥用上游：每号探测间隔 ≥ BAN_RECHECK_MIN_INTERVAL（默认 30 分钟），
连续仍封则 ×2 退避至 BAN_RECHECK_MAX_INTERVAL（默认 6 小时）封顶；
WAF 出口保护期间整体跳过；与前台对话共用单号在途租约。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from admin.config import settings
from admin.pool import POOL

_logger = logging.getLogger("admin.ban_recheck")

#: 进程内退避台账：account_id -> [连续封号次数, 上次探测时间戳]。
#: 部署是单进程（main.py 起一个 uvicorn），重启后从最小间隔重新开始，
#: 最多多探一次，可接受 —— 不值得为它加一列数据库迁移。
_backoff: dict[int, list] = {}
_backoff_lock = threading.Lock()


def _probe_model(db) -> str | None:
    """挑探测用的模型：优先免费非图像模型，其次任一启用模型。"""
    from admin.routers.models import _get_enabled_models, _get_free_models
    free = _get_free_models(db)
    if free:
        return sorted(free)[0]
    enabled = _get_enabled_models(db)
    return sorted(enabled)[0] if enabled else None


def _probe_headers(sess, uid: str, body: dict) -> dict:
    """Preserve only an explicit conversation identifier from the probe body."""
    del sess
    h = {}
    try:
        from admin.routers.proxy import _session_headers
        h.update(_session_headers(body, uid))
    except Exception:
        pass
    return h


def probe_model_capability(auth_json: str, uid: str, model: str) -> dict:
    """对一个账号发 1 token 的真实对话请求，判定「还能不能用模型」。

    返回 ``{"ok": bool, "kind": str, "code": str|None, "message": str}``。
    ok=True 仅代表对话端点放行（风控解除）；网络错误返回 kind="transport"，
    调用方不应据此改变账号状态。
    """
    import httpx
    from admin import backend, upstream_errors
    from admin.routers.proxy import _classify_error, _extract_http_status

    body = {"model": model, "stream": True, "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}]}
    try:
        sess = backend.AccountSession(auth_json)
        try:
            headers = sess.get_headers(extra=_probe_headers(sess, uid, body))
            with sess.http_client(timeout=20) as client:
                with client.stream("POST", f"{settings.BACKEND}/v2/chat/completions",
                                   headers=headers, json=body) as r:
                    if r.status_code >= 400:
                        detail = r.read()
                        text = detail[:65536].decode(errors="ignore") if isinstance(detail, bytes) else str(detail)[:65536]
                        info = upstream_errors.error_info(text, r.status_code)
                        return {"ok": False, "kind": _classify_error(r.status_code, text),
                                "code": info["code"],
                                "message": (info["message"] or text)[:200], "status": r.status_code}
                    # 200：读一小段确认是 SSE 而不是 200 包错误信封。
                    chunk = next(r.iter_bytes(4096), b"")
            text = chunk.decode(errors="ignore") if isinstance(chunk, bytes) else str(chunk)
            kind = _classify_error(200, text or "")
            if kind not in ("client", "transport") and (text or "").strip():
                info = upstream_errors.error_info(text, 200)
                return {"ok": False, "kind": kind, "code": info["code"],
                        "message": (info["message"] or text)[:200], "status": 200}
            return {"ok": True, "kind": "", "code": None, "message": "", "status": 200}
        finally:
            try:
                sess.close()
            except Exception:
                pass
    except httpx.RequestError as e:
        return {"ok": False, "kind": "transport", "code": None,
                "message": f"probe transport error: {e}", "status": 0}
    except Exception as e:
        # get_headers 内部会刷新 token；风控封号可能连刷新接口一起拒
        #（异常文本带 HTTP 401/403 或 invalid_grant），同样要能分类。
        msg = str(e)
        status = _extract_http_status(msg)
        kind = _classify_error(status, msg)
        if kind == "transport":
            return {"ok": False, "kind": "transport", "code": None,
                    "message": msg[:200], "status": 0}
        info = upstream_errors.error_info(getattr(e, "raw_body", "") or msg, status)
        return {"ok": False, "kind": kind, "code": info["code"],
                "message": msg[:200], "status": status or 0}


def probe_trusted_auth(auth_json: str) -> dict:
    """Check balance/profile before interpreting a model-route 401.

    The model list or chat route can reject a shared request shape.  Balance
    and account-profile endpoints are the trusted session probes: either one
    succeeding proves that the bearer credential is still accepted.
    """
    import re
    from admin import backend

    sess = None
    errors = []
    try:
        sess = backend.AccountSession(auth_json)
        for fn in (sess.fetch_balance, sess.fetch_account_profile):
            try:
                value = fn()
                if value:
                    return {"ok": True, "kind": "", "message": ""}
            except Exception as exc:
                errors.append(exc)
        statuses = []
        for error in errors:
            match = re.search(r"HTTP\s+(\d{3})", str(error))
            statuses.append(int(match.group(1)) if match else 0)
        if statuses and all(status in (401, 403) for status in statuses):
            return {"ok": False, "kind": "session_dead",
                    "status": statuses[0], "message": "trusted probes both rejected the credential"}
        return {"ok": False, "kind": "transport", "status": 0,
                "message": "trusted probe was inconclusive"}
    finally:
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass


def _record_banned(account_id: int) -> float:
    """记一次「复核仍被封」，返回下次应等待的秒数（指数退避、封顶可配）。"""
    minimum = max(60, settings.BAN_RECHECK_MIN_INTERVAL)
    ceiling = max(minimum, settings.BAN_RECHECK_MAX_INTERVAL)
    with _backoff_lock:
        count = _backoff.get(account_id, [0, 0.0])[0] + 1
        _backoff[account_id] = [count, time.time()]
        return min(minimum * (2 ** min(count - 1, 10)), ceiling)


def _probe_wait_seconds(account_id: int) -> float:
    """按退避台账算该账号还差多少秒才可复核；没台账（未探过/已解封）为 0。"""
    with _backoff_lock:
        entry = _backoff.get(account_id)
    if not entry:
        return 0.0
    count, last = entry
    minimum = max(60, settings.BAN_RECHECK_MIN_INTERVAL)
    ceiling = max(minimum, settings.BAN_RECHECK_MAX_INTERVAL)
    interval = min(minimum * (2 ** min(count - 1, 10)), ceiling)
    return max(0.0, last + interval - time.time())


def _clear_backoff(account_id: int) -> None:
    with _backoff_lock:
        _backoff.pop(account_id, None)


def _should_probe(acc, now: datetime) -> tuple[bool, str]:
    """账号是否需要本轮复核，返回 (是否, 原因标签)。"""
    if acc.status != "active":
        if (acc.cool_kind or "") == "manual_disabled":
            return False, "manual"
        # 上轮复核已确认登录态死亡：等人工重登，低频复查即可。
        if (acc.last_err_msg or "").startswith("复核确认登录态失效"):
            last = acc.last_err_at
            ceiling = max(60, settings.BAN_RECHECK_MAX_INTERVAL)
            if last and (now - last).total_seconds() < ceiling:
                return False, "relogin_needed"
            return True, "relogin_stale"
        # 退避期内（进程台账）或失败证据太新 → 等；老号（无时间戳）立即复核。
        if _probe_wait_seconds(acc.id) > 0:
            return False, "backoff"
        last = acc.last_err_at
        if last and (now - last).total_seconds() < max(60, settings.BAN_RECHECK_MIN_INTERVAL):
            return False, "backoff"
        return True, "disabled"
    if (acc.cool_kind or "") == "account_fault":
        # 冷却已到期但标记还在：选号随时会再踩它，先探一遍最划算。
        if not acc.cool_until or acc.cool_until <= now:
            return True, "fault_expired"
        return False, "cooling"
    return False, "healthy"


def _unban(db, acc) -> None:
    """自动解封：恢复 active 并清掉所有失败/冷却状态。"""
    acc.status = "active"
    acc.cool_until = None
    acc.cool_kind = ""
    acc.err_count = 0
    acc.breaker_fails = 0
    acc.consecutive_fails = 0
    acc.session_dead_fails = 0
    acc.breaker_until = None
    acc.degrade_until = None
    acc.last_err_at = None
    acc.last_err_msg = ""
    acc.last_upstream_error = ""
    db.commit()


def run_ban_recheck(db) -> dict:
    """复核所有风控禁用/冷却号；真实模型探测通过即自动解封。"""
    from admin.models import Account

    if not settings.BAN_RECHECK_ENABLED:
        return {"task": "ban_recheck", "ok": True, "summary": "封号自动复核未启用"}
    if POOL.waf.active():
        return {"task": "ban_recheck", "ok": True, "deferred": True,
                "retry_after": max(1, POOL.waf.remaining()),
                "summary": "出口保护期间暂停封号复核"}

    now = datetime.utcnow()
    rows = db.query(Account).all()
    candidates = [(a, _should_probe(a, now)) for a in rows]
    candidates = [(a, why) for a, (yes, why) in candidates if yes]

    model = _probe_model(db)

    unbanned, still_banned, dead, skipped = [], [], 0, 0
    gap = min(60.0, max(0.0, settings.KEEPALIVE_ACCOUNT_GAP))
    for acc, why in candidates:
        uid = acc.uid or f"account:{acc.id}"
        if not POOL.inflight.acquire_maintenance(uid):
            skipped += 1
            continue
        try:
            time.sleep(gap)
            trusted = probe_trusted_auth(acc.auth_json)
            if trusted["ok"]:
                _clear_backoff(acc.id)
                _unban(db, acc)
                unbanned.append(f"#{acc.id}")
                _logger.info("ban_recheck: trusted balance/profile probe restored id=%s uid=%s",
                             acc.id, (acc.uid or "")[:12])
                continue
            if trusted["kind"] == "transport":
                skipped += 1
                continue
            if trusted["kind"] == "session_dead":
                # The two authenticated probes are authoritative.  Do not let
                # a model route that happens to return 200 override a pair of
                # explicit 401/403 responses from the account's own endpoints.
                result = {"ok": False, "kind": "session_dead",
                          "message": trusted.get("message") or "trusted probes rejected the credential",
                          "status": trusted.get("status") or 401}
            elif model:
                # The trusted probes were inconclusive (for example a gateway
                # error without an HTTP auth status); a model probe can still
                # provide useful evidence, but transport errors never change
                # account state.
                result = probe_model_capability(acc.auth_json, acc.uid or "", model)
            else:
                skipped += 1
                continue
        finally:
            POOL.inflight.release_maintenance(uid)

        if result["ok"]:
            _clear_backoff(acc.id)
            _unban(db, acc)
            unbanned.append(f"#{acc.id}")
            _logger.info("ban_recheck: 自动解封 id=%s uid=%s（此前 %s）",
                         acc.id, (acc.uid or "")[:12], why)
            continue
        if result["kind"] == "session_dead":
            # 登录态真的死了：不是风控，等人工重登，复核没有意义。
            _clear_backoff(acc.id)
            acc.last_err_at = datetime.utcnow()
            acc.last_err_msg = (f"复核确认登录态失效，需重新登录：{result['message'][:150]}")[:255]
            db.commit()
            dead += 1
            continue
        if result["kind"] == "account_fault":
            interval = _record_banned(acc.id)
            acc.last_err_at = datetime.utcnow()
            acc.last_err_msg = (f"复核仍被风控拦截（{result['code'] or result['kind']}），"
                                f"{int(interval // 60)} 分钟后再试：{result['message'][:120]}")[:255]
            if acc.status == "active":
                # account_fault 冷却到期重探仍封：重新冷却，别让用户请求去踩。
                acc.cool_until = datetime.utcnow() + timedelta(seconds=interval)
                acc.cool_kind = "account_fault"
            db.commit()
            still_banned.append(f"#{acc.id}")
            continue
        # transport / 其它瞬时故障：本轮白跑，不改任何状态、不计退避。
        skipped += 1

    summary = f"封号复核：解封 {len(unbanned)} 个，仍封 {len(still_banned)} 个"
    if dead:
        summary += f"，登录态失效 {dead} 个（待重登）"
    if skipped:
        summary += f"，{skipped} 个跳过"
    return {"task": "ban_recheck", "ok": True,
            "unbanned": unbanned, "still_banned": still_banned,
            "session_dead": dead, "skipped": skipped, "summary": summary}
