"""模型配置管理：获取可用模型列表 / 系统级&用户级白名单 / 启用禁用。

使用方式：
  1. 先调用 POST /api/models/sync 从后端拉取可用模型列表（自动入库）
  2. 在列表中启用/禁用特定模型
  3. 代理转发时自动过滤不在白名单内的模型
"""
import httpx
import json
import time
import threading
import statistics
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin import backend
from admin.account_binding import scope_accounts
from admin.config import settings
from admin.db import get_db
from admin.models import Account, ModelConfig
from admin.security import require_admin
from model_metadata import metadata, credit_number, model_type, type_tags, is_image_model

router = APIRouter(prefix="/api/models", tags=["models"])
_sync_lock = threading.Lock()
_probe_lock = threading.Lock()


class ModelToggleIn(BaseModel):
    enabled: bool = True


class ModelBatchIn(BaseModel):
    model_ids: list[str] = []
    enabled: bool = True
    level: str = "system"  # system | user


class ModelDisplayIn(BaseModel):
    show_multiplier: bool = True
    show_busy: bool = True


@router.get("/display")
def model_display(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    from admin.model_listing import display_options
    return display_options(db)


@router.put("/display")
def save_model_display(body: ModelDisplayIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    from admin.models import SystemSetting
    row = db.get(SystemSetting, "model_display")
    if row is None:
        row = SystemSetting(key="model_display")
        db.add(row)
    row.value = json.dumps(body.dict())
    db.commit()
    return body.dict()


def _get_enabled_models(db: Session) -> set[str]:
    """获取当前启用的模型 ID 集合（用于代理过滤）。"""
    rows = db.query(ModelConfig).filter(ModelConfig.enabled == 1).all()
    return {m.model_id for m in rows}


def _get_free_models(db: Session) -> set[str]:
    """获取当前启用且免费的模型 ID 集合（credit_multiplier == 0）。"""
    rows = db.query(ModelConfig).filter(
        ModelConfig.enabled == 1,
        (ModelConfig.credit_multiplier == 0) | (ModelConfig.credit_multiplier.is_(None))
    ).all()
    return {m.model_id for m in rows if not is_image_model(m.model_id)}


def _is_model_allowed(db: Session, model_id: str) -> bool:
    """检查模型是否在白名单内。没有任何配置时放行全部（向后兼容）。"""
    configs = db.query(ModelConfig).all()
    if not configs:
        return True  # 未配置任何模型规则，放行
    enabled = {m.model_id for m in configs if m.enabled == 1}
    return model_id in enabled


@router.get("/configs")
def list_configs(
    level: Optional[str] = None,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """列出所有模型配置。"""
    q = db.query(ModelConfig)
    if level:
        q = q.filter(ModelConfig.level == level)
    rows = q.order_by(ModelConfig.id.asc()).all()
    return {
        "items": [
            {
                "id": m.id,
                "level": m.level,
                "model_id": m.model_id,
                "enabled": bool(m.enabled),
                "note": m.note,
                "description": m.description or "",
                "metadata": _stored_metadata(m),
                "model_type": model_type({"model_id": m.model_id, "metadata": _stored_metadata(m)}),
                "tags": type_tags({"model_id": m.model_id, "metadata": _stored_metadata(m)}),
                "billing_mode": "upstream_usage" if is_image_model(m.model_id) else "token_multiplier",
                "credit_multiplier": m.credit_multiplier or 0,
                "credits_raw": m.credits_raw or "",
                "is_free": not is_image_model(m.model_id) and (m.credit_multiplier or 0) == 0,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in rows
        ]
    }


def _description(model):
    value = model.get("descriptionZh") or model.get("descriptionEn") or model.get("description") or ""
    return value if isinstance(value, str) else ""


def _stored_metadata(row):
    try:
        value = json.loads(row.upstream_metadata or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


_description_checked = {}


@router.get("/descriptions")
def model_descriptions(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    # Refresh metadata without adding models or changing user configuration.
    candidates = scope_accounts(db.query(Account)).filter(Account.status == "active").order_by(Account.id).all()
    scope = tuple((a.id, a.uid) for a in candidates)
    with _sync_lock:
        if time.monotonic() - _description_checked.get(scope, -float("inf")) >= 300:
            try:
                catalog = _fetch_catalog(db)
                descriptions = {m.get("id"): m for m in catalog}
                for row in db.query(ModelConfig).all():
                    model = descriptions.get(row.model_id)
                    if model:
                        row.description = _description(model)
                        row.upstream_metadata = json.dumps(metadata(model), ensure_ascii=False)
                        price = credit_number(model.get("credits"))
                        if price is not None:
                            row.credit_multiplier = price
                            row.credits_raw = str(model["credits"])
                db.commit()
                _description_checked[scope] = time.monotonic()
                if len(_description_checked) > 128:
                    _description_checked.pop(next(iter(_description_checked)))
            except HTTPException:
                db.rollback()
                raise
        return {"items": [{"model_id": m.model_id, "description": m.description or "",
                           "metadata": _stored_metadata(m), "credit_multiplier": m.credit_multiplier,
                           "credits_raw": m.credits_raw}
                          for m in db.query(ModelConfig).all()]}


@router.get("/busy")
def model_busy(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """Upstream capacity is independent of local enablement and cooldowns."""
    from admin.model_busy import get_busy_levels, LABELS
    models = [row.model_id for row in db.query(ModelConfig.model_id).distinct().all()]
    result = get_busy_levels(db, models)
    return {"items": [{"model_id": model, "busy_level": level, "busy_label": LABELS[level]}
                      for model, level in result["levels"].items()],
            "observed_at": result["observed_at"], "stale": result["stale"]}


def _do_sync_models(db: Session, account_id: int | None = None) -> dict:
    with _sync_lock:
        return _sync_models_locked(db, account_id)


def _fetch_catalog(db, account_id=None):
    query = db.query(Account).filter(Account.status == "active")
    if account_id is not None:
        query = query.filter(Account.id == account_id)
    else:
        query = scope_accounts(query)
    candidates = query.order_by(Account.balance_remain.desc(), Account.id.asc()).all()
    if not candidates:
        raise HTTPException(status_code=503, detail="无可用账号（无法连接后端获取模型列表）")
    last_error = None
    attempts = 0
    for acc in candidates:
        try:
            from admin import maintenance
            with maintenance.account_slot(acc, db) as reason:
                if reason:
                    continue
                attempts += 1
                original = acc.auth_json
                with backend.AccountSession(original) as sess:
                    try:
                        models_raw = sess.fetch_models()
                    finally:
                        updated_auth = sess.updated_json()
                        db.query(Account).filter(Account.id == acc.id, Account.auth_json == original).update(
                            {Account.auth_json: updated_auth}, synchronize_session=False)
                        db.commit()
                break
        except Exception as error:
            db.rollback()
            maintenance.note_failure(db, acc, error)
            db.commit()
            last_error = error
            if attempts >= 3:
                raise HTTPException(status_code=502, detail="获取模型目录失败，请稍后重试")
    else:
        if last_error is None:
            raise HTTPException(status_code=503, detail="账号处于保护或在途占用，稍后同步模型")
        raise HTTPException(status_code=502, detail="获取模型目录失败，请稍后重试")

    return models_raw


def _sync_models_locked(db: Session, account_id: int | None = None) -> dict:
    """从后端拉取可用模型列表并同步到本地数据库（upsert）。

    需要有效的 active 账号；余额为 0 也允许查询模型目录。
    """
    # A newly imported account can have a stale/empty local balance until its
    # background refresh completes.  Model discovery only needs a valid token,
    # so do not reject it based on the cached credit value.
    models_raw = _fetch_catalog(db, account_id)

    import re

    added = 0
    updated = 0
    seen = {m.model_id for m in db.query(ModelConfig.model_id).all()}
    for m in models_raw:
        mid = m.get("id")
        if not mid or mid.lower() == "auto":
            continue  # 跳过上游聚合模型 "auto"，我们系统有自己的免费优先 auto 逻辑
        # 解析积分倍率
        raw_credits = m.get("credits") or ""
        multiplier = credit_number(raw_credits) or 0.0
        if raw_credits:
            cre_match = re.search(r"x\s*([0-9]+(?:\.[0-9]+)?)", str(raw_credits))
            if cre_match:
                try:
                    multiplier = float(cre_match.group(1))
                except (ValueError, TypeError):
                    pass
        if mid in seen:
            # 已存在：覆盖倍率 / 原始 credits / 备注（保留用户启停状态）
            mc = db.query(ModelConfig).filter(ModelConfig.model_id == mid).first()
            if mc:
                mc.credit_multiplier = multiplier
                mc.credits_raw = str(raw_credits)
                mc.description = _description(m)
                mc.upstream_metadata = json.dumps(metadata(m), ensure_ascii=False)
                if not mc.note:
                    mc.note = m.get("name") or ""
                updated += 1
            continue
        seen.add(mid)
        mc = ModelConfig(
            level="system",
            model_id=mid,
            enabled=1,
            note=m.get("name") or "",
            credit_multiplier=multiplier,
            credits_raw=str(raw_credits),
            description=_description(m),
            upstream_metadata=json.dumps(metadata(m), ensure_ascii=False),
        )
        db.add(mc)
        added += 1
    db.commit()
    total = db.query(ModelConfig).count()
    enabled_count = db.query(ModelConfig).filter(ModelConfig.enabled == 1).count()
    return {
        "total_fetched": len(models_raw),
        "added": added,
        "updated": updated,
        "total_in_db": total,
        "enabled": enabled_count,
    }


@router.post("/sync")
def sync_models(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """从后端拉取可用模型列表并同步到本地数据库（upsert）。"""
    return _do_sync_models(db)


@router.patch("/{config_id}")
def toggle_model(
    config_id: int,
    body: ModelToggleIn,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """启用/禁用某个模型。"""
    mc = db.query(ModelConfig).filter(ModelConfig.id == config_id).first()
    if not mc:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    mc.enabled = 1 if body.enabled else 0
    if body.enabled and mc.note:
        pass  # keep existing note
    db.commit()
    return {"id": mc.id, "model_id": mc.model_id, "enabled": bool(mc.enabled)}


@router.post("/batch-toggle")
def batch_toggle(
    body: ModelBatchIn,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """批量启用/禁用模型。"""
    if body.level not in ("system", "user"):
        raise HTTPException(400, "无效模型级别")
    query = db.query(ModelConfig).filter(ModelConfig.level == body.level)
    if body.model_ids:
        query = query.filter(ModelConfig.model_id.in_(body.model_ids))
    count = query.update({ModelConfig.enabled: int(body.enabled)}, synchronize_session=False)
    db.commit()
    return {"updated": count, "enabled": body.enabled}


@router.delete("/{config_id}")
def delete_config(
    config_id: int,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """删除某条模型配置（从白名单移除 = 禁用该模型）。"""
    mc = db.query(ModelConfig).filter(ModelConfig.id == config_id).first()
    if not mc:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    mid = mc.model_id
    db.delete(mc)
    db.commit()
    return {"id": config_id, "model_id": mid, "ok": True}


# ---------------------------------------------------------------------------
# 测速：用账号池直接对上游发请求，免 API Key
# ---------------------------------------------------------------------------

_SPEED_TEST_PROMPT = [{"role": "user", "content": "用一句话介绍一下你自己。"}]


@router.post("/speed-test")
async def speed_test(
    body: dict,
    request: Request,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not _probe_lock.acquire(blocking=False):
        raise HTTPException(429, "已有模型检测正在进行", headers={"Retry-After": "3"})
    try:
        return await _speed_test_run(body, request, db)
    finally:
        _probe_lock.release()


async def _speed_test_run(body, request, db):
    """A real, bounded model request; upstream may charge credits."""
    model = str(body.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="缺少 model")
    if is_image_model(model):
        raise HTTPException(400, "图片模型请通过生图接口调用，不支持文本测速")
    try:
        max_tokens = max(16, min(512, int(body.get("max_tokens") or 64)))
    except (TypeError, ValueError):
        max_tokens = 64
    messages = body.get("messages") or _SPEED_TEST_PROMPT

    if not _is_model_allowed(db, model):
        raise HTTPException(400, "模型未启用")
    from admin.routers.proxy import (_blocking_call, _select_account, _classify_error,
                                     _apply_account_policy, _parse_usage, _looks_like_inband_error,
                                     _error_reset_at)
    from admin.pool import POOL
    if POOL.waf.active():
        raise HTTPException(503, "出口保护中，稍后检测", headers={"Retry-After": str(max(1, POOL.waf.remaining()))})
    acc = await _blocking_call(_select_account, db, model=model, mark_picked=False)
    if not acc or not POOL.acquire(acc.uid or ""):
        raise HTTPException(503, "当前没有可检测账号（冷却或正在使用），稍后重试", headers={"Retry-After": "3"})
    from admin import upstream_http
    sess = None
    try:
        sess = await _blocking_call(backend.AccountSession, acc.auth_json, on_cancel=lambda session: session.close())
        headers = await _blocking_call(sess.get_headers)
    except BaseException:
        if sess is not None:
            sess.close()
        POOL.release(acc.uid or "")
        raise
    url = f"{settings.BACKEND}/v2/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": max_tokens,
    }

    t0 = time.perf_counter()
    ttft = None
    completion_tokens = None
    total_tokens = None
    prompt_tokens = None
    sample = ""
    sample_len = 0
    intervals = []
    previous_delta = None
    headers_ms = None
    completed = False
    raw_lines = []
    failure_kind = "transport"

    def _persist():
        try:
            acc.auth_json = sess.updated_json()  # 写回可能刷新的 token
            from admin.models import UsageLog
            usage = _parse_usage('\n'.join(raw_lines)) if completed else {}
            db.add(UsageLog(account_id=acc.id, model=model, api_key_id=0, use_case="model-probe",
                            error_kind="success" if completed else failure_kind,
                            ttfb_ms=round(ttft * 1000) if ttft is not None else None,
                            latency_ms=round((time.perf_counter() - t0) * 1000),
                            credits=usage.get('credits') or 0, prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens, total_tokens=total_tokens))
            db.commit()
        except Exception:
            db.rollback()

    try:
        async with upstream_http.client(request, 120, backend.HTTP_LIMITS) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as r:
                headers_ms = round((time.perf_counter() - t0) * 1000, 1)
                if r.status_code >= 400:
                    detail = await r.aread()
                    msg = detail.decode("utf-8", "replace")[:300]
                    failure_kind = _classify_error(r.status_code, msg)
                    await _blocking_call(_apply_account_policy, db, acc, failure_kind, r.status_code, msg,
                                         model=model, reset_at=_error_reset_at(r.headers, msg))
                    raise HTTPException(status_code=r.status_code, detail=f"上游返回错误: {msg}")
                async for chunk in r.aiter_lines():
                    # TTFT：首个非空增量（content 或 reasoning_content）即首字延迟
                    for line in chunk.splitlines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        p = line[5:].strip()
                        if p == "[DONE]":
                            completed = True
                            continue
                        if not p:
                            continue
                        raw_lines.append('data: ' + p)
                        if _looks_like_inband_error(p):
                            failure_kind = _classify_error(200, p)
                            await _blocking_call(_apply_account_policy, db, acc, failure_kind, 200, p, model=model,
                                                 reset_at=_error_reset_at(r.headers, p))
                            raise HTTPException(502, "上游模型返回错误，请查看账号模型冷却状态")
                        try:
                            obj = json.loads(p)
                        except Exception:
                            continue
                        usage = obj.get("usage")
                        if isinstance(usage, dict):
                            if usage.get("prompt_tokens") is not None:
                                prompt_tokens = usage["prompt_tokens"]
                            if usage.get("completion_tokens") is not None:
                                completion_tokens = usage["completion_tokens"]
                            if usage.get("total_tokens") is not None:
                                total_tokens = usage["total_tokens"]
                        for ch in obj.get("choices") or []:
                            if ch.get('finish_reason') is not None:
                                completed = True
                            delta = ch.get("delta") or {}
                            # 推理类模型把思考内容放在 reasoning_content（content 可能为空），
                            # 样例优先取 content，缺省回退 reasoning_content，保证总能看到输出。
                            piece = (delta.get("content") or "") or (delta.get("reasoning_content") or "")
                            if piece:
                                now = time.perf_counter()
                                if ttft is None:
                                    ttft = now - t0
                                if previous_delta is not None:
                                    intervals.append((now - previous_delta) * 1000)
                                previous_delta = now
                            if piece and sample_len < 400:
                                add = piece[:400 - sample_len]
                                sample += add
                                sample_len += len(add)
            if ttft is None or not completed:
                completed = False
                raise HTTPException(502, "测速流未完整结束或未返回有效文字")
            t_end = time.perf_counter()
    except HTTPException:
        completed = False
        raise
    except Exception as e:
        completed = False
        raise HTTPException(status_code=502, detail=f"测速请求失败: {e}")
    except BaseException:
        completed = False
        failure_kind = "cancelled"
        raise
    finally:
        import anyio
        with anyio.CancelScope(shield=True):
            try:
                await _blocking_call(_persist)
            finally:
                sess.close()
                POOL.release(acc.uid or "")
        from admin.post_login import enqueue_resources
        enqueue_resources(acc.id)

    total_dur = t_end - t0
    gen_dur = (total_dur - ttft) if ttft is not None else total_dur
    gen_dur = max(gen_dur, 0.0)
    speed_tps = round(completion_tokens / gen_dur, 2) if completion_tokens and gen_dur > 0 else None
    overall_tps = round(completion_tokens / total_dur, 2) if completion_tokens and total_dur > 0 else None
    ttft_ms = round(ttft * 1000, 1) if ttft is not None else None

    meta = backend.parse_auth_meta(acc.auth_json)
    return {
        "model": model,
        "health": "healthy",
        "observed_at": datetime.utcnow().isoformat() + 'Z',
        "headers_ms": headers_ms,
        "inter_delta_jitter_ms": round(statistics.pstdev(intervals), 1) if len(intervals) >= 2 else None,
        "jitter_samples": len(intervals),
        "jitter_scope": "inter_delta_including_generation",
        "prompt_tokens": prompt_tokens,
        "account": meta.get("nickname") or meta.get("uid") or "",
        "ttft_ms": ttft_ms,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "gen_duration_ms": round(gen_dur * 1000, 1),
        "total_duration_ms": round(total_dur * 1000, 1),
        "speed_tps": speed_tps,
        "overall_tps": overall_tps,
        "sample": sample,
    }
