"""使用记录统计：平台全部 Key 的请求数 / Token / 积分 / 耗时聚合，供前端图表使用。

数据源是 usage_logs（由代理网关逐请求写入）。全部聚合在 SQL 侧完成，
不把明细拉到 Python 里再算，避免日志量大时接口变慢。

统计口径：
- 只统计成功请求（error_kind 为空或 'success'）的 token / 积分，
  失败请求若也计进去会虚高消费；
- 请求数则统计全部调用（含失败），用于反映真实负载。
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, case
from sqlalchemy.orm import Session

from admin.db import get_db
from admin.models import Account, ApiKey, UsageLog
from admin.security import require_admin

router = APIRouter(prefix="/api/stats", tags=["stats"])

#: 视为「成功」的 error_kind 取值（空串 = 历史数据未分类，同样按成功计）
_OK_KINDS = ("", "success")


@router.get("/model-health")
def model_health_stats(hours: int = Query(default=24, ge=1, le=168),
                       _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    from admin.health import model_health
    return model_health(db, hours)


@router.get("/model-pool")
def model_pool_stats(model: str = Query(default=""),
                     _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """账号池 × 模型 的当日使用面：哪些号能用、用了多少、日额度是否耗尽。

    口径与估算
    ----------
    * 「今天」按**中国时区自然日**（展示口径，与使用记录一致）。
    * 今日已用 = 当日成功请求（error_kind 空/success）的 total_tokens 之和，
      按账号 × 模型聚合。
    * **上游 6004/6008 的「日额度」是滚动 24 小时窗口**（2026-10-24 实测：
      9 个打满账号在打满时刻回看 24h 的用量聚在 199.3M~202.1M），
      恢复时间 = 最早越界用量滑出窗口的时刻 —— 不是自然日重置。
    * 日额度耗尽 = AccountModelCooldown 里该模型 kind=model_daily 且仍在
      生效的账号（6004/6008 的确定性负缓存，带上游恢复时间）。
    * 单账号日额度估算 = 「已经打满的账号」在**打满时刻回看 24h** 的用量
      平均值（与上游同口径）；样本不足 1 个时返回 null（前端显示「未知」），
      不瞎猜。按自然日取样会严重低估（窗口错位）。
    * 剩余额度估算 = Σ max(0, 单账号日额度 - 近 24h 已用)，只算当前可用的号。
      这是**本地推断值**，不是上游承诺；真实额度以上游为准。
    """
    from admin.models import AccountModelCooldown, ModelConfig
    from admin.model_limits import next_cn_midnight
    from admin.routers.proxy import _looks_login_dead
    from admin.pool import POOL
    from admin.config import settings

    now = datetime.utcnow()
    today_start = next_cn_midnight(now) - timedelta(days=1)

    enabled = [m.model_id for m in db.query(ModelConfig).filter(
        ModelConfig.level == "system", ModelConfig.enabled == 1).order_by(ModelConfig.model_id).all()]
    usage_models = [m for (m,) in db.query(UsageLog.model).filter(
        UsageLog.created_at >= today_start, UsageLog.model != "").distinct().all()]
    known = sorted(set(enabled) | set(usage_models))
    if not model:
        # 不带 model：返回模型清单 + 各模型当日概要（供选择器与卡片）。
        counts = dict(db.query(UsageLog.model, func.count(UsageLog.id)).filter(
            UsageLog.created_at >= today_start, UsageLog.error_kind.in_(_OK_KINDS),
            UsageLog.model != "").group_by(UsageLog.model).all())
        tokens = dict(db.query(UsageLog.model, func.coalesce(func.sum(UsageLog.total_tokens), 0)).filter(
            UsageLog.created_at >= today_start, UsageLog.error_kind.in_(_OK_KINDS),
            UsageLog.model != "").group_by(UsageLog.model).all())
        cooled = db.query(AccountModelCooldown).filter(
            AccountModelCooldown.until > now, AccountModelCooldown.kind == "model_daily").all()
        exhausted = {}
        for row in cooled:
            exhausted[row.model] = exhausted.get(row.model, 0) + 1
        return {"models": [{"model": m, "enabled": m in enabled,
                            "requests_today": int(counts.get(m, 0)),
                            "tokens_today": int(tokens.get(m, 0)),
                            "exhausted_accounts": exhausted.get(m, 0)} for m in known],
                "today_start": today_start.isoformat() + "Z"}

    accounts = db.query(Account).order_by(Account.id.asc()).all()
    cooldowns = {row.account_id: row for row in db.query(AccountModelCooldown).filter(
        AccountModelCooldown.model == model, AccountModelCooldown.until > now).all()}
    usage = {int(acc_id): (int(tokens or 0), int(count or 0))
             for acc_id, tokens, count in db.query(UsageLog.account_id,
                                                   func.coalesce(func.sum(UsageLog.total_tokens), 0),
                                                   func.count(UsageLog.id)).filter(
        UsageLog.model == model, UsageLog.created_at >= today_start,
        UsageLog.error_kind.in_(_OK_KINDS)).group_by(UsageLog.account_id).all()}

    # 对照口径：滚动 24 小时（「使用记录」里「近 1 天」的算法）。
    # 自然日 vs 滚动窗的差值会让两个面板看起来「对不上」，把同模型的
    # 滚动窗数字一并返回，前端并排展示，口径差异即可自解释。
    # 上游 6004 的「日额度」同样是滚动 24h 窗口（2026-10-24 实测：9 个打满
    # 账号在打满时刻回看 24h 的用量聚在 199.3M~202.1M），额度估算与剩余
    # 额度必须用同一口径，按自然日取样会严重低估。
    roll_start = now - timedelta(days=1)
    usage_24h = {int(acc_id): (int(tokens or 0), int(count or 0))
                 for acc_id, tokens, count in db.query(UsageLog.account_id,
                                                       func.coalesce(func.sum(UsageLog.total_tokens), 0),
                                                       func.count(UsageLog.id)).filter(
        UsageLog.model == model, UsageLog.created_at >= roll_start,
        UsageLog.error_kind.in_(_OK_KINDS)).group_by(UsageLog.account_id).all()}
    roll_tokens = sum(t for t, _ in usage_24h.values())
    roll_reqs = sum(c for _, c in usage_24h.values())

    rows = []
    exhausted_used = []
    config = db.query(ModelConfig).filter(
        ModelConfig.model_id == model, ModelConfig.level == "system").first()
    free = config is not None and config.credit_multiplier == 0
    for a in accounts:
        row = cooldowns.get(a.id)
        daily = row is not None and row.kind == "model_daily"
        used_today, requests_today = usage.get(a.id, (0, 0))
        used_today, requests_today = int(used_today or 0), int(requests_today or 0)
        used_24h, requests_24h = usage_24h.get(a.id, (0, 0))
        used_24h, requests_24h = int(used_24h or 0), int(requests_24h or 0)
        cooling = any(getattr(a, f) is not None and getattr(a, f) > now
                      for f in ("cool_until", "breaker_until", "degrade_until"))
        # 该模型任何生效中的冷却（日额度/限流/排队/不可用）都意味着
        # 「此账号此刻不能服务此模型」—— 不能只有 model_daily 才排除，
        # 否则限流中的号还计进「可用账号」，与其他面板对不上。
        usable = (a.status == "active" and not cooling and row is None
                  and not _looks_login_dead(a, now)
                  and (free or (a.balance_remain or 0) > 0)
                  and not POOL.inflight.full(a.uid or "", settings.MAX_IN_FLIGHT))
        entry = {"id": a.id, "name": a.name or a.uid or f"#{a.id}",
                 "uid": (a.uid or "")[:12], "status": a.status,
                 "usable": usable, "cooling": cooling,
                 "daily_exhausted": daily, "daily_reset_at": None,
                 "limit_kind": row.kind if row is not None and not daily else "",
                 "used_today_tokens": used_today, "requests_today": requests_today,
                 "used_24h_tokens": used_24h, "requests_24h": requests_24h,
                 "balance_remain": a.balance_remain or 0,
                 "last_used_at": a.last_used_at.isoformat() if a.last_used_at else None}
        if daily:
            entry["daily_reset_at"] = row.until.isoformat() + "Z"
            # 额度样本：打满时刻回看 24h 的成功用量（与上游 6004 同口径）。
            # updated_at 之后账号已被冷却挡住，几乎没有新增成功量；
            # 取 min(now, updated_at) 截断，避免误吞解除冷却后的新一轮用量。
            w_start = row.until - timedelta(hours=24)
            w_end = min(now, row.updated_at or now)
            sample = db.query(func.coalesce(func.sum(UsageLog.total_tokens), 0)).filter(
                UsageLog.account_id == a.id, UsageLog.model == model,
                UsageLog.created_at >= w_start, UsageLog.created_at <= w_end,
                UsageLog.error_kind.in_(_OK_KINDS)).scalar() or 0
            exhausted_used.append(int(sample))
        rows.append(entry)

    quota = None
    if exhausted_used:
        # 打满即额度：耗尽账号在滚动 24h 窗口内的用量就是其额度的近似样本。
        quota = int(round(sum(exhausted_used) / len(exhausted_used)))
    for entry in rows:
        entry["est_remaining"] = None if quota is None else max(
            0, quota - entry["used_24h_tokens"]) if entry["usable"] else 0
    usable_rows = [e for e in rows if e["usable"]]
    summary = {
        "model": model, "enabled": config is not None, "free": free,
        "today_start": today_start.isoformat() + "Z",
        "total_accounts": len(rows), "usable_accounts": len(usable_rows),
        "exhausted_accounts": len(exhausted_used),
        "model_limited_accounts": sum(1 for e in rows if e["limit_kind"]),
        "cooling_accounts": sum(1 for e in rows if e["cooling"] and not e["daily_exhausted"]),
        "inactive_accounts": sum(1 for e in rows if e["status"] != "active"),
        "requests_today": sum(e["requests_today"] for e in rows),
        "tokens_today": sum(e["used_today_tokens"] for e in rows),
        "today_elapsed_hours": round((now - today_start).total_seconds() / 3600, 1),
        "rolling_24h": {"tokens": int(roll_tokens), "requests": int(roll_reqs)},
        "est_per_account_quota": quota, "quota_samples": len(exhausted_used),
        "est_total_remaining": (sum(e["est_remaining"] for e in usable_rows)
                                if quota is not None else None),
        "est_pool_capacity": quota * len(usable_rows) if quota is not None else None,
        "earliest_reset": min((e["daily_reset_at"] for e in rows if e["daily_reset_at"]),
                              default=None),
    }
    return {"summary": summary, "accounts": rows,
            "note": "额度为上游滚动 24 小时窗口口径（6004 实测：打满时刻回看 24h 的用量即单账号额度，"
                    "恢复时间 = 最早越界用量滑出窗口的时刻）；剩余额度 = 估算额度 − 近 24h 已用。"
                    "「今日已用」= 北京时间 00:00 起的自然日，与「使用记录→今日（自然日）」同口径。"
                    "额度与剩余为按已打满账号样本的本地推断值，非上游承诺"}


def _window(days: int) -> datetime:
    """返回统计窗口起点（UTC，与 usage_logs.created_at 存的口径一致）。"""
    return datetime.utcnow() - timedelta(days=max(1, days))


def _time_filters(days: int, start: Optional[datetime], end: Optional[datetime]):
    """Explicit bounds override the rolling window, using the logs' UTC clock."""
    def utc(value):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value and value.tzinfo else value
    start, end = utc(start), utc(end)
    if start and end and start > end:
        raise HTTPException(400, "开始日期不能晚于结束日期")
    filters = []
    if start is not None:
        filters.append(UsageLog.created_at >= start)
    elif end is None:
        filters.append(UsageLog.created_at >= _window(days))
    if end is not None:
        filters.append(UsageLog.created_at <= end)
    return filters


