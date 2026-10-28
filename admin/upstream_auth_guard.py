"""Guard account state against shared upstream authentication anomalies.

An HTTP 401/403 is not always proof that one account's session died.  A
wrong header, route, or upstream gateway rollout can return the same response
for every credential.  This small process-local gate turns that pattern into
an observable anomaly instead of permanently disabling the whole pool.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import defaultdict, deque


_WINDOW_SECONDS = 120.0
_SHARED_THRESHOLD = 3
_lock = threading.Lock()
_hits: dict[str, deque[tuple[float, str]]] = defaultdict(deque)


def _body_lower(body: str | None) -> str:
    return re.sub(r"\s+", " ", str(body or "").lower()).strip()


def known_request_anomaly(status: int, body: str | None) -> bool:
    """Return true for responses that identify a request/header problem.

    These are observed gateway responses, not guesses about a user's token:
    ``401 {"message":"not_found"}`` and billing ``10085`` explicitly mean
    the request shape was rejected.
    """
    if status not in (401, 403):
        return False
    text = _body_lower(body)
    if re.search(r'"message"\s*:\s*"not_found"', text):
        return True
    if re.search(r'"code"\s*:\s*"?10085"?', text):
        return True
    return any(marker in text for marker in (
        "request header is invalid",
        "请求头不合法",
        "illegal request header",
    ))


def observe(status: int, body: str | None, uid: str | None = None) -> bool:
    """Observe an auth response and return whether it is pool-wide anomaly.

    Known request-shape errors are anomalous immediately.  For other 401/403
    responses, three distinct account UIDs with the same normalized response
    inside the window are enough to stop account-level disabling.
    """
    if status not in (401, 403):
        return False
    if known_request_anomaly(status, body):
        return True
    text = _body_lower(body)
    if not text:
        text = f"http:{status}"
    signature = hashlib.sha256(f"{status}:{text}".encode("utf-8", "replace")).hexdigest()
    identity = str(uid or "-")
    now = time.monotonic()
    with _lock:
        queue = _hits[signature]
        cutoff = now - _WINDOW_SECONDS
        while queue and queue[0][0] < cutoff:
            queue.popleft()
        queue.append((now, identity))
        distinct = {item_uid for _when, item_uid in queue}
        # Keep the map bounded after a long-running process.
        if len(_hits) > 256:
            for key, values in list(_hits.items()):
                while values and values[0][0] < cutoff:
                    values.popleft()
                if not values:
                    _hits.pop(key, None)
        return len(distinct) >= _SHARED_THRESHOLD


def reset() -> None:
    """Clear observations after an operator has corrected the request path."""
    with _lock:
        _hits.clear()
