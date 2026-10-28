"""Explicit, unverified client identifiers. UA/IP are not device identities."""
import hashlib
import re
import logging
import time
from threading import Lock
from datetime import datetime

from admin.models import ClientIdentity, ClientIdentitySeen

HEADERS = {'device_id': ('x-device-id', 'x-machine-id'),
           'fingerprint': ('x-device-fingerprint', 'x-fingerprint'),
           'mac': ('x-device-mac', 'x-mac-address'),
           'browser': ('x-browser-fingerprint',)}
_recent = {}
_lock = Lock()


def remember_request(identities, ip, key_id):
    from admin.db import SessionLocal
    marker = (tuple((kind, digest) for kind, digest, _ in identities), ip, key_id)
    now = time.monotonic()
    with _lock:
        if now - _recent.get(marker, -60) < 30:
            return
        if len(_recent) >= 10000:
            _recent.clear()
        _recent[marker] = now
    try:
        with SessionLocal() as db:
            remember(db, identities, ip, key_id)
    except Exception:
        with _lock:
            _recent.pop(marker, None)
        logging.getLogger(__name__).warning('Client identity association persistence failed')


def normalize_identity(kind, value):
    if kind not in HEADERS:
        raise ValueError('未知设备标识类型')
    value = value.strip()
    if not value or len(value) > 128 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError('设备标识须为 1–128 位可见 ASCII 字符')
    if kind == 'mac':
        value = value.replace('-', '').replace(':', '').replace('.', '').lower()
        if not re.fullmatch(r'[0-9a-f]{12}', value) or value in ('000000000000', 'ffffffffffff'):
            raise ValueError('请输入有效 MAC 地址')
        value = ':'.join(value[i:i+2] for i in range(0, 12, 2))
    return value, hashlib.sha256(value.encode()).hexdigest()


def request_identities(request):
    result = []
    for kind, headers in HEADERS.items():
        for header in headers:
            try:
                value, digest = normalize_identity(kind, request.headers.get(header, ''))
            except ValueError:
                continue
            result.append((kind, digest, value))
            break
    return result


def remember(db, identities, ip, key_id):
    """Only authenticated callers create associations; bans never spread by IP."""
    dialect = db.bind.dialect.name
    if dialect == 'mysql':
        from sqlalchemy.dialects.mysql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    now = datetime.utcnow()
    for kind, digest, value in identities:
        identity = insert(ClientIdentity).values(kind=kind, digest=digest, value=value,
                                                blocked=0, created_at=now, updated_at=now)
        seen = insert(ClientIdentitySeen).values(kind=kind, digest=digest, ip=ip, key_id=key_id,
                                               first_seen=now, last_seen=now)
        if dialect == 'mysql':
            identity = identity.on_duplicate_key_update(value=identity.inserted.value)
            seen = seen.on_duplicate_key_update(last_seen=now)
        else:
            identity = identity.on_conflict_do_nothing(index_elements=['kind', 'digest'])
            seen = seen.on_conflict_do_update(index_elements=['kind', 'digest', 'ip', 'key_id'],
                                            set_={'last_seen': now})
        db.execute(identity)
        db.execute(seen)
    db.commit()
