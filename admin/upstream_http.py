"""Share TLS configuration while keeping each request's cookies isolated."""
from contextlib import asynccontextmanager
import asyncio
import hashlib
import logging
import httpcore
import ssl
import threading
from datetime import datetime, timezone

import certifi
import httpx
from admin import request_metrics, upstream_dispatch

_ssl_context = None
_ssl_lock = threading.Lock()
_logger = logging.getLogger(__name__)
UPLOAD_CHUNK_BYTES = 64 * 1024


class _UploadStream(httpx.AsyncByteStream):
    """Let write timeouts measure stalled progress, even for large JSON bodies.

    HTTPX encodes json= as one chunk. A single write can then exceed the timeout
    while a slow connection is still making progress. Keep Content-Length and
    bytes unchanged; only bound the size of each socket write.
    """
    def __init__(self, stream, evidence):
        self.stream = stream
        self.evidence = evidence

    async def __aiter__(self):
        async for data in self.stream:
            for offset in range(0, len(data), UPLOAD_CHUNK_BYTES):
                chunk = data[offset:offset + UPLOAD_CHUNK_BYTES]
                yield chunk
                # The transport resumed us after accepting this chunk. This is
                # local write progress, not proof that the upstream processed it.
                self.evidence['written_bytes'] += len(chunk)
        self.evidence['complete'] = True

    async def aclose(self):
        await self.stream.aclose()



_ssl_lock = threading.Lock()


def ssl_context():
    global _ssl_context
    with _ssl_lock:
        if _ssl_context is None:
            _ssl_context = ssl.create_default_context(cafile=certifi.where())
        return _ssl_context


class _BorrowedTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport, *, recovery=None, budget=None, limits=None, scope=None):
        self.transport = transport
        self.recovery = recovery if recovery is not None else {'used': False}
        self.budget = budget
        self.limits = limits
        self.private = []
        self.scope = scope if scope is not None else {}

    async def handle_async_request(self, request):
        captured = await _prepare_upload(request, self.scope)
        try:
            response = await self.transport.handle_async_request(request)
            if captured is not None:
                captured['http_status'] = response.status_code
            request_metrics.mark_upstream_response(response.status_code)
            return response
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError,
                httpx.RemoteProtocolError) as error:
            if captured is not None:
                captured['error'] = type(error).__name__
            request_metrics.mark_network_error(error)
            # Only replay an in-memory request before ANY response headers.
            # Read/write timeouts and errors while consuming a response are not
            # replayed here. A lost response can still mean upstream accepted
            # the POST; this is bounded recovery, not exactly-once delivery.
            try:
                request.content
            except httpx.RequestNotRead:
                raise error
            if self.recovery['used'] or (self.budget is not None and not self.budget.available()):
                raise
            self.recovery['used'] = True
            if self.budget is not None:
                self.budget.consume()
            request_metrics.mark_connection_recovery()
            await asyncio.sleep(.2)
            fresh = httpx.AsyncHTTPTransport(limits=self.limits, verify=ssl_context())
            self.private.append(fresh)
            # A new pool guarantees the retry cannot reuse the broken socket.
            try:
                captured = await _prepare_upload(request, self.scope)
                response = await fresh.handle_async_request(request)
                if captured is not None:
                    captured['http_status'] = response.status_code
                request_metrics.mark_upstream_response(response.status_code)
                return response
            except httpx.TransportError as retry_error:
                if captured is not None:
                    captured['error'] = type(retry_error).__name__
                request_metrics.mark_network_error(retry_error)
                raise
        except httpx.TransportError as error:
            if captured is not None:
                captured['error'] = type(error).__name__
            raise

    async def aclose(self):
        # The application owns the pool, not an individual request.
        for transport in self.private:
            await transport.aclose()
        self.private.clear()


def select_account(scope, account):
    # Duplicate pool rows for one UID share the same upstream clock.
    uid = str(account.uid or '').strip()
    scope['_upstream_account_key'] = ('uid:' + hashlib.sha256(uid.encode()).hexdigest()
                                      if uid else 'id:' + str(account.id))


