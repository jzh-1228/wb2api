"""OpenAI Images API over the account pool and official /v2/images routes."""
import base64
from contextlib import asynccontextmanager
import json
import math
import time
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from admin import request_metrics, upstream_http
from admin.config import settings
from admin.db import get_db
from admin.models import Account
from admin.pool import POOL
from admin.request_scheduling import pool_budget, release_connection
from admin.routers import proxy
from admin.routers.audio import _AudioMultipartParser, _until_disconnect
from admin.security import GatewayRejected, check_quota, get_key_row

router = APIRouter(tags=['images'])
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_BODY_BYTES = 32 * 1024 * 1024
FIELDS = {'model', 'prompt', 'n', 'size', 'response_format', 'quality', 'style',
          'user', 'image', 'image_url', 'image_data'}


@asynccontextmanager
async def _image_form(request):
    parser = _AudioMultipartParser(request.headers, request.stream(), max_files=1, max_fields=12)
    try:
        yield await parser.parse()
    finally:
        for handle in parser._files_to_close_on_error:
            handle.close()


def error(status, message, code):
    return JSONResponse(status_code=status, content={'error': {'message': message,
        'type': 'invalid_request_error' if status < 500 else 'server_error', 'code': code}})


async def _payload(request, edit):
    received = 0
    async def receive():
        nonlocal received
        message = await request.receive()
        received += len(message.get('body', b''))
        if received > MAX_BODY_BYTES:
            raise MultiPartException('image_body_too_large')
        return message
    limited = Request(request.scope, receive)
    multipart = request.headers.get('content-type', '').lower().startswith('multipart/form-data')
    if multipart:
        if not edit:
            raise ValueError('文生图请使用 application/json')
        async with _image_form(limited) as form:
            pairs = list(form.multi_items())
            if len({k for k, _ in pairs}) != len(pairs):
                raise ValueError('字段不可重复；当前仅支持一张输入图片')
            result = dict(pairs)
            upload = result.get('image')
            if isinstance(upload, UploadFile):
                content = await upload.read(MAX_IMAGE_BYTES + 1)
                if not content or len(content) > MAX_IMAGE_BYTES:
                    raise ValueError('图片不能为空，且不能超过 20 MiB')
                result.pop('image')
                result['image_data'] = base64.b64encode(content).decode('ascii')
    else:
        try:
            result = json.loads(await limited.body())
        except (ValueError, UnicodeError):
            raise ValueError('无效的 JSON 请求') from None
    if not isinstance(result, dict):
        raise ValueError('请求体必须为 JSON 对象')
    if set(result) - FIELDS:
        raise ValueError('不支持的参数：' + ', '.join(sorted(set(result) - FIELDS)))
    if any(isinstance(value, UploadFile) for value in result.values()):
        raise ValueError('仅 image 字段支持文件上传')
    if not isinstance(result.get('model'), str) or not result['model'].strip() or result['model'] == 'auto':
        raise ValueError('请指定已启用的生图模型，例如 hunyuan-image-alpha')
    if not isinstance(result.get('prompt'), str) or not result['prompt'].strip():
        raise ValueError('prompt 必须为非空字符串')
    raw_n = result.get('n', 1)
    if multipart and isinstance(raw_n, str) and raw_n.isdigit():
        raw_n = int(raw_n)
    if type(raw_n) is not int or not 1 <= raw_n <= 10:
        raise ValueError('n 必须为 1 到 10 的整数；具体数量还受模型限制')
    result['n'] = raw_n
    if result.get('response_format', 'url') not in ('url', 'b64_json'):
        raise ValueError('response_format 仅支持 url 或 b64_json')
    for name in ('size', 'quality', 'style', 'user'):
        if name in result and not isinstance(result[name], str):
            raise ValueError(name + ' 必须为字符串')
    sources = [name for name in ('image', 'image_data', 'image_url') if name in result]
    if (edit and len(sources) != 1) or (not edit and sources):
        raise ValueError('图生图需要且只能提供 image、image_data 或 image_url 中的一项')
    if sources:
        value = result[sources[0]]
        if not isinstance(value, str) or not value.strip():
            raise ValueError('输入图片必须为非空字符串或上传文件')
        if sources[0] == 'image_url':
            parsed = urlsplit(value)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('image_url 必须为 HTTPS 图片地址')
        else:
            encoded = value.split(',', 1)[1] if value.startswith('data:image/') and ',' in value else value
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except ValueError:
                raise ValueError('图片数据必须为有效的 base64') from None
            if not decoded or len(decoded) > MAX_IMAGE_BYTES:
                raise ValueError('图片不能为空，且不能超过 20 MiB')
            result.pop(sources[0])
            result['image_data'] = encoded
    return result


