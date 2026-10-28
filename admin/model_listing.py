"""Live display metadata for the public model list; model IDs stay stable."""
from datetime import datetime
import logging

from admin.model_busy import LABELS, normalize_response
from model_metadata import model_type, type_tags

_logger = logging.getLogger(__name__)


def display_options(db):
    import json
    from admin.models import SystemSetting
    row = db.get(SystemSetting, "model_display")
    try:
        value = json.loads(row.value) if row else {}
    except (ValueError, TypeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    return {key: value.get(key) is not False for key in ("show_multiplier", "show_busy")}


def enrich_entries(session, entries, on_error=None, options=None):
    """Read official capacity on every listing, using the catalog's identity.

    Only request models already filtered by the caller's permissions. Capacity
    failures must not hide the catalog or turn stale observations into live data.
    """
    options = options or {"show_multiplier": True, "show_busy": True}
    ids = sorted({entry["id"] for entry in entries})
    levels = {model: "unknown" for model in ids}
    observed_at = None
    stale = False
    if ids and options["show_busy"]:
        try:
            fresh = {}
            for offset in range(0, len(ids), 50):
                batch = ids[offset:offset + 50]
                fresh.update(normalize_response(batch, session.fetch_model_busy(batch)))
            levels = fresh
            observed_at = datetime.utcnow().isoformat() + "Z"
        except Exception as error:
            stale = True
            _logger.warning("模型列表繁忙状态获取失败，返回未知状态")
            if on_error is not None:
                on_error(error)
    return format_entries(entries, levels, options, observed_at, stale)


def format_entries(entries, levels=None, options=None, observed_at=None, stale=True):
    levels = levels or {}
    options = options or {"show_multiplier": True, "show_busy": True}
    result = []
    for entry in entries:
        kind = model_type(entry)
        level = levels.get(entry["id"], "unknown")
        multiplier = entry.get("credit_multiplier")
        credit_label = '按实际用量' if kind == 'image' else f"×{multiplier:g}" if multiplier is not None else "×未知"
        parts = [entry["id"]]
        if options["show_multiplier"]:
            parts.append(credit_label)
        if options["show_busy"]:
            parts.append(LABELS[level])
        result.append({**entry,
                       "model_type": kind, "tags": type_tags(entry),
                       **({"credit_multiplier": None, "billing_mode": "upstream_usage", "is_free": False} if kind == 'image' else {}),
                       "name": ' · '.join(parts),
                       "busy_level": level, "busy_label": LABELS[level],
                       "busy_observed_at": observed_at, "busy_stale": stale})
    return result
