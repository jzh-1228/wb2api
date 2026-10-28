"""API-key authenticated ASR; audio and transcripts never enter usage/audit logs."""
import asyncio
from contextlib import asynccontextmanager
import json

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from admin import backend, upstream_http
from admin.config import settings
from admin.db import get_db
from admin.models import Account
from admin.pool import POOL
from admin.security import get_key_row, check_quota, GatewayRejected
from admin.request_scheduling import pool_budget, release_connection
from admin.routers import proxy

router = APIRouter(tags=["audio"])
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # gateway limit; upstream may impose its own
MODEL = "workbuddy-asr"


class _AudioMultipartParser(MultiPartParser):
    def on_part_data(self, data, start, end):
        # Older production Starlette has no max_part_size constructor option.
        # Bound text fields explicitly on every supported runtime.
        if self._current_part.file is None and len(self._current_part.data) + end - start > 8192:
            raise MultiPartException("Text field too large")
        super().on_part_data(data, start, end)


def error(status, message, code):
    return JSONResponse(status_code=status, content={"error": {
        "message": message, "type": "invalid_request_error" if status < 500 else "server_error",
        "code": code}})


@asynccontextmanager
async def _audio_form(request):
    parser = _AudioMultipartParser(request.headers, request.stream(), max_files=1, max_fields=3)
    try:
        yield await parser.parse()
    finally:
        # Starlette only closes partial files on MultiPartException. Also close
        # them on cancellation/disconnect/truncated uploads, before a FormData
        # exists. This parser-owned list is present in both pinned app runtimes.
        for handle in parser._files_to_close_on_error:
            handle.close()


async def _until_disconnect(request, operation):
    async def disconnected():
        while not await request.is_disconnected():
            await asyncio.sleep(.25)
        raise ClientDisconnect()
    work = asyncio.create_task(operation)
    monitor = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait((work, monitor), return_when=asyncio.FIRST_COMPLETED)
        return await (work if work in done else monitor)
    finally:
        work.cancel()
        monitor.cancel()
        await asyncio.gather(work, monitor, return_exceptions=True)


@router.post("/v1/audio/transcriptions")
async def transcriptions(request: Request, db: Session = Depends(get_db),
                         x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                         authorization: str | None = Header(default=None)):
    token = x_api_key or (authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else "")
    key = await proxy._blocking_call(get_key_row, db, token) if token else None
    if key is None:
        return error(401, "缺少或无效 API Key", "invalid_api_key")
    try:
        check_quota(key)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    request.scope['_gateway_key_id'] = key.id
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        return error(400, "请使用 multipart/form-data 上传 file、model", "invalid_multipart")
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_AUDIO_BYTES + 65536:
        return error(413, "音频文件不能超过 25 MiB", "file_too_large")
    received = 0
    oversized = False
    async def limited_receive():
        nonlocal received, oversized
        message = await request.receive()
        received += len(message.get("body", b""))
        if received > MAX_AUDIO_BYTES + 65536:
            oversized = True
            # The parser closes temporary upload files on MultiPartException.
            raise MultiPartException("upload too large")
        return message
    upload_request = Request(request.scope, limited_receive)
    try:
        async with _audio_form(upload_request) as form:
            fields = list(form.multi_items())
            if any(name not in {"file", "model", "response_format"} for name, _ in fields) or len({name for name, _ in fields}) != len(fields):
                return error(400, "仅支持 file、model、response_format，且字段不可重复", "unsupported_parameter")
            if form.get("model") != MODEL:
                return error(400, f"model 必须为 {MODEL}", "invalid_model")
            response_format = form.get("response_format", "json")
            if response_format not in ("json", "text"):
                return error(400, "response_format 仅支持 json 或 text", "unsupported_response_format")
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                return error(400, "缺少音频文件 file", "missing_file")
            audio = await upload.read(MAX_AUDIO_BYTES + 1)
            if len(audio) > MAX_AUDIO_BYTES:
                return error(413, "音频文件不能超过 25 MiB", "file_too_large")
            if not audio:
                return error(400, "音频文件为空", "empty_file")
            filename = (upload.filename or "audio.webm").replace("\\", "/").split("/")[-1]
            mime = upload.content_type or "application/octet-stream"
    except (StarletteHTTPException, MultiPartException):
        return error(413 if oversized else 400, "音频文件不能超过 25 MiB" if oversized else "无效的 multipart 上传", "file_too_large" if oversized else "invalid_multipart")
    # Multipart files are closed before the upstream call; only bounded
    # in-memory bytes survive for this request's finite pool sweep.
    return await _until_disconnect(request, _transcribe(request, db, key, audio, filename, mime, response_format))


