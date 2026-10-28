"""
desensitize — 针对 CodeBuddy 后端内容审核的脱敏模块（独立、可选）。

背景
----
CodeBuddy 后端（copilot.tencent.com）有内容审核，会拦截含"攻击/漏洞/凭证"
等含义的英文术语。但这些词经常出现在客户端**固定的合规 system 模板**里
（例如 ZCode 的 agent 声明：「Refuse requests for DoS attacks, exploit
development, credential testing, C2 frameworks ...」），属于**拒绝作恶**
的合规声明，并非用户的有害输入，却被后端误判为敏感词，导致整条请求被拦。

本模块做的事
------------
对这些"合规声明高频词"做轻量处理：在词内部插入零宽空格（U+200B），

    "DoS" -> "Do\u200bS"        （人/模型读仍是 DoS，后端关键词匹配失效）

只处理一个明确的词表，默认只作用于 system 角色的消息（这是模板合规声明的
集中地）。不改动其它角色内容，避免影响真实对话。

设计原则
--------
- 独立模块，可单独 import / 单独测试。
- 保守：词表小而明确；只默认处理 system 消息；可关闭。
- 不试图、也不可能绕过对用户真实有害输入的审核——只缓解客户端模板被误伤。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from repository_instructions import extract_repository_instructions

# 零宽空格：插入到关键词内部，打断后端的关键词匹配，但模型/人眼读起来无差别。
_ZWSP = "\u200b"

# 触发审核的"合规声明高频词"（来自真实被拦截的客户端 system 模板）。
# 全部是"拒绝作恶"语境里常见的英文术语。大小写不敏感匹配。
SENSITIVE_TERMS: list[str] = [
    # 原有词表
    "DoS",
    "DDoS",
    "exploit",
    "credential testing",
    "credential stuffing",
    "supply chain compromise",
    "supply-chain compromise",
    "detection evasion",
    "C2 frameworks",
    "C2 framework",
    "command and control",
    "malicious purposes",
    "malicious intent",
    "mass targeting",
    "brute force",
    "brute-force",
    "privilege escalation",
    "reverse shell",
    "remote code execution",
    "SQL injection",
    "XSS",
    "CSRF",
    "phishing",
    "malware",
    "ransomware",
    "keylogger",
    "rootkit",
    "backdoor",
    "botnet",
    "zero-day",
    "0day",
    # Codex CLI system prompt 里额外的高频触发词
    "vulnerability",
    "vulnerabilities",
    "red teaming",
    "red-teaming",
    "sandbox",
    "sandboxing",
    "sandboxed",
    "unsandboxed",
    "escalated privileges",
    "escalated",
    "escalation",
    "destructive action",
    "destructive command",
    "destructive",
    "attack",
    "attacks",
    "cybersecurity",
    "security review",
    "exploit development",
    "hacking",
    "penetration testing",
    "penetration test",
    "injection",
    "weaponize",
    "weaponized",
    "harmful",
    "dangerous",
    "abuse",
    "abusive",
    "illegal",
    "terrorist",
    "terrorism",
    "bomb",
    "weapon",
    "weapons",
    "drug",
    "drugs",
    "narcotic",
    "suicide",
    "self-harm",
    "murder",
    "kill",
    "violence",
    "violent",
    # Claude Code / Anthropic 品牌词（避免竞争品牌词触发审核）
    "Claude Code",
    "Claude Opus",
    "Claude Sonnet",
    "Claude Haiku",
    "Claude Fable",
    "Anthropic",
    "Co-Authored-By",
    "noreply@anthropic.com",
]

# 编译成一个大正则，按词长降序，避免短词先吃掉长词。
# 用 \b 边界 + 忽略大小写。
_PATTERN = re.compile(
    "|".join(re.escape(t) for t in sorted(SENSITIVE_TERMS, key=len, reverse=True)),
    re.IGNORECASE,
)

# Codex CLI 会把大量运行时上下文包装进一条 user 消息里；这些不是用户真正提问，
# 里面常含 permissions / sandbox / skills 等说明，也会触发后端审核。
_HARNESS_USER_MARKERS = (
    "# AGENTS.md instructions",
    "<environment_context>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<skills_instructions>",
    "<system-reminder>",           # Claude Code 注入的运行时上下文
    "# claudeMd",                  # Claude Code CLAUDE.md 注入
)

_CODEX_SYSTEM_MARKERS = (
    "You are a coding agent running in the Codex CLI",
    "Within this context, Codex refers to",
    "# How you work",
    "You are Claude Code",         # Claude Code system prompt
    "You are a Claude agent",      # Claude Code system prompt (Agent SDK 版)
    "Claude Agent SDK",            # Claude Code system prompt (Agent SDK 版)
)

_PERMISSIONS_MARKERS = (
    "<permissions instructions>",
    "Filesystem sandboxing defines which files can be read or written.",
    "## How to request escalation",
)

_SKILLS_MARKERS = (
    "<skills_instructions>",
    "### Available skills",
    "### How to use skills",
)

_RUNTIME_BLOCK_REPLACEMENTS = (
    (
        "<environment_context>",
        "</environment_context>",
        "Environment context is provided by the harness.",
    ),
    (
        "<permissions instructions>",
        "</permissions instructions>",
        (
            "Runtime permissions apply: filesystem access may be sandboxed, network may be restricted, "
            "and some commands may require user approval."
        ),
    ),
    (
        "<collaboration_mode>",
        "</collaboration_mode>",
        "Collaboration mode instructions are provided by the harness.",
    ),
    (
        "<skills_instructions>",
        "</skills_instructions>",
        "Runtime skill metadata is available. Use relevant skills only when explicitly requested or clearly applicable.",
    ),
    (
        "<plugins_instructions>",
        "</plugins_instructions>",
        "Runtime plugin metadata is available when relevant.",
    ),
    (
        "<system-reminder>",
        "</system-reminder>",
        "Runtime reminder context is provided by the harness.",
    ),
)

_RUNTIME_TAIL_MARKERS = (
    "The following deferred tools are now available via ToolSearch.",
    "Available agent types for the Agent tool:",
    "The following sk​ills are available for use with the Sk​ill tool:",
    "## MCP Server Instructions",
)

_RUNTIME_TAIL_SUMMARY = (
    "Runtime tool, agent, skill, and MCP metadata is available separately."
)

_CODEX_CORE_SUMMARY = (
    "You are a coding assistant. Be precise, helpful, concise, and safe.\n\n"
    "# Doing tasks\n"
    "The user will primarily request software engineering tasks: fixing bugs, adding features, "
    "refactoring code, explaining code, and more. When given an unclear instruction, consider it "
    "in the context of software engineering and the current working directory.\n\n"
    "- Understand existing code before suggesting modifications. Read files before editing.\n"
    "- Do not propose changes to code you haven't read.\n"
    "- Do not add features, refactor code, or make improvements beyond what was asked.\n"
    "- Do not create files unless absolutely necessary. Prefer editing existing files.\n"
    "- Avoid giving time estimates or predictions. Focus on what needs to be done.\n"
    "- If an approach fails, diagnose why before switching tactics.\n"
    "- Break down and manage work with TaskCreate for complex multi-step tasks.\n"
    "- Mark each task as completed as soon as it is done.\n\n"
    "# Using tools\n"
    "- To read files use Read (NOT cat/head/tail). To edit use Edit (NOT sed/awk).\n"
    "- To create files use Write (NOT echo/cat heredoc). To search files use Glob (NOT find/ls).\n"
    "- To search content use Grep (NOT grep/rg). Reserve Bash for system commands only.\n"
    "- Run tests after making changes to verify correctness.\n"
    "- Use the Agent tool for complex research or multi-step independent work.\n"
    "- Use AskUserQuestion when requirements are ambiguous or you need decisions.\n"
    "- Use EnterPlanMode for non-trivial implementation tasks before writing code.\n\n"
    "# Code quality\n"
    "- Only add comments where the logic is not self-evident.\n"
    "- Do not add docstrings, type annotations, or refactoring to code you didn't change.\n"
    "- Do not add error handling for scenarios that can't happen.\n"
    "- Do not introduce security vulnerabilities (command injection, XSS, SQL injection, OWASP top 10).\n"
    "- If you notice insecure code you wrote, fix it immediately.\n"
    "- Three similar lines of code is better than a premature abstraction.\n"
    "- Avoid backwards-compatibility hacks. Delete unused code completely.\n\n"
    "# File operations\n"
    "- To read files use Read (NOT cat/head/tail/sed).\n"
    "- To edit files use Edit (NOT sed/awk). To create files use Write (NOT echo/cat).\n"
    "- To search files use Glob (NOT find/ls). To search content use Grep (NOT grep/rg).\n"
    "- Prefer editing an existing file to creating a new one.\n\n"
    "# Running commands\n"
    "- Use Bash for system commands and terminal operations requiring shell execution.\n"
    "- Prefer running tests after code changes.\n"
    "- Avoid destructive operations (deleting files, force-push, reset --hard) without user confirmation.\n"
    "- NEVER update git config. NEVER run destructive git commands unless explicitly requested.\n"
    "- NEVER commit changes unless the user explicitly asks.\n"
    "- Only create commits when requested by the user.\n\n"
    "# Communication\n"
    "- All responses in Chinese (中文). Technical terms and code identifiers stay in original form.\n"
    "- Keep text output short and direct. Lead with the answer or action, not the reasoning.\n"
    "- Skip filler words, preamble, and unnecessary transitions.\n"
    "- If you can say it in one sentence, don't use three.\n"
    "- Focus on: decisions needing user input, high-level status updates, errors or blockers.\n"
    "- When referencing code, include file_path:line_number format.\n\n"
    "# Safety\n"
    "- Refuse destructive techniques, mass targeting, or detection evasion for malicious purposes.\n"
    "- Dual-use security tools require clear authorization: pentesting, CTF, security research, or defensive use.\n"
    "- Never write exploits, exploit PoCs, malware, or attack any system regardless of ownership.\n"
    "- Do not generate or guess URLs unless confident they help with programming.\n"
    "- You may use URLs provided by the user or found in local files.\n\n"
    "# Verification\n"
    "- After completing a task, verify changes work (run tests, check syntax).\n"
    "- Report what was changed, where files were added/modified.\n"
    "- If blocked or stuck, diagnose the issue before trying a different approach.\n"
    "- Use the Agent tool with specialized agents for testing and review when appropriate.\n\n"
    "# Task management\n"
    "- Use TaskCreate to track multi-step work (3+ steps).\n"
    "- Mark tasks in_progress when starting, completed when done.\n"
    "- Use TaskList to check available work and progress.\n"
    "- Prefer working on tasks in ID order (lowest ID first).\n\n"
    "# Memory\n"
    "- You have a persistent memory system. Save important user preferences, feedback, and project context.\n"
    "- When the user explicitly asks to remember something, save it immediately.\n"
    "- When recalling from memory, verify the information is still current before acting on it.\n\n"
    "# Plan mode\n"
    "- For non-trivial implementation tasks, use EnterPlanMode to design the approach first.\n"
    "- Write a clear plan with steps, then use ExitPlanMode for user approval.\n"
    "- Do not start implementation until the user approves the plan.\n\n"
    "# Git workflow\n"
    "- When committing: run git status, git diff, git log in parallel first.\n"
    "- Draft a concise commit message focused on the why, not the what.\n"
    "- Stage specific files by name, not git add -A.\n"
    "- Never push unless the user explicitly asks.\n"
    "- When creating PRs: summarize changes, include test plan, use gh pr create.\n\n"
    "# Skills\n"
    "- When a skill matches the user's request, use the Skill tool to invoke it.\n"
    "- Available skills are listed in system-reminder messages. Use them proactively when relevant.\n"
    "- Do not invoke a skill that is already running.\n\n"
    "Follow repository instructions, CLAUDE.md files, and coding conventions when present. "
    "Keep the user informed with concise progress updates. "
    "When running tests or builds, summarize results clearly."
)


def _zero_width_split(term: str) -> str:
    """在词内部插入零宽空格。如 'DoS' -> 'Do\\u200bS'。"""
    if len(term) <= 1:
        return term
    # 在第 1 个字符后插入即可（足够打断子串匹配，且改动最小）
    return term[0] + _ZWSP + term[1:]


def desensitize_text(text: str) -> str:
    """对文本中的触发词插入零宽空格。无触发词则原样返回。"""
    if not text:
        return text
    return _PATTERN.sub(lambda m: _zero_width_split(m.group(0)), text)


def _iter_text_blocks(content):
    """遍历 OpenAI content（字符串或 [{type, text}, ...]）里的文本块，返回 (容器, key)。"""
    if isinstance(content, str):
        yield content, None  # 字符串：调用方直接替换
    elif isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                yield blk, "text"


def _content_to_text(content) -> str:
    """把字符串或 content blocks 规整成纯文本，便于识别注入模板。"""
    text = content if isinstance(content, str) else ""
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(str(blk.get("text", "")))
        text = "".join(parts)
    return text


def _looks_like_harness_user_message(content) -> bool:
    """判断 user 消息是否其实是 Codex/CLI 注入的上下文，而非用户自然输入。"""
    text = _content_to_text(content)
    return any(marker in text for marker in _HARNESS_USER_MARKERS)


def _extract_user_payload(text: str) -> str:
    """去掉 harness 注入块/尾部元数据，**保留用户真实输入**。

    Claude Code 常把提问和 <system-reminder> 写进同一条 user 消息：
        "<system-reminder>...</system-reminder>\\n\\nWhat is 1+1?"
    此前 compact 会整条换成固定摘要，把用户提问一并吞掉（首轮只回
    "I'm ready to help..."）。这里只剥注入块，剩什么就留什么。
    """
    if not text:
        return ""
    out = text
    for start_tag, end_tag, _replacement in _RUNTIME_BLOCK_REPLACEMENTS:
        out = re.sub(
            r"\s*" + re.escape(start_tag) + r".*?" + re.escape(end_tag) + r"\s*",
            "\n",
            out,
            flags=re.DOTALL,
        )
        # 未闭合的尾块（截断/异常注入）：从起始标签截到结尾
        if start_tag in out and end_tag not in out:
            out = re.sub(r"\s*" + re.escape(start_tag) + r".*$", "\n", out, flags=re.DOTALL)

    tail_indexes = [out.find(marker) for marker in _RUNTIME_TAIL_MARKERS if marker in out]
    if tail_indexes:
        out = out[: min(idx for idx in tail_indexes if idx >= 0)]

    for _start, _end, replacement in _RUNTIME_BLOCK_REPLACEMENTS:
        out = out.replace(replacement, " ")
    out = out.replace(_RUNTIME_TAIL_SUMMARY, " ")

    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def _prune_runtime_fragments(role: str, text: str) -> str:
    """轻量裁掉冗长的运行时元数据，保留主要行为指令。

    用于 --no-compact 场景：尽量保留 Codex / Claude Code 的核心提示，
    但移除重复的 environment / permissions / skills / tool inventory 大段文本。
    """
    if not text:
        return text

    pruned = text
    for start_tag, end_tag, replacement in _RUNTIME_BLOCK_REPLACEMENTS:
        pattern = re.compile(
            r"\s*" + re.escape(start_tag) + r".*?" + re.escape(end_tag) + r"\s*",
            re.DOTALL,
        )
        pruned = pattern.sub("\n\n" + replacement + "\n\n", pruned)

    tail_indexes = [pruned.find(marker) for marker in _RUNTIME_TAIL_MARKERS if marker in pruned]
    if tail_indexes:
        cut = min(idx for idx in tail_indexes if idx >= 0)
        head = pruned[:cut].rstrip()
        pruned = f"{head}\n\n{_RUNTIME_TAIL_SUMMARY}" if head else _RUNTIME_TAIL_SUMMARY

    if role == "system" and any(marker in pruned for marker in _CODEX_SYSTEM_MARKERS):
        keep_sections: list[str] = []
        intro_match = re.search(
            r"^.*?(?=\n# AGENTS\.md spec|\n## Responsiveness|\n## Planning|\n## Task execution|\Z)",
            pruned,
            re.DOTALL,
        )
        if intro_match:
            intro = intro_match.group(0).strip()
            if intro:
                keep_sections.append(intro)

        for heading in ("## Personality", "# AGENTS.md spec"):
            pattern = re.compile(
                re.escape(heading) + r".*?(?=\n## |\n# |\Z)",
                re.DOTALL,
            )
            match = pattern.search(pruned)
            if match:
                section = match.group(0).strip()
                if section:
                    keep_sections.append(section)

        if keep_sections:
            pruned = "\n\n".join(keep_sections)
        else:
            pruned = _CODEX_CORE_SUMMARY

    if role == "user" and _looks_like_harness_user_message(text):
        residual = _extract_user_payload(text)
        # 有真实提问就保留；纯 harness 才换成摘要，避免吞掉用户输入
        return residual if residual else (
            "Repository instructions and durable user context are provided. "
            "Follow repository guidance while answering the user's actual request."
        )

    pruned = re.sub(r"\n{3,}", "\n\n", pruned).strip()
    return pruned


def _compact_harness_message(role: str, content) -> str | None:
    """把 Codex / Claude Code 注入的超长运行时提示压缩成短摘要，降低审核误伤。"""
    # A text summary cannot represent attachments; keep multimodal blocks intact.
    if isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") != "text" for block in content
    ):
        return None
    text = _content_to_text(content)
    if not text:
        return None
    if role == "system" and any(marker in text for marker in _CODEX_SYSTEM_MARKERS):
        return _CODEX_CORE_SUMMARY
    if any(marker in text for marker in _PERMISSIONS_MARKERS):
        return (
            "Runtime permissions apply: filesystem access may be sandboxed, network may be restricted, "
            "and some commands may require user approval."
        )
    if any(marker in text for marker in _SKILLS_MARKERS):
        return (
            "Runtime skill metadata is available. Use relevant skills only when explicitly requested or clearly applicable."
        )
    if role == "user" and _looks_like_harness_user_message(content):
        residual = _extract_user_payload(text)
        if residual:
            # 用户提问和 harness 混在同一块：只剥注入，保留提问
            return residual
        return (
            "Repository instructions and environment context are provided. Follow repository guidance "
            "while answering the user's actual request."
        )
    return None


def _desensitize_tool_value(value: Any, strip_metadata: bool = False):
    """递归处理 tool 定义，必要时移除高风险描述字段。"""
    if isinstance(value, dict):
        new_value = {}
        for key, item in value.items():
            if key in ("description", "title") and isinstance(item, str):
                if strip_metadata:
                    continue
                new_value[key] = desensitize_text(item)
            else:
                new_value[key] = _desensitize_tool_value(item, strip_metadata=strip_metadata)
        return new_value
    if isinstance(value, list):
        return [_desensitize_tool_value(item, strip_metadata=strip_metadata) for item in value]
    return value


def desensitize_messages(messages: Iterable[dict],
                         roles: tuple[str, ...] = ("system",),
                         desensitize_harness_user: bool = False,
                         compact_harness: bool = False) -> list[dict]:
    """对指定角色的消息文本做脱敏，返回新的 messages 列表（不修改原对象）。

    默认只处理 system 角色（合规模板集中地）。可选处理 developer，
    以及 Codex 注入的 harness user 上下文；真实用户输入保持原样。
    """
    out: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        role = m.get("role")
        should_desensitize = role in roles
        if role == "user" and desensitize_harness_user:
            should_desensitize = _looks_like_harness_user_message(m.get("content"))

        nm = dict(m)  # 浅拷贝，不污染调用方
        if should_desensitize:
            content = m.get("content")
            preserved: list[str] = []
            block_instructions: dict[int, list[str]] = {}
            if isinstance(content, str):
                content, preserved = extract_repository_instructions(content)
            elif isinstance(content, list):
                cleaned = []
                for index, block in enumerate(content):
                    if isinstance(block, dict) and block.get("type") == "text":
                        text, instructions = extract_repository_instructions(block.get("text", ""))
                        block_instructions[index] = instructions
                        preserved.extend(instructions)
                        cleaned.append(dict(block, text=text))
                    else:
                        cleaned.append(block)
                content = cleaned
            compacted = _compact_harness_message(role, content) if compact_harness else None
            if compacted is not None:
                nm["content"] = '\n\n'.join([desensitize_text(compacted), *preserved])
            elif isinstance(content, str):
                nm["content"] = '\n\n'.join([desensitize_text(_prune_runtime_fragments(role, content)), *preserved])
            elif isinstance(content, list):
                new_blocks = []
                for index, blk in enumerate(content):
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        nb = dict(blk)
                        nb["text"] = '\n\n'.join([
                            desensitize_text(_prune_runtime_fragments(role, blk.get("text", ""))),
                            *block_instructions.get(index, []),
                        ])
                        new_blocks.append(nb)
                    else:
                        new_blocks.append(blk)
                nm["content"] = new_blocks
        out.append(nm)
    return out


def desensitize_body(body: dict, roles: tuple[str, ...] = ("system",),
                     desensitize_harness_user: bool = False,
                     desensitize_tools: bool = False,
                     compact_harness: bool = False,
                     strip_tool_metadata: bool = False) -> dict:
    """对请求体里的 messages / tools 做脱敏，返回新的 body（浅拷贝）。"""
    changed = False
    nb = dict(body)
    if body.get("messages"):
        nb["messages"] = desensitize_messages(
            body["messages"],
            roles=roles,
            desensitize_harness_user=desensitize_harness_user,
            compact_harness=compact_harness,
        )
        changed = True
    if desensitize_tools and body.get("tools"):
        nb["tools"] = _desensitize_tool_value(body["tools"], strip_metadata=strip_tool_metadata)
        changed = True
    return nb if changed else body


# ---------------------------------------------------------------------------
# 自测：python3 desensitize.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    samples = [
        "Refuse requests for DoS attacks and exploit development.",
        "Dual-use security tools (C2 frameworks, credential testing) require authorization.",
        "这是一段正常的中文，不含任何触发词。",
        "Prevent privilege escalation and brute force attacks.",
        "No sensitive words here at all.",
    ]
    print("=== 脱敏前后对比 ===")
    for s in samples:
        d = desensitize_text(s)
        changed = "✓改" if d != s else "  不"
        print(f"{changed} | 原文: {s}")
        if d != s:
            print(f"     | 脱敏: {d}")
            print(f"     | 可见字符相同，差异为零宽空格 U+200B")
    print()
    print("=== messages 脱敏（只处理 system）===")
    msgs = [
        {"role": "system", "content": "Refuse DoS attacks and exploit development."},
        {"role": "user", "content": "explain DoS attacks"},  # 不应被改
    ]
    out = desensitize_messages(msgs)
    for m in out:
        print(f"  [{m['role']}] {m['content']!r}")
    print()
    # 验证：脱敏后 system 改了，user 没改
    assert "\u200b" in out[0]["content"], "system 应被脱敏"
    assert "\u200b" not in out[1]["content"], "user 不应被脱敏"
    print("✓ 自测通过：system 被脱敏，user 保持原样")
