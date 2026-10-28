"""Independent, loopback-only waiting proxy for planned server upgrades."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import tempfile
import time

import anyio
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

NOTICE = "\u7cfb\u7edf\u5347\u7ea7\u66f4\u65b0\u4e2d\uff0c\u8bf7\u7a0d\u5019"
HOP = {b'connection', b'keep-alive', b'proxy-authenticate',
       b'proxy-authorization', b'te', b'trailer', b'transfer-encoding', b'upgrade'}
STREAM_PATHS = {'/v1/chat/completions', '/v1/responses', '/v1/messages'}


class ClosingStreamingResponse(StreamingResponse):
    def __init__(self, *args, cleanup, upstream=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.cleanup = cleanup
        self.upstream = upstream

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A disconnect can arrive while sending headers, before iteration
            # starts. Explicit ownership avoids relying on async-generator GC.
            with anyio.CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                finally:
                    self.cleanup()
                    if self.upstream is not None:
                        await self.upstream.aclose()


def clean_headers(headers):
    excluded = set(HOP)
    for key, value in headers:
        if key.lower() == b'connection':
            excluded.update(token.strip().lower() for token in value.split(b','))
    return [(key, value) for key, value in headers if key.lower() not in excluded]


def error_payload(message, code='system_upgrading'):
    return {'error': {'type': 'server_error', 'code': code, 'message': message}}


def stream_error(path, payload):
    error = payload.get('error', payload)
    if not isinstance(error, dict):
        error = {'type': 'server_error', 'message': str(error)}
    if path.endswith('/messages'):
        event = {'type': 'error', 'error': error}
        return 'event: error\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
    if path.endswith('/responses'):
        event = {'type': 'error', 'code': error.get('code') or error.get('type') or 'server_error',
                 'message': error.get('message', 'Request failed'),
                 'param': error.get('param'), 'sequence_number': 0}
        return 'event: error\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
    return 'data: ' + json.dumps({'error': error}, ensure_ascii=False) + '\n\ndata: [DONE]\n\n'


class UpgradeGate:
    def __init__(self, marker, backend, *, transport=None, heartbeat=3,
                 max_wait=180, max_pending=128, max_body=64 * 1024 * 1024):
        self.marker = Path(marker)
        self.backend = backend.rstrip('/')
        self.transport = transport
        self.heartbeat = heartbeat
        self.max_wait = max_wait
        self.max_pending = max_pending
        self.max_body = max_body
        self.pending = 0
        self.client = None
        self.probe_lock = asyncio.Lock()
        self.probe_at = 0
        self.ready = False

    def upgrading(self):
        try:
            return float(self.marker.read_text()) > time.time()
        except (OSError, ValueError):
            return False

    async def backend_ready(self):
        async with self.probe_lock:
            if time.monotonic() - self.probe_at < .25:
                return self.ready
            try:
                response = await self.client.get(self.backend + '/api/app/version', timeout=2)
                self.ready = response.status_code == 200
            except httpx.HTTPError:
                self.ready = False
            self.probe_at = time.monotonic()
            return self.ready

    async def wait_ready(self, deadline):
        while time.monotonic() < deadline:
            if not self.upgrading() and await self.backend_ready():
                return
            await asyncio.sleep(min(.25, max(0, deadline - time.monotonic())))
        raise TimeoutError(NOTICE + "\uff0c\u7b49\u5f85\u8d85\u65f6\uff0c\u8bf7\u91cd\u8bd5")

    async def handle(self, request: Request):
        if request.url.path == '/__upgrade_health':
            return JSONResponse({'ok': True, 'pending': self.pending,
                                 'upgrading': self.upgrading()})
        if self.pending >= self.max_pending:
            return JSONResponse(error_payload(NOTICE), status_code=503,
                                headers={'Retry-After': '3'})
        self.pending += 1
        body = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
        transferred = False
        released = False

        def release():
            nonlocal released
            if not released:
                released = True
                body.close()
                self.pending -= 1

        try:
            length = 0
            async for chunk in request.stream():
                length += len(chunk)
                if length > self.max_body:
                    return JSONResponse(error_payload('Request body too large', 'request_too_large'),
                                        status_code=413)
                body.write(chunk)
            body.seek(0)
            path = request.url.path
            streaming = False
            if path in STREAM_PATHS and 'application/json' in request.headers.get('content-type', ''):
                try:
                    streaming = json.load(body).get('stream') is True
                except (ValueError, AttributeError, UnicodeError):
                    pass
                body.seek(0)
            deadline = time.monotonic() + self.max_wait
            if streaming:
                response = ClosingStreamingResponse(self.stream(request, body, deadline, release),
                    cleanup=release,
                    media_type='text/event-stream', headers={
                        'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no',
                        'X-WB2API-Upgrade': 'waiting'})
                transferred = True
                return response
            # Waiting concurrently with disconnect detection also releases uploaded
            # bodies when a non-streaming caller cancels during the upgrade.
            waiter = asyncio.create_task(self.wait_ready(deadline))
            try:
                while not waiter.done():
                    if await request.is_disconnected():
                        return JSONResponse(error_payload('Client disconnected'), status_code=499)
                    await asyncio.wait({waiter}, timeout=.25)
                await waiter
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            upstream = await self.forward(request, body)

            async def relay():
                try:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
                finally:
                    release()
                    with anyio.CancelScope(shield=True):
                        await upstream.aclose()

            response = ClosingStreamingResponse(relay(), cleanup=release, upstream=upstream,
                                                status_code=upstream.status_code)
            response.raw_headers = clean_headers(upstream.headers.raw)
            transferred = True
            return response
        except (TimeoutError, httpx.HTTPError) as exc:
            return JSONResponse(error_payload(NOTICE if isinstance(exc, httpx.HTTPError) else str(exc)),
                                status_code=503, headers={'Retry-After': '3'})
        finally:
            if not transferred:
                release()

    async def forward(self, request, body):
        async def chunks():
            while True:
                chunk = body.read(65536)
                if not chunk:
                    break
                yield chunk
        headers = [(k, v) for k, v in clean_headers(request.headers.raw)
                   if k.lower() != b'accept-encoding']
        headers.append((b'accept-encoding', b'identity'))
        url = self.backend + request.scope.get('raw_path', request.url.path.encode()).decode('ascii')
        if request.scope.get('query_string'):
            url += '?' + request.scope['query_string'].decode('ascii')
        outbound = self.client.build_request(request.method, url, headers=headers, content=chunks())
        # Never retry a POST: a failed read does not prove it was not processed.
        return await self.client.send(outbound, stream=True)

    async def stream(self, request, body, deadline, release=None):
        waiter = asyncio.create_task(self.wait_ready(deadline))
        upstream = None
        try:
            while not waiter.done():
                yield ': ' + NOTICE + '\n\n'
                await asyncio.wait({waiter}, timeout=self.heartbeat)
            await waiter
            upstream = await self.forward(request, body)
            if upstream.status_code >= 400 or 'text/event-stream' not in upstream.headers.get('content-type', ''):
                raw = await upstream.aread()
                try:
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise ValueError('Invalid error payload')
                except (ValueError, UnicodeError):
                    payload = error_payload('Backend returned HTTP ' + str(upstream.status_code), 'backend_error')
                yield stream_error(request.url.path, payload)
                return
            async for chunk in upstream.aiter_raw():
                yield chunk
        except (TimeoutError, httpx.HTTPError) as exc:
            yield stream_error(request.url.path, error_payload(
                str(exc) if isinstance(exc, TimeoutError) else 'Backend connection interrupted',
                'system_upgrading' if isinstance(exc, TimeoutError) else 'backend_connection_error'))
        finally:
            waiter.cancel()
            if release is not None:
                release()
            else:
                body.close()
                self.pending -= 1
            with anyio.CancelScope(shield=True):
                await asyncio.gather(waiter, return_exceptions=True)
                if upstream is not None:
                    await upstream.aclose()


def create_app(marker=None, backend=None, **options):
    gate = UpgradeGate(marker or os.getenv('WB_UPGRADE_MARKER', '/run/wb2api-upgrade/active'),
                       backend or os.getenv('WB_UPGRADE_BACKEND', 'http://127.0.0.1:8790'), **options)

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(transport=gate.transport, trust_env=False,
                timeout=httpx.Timeout(600, connect=5),
                limits=httpx.Limits(max_connections=256, max_keepalive_connections=32)) as client:
            gate.client = client
            yield

    app = Starlette(routes=[Route('/{path:path}', gate.handle,
        methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'])], lifespan=lifespan)
    app.state.gate = gate
    return app


app = create_app()
