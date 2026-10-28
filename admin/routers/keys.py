"""API Key 管理：创建（带积分限额）/ 列表 / 查看 / 调整 / 吊销 / 用量。"""
import base64
import secrets
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from admin.account_binding import validate_binding
from admin.db import get_db
from admin.models import Account, ApiKey, BlockedIP, ModelGroup, UsageLog
from admin.security import hash_key, require_admin

router = APIRouter(prefix="/api/keys", tags=["keys"])
_OK_KINDS = ("", "success")
_UNKNOWN_IPS = ("", "unknown")


class KeyIn(BaseModel):
    name: str = ""
    credit_limit: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    unlimited: bool | None = None
    note: str = ""
    group_id: int = 0  # 0 = 不绑定分组（可用全部启用模型）
    account_id: int | None = None  # 0 / NULL = 公共池
    # 该 Key 并发上限：None=跟随全局；0=不限；N=最多 N 路在途
    max_concurrent: int | None = None
    rate_per_minute: int | None = None


def _gen_key() -> str:
    # sk- 前缀 + 256-bit 密码学安全随机（secrets，无时间戳/序号/可推断规律）
    return "sk-" + secrets.token_hex(32)


def _mask(key: str) -> str:
    return key[:10] + "****" + key[-4:] if len(key) > 16 else key[:6] + "****"


def _encode_key(raw: str) -> str:
    """简单编码存储完整 key（base64，非加密；管理后台已有 JWT 保护）。"""
    return base64.b64encode(raw.encode()).decode()


def _decode_key(encoded: str) -> str:
    try:
        return base64.b64decode(encoded.encode()).decode()
    except Exception:
        return ""


def _key_ip_summaries(db: Session, key_ids: list[int], *, days: int = 1) -> dict[int, dict]:
    """Aggregate history once for all keys; live requests do not add completed calls."""
    summaries = {key_id: {"ips": set(), "requests": 0, "active_ips": set(),
                          "active_requests": 0, "active_ip_counts": {}, "ip_requests": {}} for key_id in key_ids}
    if not summaries:
        return {}
    start = datetime.utcnow() - timedelta(days=days)
    rows = (db.query(UsageLog.api_key_id, UsageLog.client_ip, func.count(UsageLog.id))
            .filter(UsageLog.api_key_id.in_(key_ids), UsageLog.created_at >= start,
                    UsageLog.client_ip.isnot(None), UsageLog.client_ip.notin_(_UNKNOWN_IPS))
            .group_by(UsageLog.api_key_id, UsageLog.client_ip).all())
    for key_id, ip, count in rows:
        item = summaries.get(int(key_id))
        if item is not None:
            item["ips"].add(ip)
            item["requests"] += int(count or 0)
            item["ip_requests"][ip] = int(count or 0)

    # Label the live scope explicitly for deployments with multiple workers.
    from admin import request_metrics
    for key_id, active in request_metrics.key_ip_snapshot(key_ids).items():
        item = summaries.get(key_id)
        if item is None:
            continue
        item["active_ips"].update(active["ips"])
        item["active_requests"] += int(active["active_requests"] or 0)
        item["active_ip_counts"] = {ip: value['active_requests'] for ip, value in active['ips'].items()}

    return {key_id: {
        "days": days,
        "unique_ips": len(item["ips"] | item["active_ips"]),
        "requests": item["requests"],
        "active_ips": len(item["active_ips"]),
        "active_requests": item["active_requests"],
        "active_ip_counts": item["active_ip_counts"],
        "top_ips": [{"ip": ip, "active_requests": item["active_ip_counts"].get(ip, 0),
                     "requests": item["ip_requests"].get(ip, 0)}
                    for ip in sorted(item["ips"] | item["active_ips"],
                                     key=lambda ip: (-item["active_ip_counts"].get(ip, 0),
                                                     -item["ip_requests"].get(ip, 0), ip))[:3]],
        "active_scope": "当前服务进程",
    } for key_id, item in summaries.items()}


