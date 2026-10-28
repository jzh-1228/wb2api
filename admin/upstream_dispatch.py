"""Fairly stagger inference request starts while keeping streams concurrent."""
import asyncio
from collections import Counter, deque
import logging
import random
import threading
import time

from admin.config import settings
from admin import gateway_limits, request_metrics
from admin.security import GatewayRejected, recheck_key

_logger = logging.getLogger(__name__)
_lock = threading.Lock()
_queue = deque()
_lanes = {}
_rotation = {}
_counts = Counter()
_last_start = {}
_last_prune = 0.0
MAX_WAITERS = 512
MAX_WAITERS_PER_KEY = 32

# Each account has its own clock and fair API-key queue; streams stay concurrent.
_START = """
local t = redis.call('TIME')
local now = tonumber(t[1])*1000 + tonumber(t[2])/1000
local last = tonumber(redis.call('GET', KEYS[1]) or '0')
local delay = last + tonumber(ARGV[1]) - now
if delay > 0 then return math.ceil(delay) end
redis.call('SET', KEYS[1], tostring(now+tonumber(ARGV[2])), 'PX', math.max(120000, tonumber(ARGV[1])*2))
return 0
"""


def _claim(account_key, interval_ms):
    global _last_prune
    jitter_ms = random.uniform(0, min(15, interval_ms * .2))
    client = gateway_limits._redis_client()
    if client is not None:
        return int(client.eval(_START, 1, 'wb:gateway:{upstream}:' + account_key + ':last_start',
                               interval_ms, jitter_ms)) / 1000
    with _lock:
        now = time.monotonic()
        if now - _last_prune >= 60:
            for key, last in list(_last_start.items()):
                if now - last > 120:
                    del _last_start[key]
            _last_prune = now
        delay = _last_start.get(account_key, 0) + interval_ms / 1000 - now
        if delay > 0:
            return delay
        _last_start[account_key] = now + jitter_ms / 1000
        return 0


def _context(key_id):
    from admin.db import SessionLocal
    from admin.routing_policy import upstream_interval_ms
    with SessionLocal() as db:
        if key_id != 'anonymous':
            recheck_key(db, key_id)
        return upstream_interval_ms(db)


def _busy(code, message, status=429):
    return GatewayRejected(status, {'error': {'message': message, 'type': 'gateway_busy',
                                             'code': code, 'source': 'gateway'}})


def _is_head(ticket):
    key_id = ticket['key_id']
    account = ticket['account']
    with _lock:
        lane = _lanes.get(account, {}).get(key_id)
        rotation = _rotation.get(account)
        return bool(rotation and rotation[0] == key_id and lane and lane[0] is ticket)


def _remove(ticket, *, granted=False):
    with _lock:
        try:
            _queue.remove(ticket)
        except ValueError:
            return
        key_id = ticket['key_id']
        account = ticket['account']
        lanes, rotation = _lanes[account], _rotation[account]
        lane = lanes.get(key_id)
        if lane:
            try:
                lane.remove(ticket)
            except ValueError:
                pass
            if not lane:
                lanes.pop(key_id, None)
                try:
                    rotation.remove(key_id)
                except ValueError:
                    pass
            elif granted and rotation and rotation[0] == key_id:
                rotation.rotate(-1)
        if not lanes:
            del _lanes[account]
            del _rotation[account]
        _counts[key_id] -= 1
        if not _counts[key_id]:
            del _counts[key_id]


def _wake_head(account):
    with _lock:
        rotation = _rotation.get(account)
        if rotation:
            lane = _lanes.get(account, {}).get(rotation[0])
            if lane:
                lane[0]['wake'].set()


async def before_send(scope, account_key):
    """Wait without holding a DB connection; cancellation always removes a ticket."""
    key_id = scope.get('_gateway_key_id')
    if key_id is None:
        key_id = 'anonymous'
    if not account_key:
        raise _busy('upstream_account_missing', '上游账号尚未确定', 503)
    ticket = {'key_id': key_id, 'account': account_key, 'wake': asyncio.Event()}
    with _lock:
        if len(_queue) >= MAX_WAITERS or _counts[key_id] >= MAX_WAITERS_PER_KEY:
            raise _busy('upstream_queue_full', '上游发车队列已满，请稍后重试')
        lanes = _lanes.setdefault(account_key, {})
        rotation = _rotation.setdefault(account_key, deque())
        lane = lanes.get(key_id)
        if lane is None:
            lane = lanes[key_id] = deque()
            rotation.append(key_id)
        lane.append(ticket)
        _queue.append(ticket)
        _counts[key_id] += 1
    started = time.monotonic()
    deadline = started + max(0, getattr(settings, 'GATEWAY_QUEUE_TIMEOUT', 90))
    checked = 0.0
    interval = 0
    try:
        while True:
            ticket['wake'].clear()
            now = time.monotonic()
            head = _is_head(ticket)
            if head or now >= checked + .5:
                interval = await gateway_limits._finish_thread(_context, key_id)
                checked = time.monotonic()
            if interval == 0:
                return
            if head:
                delay = await gateway_limits._finish_thread(_claim, account_key, interval)
                if delay <= 0:
                    _remove(ticket, granted=True)
                    _wake_head(account_key)
                    return
            else:
                delay = .5
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _busy('upstream_queue_timeout', '等待上游发车超时，请稍后重试', 503)
            request_metrics.set_phase('upstream_dispatch_wait')
            try:
                await asyncio.wait_for(ticket['wake'].wait(), timeout=min(delay, remaining, .5))
            except asyncio.TimeoutError:
                pass
    except GatewayRejected:
        raise
    except Exception:
        _logger.exception('Upstream dispatch gate unavailable')
        raise _busy('upstream_dispatch_unavailable', '上游流量控制暂不可用，请稍后重试', 503) from None
    finally:
        _remove(ticket)
        _wake_head(account_key)
        request_metrics.add_stage('upstream_queue_wait', (time.monotonic() - started) * 1000)
