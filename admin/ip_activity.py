"""Bounded hourly ingress counters, flushed in batches rather than per request."""
import asyncio
from datetime import datetime, timedelta
import logging
from threading import Lock

from admin.db import SessionLocal
from admin.models import IPRequestBucket

logger = logging.getLogger(__name__)
_lock = Lock()
_flush_lock = Lock()
_pending = {}
_task = None
_MAX_PENDING = 10000
_last_cleanup = None


def record_request(ip, status, blocked):
    global _task
    if ip == 'unknown':
        return
    now = datetime.utcnow()
    key = (ip, now.replace(minute=0, second=0, microsecond=0))
    with _lock:
        if key not in _pending:
            if len(_pending) >= _MAX_PENDING:
                return
            _pending[key] = [0, 0, 0, now, now]
        row = _pending[key]
        row[0] += 1
        row[1] += int(status >= 400)
        row[2] += int(blocked)
        row[4] = now
    if _task is None or _task.done():
        _task = asyncio.create_task(_flush_later())


async def _flush_later():
    while True:
        await asyncio.sleep(2)
        await asyncio.to_thread(flush)
        with _lock:
            if not _pending:
                return


def flush():
    global _last_cleanup
    with _flush_lock:
        with _lock:
            batch = dict(_pending)
            _pending.clear()
        if not batch:
            return
        try:
            with SessionLocal() as db:
                dialect = db.bind.dialect.name
                if dialect == 'mysql':
                    from sqlalchemy.dialects.mysql import insert
                else:
                    from sqlalchemy.dialects.sqlite import insert
                for (ip, hour), (requests, failed, blocked, first, last) in batch.items():
                    statement = insert(IPRequestBucket).values(
                        ip=ip, hour=hour, requests=requests, failed=failed, blocked=blocked,
                        first_seen=first, last_seen=last)
                    changes = dict(requests=IPRequestBucket.requests + requests,
                                   failed=IPRequestBucket.failed + failed,
                                   blocked=IPRequestBucket.blocked + blocked, last_seen=last)
                    if dialect == 'mysql':
                        statement = statement.on_duplicate_key_update(**changes)
                    else:
                        statement = statement.on_conflict_do_update(index_elements=['ip', 'hour'], set_=changes)
                    db.execute(statement)
                now = datetime.utcnow()
                if _last_cleanup is None or now - _last_cleanup >= timedelta(hours=1):
                    db.query(IPRequestBucket).filter(IPRequestBucket.hour < now-timedelta(days=90)).delete()
                db.commit()
                _last_cleanup = now
        except Exception:
            logger.warning('IP activity persistence failed; retaining bounded pending counters')
            with _lock:
                for key, old in batch.items():
                    if key in _pending:
                        row = _pending[key]
                        row[:3] = [row[i] + old[i] for i in range(3)]
                        row[3] = min(row[3], old[3])
                        row[4] = max(row[4], old[4])
                    elif len(_pending) < _MAX_PENDING:
                        _pending[key] = old
