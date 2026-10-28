"""Block public gateway clients before authentication or upstream dispatch."""
import time
from threading import Lock

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from admin.client_ip import request_ip as trusted_request_ip
from admin.db import SessionLocal
from admin.models import BlockedIP, ClientIdentity

_CACHE_TTL_SECONDS = 1.0
_cache_lock = Lock()
_blocked = frozenset()
_blocked_devices = frozenset()
_refresh_at = 0.0


def invalidate_ip_cache(ip=None):
    global _refresh_at
    with _cache_lock:
        _refresh_at = 0.0


def is_blocked_ip(ip):
    global _blocked, _blocked_devices, _refresh_at
    with _cache_lock:
        now = time.monotonic()
        if now >= _refresh_at:
            try:
                with SessionLocal() as db:
                    _blocked = frozenset(row.ip for row in db.query(BlockedIP.ip)
                                         .filter(BlockedIP.enabled == 1))
                    _blocked_devices = frozenset((row.kind, row.digest) for row in
                        db.query(ClientIdentity.kind, ClientIdentity.digest).filter(ClientIdentity.blocked == 1))
            except Exception:
                # Keep known bans if the database is temporarily unavailable.
                pass
            _refresh_at = now + _CACHE_TTL_SECONDS
        return ip in _blocked


def blocked_client(ip, identities):
    if is_blocked_ip(ip):
        return 'ip_blocked'
    with _cache_lock:
        if any((kind, digest) in _blocked_devices for kind, digest, _ in identities):
            return 'device_blocked'
    return None


def _is_public_gateway_path(path):
    return path in {'/v1', '/gw'} or path.startswith(('/v1/', '/gw/'))


class IPBlockMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http' or not _is_public_gateway_path(scope.get('path', '')):
            return await self.app(scope, receive, send)
        from admin.ip_activity import record_request
        from admin.client_identity import request_identities
        request = Request(scope)
        ip = trusted_request_ip(request)
        identities = request_identities(request)
        scope['_client_identities'] = identities
        blocked_kind = await run_in_threadpool(blocked_client, ip, identities)
        blocked = bool(blocked_kind)
        started = False
        async def observe(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
                # Count active streams at response admission time.
                record_request(ip, message['status'], blocked)
            await send(message)
        try:
            if blocked:
                response = JSONResponse(
                    {'error': {'message': '该设备标识已被封禁' if blocked_kind == 'device_blocked' else '该 IP 已被封禁', 'type': blocked_kind}},
                    status_code=403, headers={'Cache-Control': 'no-store'})
                return await response(scope, receive, observe)
            await self.app(scope, receive, observe)
        except BaseException:
            if not started:
                record_request(ip, 500, False)
            raise
