"""Bounded timings and upstream error messages; no headers or request bodies."""
from collections import deque
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import re
import json
import threading
import time
import uuid

current = ContextVar('local_request_timing', default=None)
_lock = threading.Lock()
_recent = deque(maxlen=40)
_active = {}


def safe_error_text(value):
    text = str(value)
    text = re.sub(r'(?i)\bBearer\s+[^\s,;\"\']+', 'Bearer [redacted]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[redacted-key]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[redacted-token]', text)
    text = re.sub(r'''(?i)((?:["']?)(?:access[_-]?token|refresh[_-]?token|token|password|secret|authorization|api[_-]?key|auth)(?:["']?)\s*[:=]\s*["']?)[^\s,}"']+''', r'\1[redacted]', text)
    text = re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1[redacted]@', text)
    text = re.sub(r'(https?://[^\s?]+)\?[^\s]+', r'\1?[redacted]', text)
    return text[:4096]


def exception_text(error):
    chain, seen = [], set()
    while error is not None and id(error) not in seen and len(chain) < 5:
        seen.add(id(error))
        message = safe_error_text(error).strip()
        chain.append(type(error).__name__ + (': ' + message if message else ''))
        # Network libraries suppress low-level socket/SSL errors in their public
        # message. Keep the bounded, redacted chain for diagnosis as well.
        error = error.__cause__ or error.__context__
    return ' <- '.join(chain)[:4096]


def mark_attempt(account_id, model):
    record = current.get()
    if record is not None:
        with _lock:
            record['last_account_id'] = int(account_id)
            record['last_model'] = str(model)[:120]
            record.pop('upstream_status', None)
            record.pop('_network_failure_phase', None)


def mark_upstream_response(status):
    record = current.get()
    if record is not None:
        with _lock:
            record['upstream_status'] = int(status)


def mark_gateway_result(status):
    record = current.get()
    if record is not None:
        with _lock:
            record['gateway_result_status'] = int(status)


def mark_gateway_error(status, detail):
    record = current.get()
    if record is not None:
        with _lock:
            record['gateway_result_status'] = int(status)
            record['gateway_error'] = safe_error_text(json.dumps(detail, ensure_ascii=False))


def mark_network_error(error):
    record = current.get()
    if record is not None:
        event = {'type': type(error).__name__, 'message': exception_text(error),
                 'phase': record.get('_network_failure_phase') or record.get('phase'), 'account_id': record.get('last_account_id')}
        if record.get('upstream_status'):
            event['http_status'] = record['upstream_status']
        with _lock:
            record['network_error'] = event['type']
            events = record.setdefault('network_errors', [])
            if len(events) < 12 and (not events or events[-1] != event):
                events.append(event)


def mark_connection_recovery():
    record = current.get()
    if record is not None:
        with _lock:
            record['connection_recoveries'] = record.get('connection_recoveries', 0) + 1
            record.pop('upstream_status', None)
            record.pop('_network_failure_phase', None)


def usage_evidence(error_kind):
    """Persist only bounded diagnostics; never request bodies or headers."""
    record = current.get()
    if record is None:
        return None, 0
    with _lock:
        fields = ('id', 'path', 'started_at', 'status', 'gateway_result_status', 'gateway_error', 'upstream_status', 'upstream_error',
                  'network_error', 'network_errors', 'connection_recoveries', 'last_account_id',
                  'last_model', 'account_policy_events', 'upstream_attempts', 'first_delta_ms',
                  'stages', 'request_bytes', 'upstream_uploads')
        evidence = {key: record[key] for key in fields if key in record}
        evidence['error_kind'] = error_kind
        evidence['elapsed_ms'] = round((time.perf_counter()-record['_perf_start'])*1000, 1)
        health = record.get('_runtime_health')
        if health is not None:
            evidence.update(health.since(record['_perf_start']))
        encoded = json.dumps(evidence, ensure_ascii=False)
        # MySQL TEXT is limited by bytes, including multi-byte messages. Never
        # lose the entire usage row because a verbose failure exceeds it.
        if len(encoded.encode('utf-8')) > 60000:
            evidence['diagnostic_truncated'] = True
            for history in ('network_errors', 'account_policy_events'):
                evidence[history] = [dict(event) for event in evidence.get(history, [])]
                for event in evidence[history]:
                    for field in ('message', 'upstream_message', 'upstream_raw'):
                        value = event.get(field)
                        if isinstance(value, str) and len(value) > 256:
                            event[field] = value[:256]
                            event['message_truncated'] = True
            encoded = json.dumps(evidence, ensure_ascii=False)
        return encoded, record.get('last_account_id', 0)