async def _base64_image(url):
    # No gateway credentials, redirects, or arbitrary URL fetching. The real
    # upstream currently returns signed Tencent COS URLs even for b64_json.
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not host.endswith('.myqcloud.com') or '.cos.' not in host):
        raise ValueError('上游返回的图片地址不支持安全转换，请使用 response_format=url')
    content = bytearray()
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
        async with client.stream('GET', url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_IMAGE_BYTES:
                    raise ValueError('上游图片超过 20 MiB，请使用 response_format=url')
    if not content:
        raise ValueError('上游返回空图片')
    return base64.b64encode(content).decode('ascii')


def _result(payload):
    if not isinstance(payload, dict) or payload.get('code', 0) not in (0, '0', None):
        return None
    result = payload.get('data') if isinstance(payload.get('data'), dict) else payload
    rows = result.get('data')
    if not isinstance(rows, list) or not rows:
        return None
    if not all(isinstance(row, dict) and (isinstance(row.get('url'), str) and row['url']
               or isinstance(row.get('b64_json'), str) and row['b64_json']) for row in rows):
        return None
    return result


@router.post('/v1/images/generations')
@router.post('/v1/images/edits')
async def images(request: Request, db: Session = Depends(get_db),
                 x_api_key: str | None = Header(default=None, alias='X-API-Key'),
                 authorization: str | None = Header(default=None)):
    token = x_api_key or (authorization[7:].strip() if authorization and authorization.startswith('Bearer ') else '')
    key = await proxy._blocking_call(get_key_row, db, token) if token else None
    if key is None:
        return error(401, '缺少或无效 API Key', 'invalid_api_key')
    try:
        check_quota(key)
        request.scope['_gateway_key_id'] = key.id
        await proxy._blocking_call(release_connection, db)
        edit = request.url.path.endswith('/edits')
        try:
            body = await _payload(request, edit)
        except MultiPartException as exc:
            return error(413 if str(exc) == 'image_body_too_large' else 400, '图片请求过大或上传格式错误', 'invalid_image_upload')
        except (ValueError, StarletteHTTPException) as exc:
            return error(400, str(exc), 'invalid_image_request')
        model = body['model']
        allowed = await proxy._blocking_call(proxy._key_allowed_models, db, key)
        resolved = await proxy._blocking_call(proxy._pick_best_model, db, model, allowed)
        if resolved is None:
            return proxy._model_access_error(key, model) if allowed is not None else error(400, '模型不存在或已禁用', 'model_not_found')
        record = request_metrics.current.get()
        if record is not None:
            record['key_id'] = key.id
            record['requested_model'] = model
        await proxy._blocking_call(release_connection, db)
        return await _until_disconnect(request, _generate(request, db, key, body, edit))
    except GatewayRejected as exc:
        await proxy._record_gateway_rejection(exc, key.id, request)
        return JSONResponse(status_code=exc.status_code, content=exc.payload())
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)


