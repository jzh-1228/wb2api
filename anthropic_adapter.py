"""
anthropic_adapter.py — Anthropic Messages API ↔ OpenAI Chat Completions API 适配层。

Claude Code / CC Switch 使用 Anthropic Messages API（POST /v1/messages），
而 CodeBuddy 后端只支持 OpenAI Chat Completions 协议。本模块做双向转换：
  请求：Anthropic Messages 格式 → OpenAI Chat 格式
  响应：OpenAI Chat SSE → Anthropic Messages SSE 事件流

Anthropic Messages API 参考：https://docs.anthropic.com/en/docs/messages
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ID 生成
# ---------------------------------------------------------------------------

def _rand_id(prefix: str = "") -> str:
    return prefix + os.urandom(12).hex()


# ---------------------------------------------------------------------------
# 请求转换：Anthropic → Chat
# ---------------------------------------------------------------------------

def anthropic_request_to_chat(body: dict) -> dict:
    """将 Anthropic Messages API 请求体转换为 OpenAI Chat Completions 请求体。

    关键映射：
      system → messages[0] role=system
      messages[].content (blocks) → content (string) / tool_calls / tool role
      tools[].input_schema → tools[].function.parameters
      thinking → reasoning_effort（见下）
      metadata → 丢弃
    """
    messages: list[dict] = []

    # system → 首条 system 消息
    system = body.get("system")
    if system:
        sys_content = _extract_system_text(system)
        if sys_content:
            messages.append({"role": "system", "content": sys_content})

    # messages → 消息转换
    for m in body.get("messages", []):
        if not isinstance(m, dict):
            continue
        messages.extend(_convert_anthropic_message(m))

    chat: dict[str, Any] = {"messages": messages, "stream": True}

    # model（透传，不做映射）
    if "model" in body:
        chat["model"] = body["model"]

    # max_tokens
    if "max_tokens" in body:
        chat["max_tokens"] = body["max_tokens"]

    # tools
    tools = body.get("tools")
    if tools:
        chat["tools"] = _convert_anthropic_tools(tools)

    if "tool_choice" in body:
        tc = body["tool_choice"]
        if isinstance(tc, dict):
            choice_type = tc.get("type", "any")
            if choice_type in ("auto", "any", "none"):
                chat["tool_choice"] = "required" if choice_type == "any" else choice_type
            elif choice_type == "tool":
                chat["tool_choice"] = {"type": "function", "function": {"name": tc.get("name", "")}}
            else:
                chat["tool_choice"] = dict(tc)
        elif isinstance(tc, str):
            chat["tool_choice"] = tc if tc in ("none", "auto", "required") else {"type": "function", "function": {"name": tc}}

    # 透传常见参数
    for key in ("temperature", "top_p", "stop", "top_k"):
        if key in body:
            chat[key] = body[key]

    # thinking → reasoning_effort
    # Anthropic 用 thinking:{type:"enabled", budget_tokens:N} 表达思考；
    # 上游只认扁平的 reasoning_effort，且**不识别** budget_tokens（实测传
    # thinking/budget_tokens 都被静默忽略，reasoning_content 为空）。
    # 因此这里把「要不要思考」翻译成 reasoning_effort：
    #   - thinking.type == "disabled" → 显式关闭
    #   - thinking.type == "enabled"  → 有档位则用档位，否则给默认 high
    # （实测 high 是 deepseek 系上游的默认档，且必然开启思维链）
    effort_hint = ""
    for value in (body.get("output_config"), body.get("reasoning_effort"),
                  body.get("reasoningEffort"), body.get("reasoning")):
        value = value.get("effort") if isinstance(value, dict) else value
        if isinstance(value, str) and value.strip():
            effort_hint = value.strip()
            break
    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        ttype = str(thinking.get("type") or "").strip().lower()
        if ttype == "disabled":
            chat["thinking"] = {"type": "disabled"}
        elif ttype in ("enabled", "adaptive"):
            effort = thinking.get("effort")
            if not isinstance(effort, str) or not effort.strip():
                effort = effort_hint
            if not (isinstance(effort, str) and effort.strip()):
                effort = "high"
            chat.setdefault("reasoning_effort", effort.strip())
    elif isinstance(thinking, str) and thinking.strip().lower() in ("enabled", "adaptive"):
        chat.setdefault("reasoning_effort", effort_hint or "high")
    elif isinstance(thinking, str) and thinking.strip().lower() == "disabled":
        chat["thinking"] = {"type": "disabled"}
    elif effort_hint:
        chat["reasoning_effort"] = effort_hint

    from upstream_compat import THINKING_OFF_SPELLINGS
    if str(chat.get("reasoning_effort") or "").lower() in THINKING_OFF_SPELLINGS:
        chat.pop("reasoning_effort", None)
        chat["thinking"] = {"type": "disabled"}

    return chat


def _extract_system_text(system) -> str:
    """提取 system 字段为纯文本字符串。支持 string 和 [{type:text, text:...}] 数组。"""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def _image_block_to_chat_part(block: dict) -> dict:
    """Preserve Anthropic base64/URL images; reject invalid sources instead of losing them."""
    source = block.get("source")
    if not isinstance(source, dict):
        raise ValueError("image.source must be an object")
    source_type = source.get("type")
    if source_type == "base64":
        media = source.get("media_type") or "image/png"
        data = source.get("data")
        if not isinstance(media, str) or not media.strip().startswith("image/"):
            raise ValueError("image.source.media_type must be an image MIME type")
        if not isinstance(data, str) or not data.strip():
            raise ValueError("image.source.data must be a nonempty base64 string")
        url = f"data:{media.strip()};base64,{data.strip()}"
    elif source_type == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("image.source.url must be a nonempty string")
        url = url.strip()
    else:
        raise ValueError("image.source.type must be base64 or url")
    return {"type": "image_url", "image_url": {"url": url}}


def _convert_anthropic_message(msg: dict) -> list[dict]:
    """将单个 Anthropic 消息转换为 OpenAI 格式的消息（可能为多条）。"""
    role = msg.get("role", "")
    content = msg.get("content")

    # 简单字符串 content
    if isinstance(content, str):
        return [{"role": role, "content": content}]

    # 空 content
    if not isinstance(content, list) or not content:
        return []

    # content blocks → 需要解析
    blocks = content

    # 检查是否包含 tool_result（role=user 时）
    #
    # OpenAI/上游要求：assistant.tool_calls 之后必须**紧接**对应的 role:tool。
    # 因此 tool_result 必须先输出；user 文本/图片只能排在 tool 结果之后，
    # 绝不能 insert 到最前——否则会切断配对，上游报 11148。
    if role == "user":
        tool_msgs: list[dict] = []
        content_parts: list[dict] = []
        has_images = False
        for block in blocks:
            if not isinstance(block, dict):
                continue
            bt = block.get("type", "")
            if bt == "text":
                content_parts.append({"type": "text", "text": block.get("text", "")})
            elif bt == "image":
                content_parts.append(_image_block_to_chat_part(block))
                has_images = True
            elif bt == "tool_result":
                # tool_result → 独立的 tool 消息（必须紧随 assistant.tool_calls）
                tc_id = block.get("tool_use_id") or ""
                if not tc_id:
                    # 空 id 无法与 tool_use 配对，上送必触发 11148；直接丢弃
                    _logger.debug("drop tool_result with empty tool_use_id")
                    continue
                output = _tool_result_to_text(block.get("content", ""))
                if block.get("is_error"):
                    output = f"Error: {output}" if output else "Error: tool execution failed"
                tool_msgs.append({"role": "tool", "tool_call_id": tc_id, "content": output})
        result = list(tool_msgs)
        if content_parts:
            content = content_parts if has_images else "".join(part["text"] for part in content_parts)
            result.append({"role": "user", "content": content})
        return result

    # assistant 角色
    if role == "assistant":
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            bt = block.get("type", "")
            if bt == "text":
                text_parts.append(block.get("text", ""))
            elif bt == "tool_use":
                # 键缺失或值为 null/"" 都要生成新 id，保证与 tool_result 可配对
                tc = {
                    "id": block.get("id") or _rand_id("call_"),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                }
                tool_calls.append(tc)
        msg_out: dict[str, Any] = {"role": "assistant"}
        if text_parts:
            msg_out["content"] = "".join(text_parts)
        else:
            msg_out["content"] = None
        if tool_calls:
            msg_out["tool_calls"] = tool_calls
        return [msg_out]

    # 其他角色：尝试提取文本
    text = _extract_blocks_text(blocks)
    return [{"role": role, "content": text}] if text else []


def _tool_result_to_text(output) -> str:
    """把 Anthropic tool_result.content 转成纯文本。

    - str → 原样
    - list → 拼接 text 块；非 text 块序列化为短 JSON，避免静默丢成空串
    - 其它/None → 空串
    """
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if not isinstance(output, list):
        return str(output)
    parts: list[str] = []
    for b in output:
        if not isinstance(b, dict):
            parts.append(str(b))
            continue
        if b.get("type") == "text":
            parts.append(b.get("text", "") or "")
        else:
            try:
                parts.append(json.dumps(b, ensure_ascii=False)[:500])
            except Exception:
                parts.append(f"<{b.get('type') or 'block'}>")
    return "".join(parts)


def _extract_blocks_text(blocks: list) -> str:
    """从 content blocks 中提取所有 text 块合并为字符串。"""
    parts = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _convert_anthropic_tools(tools: list) -> list:
    """将 Anthropic 格式的 tools 转为 OpenAI Chat 格式。

    Anthropic:  {"name": "...", "description": "...", "input_schema": {...}}
    Chat:       {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    """
    result = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        # 已经是 Chat 格式
        if "function" in t:
            result.append(t)
            continue
        fn: dict[str, Any] = {"name": t.get("name", "")}
        if "description" in t:
            fn["description"] = t["description"]
        if "input_schema" in t:
            fn["parameters"] = t["input_schema"]
        result.append({"type": "function", "function": fn})
    return result


# ---------------------------------------------------------------------------
# 响应转换：Chat SSE → Anthropic Messages SSE
# ---------------------------------------------------------------------------

class AnthropicStreamConverter:
    """将 OpenAI Chat SSE 流实时转换为 Anthropic Messages SSE 事件流。

    用法：
      converter = AnthropicStreamConverter(model="deepseek-v4-pro")
      for line in backend_sse:
          events = converter.feed_line(line)
          if events:
              yield events.encode()
      yield converter.finish().encode()
    """

    def __init__(self, model: str = "unknown"):
        self.msg_id = _rand_id("msg_")
        self.model = model
        self.created_at = int(time.time())

        # 状态
        self._emitted_start = False

        # thinking 内容块（思维链）
        self._thinking_content = ""
        self._thinking_block_open = False
        self._thinking_block_idx = 0

        # text 内容块
        self._text_content = ""
        self._text_block_open = False
        self._text_block_idx = 0

        # tool_use 内容块（index → {id, name, args, block_idx, open}）
        self._tool_uses: dict[int, dict] = {}
        self._next_block_idx = 0

        # 结束信息
        self._finish_reason: str | None = None
        self._usage: dict | None = None
        self._content_filter: bool = False

    # ---- 公开接口 ----

    def feed_line(self, line: str) -> str:
        """处理一行 SSE（如 'data: {...}'），返回 Anthropic SSE 事件字符串。"""
        line = line.strip()
        if not line or not line.startswith("data:"):
            return ""
        data = line[5:].strip()
        if data == "[DONE]":
            return ""
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return ""
        return self._process_chunk(chunk)

    def finish(self) -> str:
        """流结束，发出收尾事件。"""
        events: list[str] = []

        # 关闭 thinking 块（若正文未开始就结束了，这里收口）
        if self._thinking_block_open:
            events.append(self._evt(
                "content_block_stop", {"index": self._thinking_block_idx}
            ))
            self._thinking_block_open = False

        # 关闭 text 块
        if self._text_block_open:
            events.append(self._evt(
                "content_block_stop", {"index": self._text_block_idx}
            ))
            self._text_block_open = False

        # 关闭 tool_use 块
        for tc in self._tool_uses.values():
            if tc.get("open"):
                events.append(self._evt(
                    "content_block_stop", {"index": tc["block_idx"]}
                ))
                tc["open"] = False

        # stop_reason 映射
        sr = self._finish_reason or "stop"
        stop_map = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
        }
        stop_reason = stop_map.get(sr, "end_turn")

        # message_delta
        delta: dict[str, str | None] = {"stop_reason": stop_reason, "stop_sequence": None}
        usage = None
        if self._usage:
            u = self._usage
            usage = {
                "input_tokens": u.get("prompt_tokens", 0),
                "output_tokens": u.get("completion_tokens", 0),
            }
        events.append(self._evt("message_delta", {"delta": delta, "usage": usage}))

        # message_stop
        events.append(self._evt("message_stop", {}))

        return "".join(events)

    def get_nonstream_response(self) -> dict:
        """获取完整的非流式 Message 响应对象。"""
        content = self._build_content_blocks()
        sr = self._finish_reason or "stop"
        stop_map = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
        }
        stop_reason = stop_map.get(sr, "end_turn")

        resp: dict[str, Any] = {
            "id": self.msg_id,
            "type": "message",
            "role": "assistant",
            "content": content,
            "model": self.model,
            "stop_reason": stop_reason,
            "stop_sequence": None,
        }
        if self._usage:
            resp["usage"] = {
                "input_tokens": self._usage.get("prompt_tokens", 0),
                "output_tokens": self._usage.get("completion_tokens", 0),
            }
        return resp

    # ---- 内部 ----

    def build_message(self) -> dict:
        """把已消费完的流转成一个完整的 Anthropic Message 对象。

        供非流式（stream=false）响应使用：调用方照常把上游 SSE 逐行喂给
        feed_line()，最后再调本方法拿聚合结果。
        """
        usage = self._usage or {}
        # Chat 的 finish_reason → Anthropic 的 stop_reason
        stop_map = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}
        return {
            "id": self.msg_id,
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": self._build_content_blocks(),
            "stop_reason": stop_map.get(self._finish_reason or "stop", "end_turn"),
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        }

    def error_event(self, message: str, err_type: str = "api_error") -> str:
        """构造 Anthropic 风格的 error 事件（上游全部不可用时返回给客户端）。"""
        return self._evt("error", {"error": {"type": err_type, "message": message}})

    def _process_chunk(self, chunk: dict) -> str:
        events: list[str] = []

        if chunk.get("model"):
            self.model = chunk["model"]

        # 首次 → message_start
        if not self._emitted_start:
            events.append(self._evt("message_start", {
                "message": {
                    "id": self.msg_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": self.model,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            }))
            self._emitted_start = True

        if chunk.get("usage"):
            self._usage = chunk["usage"]

        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            finish = choice.get("finish_reason")

            # thinking delta（思维链）
            # 上游把思维链放在 delta.reasoning_content；Anthropic 协议用
            # thinking 内容块承载。此前这里完全没处理，导致 Claude Code 等
            # 客户端拿不到任何思考内容。
            reasoning = delta.get("reasoning_content")
            if reasoning:
                self._thinking_content += reasoning
                if not self._thinking_block_open:
                    self._thinking_block_idx = self._next_block_idx
                    self._next_block_idx += 1
                    events.append(self._evt("content_block_start", {
                        "index": self._thinking_block_idx,
                        "content_block": {"type": "thinking", "thinking": ""},
                    }))
                    self._thinking_block_open = True
                events.append(self._evt("content_block_delta", {
                    "index": self._thinking_block_idx,
                    "delta": {"type": "thinking_delta", "thinking": reasoning},
                }))

            # content delta
            content = delta.get("content")
            if content:
                # 思维链结束后正文开始：先收口 thinking 块，保证块顺序 thinking → text
                if self._thinking_block_open:
                    events.append(self._evt("content_block_stop", {
                        "index": self._thinking_block_idx}))
                    self._thinking_block_open = False
                self._text_content += content
                if not self._text_block_open:
                    self._text_block_idx = self._next_block_idx
                    self._next_block_idx += 1
                    events.append(self._evt("content_block_start", {
                        "index": self._text_block_idx,
                        "content_block": {"type": "text", "text": ""},
                    }))
                    self._text_block_open = True
                events.append(self._evt("content_block_delta", {
                    "index": self._text_block_idx,
                    "delta": {"type": "text_delta", "text": content},
                }))

            # tool_calls delta
            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0)
                if idx not in self._tool_uses:
                    block_idx = self._next_block_idx
                    self._next_block_idx += 1
                    self._tool_uses[idx] = {
                        "id": tc.get("id", ""),
                        "name": "",
                        "args": "",
                        "block_idx": block_idx,
                        "open": False,
                    }
                slot = self._tool_uses[idx]
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    slot["name"] = fn["name"]

                if not slot["open"]:
                    events.append(self._evt("content_block_start", {
                        "index": slot["block_idx"],
                        "content_block": {"type": "tool_use", "id": slot["id"], "name": slot["name"], "input": {}},
                    }))
                    slot["open"] = True

                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
                    events.append(self._evt("content_block_delta", {
                        "index": slot["block_idx"],
                        "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                    }))

            if finish:
                self._finish_reason = finish

                # finish_reason 出现时关闭当前打开的块
                if self._text_block_open:
                    events.append(self._evt("content_block_stop", {
                        "index": self._text_block_idx
                    }))
                    self._text_block_open = False

                for tc in self._tool_uses.values():
                    if tc.get("open"):
                        events.append(self._evt("content_block_stop", {
                            "index": tc["block_idx"]
                        }))
                        tc["open"] = False

        return "".join(events)

    def _evt(self, event_type: str, data: dict) -> str:
        """格式化一个 Anthropic SSE 事件（含 event: 行）。"""
        payload = {"type": event_type, **data}
        return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _build_content_blocks(self) -> list[dict]:
        """构造完整的 content blocks 数组（用于非流式响应）。"""
        blocks: list[dict] = []

        # thinking block（先于 text，与流式块顺序一致）
        if self._thinking_content or self._thinking_block_open:
            blocks.append({"type": "thinking", "thinking": self._thinking_content,
                           "signature": ""})

        # text block
        if self._text_content or self._text_block_open:
            blocks.append({"type": "text", "text": self._text_content})

        # tool_use blocks
        for _, tc in sorted(self._tool_uses.items()):
            block: dict[str, Any] = {
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": {},
            }
            # 尝试将 args 解析为 JSON object
            try:
                block["input"] = json.loads(tc["args"])
            except (json.JSONDecodeError, ValueError):
                block["input"] = tc["args"]
            blocks.append(block)

        return blocks