def add_stage(name, milliseconds):
    record = current.get()
    if record is not None:
        with _lock:
            if name in ('client_send_wait', 'client_upload_wait'):
                existing = next((item for item in record['stages'] if item['name'] == name), None)
                if existing is not None:
                    existing['ms'] = round(existing['ms'] + milliseconds, 1)
                    return
            if len(record['stages']) < 80:
                record['stages'].append({'name': name, 'ms': round(milliseconds, 1)})


@contextmanager
def measure(name):
    start = time.perf_counter()
    set_phase(name)
    try:
        yield
    finally:
        add_stage(name, (time.perf_counter()-start)*1000)


def network_trace():
    starts = {}
    def trace(name, info):
        if name.endswith('.started'):
            label = name.rsplit('.', 1)[0]
            starts[label] = time.perf_counter()
            set_phase(label)
            if label == 'http11.send_request_headers':
                record = current.get()
                if record is not None:
                    with _lock:
                        record['upstream_attempts'] = record.get('upstream_attempts', 0) + 1
        elif name.endswith(('.complete', '.failed')):
            label = name.rsplit('.', 1)[0]
            began = starts.pop(label, None)
            if began is not None:
                add_stage(label, (time.perf_counter()-began)*1000)
            if name.endswith('.failed'):
                record = current.get()
                if record is not None:
                    # Retain a fixed exception category, never exception text.
                    category = type(info.get('exception')).__name__
                    allowed = {'ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout',
                               'ConnectError', 'ReadError', 'WriteError', 'RemoteProtocolError'}
                    if category != 'CancelledError':
                        with _lock:
                            record['_network_failure_phase'] = label
                            record['network_error'] = category if category in allowed else 'TransportError'
    return trace


def mark_first_delta():
    record = current.get()
    if record is not None:
        with _lock:
            if record['first_delta_ms'] is None:
                record['first_delta_ms'] = round((time.perf_counter()-record['_perf_start'])*1000, 1)


def mark_terminal():
    record = current.get()
    if record is not None:
        with _lock:
            record['_terminal_ready'] = True


def mark_stream_error():
    record = current.get()
    if record is not None:
        with _lock:
            record['stream_error'] = True


def mark_request_error(kind):
    record = current.get()
    if record is not None:
        with _lock:
            record['request_error'] = kind


def mark_upstream_error(kind, status=None):
    record = current.get()
    allowed = {'hard_credit', 'soft_rate', 'model_rate', 'model_block', 'account_fault',
               'session_dead', 'waf', 'server', 'not_found', 'transport', 'client',
               'no_account', 'success', 'unknown', 'model_queue', 'retry_exhausted',
               'upstream_internal', 'model_behavior', 'upstream_auth_anomaly'}
    if record is not None:
        with _lock:
            record['upstream_error'] = kind if kind in allowed else 'unknown'
            if isinstance(status, int) and 100 <= status <= 599:
                record['upstream_status'] = status


def mark_account_policy(account_id, model, kind, status, code, *, message=None, cool_until=None,
                        breaker_until=None, degrade_until=None, raw=None):
    """Keep bounded routing evidence and original messages, excluding request data."""
    record = current.get()
    if record is None:
        return
    event = {'account_id': int(account_id), 'kind': kind, 'http_status': status}
    if kind == 'transport' and not status and record.get('upstream_status'):
        event['http_status'] = record['upstream_status']
    if isinstance(model, str) and re.fullmatch(r'[\w./:-]{1,120}', model, re.ASCII):
        event['model'] = model
    value = str(code) if code is not None else ''
    if value:
        event['upstream_code'] = value[:128]
    if isinstance(message, str):
        event['upstream_message'] = safe_error_text(message)
        if len(message) > 4096:
            event['message_truncated'] = True
    if status and raw is not None:
        event['upstream_raw'] = safe_error_text(raw)
        if len(str(raw)) > 4096:
            event['raw_truncated'] = True
    for name, until in (('cool_until', cool_until), ('breaker_until', breaker_until),
                        ('degrade_until', degrade_until)):
        if isinstance(until, datetime):
            event[name] = until.isoformat(timespec='seconds') + 'Z'
    with _lock:
        history = record.setdefault('account_policy_events', [])
        if len(history) < 12:
            history.append(event)


def set_phase(name):
    record = current.get()
    if record is not None:
        with _lock:
            record['phase'] = name


