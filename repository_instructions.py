"""Preserve repository guidance embedded in runtime reminder messages."""
from __future__ import annotations

import re
from collections.abc import Callable

_REMINDER = re.compile(r"<system-reminder\b[^>]*>(.*?)</system-reminder\s*>", re.I | re.S)
_PRESERVED = re.compile(
    r'<repository-instructions source="(?P<source>(?:AGENTS|CLAUDE)(?:\.local)?\.md)">'
    r'(?P<body>.*?)</repository-instructions>',
    re.S,
)
_SOURCE = re.compile(
    r"^[ \t]*Instructions from:[ \t]*(?P<source>[^\r\n]+?)(?::[ \t]*)?[ \t]*(?:\r?\n|$)",
    re.I | re.M,
)
_INLINE_INSTRUCTIONS = re.compile(
    r"^[ \t]*Instructions from:[ \t]*(?P<source>[^\r\n]*?(?:AGENTS|CLAUDE)(?:\.local)?\.md[\"'`]?):[ \t]*",
    re.I | re.M,
)


def _instruction_source(source: str) -> str | None:
    name = source.strip().strip('`"\'').replace('\\', '/').rsplit('/', 1)[-1]
    return next((known for known in ('AGENTS.md', 'CLAUDE.md', 'AGENTS.local.md', 'CLAUDE.local.md')
                 if name.casefold() == known.casefold()), None)


def _instruction_spans(text: str):
    if '<' not in text:
        return []
    spans = [(m.start(), m.end(), m.group('body'), m.group('source')) for m in _PRESERVED.finditer(text)]
    for reminder in _REMINDER.finditer(text):
        if any(start <= reminder.start() < end for start, end, *_ in spans):
            continue
        inner = reminder.group(1)
        sources = {m.start(): m for m in _SOURCE.finditer(inner)}
        # Inline bodies can contain colons; prefer the explicit AGENTS.md boundary.
        sources.update({m.start(): m for m in _INLINE_INSTRUCTIONS.finditer(inner)})
        headers = sorted(sources.values(), key=lambda m: m.start())
        for index, header in enumerate(headers):
            source = _instruction_source(header.group('source'))
            if source is None:
                continue
            end = headers[index + 1].start() if index + 1 < len(headers) else len(inner)
            body = inner[header.end():end]
            if body.strip():
                offset = reminder.start(1)
                spans.append((offset + header.start(), offset + end, body, source))
    return sorted(spans)


def extract_repository_instructions(text: str) -> tuple[str, list[str]]:
    """Remove only recognized source sections; keep their complete bodies separately."""
    parts, instructions = [], []
    cursor = 0
    for start, end, body, source in _instruction_spans(text):
        parts.append(text[cursor:start])
        instructions.append(
            '<repository-instructions source="' + source + '">' + body + '</repository-instructions>'
        )
        cursor = end
    parts.append(text[cursor:])
    return ''.join(parts), instructions


def rewrite_non_repository_text(text: str, rewrite: Callable[[str], str]) -> str:
    """Run template compatibility rewrites without modifying repository guidance."""
    parts = []
    cursor = 0
    for start, end, _body, _source in _instruction_spans(text):
        parts.extend((rewrite(text[cursor:start]), text[start:end]))
        cursor = end
    parts.append(rewrite(text[cursor:]))
    return ''.join(parts)