async def _transcribe(request, db, key, audio, filename, mime, response_format):
    budget = await proxy._blocking_call(pool_budget, db, key, [MODEL])
    tried = set()
    last_status = 503
    while budget.available():
        if POOL.waf.active():
            return error(503, "出口保护中，请稍后重试", "service_unavailable")
        acc = await proxy._blocking_call(proxy._select_account, db, exclude_ids=tried,
                                       account_id=key.account_id, key_id=key.id, min_balance=0, model=MODEL)
        if acc is None:
            break
        budget.consume()
        peers = await proxy._blocking_call(lambda: db.query(Account.id).filter(Account.uid == acc.uid).all()) if acc.uid else []
        tried.update([acc.id, *(row[0] for row in peers)])
        if not POOL.acquire_account(acc):
            continue
        sess = None
        try:
            sess = await proxy._account_session_async(db, acc)
            if sess is None:
                continue
            upstream_http.select_account(request.scope, acc)
            # Reuse only headers produced by the account session. This route
            # must not synthesize client-profile/device-risk headers.
            headers = proxy._headers_for_attempt(sess, {})
            headers = {k: v for k, v in headers.items() if k.lower() not in ("content-type", "content-length")}
            await proxy._blocking_call(release_connection, db)
            async with upstream_http.client(request, httpx.Timeout(30, connect=10), backend.HTTP_LIMITS, retry_budget=budget) as client:
                response = await client.post(settings.BACKEND + "/agenttool/v1/asr", headers=headers,
                                             files={"audio": (filename, audio, mime)})
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if response.is_success and isinstance(payload, dict) and payload.get("code", 0) in (0, "0", None) and isinstance(payload.get("text"), str):
                await proxy._blocking_call(proxy._note_success, db, acc)
                text = payload["text"]  # silence is a successful empty transcript
                return PlainTextResponse(text) if response_format == "text" else JSONResponse({"text": text})
            # Never persist upstream messages: they could echo audio/transcripts.
            code = payload.get("code") if isinstance(payload, dict) else None
            safe = json.dumps({"code": str(code)[:16] if str(code).isdigit() else None,
                               "message": "语音识别上游请求失败"}, ensure_ascii=False)
            last_status = response.status_code if response.status_code >= 400 else 502
            kind = proxy._classify_error(response.status_code, safe)
            if kind in proxy._RETRYABLE_KINDS:
                await proxy._blocking_call(proxy._apply_account_policy, db, acc, kind, last_status, safe,
                                           model=MODEL, reset_at=proxy._error_reset_at(response.headers, safe))
                # An upstream account's auth failure is not an invalid gateway
                # API key. Preserve rate limiting, normalize exhausted upstream
                # authentication/service failures to a gateway error.
                last_status = 429 if kind in {"hard_credit", "soft_rate", "model_rate"} else 502
            else:
                return error(last_status, "语音识别失败，请检查音频格式后重试", "transcription_failed")
        except GatewayRejected as exc:
            await proxy._record_gateway_rejection(exc, key.id, request)
            return JSONResponse(status_code=exc.status_code, content=exc.payload())
        except httpx.TransportError:
            last_status = 502
        finally:
            try:
                if sess is not None:
                    sess.close()
            finally:
                POOL.release(acc.uid or "")
    return error(last_status, "语音识别暂不可用，请稍后重试", "transcription_unavailable")