async def _generate(request, db, key, body, edit):
    started = time.perf_counter()
    model = body['model']
    budget = await proxy._blocking_call(pool_budget, db, key, [model])
    tried = set()
    status, kind = 503, 'no_account'
    while budget.available():
        acc = await proxy._select_account_with_wait(db, exclude_ids=tried, model=model,
            account_id=key.account_id, key_id=key.id)
        if acc is None:
            break
        sess = None
        held = True
        try:
            budget.consume()
            # Duplicate rows representing one upstream identity are tried once.
            peers = await proxy._blocking_call(lambda: db.query(Account.id).filter(Account.uid == acc.uid).all()) if acc.uid else []
            tried.update([acc.id, *(row[0] for row in peers)])
            request_metrics.mark_attempt(acc.id, model)
            upstream_http.select_account(request.scope, acc)
            sess = await proxy._account_session_async(db, acc)
            if sess is None:
                continue
            extra = await proxy._blocking_call(proxy._upstream_extra_headers, request, 'image-generation')
            headers = await proxy._blocking_call(proxy._headers_for_attempt, sess, extra)
            await proxy._blocking_call(release_connection, db)
            # Unknown POST outcomes must not create a second paid image.
            request.scope['_upstream_recovery'] = {'used': True}
            outgoing = {**body, 'response_format': 'url'}
            if edit:
                for name in ('image', 'image_data', 'image_url'):
                    if name in outgoing:
                        outgoing[name] = [outgoing[name]]
            async with upstream_http.client(request, httpx.Timeout(110, connect=10), proxy.HTTP_LIMITS, retry_budget=budget) as client:
                response = await client.post(settings.BACKEND + '/v2/images/' + ('edits' if edit else 'generations'),
                                             headers=headers, json=outgoing)
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            result = _result(payload) if response.is_success else None
            if result is not None:
                usage = result.get('usage') if isinstance(result.get('usage'), dict) else {}
                credits = usage.get('credit', usage.get('credits'))
                credits = float(credits) if type(credits) in (int, float) and math.isfinite(credits) and credits >= 0 else None
                await proxy._blocking_call(proxy._note_success, db, acc)
                await proxy._blocking_call(proxy._record_usage, key.id, acc.id, model, credits, None,
                    client_ip=proxy._client_ip(request), use_case='images/edits' if edit else 'images/generations',
                    prompt_tokens=usage.get('input_tokens'), completion_tokens=usage.get('output_tokens'),
                    total_tokens=usage.get('total_tokens'), error_kind='success',
                    latency_ms=int((time.perf_counter()-started)*1000))
                # Download/encoding no longer consumes upstream inference capacity.
                POOL.release(acc.uid or '')
                held = False
                rows = [{k: row[k] for k in ('url', 'b64_json', 'revised_prompt') if k in row} for row in result['data']]
                if body.get('response_format') == 'b64_json':
                    try:
                        for row in rows:
                            if 'b64_json' not in row:
                                row['b64_json'] = await _base64_image(row['url'])
                            row.pop('url', None)
                    except (ValueError, httpx.HTTPError):
                        # Generation already succeeded and was accounted. Keep
                        # its usable URLs so callers need not pay to regenerate.
                        return JSONResponse({
                            'warning': {'message': '生图已完成，base64 转换失败；请使用 data 中的图片地址', 'code': 'image_conversion_failed'},
                            'created': result.get('created', int(time.time())), 'data': result['data'],
                            **({'usage': usage} if usage else {})})
                return JSONResponse({'created': result.get('created', int(time.time())), 'data': rows, **({'usage': usage} if usage else {})})
            status = response.status_code if response.status_code >= 400 else 502
            detail = json.dumps(payload, ensure_ascii=False)[:65536]
            kind = proxy._classify_error(status, detail)
            request_metrics.mark_upstream_error(kind, status)
            # Retry only explicit account/quota rejections. 5xx and transport
            # failures may hide a completed, billable image generation.
            if response.status_code < 500 and kind in {
                    'session_dead', 'hard_credit', 'soft_rate', 'model_rate', 'model_block', 'account_fault'}:
                await proxy._blocking_call(proxy._apply_account_policy, db, acc, kind, status, detail, model=model,
                    reset_at=proxy._error_reset_at(response.headers, detail))
                continue
            break
        except httpx.TransportError:
            status, kind = 502, 'transport'
            break
        finally:
            try:
                if sess is not None:
                    sess.close()
            finally:
                if held:
                    POOL.release(acc.uid or '')
    await proxy._blocking_call(proxy._record_usage, key.id, 0, model, 0.0, None,
        client_ip=proxy._client_ip(request), use_case='images/edits' if edit else 'images/generations', error_kind=kind,
        latency_ms=int((time.perf_counter()-started)*1000))
    status = 503 if kind == 'no_account' else status if status in (400, 413, 422, 429) else 502
    return error(status, '生图失败，请检查模型支持的图片参数或稍后重试', 'image_generation_failed')