async def _prepare_upload(request, scope=None):
    captured = None
    try:
        from admin.request_payload import capture_upstream
        captured = capture_upstream(request)
    except Exception:
        _logger.warning('Optional upstream request capture failed')
    account_key = (scope or {}).get('_upstream_account_key')
    length = request.headers.get('content-length', '')
    evidence = {'body_bytes': int(length) if length.isdigit() else None,
                'written_bytes': 0, 'complete': False}
    if captured is not None:
        captured['upload'] = evidence
    record = request_metrics.current.get()
    if record is not None:
        uploads = record.setdefault('upstream_uploads', [])
        if len(uploads) < 12:
            uploads.append(evidence)
    stream = request.stream.stream if isinstance(request.stream, _UploadStream) else request.stream
    request.stream = _UploadStream(stream, evidence)
    # Attach timings to the actual request; no URLs, headers or bodies retained.
    existing_trace = request.extensions.get('_upload_original_trace', request.extensions.get('trace'))
    request.extensions['_upload_original_trace'] = existing_trace
    sync_trace = request_metrics.network_trace()
    async def trace(name, info):
        # Gate at the real HTTP write, after DNS/TCP/TLS/pool acquisition.
        # Gating before connection setup can bunch different-speed sockets.
        if name in ('http11.send_request_headers.started', 'http2.send_request_headers.started'):
            await upstream_dispatch.before_send(scope or {}, account_key)
            evidence['dispatch_at'] = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
            if captured is not None:
                captured['dispatch_at'] = evidence['dispatch_at']
        if name in ('http11.receive_response_headers.complete', 'http2.receive_response_headers.complete') and captured is not None:
            returned = info.get('return_value')
            if isinstance(returned, tuple) and len(returned) > 1 and isinstance(returned[1], int):
                captured['http_status'] = returned[1]
        sync_trace(name, info)
        if name.endswith('.failed') and isinstance(info.get('exception'),
                                                   (httpcore.NetworkError, httpcore.TimeoutException)):
            evidence['error'] = type(info['exception']).__name__
            if name.endswith('send_request_body.failed'):
                _logger.warning('Upstream upload failed (%s): body_bytes=%s written_bytes=%s',
                                evidence['error'], evidence['body_bytes'], evidence['written_bytes'])
        if existing_trace is not None:
            await existing_trace(name, info)
    request.extensions['trace'] = trace
    return captured


async def start(app, limits):
    # Reuse a few recent connections instead of doing DNS/TCP/TLS on every
    # mobile call. Recovery below is limited to one pre-header disconnect.
    mobile_limits = httpx.Limits(max_connections=limits.max_connections,
                               max_keepalive_connections=4, keepalive_expiry=30)
    app.state.upstream_transport = httpx.AsyncHTTPTransport(limits=mobile_limits, verify=ssl_context())


async def stop(app):
    transport = getattr(app.state, 'upstream_transport', None)
    app.state.upstream_transport = None
    if transport is not None:
        await transport.aclose()


@asynccontextmanager
async def client(request, timeout, limits, *, retry_budget=None):
    # Direct route tests can create a Request without an application in scope.
    app = request.scope.get('app') if hasattr(request, 'scope') else getattr(request, 'app', None)
    transport = getattr(getattr(app, 'state', None), 'upstream_transport', None)
    kwargs = {'timeout': timeout, 'limits': limits}
    scope = getattr(request, 'scope', {})
    recovery = scope.setdefault('_upstream_recovery', {'used': False})
    owned = transport is None
    if owned:
        transport = httpx.AsyncHTTPTransport(limits=limits, verify=ssl_context())
    kwargs['transport'] = _BorrowedTransport(transport, recovery=recovery,
                                             budget=retry_budget, limits=limits, scope=scope)
    try:
        async with httpx.AsyncClient(**kwargs) as connection:
            yield connection
    finally:
        if owned:
            await transport.aclose()
