"""积分到期统计：账号级到期排行 + 「最近快到期的积分包」聚合。

数据来源是 `accounts` 表上的积分快照列（由 `refresh_credits` 定时任务写入，
见 admin/credits.py）。**不在这里发上游请求** —— 统计接口会被前端按需轮询，
每次都对几十个账号打上游既慢又会把自己变成风控特征。

为什么单独成路由而不是塞进 `routers/stats.py`：那里是 `usage_logs` 的
用量统计（按请求聚合），这里是**存量**的到期统计（按账号/积分包聚合）。
两者数据源、刷新节奏、失败模式完全不同，混在一起会让「统计接口慢」
的排查变得没有边界。

借鉴来源（E:\\反代理\\workbuddy-switch-main）
-------------------------------------------
  crates/wb-switch-core/src/modules/credit_usage.rs  build_statistics
  src/pages/CreditStatsPage.tsx                      积分总览 / 账号消耗构成
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from admin import routing_policy
from typing import Literal
from sqlalchemy import func
from sqlalchemy.orm import Session

from admin import credits as credit_rules
from admin.db import get_db
from admin.models import Account, UsageLog
from admin.security import require_admin
from admin.selection_order import ordered_accounts

router = APIRouter(prefix="/api/credits", tags=["credits"])


def _snapshot_of(acc: Account) -> dict:
    """解析账号的积分快照；坏数据降级为空（不让一条脏记录打挂整个统计）。"""
    raw = (acc.credits_snapshot or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ms(dt: Optional[datetime]) -> Optional[int]:
    """naive-UTC datetime → 毫秒时间戳。"""
    if not dt:
        return None
    return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp() * 1000)


def _package_row(r: dict) -> dict:
    """单个积分包 → 前端展示所需的精简字段。

    刻意**不带** `days_left` / `package_name` / `status` / `used`：
    列表每行只画「剩余额度 + 包名 + 到期日（已格式化成 expire_date）」，
    多带一个字段 × 数百个包就是几十 KB 的无谓流量。
    需要完整字段的走单账号明细接口。
    """
    return {
        "package_code": r.get("package_code") or "",
        "display_name": r.get("display_name") or r.get("package_name") or "积分包",
        "remaining": round(float(r.get("remaining") or 0), 4),
        "total": round(float(r.get("total") or 0), 4),
        "expire_at": r.get("expire_at"),
        "expire_label": credit_rules.format_expire_at(r.get("expire_at")),
        "expire_date": credit_rules.expire_date_label(r.get("expire_at")),
        "expired": bool(r.get("expired")),
        "expiring_soon": bool(r.get("expiring_soon")),
    }


def _account_row(acc: Account, snapshot: dict, next_n: int = 4) -> dict:
    """账号 → 到期统计行。

    **刻意不返回该账号的全部积分包明细**。原因：一个账号实测有 45~49 个包，
    全池就是上千条包记录；而账号排行表只渲染每行前 2 个包。
    把全部明细塞进列表接口会让响应从几十 KB 膨胀到 240 KB，
    且随账号数**线性增长**（越用越卡）。

    要看某个账号的全部包，走 `/api/accounts/{id}/credit-details`（单账号按需拉）。
    """
    resources = snapshot.get("resources") or []
    active = [r for r in resources if float(r.get("remaining") or 0) > 0]
    expiring = [r for r in active if r.get("expiring_soon")]
    soonest_ms = _ms(acc.credits_soonest_expire_at)
    days_left = snapshot.get("soonest_days_left")
    if days_left is None and soonest_ms:
        days_left = max(0.0, (soonest_ms - credit_rules.now_ms()) / 86400000.0)
    return {
        "account_id": acc.id,
        "account_name": acc.name or acc.uid or f"账号#{acc.id}",
        "uid": acc.uid or "",
        "status": acc.status or "active",
        "selection_priority": acc.selection_priority,
        "balance_remain": int(acc.balance_remain or 0),
        "total_remaining": round(float(snapshot.get("total_remaining") or 0), 4),
        "total_capacity": round(float(snapshot.get("total_capacity") or 0), 4),
        "expiring_soon_remaining": int(acc.credits_expiring_soon or 0),
        "evergreen_remaining": int(acc.credits_evergreen or 0),
        "expired_remaining": int(acc.credits_expired or 0),
        "soonest_expire_at": soonest_ms,
        "soonest_days_left": round(days_left, 2) if days_left is not None else None,
        "soonest_label": credit_rules.days_left_label(days_left),
        "expiring_soon": bool(expiring),
        "expiring_package_count": len(expiring),
        "package_count": int(snapshot.get("package_count") or len(resources)),
        "active_package_count": len(active),
        "synced_at": _ms(acc.credits_synced_at),
        # 「最近快到期的积分包」：按到期升序的前 N 个（快照本身已排好序），
        # 账号排行表每行只需 2 个，留 4 个余量。
        "next_expiring": [_package_row(r) for r in active[:max(0, next_n)]],
    }


#: 默认只统计活跃账号 —— 禁用号的积分不会再被消耗，把它们混进
#: 「快到期额度合计」会虚高危险额度，让用户以为有大笔积分要作废。
def _load_rows(db: Session, include_disabled: bool = False) -> list[Account]:
    q = db.query(Account)
    if not include_disabled:
        q = q.filter(Account.status == "active")
    return q.order_by(Account.id.asc()).all()


@router.get("/expiry")
def credit_expiry_stats(
    days: int = 7,
    include_disabled: bool = False,
    package_limit: int = 60,
    package_offset: int = 0,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """积分到期总览：**按紧迫度排序**的账号列表 + 最近到期的积分包。

    这是「快到期的先用完」这条策略的观察窗：谁最紧迫、还差多久、
    一共有多少额度会在 30 天内作废，全部一目了然。

    返回：
      summary  —— 全池汇总（快到期额度、长期有效额度、最紧迫的账号）
      accounts —— 账号级明细，**已按到期紧迫度升序排好**（最紧迫的在最前）
      packages —— 逐积分包平铺，按到期时间升序，**分页**（见下）
      windows  —— 各时间档到期额度的分布，供画危险度图

    关于分页：积分包总数随「账号数 × 每号包数」增长（实测可达上千个包），
    一次全量返回会让响应到几百 KB 且**越用越大**。所以：

      * `summary` / `windows` / `accounts` 里的聚合值都在**服务端**算好，
        前端不再需要全量明细就能渲染概览与图表；
      * `packages` 只返回一页（默认 60 条），用 `package_offset` 继续往后翻；
      * `packages_total` 告诉前端还有多少条，用于「加载更多」。

    ``window_amounts_30d`` 是 30 天内到期的额度合计 —— 前端「到期风险分布」
    里「30 天以外」那一段靠 `total_remaining - (≤30天) - 长期有效` 得出。
    """
    now = credit_rules.now_ms()
    accounts = _load_rows(db, include_disabled)
    # 列表接口每个账号只带 4 个「最近到期」包（表格每行只用 2 个），
    # 完整明细走 /api/accounts/{id}/credit-details —— 这是响应体积的关键。
    rows = [_account_row(a, _snapshot_of(a), next_n=4) for a in accounts]

    # 按紧迫度排序：**直接复用调度用的那一份分级函数**
    # （`admin.credits.urgency_rank`）。这里刻意不自己再写一遍排序规则 ——
    # 两处口径一旦漂移，用户会看到「界面排第一的号」并不是「下一个被选中的号」，
    # 从而认为调度坏了。
    selection_accounts = ordered_accounts(db.query(Account).all())
    positions = {a.id: i for i, a in enumerate(selection_accounts)}
    rows.sort(key=lambda row: positions[row['account_id']])

    total_remaining = sum(r["total_remaining"] for r in rows)
    expiring_soon = sum(r["expiring_soon_remaining"] for r in rows)
    evergreen = sum(r["evergreen_remaining"] for r in rows)
    expired = sum(r["expired_remaining"] for r in rows)
    urgent = [r for r in rows if r["expiring_soon_remaining"] > 0]
    soonest_row = min(
        (r for r in rows if r["soonest_expire_at"]),
        key=lambda r: r["soonest_expire_at"], default=None)

    # 「可用积分」用**余额口径**，不用快照口径。
    #
    # `total_remaining` 来自逐包到期快照，只覆盖「采集过到期数据」的账号
    # （只覆盖采集过到期快照的账号），所以它天然偏小；
    # 而账号面板的 `balance_remain` 取 `Account.balance_remain`（每个号都有）。
    # 两者混用会让同一块屏上出现两个不同的「总剩余积分」——
    # 差额正是那些尚未采集快照的号的余额。
    #
    # 所以：**概览卡片用余额口径**（与账号面板一致），
    # 到期分档条继续用快照口径（它本来就只描述有到期数据的那部分），
    # 并把快照覆盖数一并给出，便于界面如实标注。
    available_remaining = sum(float(a.balance_remain or 0) for a in accounts)
    snapshot_accounts = sum(1 for a in accounts if a.credits_synced_at)

    # 逐包平铺与分档：**直接读各自账号的快照**（而不是 accounts[] 里那 4 条），
    # 这样聚合口径完整，且不必把全量明细发给前端。
    cuts = [1, 3, 7, 14, 30]
    buckets = {c: {"amount": 0.0, "packages": 0} for c in cuts}
    flat: list[dict] = []
    rows_by_id = {r["account_id"]: r for r in rows}
    for acc in accounts:
        acc_row = rows_by_id[acc.id]
        snap = _snapshot_of(acc)
        for p in (snap.get("resources") or []):
            remaining = float(p.get("remaining") or 0)
            if remaining <= 0:
                continue
            exp = p.get("expire_at")
            if exp and now < exp:
                for c in cuts:
                    if exp <= now + c * 86400000:
                        buckets[c]["amount"] += remaining
                        buckets[c]["packages"] += 1
                        break
            flat.append({
                **_package_row(p),
                "account_id": acc_row["account_id"],
                "account_name": acc_row["account_name"],
            })
    flat.sort(key=lambda p: (p.get("expire_at") is None, p.get("expire_at") or 0))
    windows = [{"days": c, "amount": round(buckets[c]["amount"], 4),
                "packages": buckets[c]["packages"]} for c in cuts]

    package_limit = max(1, min(500, int(package_limit or 60)))
    package_offset = max(0, int(package_offset or 0))
    page = flat[package_offset:package_offset + package_limit]

    return {
        "generated_at": now,
        "expiring_soon_days": credit_rules.EXPIRING_SOON_DAYS,
        "suggested_days": days,
        "summary": {
            "accounts": len(rows),
            # 概览卡片用这个（余额口径，与账号面板一致）
            "available_remaining": round(available_remaining, 4),
            # 快照口径：只覆盖「采集过到期数据」的账号，供到期分档条使用
            "total_remaining": round(total_remaining, 4),
            "snapshot_accounts": snapshot_accounts,
            "expiring_soon_remaining": round(expiring_soon, 4),
            "evergreen_remaining": round(evergreen, 4),
            "expired_remaining": round(expired, 4),
            "expiring_accounts": len(urgent),
            "expiring_packages": sum(r["expiring_package_count"] for r in rows),
            "soonest_expire_at": soonest_row["soonest_expire_at"] if soonest_row else None,
            "soonest_days_left": soonest_row["soonest_days_left"] if soonest_row else None,
            "soonest_account_id": soonest_row["account_id"] if soonest_row else None,
            "soonest_account_name": soonest_row["account_name"] if soonest_row else None,
            # 「再不烧就作废」占全部剩余的比例 —— 越高说明越该赶紧用
            "at_risk_ratio": (round(expiring_soon / total_remaining, 4)
                              if total_remaining > 0 else 0.0),
            "packages_total": len(flat),
        },
        "windows": windows,
        "accounts": rows,
        "selection_mode": 'manual' if any(a.selection_priority is not None for a in selection_accounts) else 'automatic',
        "selection_order": [a.id for a in selection_accounts],
        "routing_policy": routing_policy.selection_policy(db),
        # 分页的逐包列表；packages_total 在 summary 里
        "packages": page,
        "packages_total": len(flat),
        "package_offset": package_offset,
        "package_limit": package_limit,
        "has_more": package_offset + len(page) < len(flat),
    }


class SelectionPolicyIn(BaseModel):
    mode: str
    upstream_start_interval_ms: int = Field(ge=0, le=60000, strict=True)


@router.get('/selection-policy')
def get_selection_policy(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    return routing_policy.selection_policy(db)


@router.put('/selection-policy')
def save_selection_policy(body: SelectionPolicyIn, _: bool = Depends(require_admin),
                          db: Session = Depends(get_db)):
    if body.mode not in routing_policy.MODE_IDS:
        raise HTTPException(400, '未知的账号调度模式')
    accounts = db.query(Account).with_for_update().all()
    for account in accounts:
        account.selection_priority = None
    routing_policy.save_value(db, routing_policy.SELECTION_KEY, body.mode)
    routing_policy.save_value(db, routing_policy.INTERVAL_KEY, body.upstream_start_interval_ms)
    db.commit()
    return routing_policy.selection_policy(db)


class SelectionOrderIn(BaseModel):
    mode: Literal['manual', 'automatic'] = 'manual'
    ids: list[int] = []
    expected_ids: list[int]
    confirm: bool = False


@router.post('/selection-order')
def save_selection_order(body: SelectionOrderIn, _: bool = Depends(require_admin),
                         db: Session = Depends(get_db)):
    if not body.confirm:
        raise HTTPException(400, '请确认：此操作会改变真实账号的使用优先级')
    accounts = db.query(Account).order_by(Account.id).with_for_update().all()
    current = [a.id for a in ordered_accounts(accounts)]
    if current != body.expected_ids:
        raise HTTPException(409, '账号或排序已变化，请刷新后重试')
    if body.mode == 'manual' and (len(body.ids) != len(current) or set(body.ids) != set(current)):
        raise HTTPException(400, '排序必须包含全部账号且不能重复')
    positions = {account_id: i for i, account_id in enumerate(body.ids)}
    for account in accounts:
        account.selection_priority = positions[account.id] if body.mode == 'manual' else None
    db.commit()
    return {'ok': True, 'mode': body.mode, 'ids': [a.id for a in ordered_accounts(accounts)]}


@router.get("/usage")
def credit_usage_stats(
    days: int = 7,
    account_id: Optional[int] = None,
    top: int = 20,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """积分消耗构成：按账号 / 按模型的消耗排行（数据源 usage_logs）。

    与 `/api/stats/usage` 的区别：那边是「平台整体用量」视角，
    这边是**积分视角**（只看 credits，且默认把「余额 × 到期」串起来看），
    供积分统计页展示「谁在烧、烧了多少、还剩多少、还剩多久」。
    """
    since = datetime.utcnow() - timedelta(days=max(1, days))
    ok_filter = UsageLog.error_kind.in_(("", "success"))

    q = db.query(
        UsageLog.account_id,
        func.count(UsageLog.id),
        func.coalesce(func.sum(UsageLog.credits), 0.0),
        func.coalesce(func.sum(UsageLog.total_tokens), 0),
    ).filter(UsageLog.created_at >= since)
    if account_id is not None:
        q = q.filter(UsageLog.account_id == account_id)
    rows = (q.filter(ok_filter)
            .group_by(UsageLog.account_id)
            .order_by(func.coalesce(func.sum(UsageLog.credits), 0.0).desc())
            .limit(max(1, min(200, top)))
            .all())

    accounts = {a.id: a for a in _load_rows(db, True)}
    usage = []
    for aid, reqs, credits_used, tokens in rows:
        acc = accounts.get(aid)
        snap = _snapshot_of(acc) if acc else {}
        usage.append({
            "account_id": aid,
            "account_name": (acc.name if acc and acc.name else f"#{aid}"),
            "requests": int(reqs or 0),
            "credits": round(float(credits_used or 0), 4),
            "tokens": int(tokens or 0),
            "remaining": int(acc.balance_remain or 0) if acc else 0,
            "expiring_soon_remaining": int(acc.credits_expiring_soon or 0) if acc else 0,
            "soonest_days_left": snap.get("soonest_days_left"),
        })

    by_model = (
        db.query(
            UsageLog.model,
            func.count(UsageLog.id),
            func.coalesce(func.sum(UsageLog.credits), 0.0),
        )
        .filter(UsageLog.created_at >= since, ok_filter)
        .group_by(UsageLog.model)
        .order_by(func.coalesce(func.sum(UsageLog.credits), 0.0).desc())
        .limit(30)
        .all()
    )

    return {
        "range_days": days,
        "total_credits": round(sum(u["credits"] for u in usage), 4),
        "accounts": usage,
        "models": [
            {"model": m or "未知", "requests": int(c or 0),
             "credits": round(float(cr or 0), 4)}
            for m, c, cr in by_model
        ],
    }
