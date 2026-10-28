"""Bounded, work-conserving round robin before account leases are acquired.

Only the selected caller touches SQL. Other callers sleep until a release or
a bounded retry tick (for cooldowns and configuration changes). No worker owns
another request's Session, so cancellation can safely drain its own SQL work.
Fairness is process-local, like the account leases themselves.
"""
import asyncio
from collections import deque
import threading
import time
import weakref

from admin.security import GatewayRejected

_queues = weakref.WeakKeyDictionary()
_lock = threading.Lock()


def _busy(code, status=429):
    return GatewayRejected(status, {'error': {
        'message': '账号容量队列已满，请稍后重试', 'type': 'gateway_busy', 'code': code}})


class CapacityQueue:
    def __init__(self, max_waiters=512, per_key=64, retry_interval=.5):
        self.max_waiters = max_waiters
        self.per_key = per_key
        self.retry_interval = retry_interval
        self.lanes = {}
        self.rotation = deque()
        self.active = None
        self.size = 0
        self.retry_at = {}
        self.generation = 0

    def notify(self):
        self.generation += 1
        self.retry_at.clear()
        for lane in self.lanes.values():
            for ticket in lane:
                ticket['ready'] = 0
        self._wake()

    def _next(self):
        if self.active is not None:
            return None
        now = time.monotonic()
        for key in self.rotation:
            for ticket in self.lanes[key]:
                if ticket['ready'] <= now and self.retry_at.get(ticket['scope'], 0) <= now:
                    return ticket
        return None

    def _wake(self):
        ticket = self._next()
        if ticket is not None:
            ticket['wake'].set()

    def _remove(self, ticket):
        key = ticket['key']
        lane = self.lanes.get(key)
        if lane is None or ticket not in lane:
            return
        lane.remove(ticket)
        self.size -= 1
        if not lane:
            del self.lanes[key]
            self.rotation.remove(key)
        if self.active is ticket:
            self.active = None
        if not any(t['scope'] == ticket['scope'] for q in self.lanes.values() for t in q):
            self.retry_at.pop(ticket['scope'], None)
        self._wake()

    async def run(self, key, attempt, timeout, scope=None):
        """attempt returns (leased_account, can_retry); caller retains ownership."""
        key = str(key)
        if self.size >= self.max_waiters or len(self.lanes.get(key, ())) >= self.per_key:
            raise _busy('account_queue_full')
        ticket = {'key': key, 'scope': (key, scope), 'ready': 0, 'wake': asyncio.Event()}
        if key not in self.lanes:
            self.lanes[key] = deque()
            self.rotation.append(key)
        self.lanes[key].append(ticket)
        self.size += 1
        deadline = time.monotonic() + max(0, timeout)
        attempted = False
        try:
            while True:
                if attempted and time.monotonic() >= deadline:
                    return None
                if self._next() is ticket:
                    self.active = ticket
                    generation = self.generation
                    account, retry = await attempt()
                    attempted = True
                    self.active = None
                    # Rotate after each attempt, including unsuccessful scopes.
                    # A busy bound pool/model cannot hold up a healthy scope.
                    if key in self.rotation:
                        while self.rotation[0] != key:
                            self.rotation.rotate(-1)
                        self.rotation.rotate(-1)
                    if account is not None or not retry:
                        return account
                    ticket['ready'] = (time.monotonic() + self.retry_interval
                                       if generation == self.generation else 0)
                    self.retry_at[ticket['scope']] = ticket['ready']
                    lane = self.lanes[key]
                    lane.remove(ticket)
                    lane.append(ticket)
                    self._wake()
                ticket['wake'].clear()
                self._wake()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(ticket['wake'].wait(), min(self.retry_interval, remaining))
                except asyncio.TimeoutError:
                    self._wake()
        finally:
            self._remove(ticket)


def current_queue():
    loop = asyncio.get_running_loop()
    with _lock:
        queue = _queues.get(loop)
        if queue is None:
            queue = _queues[loop] = CapacityQueue()
        return queue


def notify_capacity():
    # Releases also happen in SQL/maintenance threads. Events belong to their
    # event loop and must only be signalled through call_soon_threadsafe.
    with _lock:
        queues = list(_queues.items())
    for loop, queue in queues:
        try:
            loop.call_soon_threadsafe(queue.notify)
        except RuntimeError:
            pass  # loop is shutting down
