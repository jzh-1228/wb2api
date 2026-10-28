"""Authoritative, non-destructive account-pool health checks."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime

import httpx

from admin import backend, maintenance
from admin.config import settings
from admin.models import Account, ModelConfig
from admin.pool import POOL

_logger = logging.getLogger("admin.account_health")


def _error_result(exc: Exception) -> dict:
    from admin.routers.proxy import _extract_http_status

    status = getattr(exc, "http_status", 0) or _extract_http_status(str(exc))
    from admin.upstream_errors import error_info
    info = error_info(getattr(exc, "raw_body", "") or str(exc), status)
    return {"ok": False, "status": status or info.get("http_status") or 0,
            "code": info.get("code"),
            "message": str(info.get("message") or "上游请求失败")[:180]}


def _check_call(fn, *, validator=None) -> dict:
    started = time.perf_counter()
    try:
        value = fn()
        if validator and not validator(value):
            raise ValueError("上游返回为空或结构不完整")
        return {"ok": True, "status": 200,
                "elapsed_ms": round((time.perf_counter() - started) * 1000)}
    except Exception as exc:
        result = _error_result(exc)
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        return result


def _model_id(row) -> str:
    return str(row.get("id") or "").strip() if isinstance(row, dict) else str(row or "").strip()


def _select_chat_model(db, rows) -> tuple[str, bool]:
    """Prefer a current free text model; fall back to an enabled text model."""
    from model_metadata import is_image_model

    model_rows = [row for row in (rows or [])
                  if _model_id(row) and _model_id(row).lower() != "auto"
                  and not is_image_model(_model_id(row))]
    if not model_rows:
        return "", False

    free_ids = {row.model_id for row in db.query(ModelConfig).filter(
        ModelConfig.enabled == 1,
        (ModelConfig.credit_multiplier == 0) | (ModelConfig.credit_multiplier.is_(None)),
    ).all()}
    for row in model_rows:
        model = _model_id(row)
        credits = str(row.get("credits") or "").lower() if isinstance(row, dict) else ""
        if model in free_ids or "x0.00" in credits or "free" in credits:
            return model, True

    enabled_ids = {row.model_id for row in db.query(ModelConfig).filter(ModelConfig.enabled == 1).all()}
    for row in model_rows:
        model = _model_id(row)
        if model in enabled_ids:
            return model, False
    return _model_id(model_rows[0]), False


def _conversation_probe(sess, model: str) -> dict:
    """Send one minimal real upstream chat and require a normal completed stream."""
    from admin.routers.proxy import (
        _looks_like_inband_error, _sse_has_content, _sse_has_error_event,
        _sse_is_truncated,
    )

    body = {"model": model, "stream": True, "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}]}
    started = time.perf_counter()
    chunks: list[str] = []
    try:
        with sess.http_client(timeout=45) as client:
            with client.stream(
                    "POST", f"{settings.BACKEND}/v2/chat/completions",
                    headers=sess.get_headers(), json=body) as response:
                if response.status_code >= 400:
                    detail = response.read()
                    text = detail.decode("utf-8", "replace") if isinstance(detail, bytes) else str(detail)
                    from admin.upstream_errors import error_info
                    info = error_info(text[:65536], response.status_code)
                    return {"ok": False, "status": response.status_code,
                            "code": info.get("code"),
                            "message": str(info.get("message") or "上游对话请求失败")[:180],
                            "model": model,
                            "elapsed_ms": round((time.perf_counter() - started) * 1000)}
                for chunk in response.iter_text():
                    chunks.append(chunk)
                    if sum(map(len, chunks)) > 1_000_000:
                        raise ValueError("上游对话返回超过健康检查读取上限")
        text = "".join(chunks)
        if _looks_like_inband_error(text) or _sse_has_error_event(text):
            info = _error_result(RuntimeError(text[:65536]))
            return {**info, "ok": False, "model": model,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000)}
        if not _sse_has_content(text) or _sse_is_truncated(text):
            return {"ok": False, "status": 200, "code": None,
                    "message": "上游未返回完整的对话正文或结束标志",
                    "model": model,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000)}
        return {"ok": True, "status": 200, "code": None, "model": model,
                "elapsed_ms": round((time.perf_counter() - started) * 1000)}
    except Exception as exc:
        result = _error_result(exc)
        result["model"] = model
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        return result


def check_account(db, account: Account) -> dict:
    """Check balance, current model catalog, and one real conversation.

    This operation reports evidence but never disables an account because a
    single check failed. A successful three-part check restores an account
    previously disabled by session-dead automation, except for manual stops.
    """
    from admin.scheduler import _account_label

    label = _account_label(account)
    if account.status == "disabled" and (account.cool_kind or "") == "manual_disabled":
        return {"account_id": account.id, "account": label, "ok": False,
                "healthy": False, "status": "skipped", "reason": "账号为手动停用状态，未发起上游请求"}
    if POOL.waf.active():
        return {"account_id": account.id, "account": label, "ok": False,
                "healthy": False, "status": "skipped", "reason": "出口保护期间暂停健康检查"}

    uid = account.uid or f"account:{account.id}"
    lock = maintenance._batch_lock
    if not lock.acquire(blocking=False):
        return {"account_id": account.id, "account": label, "ok": False,
                "healthy": False, "status": "skipped", "reason": "另一个后台维护任务正在运行"}
    reserved = False
    sess = None
    original = account.auth_json
    try:
        reserved = POOL.inflight.acquire_maintenance(uid)
        if not reserved:
            return {"account_id": account.id, "account": label, "ok": False,
                    "healthy": False, "status": "skipped", "reason": "账号当前有在途请求或维护任务"}

        sess = backend.AccountSession(original)
        balance_value: dict = {}

        def fetch_balance():
            value = sess.fetch_balance()
            balance_value["value"] = value
            return value

        balance = _check_call(fetch_balance,
                              validator=lambda value: isinstance(value, dict)
                              and "total" in value and "remain" in value)
        if balance["ok"]:
            balance["total"] = int(balance_value["value"].get("total") or 0)
            balance["remain"] = int(balance_value["value"].get("remain") or 0)
        model_value: dict = {}

        def fetch_models():
            value = sess.fetch_models()
            model_value["value"] = value
            return value

        models = _check_call(fetch_models,
                             validator=lambda value: isinstance(value, list) and bool(value))
        model_rows = model_value.get("value") or []
        if models["ok"]:
            models["count"] = len(model_rows)
        model, free = _select_chat_model(db, model_rows) if models["ok"] else ("", False)
        if model:
            conversation = _conversation_probe(sess, model)
            conversation["free_model"] = free
        else:
            conversation = {"ok": False, "status": 0, "code": None,
                            "message": "无法取得可用于文本对话的模型目录",
                            "model": "", "free_model": False}

        healthy = bool(balance["ok"] and models["ok"] and conversation["ok"])
        account.auth_json = sess.updated_json()
        was_disabled = account.status == "disabled"
        if healthy:
            balance_data = balance_value["value"]
            account.balance_total = int(balance_data.get("total") or 0)
            account.balance_remain = int(balance_data.get("remain") or 0)
            account.last_sync_at = datetime.utcnow()
            account.session_dead_fails = 0
            if account.status == "disabled" and (account.cool_kind or "") == "session_dead":
                account.status = "active"
                account.cool_until = None
                account.cool_kind = ""
                account.err_count = 0
                account.breaker_fails = 0
                account.breaker_until = None
                account.degrade_until = None
                account.consecutive_fails = 0
                account.last_err_at = None
                account.last_err_msg = ""
                account.last_upstream_error = ""
            elif (account.last_err_msg or "").startswith(("登录态被上游拒绝", "上游认证接口异常")):
                account.last_err_at = None
                account.last_err_msg = ""
                account.last_upstream_error = ""
            db.commit()
        else:
            # Persist rotated credentials only. The health probe is diagnostic,
            # so an endpoint error cannot mutate account status/cooldowns.
            db.query(Account).filter(Account.id == account.id,
                                     Account.auth_json == original).update(
                {Account.auth_json: account.auth_json}, synchronize_session=False)
            db.commit()
        _logger.info("health_check id=%s uid=%s healthy=%s balance=%s models=%s chat=%s",
                     account.id, (account.uid or "")[:12], healthy,
                     balance["ok"], models["ok"], conversation["ok"])
        stages = {"balance": balance, "models": models, "conversation": conversation}
        reasons = [f"{name}: {result.get('message')}" for name, result in stages.items()
                   if not result["ok"]]
        return {"account_id": account.id, "account": label, "ok": healthy,
                "healthy": healthy, "status": "healthy" if healthy else "failed",
                "restored": bool(was_disabled and account.status == "active"), "stages": stages,
                "reason": "三项检查全部通过" if healthy else "；".join(reasons)}
    except Exception as exc:
        db.rollback()
        return {"account_id": account.id, "account": label, "ok": False,
                "healthy": False, "status": "error",
                "reason": str(_error_result(exc).get("message") or "健康检查失败")[:180]}
    finally:
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass
        if reserved:
            POOL.inflight.release_maintenance(uid)
        lock.release()


def run_health_batch(ids: list[int], job) -> dict:
    from admin.db import SessionLocal

    detail = []
    with SessionLocal() as db:
        for index, account_id in enumerate(ids):
            account = db.get(Account, account_id)
            if account is None:
                item = {"account_id": account_id, "account": str(account_id),
                        "ok": False, "healthy": False, "status": "error",
                        "reason": "账号不存在"}
            else:
                job.beat(account.name or account.uid or str(account.id))
                item = check_account(db, account)
            detail.append(item)
            job.add_item(item)
            if index + 1 < len(ids):
                time.sleep(min(60.0, max(0.0, settings.KEEPALIVE_ACCOUNT_GAP)))
    healthy = sum(bool(item.get("healthy")) for item in detail)
    skipped = sum(item.get("status") == "skipped" for item in detail)
    failed = len(detail) - healthy - skipped
    return {"ok": failed == 0 and skipped == 0, "total": len(detail),
            "healthy": healthy, "failed": failed, "skipped": skipped,
            "detail": detail,
            "summary": f"健康检查完成：健康 {healthy}/{len(detail)}，失败 {failed}，跳过 {skipped}"}
