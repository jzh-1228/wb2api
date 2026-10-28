"""Reject registration bursts before synchronous routes consume worker threads."""
import threading

from starlette.responses import JSONResponse

from admin.config import settings


_JOIN_PATHS = {
    '/api/join/start',
    '/api/join/send',
    '/api/join/verify',
    '/api/join/official/start',
    '/api/join/official/poll',
}


class JoinAdmissionMiddleware:
    def __init__(self, app, max_active=4):
        self.app = app
        self.max_active = max(1, int(max_active))
        self._lock = threading.Lock()
        self._active = 0

    async def __call__(self, scope, receive, send):
        if (scope.get('type') != 'http' or scope.get('method', '').upper() != 'POST'
                or scope.get('path') not in _JOIN_PATHS):
            return await self.app(scope, receive, send)

        with self._lock:
            # 实时读配置：后台「运行配置」改 /join 并发后无需重启即生效。
            limit = self.max_active
            try:
                limit = max(1, int(getattr(settings, "JOIN_MAX_CONCURRENT", self.max_active)))
            except (TypeError, ValueError):
                pass
            busy = self._active >= limit
            if not busy:
                self._active += 1
        if busy:
            body = ({'done': False, 'busy': True} if scope.get('path') == '/api/join/official/poll'
                    else {'detail': {'message': '注册请求较多，请稍后重试',
                                     'step': 'busy', 'reason': 'join_capacity'}})
            response = JSONResponse(
                body,
                status_code=200 if scope.get('path') == '/api/join/official/poll' else 503,
                headers={'Retry-After': '3'})
            return await response(scope, receive, send)
        try:
            return await self.app(scope, receive, send)
        finally:
            with self._lock:
                self._active = max(0, self._active - 1)
