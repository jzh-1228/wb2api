"""Opt-in expiring process cache; request content never enters the database."""
from collections import OrderedDict
from datetime import datetime, timezone
import json
import codecs
import re
import threading
import time

from admin.config import settings
from admin.request_metrics import current

TTL_SECONDS = 3600
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 200
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_HEADER_BYTES = 32 * 1024
_SECRET = re.compile(r'authorization|cookie|token|secret|password|credential|api[-_]?key|auth', re.I)
_lock = threading.RLock()
_enabled = bool(settings.LOG_REQUEST_PAYLOAD)
_entries = OrderedDict()
_bytes = 0
_active_bytes = 0
_generation = 0


def _prune():
    global _bytes
    now = time.monotonic()
    for key, (expires, _, size) in list(_entries.items()):
        if expires <= now:
            _bytes -= size
            del _entries[key]


def status():
    with _lock:
        _prune()
        return {'enabled': _enabled, 'ttl_seconds': TTL_SECONDS,
                'max_cache_bytes': MAX_CACHE_BYTES, 'max_entries': MAX_ENTRIES,
                'cached_requests': len(_entries), 'cache_bytes': _bytes,
                'active_bytes': _active_bytes, 'record_max_bytes': MAX_RECORD_BYTES,
                'body_max_bytes': max(1, min(8, settings.PAYLOAD_MAX_MB)) * 1024 * 1024,
                'storage': 'process_memory', 'restart_clears': True}


def configure(enabled):
    global _enabled
    with _lock:
        _enabled = enabled
    return status()


def clear():
    global _bytes, _generation
    with _lock:
        _entries.clear()
        _bytes = 0
        _generation += 1
    return status()


def safe_headers(headers):
    allowed = {'content-type', 'content-length', 'user-agent', 'accept',
               'accept-encoding', 'x-request-id', 'anthropic-version', 'anthropic-beta'}
    safe = {}
    for key, value in headers.items():
        name = key.lower()
        safe[name] = '[REDACTED]' if _SECRET.search(name) or name not in allowed else value[:2048]
    if len(json.dumps(safe, ensure_ascii=True).encode()) > MAX_HEADER_BYTES:
        return {'_truncated': True}
    return safe


def _body(record, raw):
    global _active_bytes
    budget = record.get('_capture_bytes', 0)
    maximum = min(MAX_RECORD_BYTES-budget,
                  max(1, min(8, settings.PAYLOAD_MAX_MB)) * 1024 * 1024)
    with _lock:
        maximum = max(0, min(maximum, MAX_CACHE_BYTES-_active_bytes))
        size = min(len(raw), maximum)
        _active_bytes += size
        record['_capture_bytes'] = budget + size
    encoding = json.detect_encoding(raw[:4]) if raw else 'utf-8'
    retained = raw[:size]
    truncated = len(raw) > size
    if encoding != 'utf-8':
        retained = retained.decode(encoding, errors='ignore').encode('utf-8')
        truncated = truncated or len(retained) > size
        retained = retained[:size]
    return {'_raw_body': retained, 'body_bytes': len(raw),
            'body_cached_bytes': len(retained), 'body_truncated': truncated}


def capture(headers, raw_body):
    record = current.get()
    if record is None:
        return
    with _lock:
        enabled = _enabled
        record['_capture_generation'] = _generation
    record['_capture_enabled'] = enabled
    if not enabled:
        return
    record['_request_capture'] = {'captured_at': datetime.now(timezone.utc).isoformat(),
        'ingress': {'headers': safe_headers(headers), **_body(record, raw_body)},
        'upstream': [], 'upstream_omitted': 0}


def capture_upstream(request):
    """Final serialized HTTPX bytes, without consuming an upload stream."""
    record = current.get()
    data = record.get('_request_capture') if record else None
    if data is None:
        return None
    if len(data['upstream']) >= 12:
        data['upstream_omitted'] += 1
        return None
    try:
        raw = request.content
    except Exception:
        raw = None
    url = str(request.url.copy_with(query=None, username=None, password=None))
    item = {'attempt': len(data['upstream'])+1, 'method': request.method,
            'url': url, 'query_keys': list(request.url.params.keys()),
            'account_id': record.get('last_account_id'),
            'headers': safe_headers(request.headers), 'dispatch_at': None,
            'http_status': None, 'body_available': raw is not None}
    if raw is not None:
        item.update(_body(record, raw))
    data['upstream'].append(item)
    return item


def store(log_id):
    global _bytes
    record = current.get()
    data = record.get('_request_capture') if record else None
    if data is None or not log_id:
        return
    # Freeze only the small mutable metadata. Body bytes are immutable and
    # shared with the request until cleanup, with no full JSON serialization.
    data = {**data, 'ingress': dict(data['ingress']),
            'upstream': [{**item, 'upload': dict(item.get('upload', {}))}
                         for item in data['upstream']]}
    size = sum(len(part.get('_raw_body', b'')) for part in [data['ingress'], *data['upstream']])
    size += len(json.dumps(_public(data, False), ensure_ascii=False).encode('utf-8'))
    if size > MAX_CACHE_BYTES:
        return
    with _lock:
        if record.get('_capture_generation') != _generation:
            return
        _prune()
        previous = _entries.pop(log_id, None)
        if previous:
            _bytes -= previous[2]
        while _entries and (_bytes+size > MAX_CACHE_BYTES or len(_entries) >= MAX_ENTRIES):
            _, (_, _, evicted_size) = _entries.popitem(last=False)
            _bytes -= evicted_size
        _entries[log_id] = (time.monotonic()+TTL_SECONDS, data, size)
        _bytes += size


def _part_public(part, include_body):
    result = {key: value for key, value in part.items() if key != '_raw_body'}
    if include_body and '_raw_body' in part:
        result['body'] = part['_raw_body'].decode('utf-8', errors='ignore')
    return result


def _public(data, include_bodies):
    return {**data, 'ingress': _part_public(data['ingress'], include_bodies),
            'upstream': [_part_public(item, include_bodies) for item in data['upstream']]}


def _cached(log_id):
    with _lock:
        _prune()
        entry = _entries.get(log_id)
        return entry[1] if entry else None


def get(log_id, include_bodies=True):
    data = _cached(log_id)
    return _public(data, include_bodies) if data is not None else None


def body_page(log_id, part, offset=0, limit=65536):
    data = _cached(log_id)
    if data is None:
        return None
    if part == 'ingress':
        selected = data['ingress']
    elif part.startswith('upstream:') and part[9:].isdigit():
        index = int(part[9:])
        if index >= len(data['upstream']):
            return None
        selected = data['upstream'][index]
    else:
        return None
    raw = selected.get('_raw_body')
    if raw is None:
        return None
    offset = min(max(0, offset), len(raw))
    end = min(len(raw), offset + min(65536, max(4, limit)))
    decoder = codecs.getincrementaldecoder('utf-8')(errors='ignore')
    text = decoder.decode(raw[offset:end], final=end == len(raw))
    next_offset = end - len(decoder.getstate()[0])
    return {'text': text, 'offset': offset, 'next_offset': next_offset,
            'has_more': next_offset < len(raw), 'cached_bytes': len(raw),
            'body_bytes': selected.get('body_bytes'), 'body_truncated': selected.get('body_truncated', False)}


def release(record):
    global _active_bytes
    with _lock:
        _active_bytes = max(0, _active_bytes-record.pop('_capture_bytes', 0))
    record.pop('_request_capture', None)
    record.pop('_capture_enabled', None)
    record.pop('_capture_generation', None)