def activity_snapshot(paths, key_ids=None, limit=100):
    """Copy only bounded display fields; leave diagnostic histories untouched."""
    fields = ('id', 'key_id', 'path', 'requested_model', 'started_at', 'status',
              'outcome', 'phase', 'request_error', 'network_error', 'upstream_error',
              'upstream_attempts', 'last_account_id', 'client_ip')
    def visible(record):
        return (record.get('key_id') and record.get('path') in paths
                and (key_ids is None or record['key_id'] in key_ids))
    def copy(record):
        result = {key: record.get(key) for key in fields}
        result['elapsed_ms'] = record['total_ms'] if record['total_ms'] is not None else round((time.perf_counter()-record['_perf_start'])*1000, 1)
        return result
    with _lock:
        active = [record for record in _active.values() if visible(record)]
        return {'active_count': len(active), 'active': [copy(r) for r in active[:limit]],
                'recent': [copy(r) for r in reversed(_recent) if visible(r)][:limit]}


def key_ip_snapshot(key_ids=None):
    """Copy only per-key/IP counts, start times and in-flight models."""
    wanted = set(key_ids) if key_ids is not None else None
    grouped = {}
    with _lock:
        for record in _active.values():
            key_id = record.get('key_id')
            ip = record.get('client_ip')
            if (not isinstance(key_id, int) or key_id <= 0
                    or (wanted is not None and key_id not in wanted)
                    or not ip or ip == 'unknown'):
                continue
            summary = grouped.setdefault(key_id, {
                'active_requests': 0, 'active_ips': 0, 'ips': {},
            })
            item = summary['ips'].setdefault(ip, {
                'active_requests': 0, 'first_started_at': None,
                'last_started_at': None, 'models': {},
            })
            started = record.get('started_at')
            item['active_requests'] += 1
            summary['active_requests'] += 1
            model = record.get('requested_model') or record.get('last_model')
            if model:
                item['models'][model] = item['models'].get(model, 0) + 1
            if started and (item['first_started_at'] is None or started < item['first_started_at']):
                item['first_started_at'] = started
            if started and (item['last_started_at'] is None or started > item['last_started_at']):
                item['last_started_at'] = started
        for summary in grouped.values():
            summary['active_ips'] = len(summary['ips'])
    return grouped


def activity_live_signature(paths, limit=100):
    """In-memory only: has anything worth re-rendering changed?

    The SSE stream uses this to decide **whether to touch the database at all**.
    A cheap "no" here keeps an idle panel at zero queries instead of one per
    second. Deliberately excludes the running clock (`elapsed_ms`) — otherwise
    every in-flight second would look like a change; the browser recomputes
    elapsed time locally between frames.
    """
    parts = []
    with _lock:
        for record in _active.values():
            if not (record.get('key_id') and record.get('path') in paths):
                continue
            parts.append('%s/%s/%s/%s' % (record.get('id'), record.get('phase'),
                                       record.get('status'), record.get('last_account_id')))
            if len(parts) >= limit:
                break
        recent = []
        for record in reversed(_recent):
            if not (record.get('key_id') and record.get('path') in paths):
                continue
            recent.append('%s/%s' % (record.get('id'), record.get('outcome')))
            if len(recent) >= limit:
                break
    return '|'.join(sorted(parts)) + '#' + '|'.join(recent)


def snapshot():
    def copy(record):
        result = {key: value for key, value in record.items() if not key.startswith('_')}
        result['stages'] = [dict(stage) for stage in record['stages']]
        if 'account_policy_events' in record:
            result['account_policy_events'] = [dict(event) for event in record['account_policy_events']]
        if 'network_errors' in record:
            result['network_errors'] = [dict(event) for event in record['network_errors']]
        health = record.get('_runtime_health')
        if health is not None:
            result.update(health.since(record['_perf_start']))
        result['elapsed_ms'] = record['total_ms'] if record['total_ms'] is not None else round((time.perf_counter()-record['_perf_start'])*1000, 1)
        return result
    with _lock:
        return {'active': [copy(record) for record in _active.values()],
                'recent': [copy(record) for record in reversed(_recent)]}