@router.get("")
def list_keys(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.query(ApiKey).order_by(ApiKey.id.desc()).all()
    group_names = {g.id: g.name for g in db.query(ModelGroup).all()}
    account_names = {a.id: a.name or a.uid for a in db.query(Account).all()}
    ip_summaries = _key_ip_summaries(db, [int(k.id) for k in rows])
    items = [
        {
            "id": k.id,
            "name": k.name,
            "key_prefix": k.key_prefix,
            "masked": _mask(k.key_prefix + "****************"),
            "credit_limit": k.credit_limit,
            "credit_used": k.credit_used,
            "unlimited": bool(k.unlimited),
            "max_concurrent": k.max_concurrent,
            "rate_per_minute": k.rate_per_minute,
            "status": k.status,
            "note": k.note,
            "group_id": int(k.group_id or 0),
            "group_name": group_names.get(int(k.group_id or 0), ""),
            "account_id": k.account_id,
            "account_name": account_names.get(k.account_id, ""),
            "created_at": k.created_at.isoformat() if k.created_at else None,
            "ip_summary": ip_summaries.get(k.id, {"days": 1, "unique_ips": 0,
                                                       "requests": 0, "active_ips": 0,
                                                       "active_requests": 0,
                                                       "active_scope": "当前服务进程"}),
        }
        for k in rows
    ]
    return {"items": items}


def _normalize_max_concurrent(value) -> int | None:
    """规范化流量上限：None=跟随全局；0=不限；N>=1=该项上限。"""
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _validate_group(db: Session, group_id: int) -> int:
    """校验分组存在；0 表示不绑定（允许）。返回规范化后的 group_id。"""
    gid = int(group_id or 0)
    if gid and not db.query(ModelGroup).filter(ModelGroup.id == gid).first():
        raise HTTPException(status_code=400, detail=f"分组 #{gid} 不存在")
    return gid


@router.post("")
def create_key(body: KeyIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    raw = _gen_key()
    gid = _validate_group(db, body.group_id)
    aid = validate_binding(db, body.account_id)
    k = ApiKey(
        name=body.name or "未命名",
        key_hash=hash_key(raw),
        key_prefix=raw[:14],
        key_full=_encode_key(raw),
        credit_limit=body.credit_limit if body.credit_limit is not None else 0,
        unlimited=int(body.credit_limit is None or body.unlimited is True),
        max_concurrent=_normalize_max_concurrent(body.max_concurrent),
        rate_per_minute=_normalize_max_concurrent(body.rate_per_minute),
        note=body.note,
        group_id=gid,
        account_id=aid,
    )
    db.add(k)
    _commit_key(db)
    db.refresh(k)
    # 明文 key 仅此返回一次
    return {
        "id": k.id,
        "name": k.name,
        "key": raw,
        "key_prefix": k.key_prefix,
        "credit_limit": k.credit_limit,
        "unlimited": bool(k.unlimited),
        "max_concurrent": k.max_concurrent,
        "rate_per_minute": k.rate_per_minute,
        "group_id": int(k.group_id or 0),
        "account_id": k.account_id,
    }


@router.get("/{key_id}/view")
def view_key(key_id: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """查看 Key 完整值（创建后可再次查看）。"""
    k = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not k:
        raise HTTPException(status_code=404, detail="Key 不存在")
    raw = _decode_key(k.key_full)
    if not raw:
        raise HTTPException(status_code=410, detail="该 Key 创建于「查看功能」上线前，完整值未存储。建议重新生成一个新 Key。")
    return {
        "id": k.id,
        "name": k.name,
        "key": raw,
        "key_prefix": k.key_prefix,
        "created_at": k.created_at.isoformat() if k.created_at else None,
    }


@router.get("/{key_id}/usage")
def key_usage(key_id: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """查看 Key 的积分额度消耗情况。"""
    k = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not k:
        raise HTTPException(status_code=404, detail="Key 不存在")
    pct = ((k.credit_used / k.credit_limit) * 100) if k.credit_limit > 0 and not k.unlimited else None
    return {
        "id": k.id,
        "name": k.name,
        "credit_limit": k.credit_limit,
        "credit_used": round(k.credit_used, 2),
        "credit_remaining": (round(max(0, k.credit_limit - k.credit_used), 2) if not k.unlimited else None),
        "usage_percent": round(pct, 1) if pct is not None else None,
        "unlimited": bool(k.unlimited),
        "status": k.status,
        "created_at": k.created_at.isoformat() if k.created_at else None,
    }


@router.get("/{key_id}/ips")
def key_ips(
    key_id: int,
    days: int = Query(default=1, ge=1, le=90),
    sort: Literal["requests", "failed", "credits", "tokens", "last_seen", "ip"] = "requests",
    direction: Literal["asc", "desc"] = "desc",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: bool = Depends(require_admin), db: Session = Depends(get_db),
):
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if key is None:
        raise HTTPException(404, "Key 不存在")
    start = datetime.utcnow() - timedelta(days=days)
    ok = UsageLog.error_kind.in_(_OK_KINDS)
    counts = (db.query(
        UsageLog.client_ip.label("ip"), func.count(UsageLog.id).label("requests"),
        func.sum(case((ok, 0), else_=1)).label("failed"),
        func.coalesce(func.sum(case((ok, UsageLog.credits), else_=0)), 0).label("credits"),
        func.coalesce(func.sum(case((ok, UsageLog.total_tokens), else_=0)), 0).label("tokens"),
        func.max(UsageLog.created_at).label("last_seen"))
        .filter(UsageLog.api_key_id == key_id, UsageLog.created_at >= start,
                UsageLog.client_ip.isnot(None), UsageLog.client_ip.notin_(_UNKNOWN_IPS))
        .group_by(UsageLog.client_ip).subquery())
    column = getattr(counts.c, sort)
    order = column.asc() if direction == "asc" else column.desc()
    rows = (db.query(counts).order_by(order, counts.c.ip.asc())
            .offset(offset).limit(limit).all())
    total_ips, total_requests = db.query(
        func.count(), func.coalesce(func.sum(counts.c.requests), 0)).select_from(counts).one()
    from admin import request_metrics
    live = request_metrics.key_ip_snapshot([key_id]).get(key_id, {})
    active = live.get("ips", {})
    # 每个 IP 的模型用量（调用记录窗口内）：供「来源 IP」弹窗直接展示。
    # recent = 15 分钟内用过；live = 此刻在途 —— 两者合并成一份 badge 数据。
    model_rows = (db.query(UsageLog.client_ip, UsageLog.model,
                           func.count(UsageLog.id),
                           func.coalesce(func.sum(UsageLog.total_tokens), 0),
                           func.max(UsageLog.created_at))
                  .filter(UsageLog.api_key_id == key_id, UsageLog.created_at >= start,
                          UsageLog.model.isnot(None), UsageLog.model != "",
                          UsageLog.client_ip.isnot(None), UsageLog.client_ip.notin_(_UNKNOWN_IPS))
                  .group_by(UsageLog.client_ip, UsageLog.model).all())
    recent_cut = datetime.utcnow() - timedelta(minutes=15)
    per_ip_models = {}
    for ip, model, count, tokens, last_seen in model_rows:
        per_ip_models.setdefault(ip, []).append({
            "model": model, "requests": int(count), "tokens": int(tokens),
            "last_seen": last_seen, "recent": bool(last_seen and last_seen >= recent_cut)})
    def _models_for(ip):
        entries = {e["model"]: dict(e) for e in per_ip_models.get(ip, [])}
        for model, count in sorted((active.get(ip, {}).get("models") or {}).items(),
                                   key=lambda kv: -kv[1]):
            entry = entries.setdefault(model, {"model": model, "requests": 0, "tokens": 0,
                                               "last_seen": None, "recent": False})
            entry["live"] = True
            entry["live_requests"] = int(count)
        return sorted(entries.values(), key=lambda e: (-(e.get("live_requests") or 0),
                                                       -e["requests"]))[:6]
    addresses = {row.ip for row in rows} | set(active)
    blocked = {row.ip: row.id for row in db.query(BlockedIP).filter(
        BlockedIP.enabled == 1, BlockedIP.ip.in_(addresses)).all()} if addresses else {}
    from admin.routers.ip_management import identity_items
    devices = identity_items(db, key_id=key_id, start=start)
    return {
        "id": key_id, "name": key.name, "days": days, "sort": sort,
        "direction": direction, "offset": offset, "limit": limit,
        "total_ips": int(total_ips), "total_requests": int(total_requests),
        "active_ips": len(active), "active_requests": live.get("active_requests", 0),
        "active_scope": "当前服务进程",
        "devices": devices,
        "active": [{"ip": ip, **item,
                    "models": [{"model": m, "live_requests": c} for m, c
                               in sorted((item.get("models") or {}).items(),
                                         key=lambda kv: -kv[1])],
                    "blocked": ip in blocked,
                    "blocked_id": blocked.get(ip)} for ip, item in sorted(active.items())],
        "items": [{"ip": row.ip, "requests": int(row.requests), "failed": int(row.failed or 0),
                   "credits": round(float(row.credits), 4), "tokens": int(row.tokens),
                   "last_seen": row.last_seen.isoformat() if row.last_seen else None,
                   "models": _models_for(row.ip),
                   "active_requests": active.get(row.ip, {}).get("active_requests", 0),
                   "blocked": row.ip in blocked, "blocked_id": blocked.get(row.ip)} for row in rows],
    }


@router.patch("/{key_id}")
def patch_key(key_id: int, body: dict, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    if "status" in body and body["status"] not in ("active", "revoked"):
        raise HTTPException(400, "Key 状态必须为 active 或 revoked")
    k = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not k:
        raise HTTPException(status_code=404, detail="Key 不存在")
    if "account_id" in body:
        k.account_id = validate_binding(db, body["account_id"], key_id)
    if "name" in body:
        k.name = body["name"]
    if "credit_limit" in body:
        value = body["credit_limit"]
        if value is None or value == "":
            k.credit_limit = 0
            k.unlimited = 1
        else:
            import math
            try:
                value = float(value)
                if not math.isfinite(value) or value < 0:
                    raise ValueError()
            except (ValueError, TypeError):
                raise HTTPException(400, "Key 额度必须为空或大于等于 0 的有限数字") from None
            k.credit_limit = value
            k.unlimited = 0
    if "unlimited" in body:
        if body["unlimited"]:
            k.unlimited = 1
        elif "credit_limit" not in body or body["credit_limit"] not in (None, ""):
            k.unlimited = 0
    if "max_concurrent" in body:
        k.max_concurrent = _normalize_max_concurrent(body["max_concurrent"])
    if "rate_per_minute" in body:
        k.rate_per_minute = _normalize_max_concurrent(body["rate_per_minute"])
    if "status" in body:
        k.status = body["status"]
    if "note" in body:
        k.note = body["note"]
    if "group_id" in body:
        k.group_id = _validate_group(db, body["group_id"])
    if body.get("reset_used"):
        k.credit_used = 0
    _commit_key(db)
    return {"id": k.id, "ok": True}


def _commit_key(db):
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "该账号已被其他 API Key 独占绑定，请刷新后重试") from None


@router.delete("/{key_id}")
def delete_key(key_id: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    k = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not k:
        raise HTTPException(status_code=404, detail="Key 不存在")
    db.delete(k)
    db.commit()
    return {"id": key_id, "ok": True}
