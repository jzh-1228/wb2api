"""Persist request outcomes without changing accounting or retaining payloads."""
from datetime import datetime, timezone
import logging

from admin.db import SessionLocal
from admin.models import RequestAudit

logger = logging.getLogger(__name__)
INFERENCE_PATHS = frozenset(('/v1/chat/completions', '/v1/responses', '/v1/messages',
                             '/v1/images/generations', '/v1/images/edits'))


def save(record):
    key_id = record.get('key_id')
    if not isinstance(key_id, int) or key_id <= 0 or record.get('path') not in INFERENCE_PATHS:
        return
    try:
        with SessionLocal() as db:
            db.add(RequestAudit(
                request_id=record['id'], api_key_id=key_id,
                account_id=record.get('last_account_id'),
                client_ip=(record.get('client_ip') or '')[:64],
                user_agent=(record.get('user_agent') or '')[:512],
                path=record['path'][:80], model=record.get('requested_model', '')[:120],
                started_at=datetime.fromisoformat(record['started_at']).astimezone(timezone.utc).replace(tzinfo=None),
                finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
                status=record.get('status'), outcome=record['outcome'],
                reason=(record.get('request_error') or record.get('network_error') or
                        record.get('upstream_error') or '')[:64],
                phase=record.get('phase', '')[:80], elapsed_ms=int(record['total_ms']),
                upstream_attempts=record.get('upstream_attempts', 0),
            ))
            db.commit()
    except Exception:
        # Do not attach database exception text: connection errors can include
        # deployment credentials. An audit outage must not retry inference.
        logger.warning('Request audit persistence failed for request %s', record['id'])
