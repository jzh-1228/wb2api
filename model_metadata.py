"""Official product model metadata and time-dependent promotion display.

Product config enriches the entitled CLI catalog; it must never import the
product's completion/video/internal models into the chat gateway.
"""
from copy import deepcopy
from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MODE_IDS = ("fast-model", "balanced-model", "deep-model")
METADATA_FIELDS = (
    "name", "descriptionZh", "descriptionEn", "description", "credits",
    "tags", "promotions", "tier", "vendor", "maxInputTokens",
    "maxOutputTokens", "maxAllowedSize", "reasoning", "supportsImages",
    "supportsReasoning", "supportsToolCall", "onlyReasoning",
    "canDisableThinking", "temperature", "top_p", "top_k",
    "repetition_penalty", "iconUrl",
)


def client_capabilities(model):
    """Expose catalog limits and reasoning options in client-friendly fields."""
    reasoning = model.get("reasoning")
    efforts = []
    default_effort = ""
    if isinstance(reasoning, dict):
        raw_efforts = reasoning.get("supportedEfforts")
        if isinstance(raw_efforts, list):
            efforts = list(dict.fromkeys(
                str(value).strip() for value in raw_efforts if str(value).strip()
            ))
        if not efforts and reasoning.get("effort"):
            efforts = [str(reasoning["effort"]).strip()]
        default_effort = str(reasoning.get("defaultEffort") or "").strip()
    return {
        "context_length": model.get("maxInputTokens"),
        "context_window": model.get("maxInputTokens"),
        "max_input_tokens": model.get("maxInputTokens"),
        "max_output_tokens": model.get("maxOutputTokens"),
        "max_allowed_size": model.get("maxAllowedSize"),
        "supports_images": bool(model.get("supportsImages")),
        "supports_reasoning": bool(model.get("supportsReasoning")),
        "supports_tool_call": bool(model.get("supportsToolCall")),
        "only_reasoning": bool(model.get("onlyReasoning")),
        "can_disable_thinking": model.get("canDisableThinking"),
        "reasoning_supported_efforts": efforts,
        "reasoning_default_effort": default_effort if default_effort in efforts else "",
    }


def is_image_model(model_id):
    """Known image-only IDs must not enter automatic chat fallback."""
    return str(model_id or '').lower().startswith('hunyuan-image-')


def model_type(model):
    """Classify output modality; vision inputs still belong to text models."""
    meta = model.get('metadata') if isinstance(model.get('metadata'), dict) else model
    tags = meta.get('tags') or []
    tags = {tag.lower() for tag in tags if isinstance(tag, str)} if isinstance(tags, list) else set()
    return 'image' if (is_image_model(model.get('model_id') or model.get('id'))
        or tags & {'text-to-image', 'image-to-image'} or meta.get('kind') == 'image') else 'text'


def type_tags(model):
    meta = model.get('metadata') if isinstance(model.get('metadata'), dict) else model
    tags = meta.get('tags') or []
    tags = [tag for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else []
    return list(dict.fromkeys([*tags, model_type(model)]))


def merge_product_catalog(payload, product):
    result = deepcopy(payload)
    data = result.get("data")
    if not isinstance(data, dict) or not isinstance(product, dict):
        return result
    configured = {m["id"]: m for m in product.get("models") or []
                  if isinstance(m, dict) and isinstance(m.get("id"), str)}
    models = data.setdefault("models", [])
    known = {m.get("id") for m in models if isinstance(m, dict)}
    # These are official routing IDs, sent to upstream unchanged.
    for mid in MODE_IDS:
        if mid in configured and mid not in known and not configured[mid].get("disabled"):
            models.append(deepcopy(configured[mid]))
    for model in models:
        if not isinstance(model, dict):
            continue
        mid = model.get("id")
        extra = configured.get(mid, {})
        disabled = model.get("disabled")
        model.update(deepcopy(extra))
        if disabled:  # product metadata cannot remove an entitlement restriction
            model["disabled"] = disabled
        model["promotions"] = [deepcopy(p) for p in product.get("modelPromotions") or []
                               if isinstance(p, dict) and mid in (p.get("modelIds") or [])]
        tiers = [t for t in product.get("modelTiers") or []
                 if isinstance(t, dict) and mid in (t.get("modelIds") or [])]
        if tiers:
            model["tier"] = deepcopy(tiers[0])
    for agent in data.get("agents") or []:
        if isinstance(agent, dict) and agent.get("name") == "cli":
            ids = agent.setdefault("models", [])
            for mid in MODE_IDS:
                if mid in configured and not configured[mid].get("disabled") and mid not in ids:
                    ids.append(mid)
    return result


def metadata(model):
    return {key: model[key] for key in METADATA_FIELDS if key in model}


def credit_number(value):
    match = re.search(r"(?:[x×]\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*[x×])", str(value or ""))
    return float(match.group(1) or match.group(2)) if match else None


def promotion_active(promotion, now=None):
    now = now or datetime.now(timezone.utc)
    schedule = promotion.get("schedule") or {}
    for key, upper in (("validFrom", False), ("validUntil", True)):
        try:
            boundary = datetime.fromisoformat(schedule[key].replace("Z", "+00:00"))
            if boundary.tzinfo is None:
                boundary = boundary.replace(tzinfo=timezone.utc)
            if (upper and now >= boundary) or (not upper and now < boundary):
                return False
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
    windows = schedule.get("daily")
    if not windows:
        return True
    try:
        local = now.astimezone(ZoneInfo(schedule.get("timezone") or "Asia/Shanghai"))
    except (ZoneInfoNotFoundError, ValueError):
        return False
    minute = local.hour * 60 + local.minute
    for window in windows:
        try:
            values = [re.fullmatch(r"(\d{1,2}):(\d{2})", window[key].strip()) for key in ("start", "end")]
            if not all(value and int(value[1]) < 24 and int(value[2]) < 60 for value in values):
                continue
            start, end = (int(value[1]) * 60 + int(value[2]) for value in values)
            if start == end or (start <= minute < end if start < end else minute >= start or minute < end):
                return True
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return False


def promotion_display(model, now=None):
    rules = [p for p in model.get("promotions", []) if isinstance(p, dict) and p.get("enabled") is not False]
    active = [p for p in rules if promotion_active(p, now)]
    def priority(p):
        value = p.get("priority", 0)
        return value if isinstance(value, (int, float)) else 0
    selected = max(active or rules, key=priority, default={})
    is_active = bool(selected) and selected in active
    badge = selected.get("badge") or {}
    visible = is_active or badge.get("display") == "always"
    base = credit_number(model.get("credits"))
    price = base
    discount = selected.get("discount") or {}
    if is_active and selected.get("kind") == "discount":
        explicit = credit_number(discount.get("discountedCredits"))
        factor = discount.get("factor")
        if explicit is not None:
            price = explicit
        elif base is not None and isinstance(factor, (int, float)) and 0 < factor < 1:
            price = round(base * factor, 4)
    return {"badge": badge if visible else {}, "promotion_text":
            (selected.get("hover") or {}).get("textZh", "") if is_active else "",
            "credit_multiplier": price, "original_credit_multiplier": base,
            "discount_display": discount.get("displayMode", "replace") if is_active else "replace"}