class TimingMiddleware:
    def __init__(self, app, persist=False):
        self.app = app
        self.persist = persist

    async def __call__(self, scope, receive, send):
        if (scope['type'] != 'http' or not scope.get('path', '').startswith('/v1/')
                or scope.get('path', '').rstrip('/') == '/v1/audio/transcriptions'):
            return await self.app(scope, receive, send)
        start = time.perf_counter()
        # Keep live IPs consistent with usage logging and IP blocking.
        try:
            from starlette.requests import Request
            from admin.client_ip import request_ip
            request = Request(scope)
            client_ip = request_ip(request)
            user_agent = request.headers.get('user-agent', '').strip()[:512]
        except Exception:
            client_ip = 'unknown'
            user_agent = ''
        record = {'id': uuid.uuid4().hex, 'path': scope['path'],
                  'method': scope.get('method', ''),
                  'client_ip': client_ip,
                  'user_agent': user_agent,
                  'started_at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                  'status': None, 'headers_ms': None, 'first_body_ms': None, 'total_ms': None,
                  'first_delta_ms': None, 'request_bytes': 0, 'response_bytes': 0,
                  'upload_complete': False, 'response_complete': False, 'terminal_sent': False,
                  'streaming': False,
                  'stream_error': False, 'request_error': None,
                  'outcome': 'active', 'phase': 'gateway_admission', '_perf_start': start,
                  'stages': []}
        connection = scope.get('_mobile_http')
        if connection:
            record['ingress_ms'] = round((start - connection['received_at']) * 1000, 1)
            record['asgi_queue_ms'] = round((start - connection['headers_at']) * 1000, 1)
            record['_runtime_health'] = connection['health']
        token = current.set(record)
        with _lock:
            _active[record['id']] = record
        async def timed_receive():
            began = time.perf_counter()
            reading_upload = not record['upload_complete']
            message = await receive()
            # StreamingResponse also calls receive() to watch for disconnects
            # throughout a reply. That wait is not request-body upload time.
            if reading_upload and message['type'] in ('http.request', 'http.disconnect'):
                add_stage('client_upload_wait', (time.perf_counter()-began)*1000)
            if message['type'] == 'http.request':
                record['request_bytes'] += len(message.get('body', b''))
                record['upload_complete'] = not message.get('more_body', False)
            elif message['type'] == 'http.disconnect':
                # ASGI may report closure after the final frame; that is a
                # normal lifecycle event, not a failed response.
                if not (record['response_complete'] or record['terminal_sent']):
                    record['outcome'] = 'disconnected'
            return message
        async def timed_send(message):
            elapsed = round((time.perf_counter()-start)*1000, 1)
            if message['type'] == 'http.response.start':
                record['status'] = message['status']
                record['headers_ms'] = elapsed
                record['streaming'] = any(key.lower() == b'content-type' and b'text/event-stream' in value
                                          for key, value in message.get('headers', []))
                message = {**message, 'headers': list(message.get('headers', [])) +
                           [(b'x-local-request-id', record['id'].encode())]}
            elif message['type'] == 'http.response.body' and message.get('body'):
                if record['first_body_ms'] is None:
                    record['first_body_ms'] = elapsed
                    if not record['streaming']:
                        record['first_delta_ms'] = elapsed
            began = time.perf_counter()
            await send(message)
            if message['type'] == 'http.response.body':
                record['response_bytes'] += len(message.get('body', b''))
                if record.get('_terminal_ready') and message.get('body'):
                    record['terminal_sent'] = True
                if not message.get('more_body', False):
                    record['response_complete'] = True
                add_stage('client_send_wait', (time.perf_counter()-began)*1000)
        try:
            await self.app(scope, timed_receive, timed_send)
        except BaseException as error:
            if not record.get('request_error'):
                record['request_error'] = 'request_cancelled' if isinstance(error, asyncio.CancelledError) else 'gateway_exception'
            if record['outcome'] == 'active':
                record['outcome'] = 'interrupted'
            raise
        finally:
            from admin.request_payload import release
            release(record)
            record['total_ms'] = round((time.perf_counter()-start)*1000, 1)
            if record['request_error'] == 'client_disconnected' and not record['upload_complete']:
                record['outcome'] = 'disconnected'
            elif (record['stream_error'] or (record['status'] or 0) >= 400
                  or record.get('upstream_error') not in (None, 'success')):
                record['outcome'] = 'http_error'
            elif record['terminal_sent'] or (record['response_complete'] and
                    (not record['streaming'] or record.get('upstream_error') == 'success')):
                record['outcome'] = 'completed'
            elif record['streaming'] and record['response_complete']:
                record['outcome'] = 'interrupted'
            elif record['outcome'] == 'active':
                record['outcome'] = 'interrupted'
            with _lock:
                if record.get('_runtime_health') is not None:
                    record.update(record.pop('_runtime_health').since(start))
                _active.pop(record['id'], None)
                _recent.append(record)
            try:
                if self.persist and record.get('key_id'):
                    from admin.gateway_limits import _finish_thread
                    from admin.request_audit import save
                    await _finish_thread(save, record)
            finally:
                current.reset(token)