def _ok_filter(q):
    return q.filter(UsageLog.error_kind.in_(_OK_KINDS))


@router.get("/usage")
def usage_stats(
    days: int = 1,
    granularity: str = "hour",  # day | hour
    key_id: Optional[int] = None,
    model: Optional[str] = None,
    account_id: Optional[int] = None,
    use_case: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """使用记录总览：概览卡片 + 模型分布 + 分组分布 + 端点分布 + 趋势。

    默认 **近 1 天 / 按小时**：这是「今天用得怎么样」最常见的诉求，
    默认 14 天会把当天的问题稀释在两周的曲线里，看不出异常。

    支持按 `key_id` / `model` / `account_id` / `use_case` 筛选：
    筛选后**所有**图与卡片都跟着变（同一份 where 条件），
    这样「某个 Key 今天花了多少」是直接可读的，不用自己心算做减法。

    返回结构直接对应前端「使用记录」页面，避免前端多次请求拼装。
    """
    # ── 统一筛选条件 ────────────────────────────────────────────────────
    # 只构造一次，保证概览/分布/趋势三个口径完全一致 ——
    # 分开写很容易出现「卡片数字和图表对不上」的经典问题。
    filters = _time_filters(days, start, end)
    if key_id is not None:
        filters.append(UsageLog.api_key_id == key_id)
    if model:
        filters.append(UsageLog.model == model)
    if account_id is not None:
        filters.append(UsageLog.account_id == account_id)
    if use_case:
        filters.append(UsageLog.use_case == use_case)

    def _f(q):
        return q.filter(*filters)

    # ── 概览 ────────────────────────────────────────────────────────────
    ov = _ok_filter(
        _f(db.query(
            func.count(UsageLog.id),
            func.coalesce(func.sum(UsageLog.credits), 0.0),
            func.coalesce(func.sum(UsageLog.prompt_tokens), 0),
            func.coalesce(func.sum(UsageLog.completion_tokens), 0),
            func.coalesce(func.sum(UsageLog.total_tokens), 0),
            func.coalesce(func.sum(UsageLog.cached_tokens), 0),
            func.avg(UsageLog.latency_ms),
        ))
    ).one()

    total_req, credits, ptok, ctok, ttok, cached, avg_latency = ov
    # 总请求数按全部调用计（含失败），与图表口径区分
    all_req = _f(db.query(func.count(UsageLog.id))).scalar() or 0
    ok_req = int(total_req or 0)
    fail_req = max(0, int(all_req or 0) - ok_req)

    # ── 模型分布 ────────────────────────────────────────────────────────
    by_model = (
        _ok_filter(
            _f(db.query(
                UsageLog.model,
                func.count(UsageLog.id),
                func.coalesce(func.sum(UsageLog.total_tokens), 0),
                func.coalesce(func.sum(UsageLog.credits), 0.0),
            ))
        )
        .group_by(UsageLog.model)
        .order_by(func.coalesce(func.sum(UsageLog.credits), 0.0).desc())
        .all()
    )

    # ── 端点分布 ────────────────────────────────────────────────────────
    # use_case 由网关写入（如 responses / chat / messages），作为"端点"维度
    by_endpoint = (
        _ok_filter(
            _f(db.query(
                UsageLog.use_case,
                func.count(UsageLog.id),
                func.coalesce(func.sum(UsageLog.total_tokens), 0),
                func.coalesce(func.sum(UsageLog.credits), 0.0),
            ))
        )
        .group_by(UsageLog.use_case)
        .order_by(func.coalesce(func.sum(UsageLog.credits), 0.0).desc())
        .all()
    )

    # ── 按 Key 分组分布（对应参考图的"分组使用分布"）────────────────────
    by_key = (
        _ok_filter(
            _f(db.query(
                UsageLog.api_key_id,
                func.count(UsageLog.id),
                func.coalesce(func.sum(UsageLog.total_tokens), 0),
                func.coalesce(func.sum(UsageLog.credits), 0.0),
            ))
        )
        .group_by(UsageLog.api_key_id)
        .order_by(func.coalesce(func.sum(UsageLog.credits), 0.0).desc())
        .all()
    )
    key_names = {k.id: k.name for k in db.query(ApiKey).all()}
    account_names = {a.id: a.name or a.uid or f"账号 #{a.id}"
                     for a in db.query(Account).all()}
    by_account = (_ok_filter(_f(db.query(
        UsageLog.account_id, func.count(UsageLog.id),
        func.coalesce(func.sum(UsageLog.total_tokens), 0),
        func.coalesce(func.sum(UsageLog.credits), 0.0))))
        .group_by(UsageLog.account_id)
        .order_by(func.count(UsageLog.id).desc()).all())
    by_result = (_f(db.query(UsageLog.error_kind, func.count(UsageLog.id)))
                 .group_by(UsageLog.error_kind).order_by(func.count(UsageLog.id).desc()).all())
    account_models = []
    if account_id is not None:
        from admin.routers.accounts import _active_model_limits
        limits = _active_model_limits(db, datetime.utcnow()).get(account_id, [])
        limit_map = {entry['model']: entry for entry in limits}
        ok = UsageLog.error_kind.in_(_OK_KINDS)
        rows = (_f(db.query(UsageLog.model, func.count(UsageLog.id),
                           func.sum(case((ok, 1), else_=0)),
                           func.sum(case((ok, UsageLog.total_tokens), else_=0)),
                           func.sum(case((ok, UsageLog.credits), else_=0))))
                .group_by(UsageLog.model).order_by(func.count(UsageLog.id).desc()).all())
        for mid, count, successes, tokens, used in rows:
            account_models.append({'model': mid, 'requests': count,
                                   'successes': int(successes or 0),
                                   'failures': count - int(successes or 0),
                                   'tokens': int(tokens or 0), 'credits': round(float(used or 0), 4),
                                   'limit': limit_map.pop(mid, None)})
        # Keep a current limit visible even if this date window has no calls.
        for mid, entry in limit_map.items():
            if not model or model == mid:
                account_models.append({'model': mid, 'requests': 0, 'successes': 0,
                                       'failures': 0, 'tokens': 0, 'credits': 0, 'limit': entry})

    # ── 趋势（按天/按小时分桶）──────────────────────────────────────────
    if db.get_bind().dialect.name == "sqlite":
        bucket = func.strftime("%Y-%m-%d %H:00" if granularity == "hour" else "%Y-%m-%d",
                               UsageLog.created_at)
    elif granularity == "hour":
        bucket = func.date_format(UsageLog.created_at, "%Y-%m-%d %H:00")
    else:
        bucket = func.date_format(UsageLog.created_at, "%Y-%m-%d")
    trend_rows = (
        _ok_filter(
            _f(db.query(
                bucket.label("b"),
                func.coalesce(func.sum(UsageLog.prompt_tokens), 0),
                func.coalesce(func.sum(UsageLog.completion_tokens), 0),
                func.coalesce(func.sum(UsageLog.cached_tokens), 0),
                func.coalesce(func.sum(UsageLog.credits), 0.0),
                func.count(UsageLog.id),
            ))
        )
        .group_by("b")
        .order_by("b")
        .all()
    )

    def _dist(rows, label_fn):
        return [
            {
                "label": label_fn(r[0]),
                "requests": int(r[1] or 0),
                "tokens": int(r[2] or 0),
                "credits": round(float(r[3] or 0), 4),
            }
            for r in rows
        ]

    return {
        "range_days": None if start or end else days,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "granularity": granularity,
        "filters": {
            "key_id": key_id,
            "model": model,
            "account_id": account_id,
            "use_case": use_case,
        },
        "overview": {
            "requests": int(all_req or 0),
            "ok_requests": ok_req,
            "failed_requests": fail_req,
            "credits": round(float(credits or 0), 4),
            "prompt_tokens": int(ptok or 0),
            "completion_tokens": int(ctok or 0),
            "total_tokens": int(ttok or 0),
            "cached_tokens": int(cached or 0),
            "avg_latency_ms": int(avg_latency) if avg_latency else 0,
        },
        "by_model": _dist(by_model, lambda v: v or "未知"),
        "by_endpoint": _dist(by_endpoint, lambda v: v or "chat"),
        "by_key": _dist(by_key, lambda v: key_names.get(v) or f"#{v}"),
        "by_account": [{**item, "account_id": row[0]} for row, item in zip(
            by_account, _dist(by_account, lambda v: account_names.get(v) or
                             (f"已删除账号 #{v}" if v else "未分配账号")))],
        "by_result": [{"label": "成功" if kind in _OK_KINDS else kind or "未分类",
                       "requests": int(count)} for kind, count in by_result],
        "account_models": account_models,
        "usage_scope": "gateway_only",
        "trend": [
            {
                "bucket": r[0],
                "prompt_tokens": int(r[1] or 0),
                "completion_tokens": int(r[2] or 0),
                "cached_tokens": int(r[3] or 0),
                "credits": round(float(r[4] or 0), 4),
                "requests": int(r[5] or 0),
            }
            for r in trend_rows
        ],
    }


@router.get("/usage/options")
def usage_filter_options(
    days: int = 30,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """筛选下拉的可选项：实际用过的 Key / 模型 / 端点。

    只返回**窗口内真实出现过**的值，避免下拉里塞一堆从没用过的模型，
    让人以为「有数据」却筛出空结果。
    """
    filters = _time_filters(days, start, end)

    models = [
        r[0] for r in
        db.query(UsageLog.model)
        .filter(*filters, UsageLog.model.isnot(None))
        .group_by(UsageLog.model)
        .order_by(func.count(UsageLog.id).desc())
        .all() if r[0]
    ]
    cases = [
        r[0] for r in
        db.query(UsageLog.use_case)
        .filter(*filters, UsageLog.use_case.isnot(None))
        .group_by(UsageLog.use_case)
        .order_by(func.count(UsageLog.id).desc())
        .all() if r[0]
    ]
    keys = [
        {"id": k.id, "name": k.name}
        for k in db.query(ApiKey).order_by(ApiKey.id.asc()).all()
    ]
    account_names = {a.id: a.name or a.uid or f"账号 #{a.id}" for a in db.query(Account).all()}
    accounts = [{"id": aid, "name": account_names.get(aid) or f"已删除账号 #{aid}"}
                for (aid,) in db.query(UsageLog.account_id).filter(*filters, UsageLog.account_id.isnot(None))
                .group_by(UsageLog.account_id).order_by(func.count(UsageLog.id).desc()).all()]
    seen = {a['id'] for a in accounts}
    accounts.extend({'id': aid, 'name': name} for aid, name in account_names.items() if aid not in seen)
    return {"models": models, "use_cases": cases, "keys": keys, "accounts": accounts}
