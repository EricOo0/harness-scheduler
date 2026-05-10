from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal


SemanticKind = Literal["text", "thinking", "tool_use", "tool_result", "final", "metadata", "unknown"]


@dataclass(slots=True)
class SemanticEvent:
    kind: SemanticKind
    text: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_result: Any | None = None
    is_error: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


def normalize_stream_events(provider: str, events: list[dict[str, Any]], *, limit: int = 500) -> list[dict[str, Any]]:
    adapter = _adapter_for(provider)
    builder = SpanBuilder()
    for raw in events[:limit]:
        if not isinstance(raw, dict):
            continue
        unwrapped = _unwrap_event(raw)
        for event in adapter.extract(unwrapped):
            builder.ingest(event)
    return builder.finish()


def _adapter_for(provider: str):
    if provider == "claude_cli":
        return ClaudeCodeEventAdapter()
    if provider == "codex_app_server":
        return CodexEventAdapter()
    return GenericEventAdapter()


class ClaudeCodeEventAdapter:
    def extract(self, raw: dict[str, Any]) -> list[SemanticEvent]:
        events: list[SemanticEvent] = []
        event_type = str(raw.get("type") or "").lower()
        delta = raw.get("delta") if isinstance(raw.get("delta"), dict) else {}
        if event_type == "content_block_delta" and str(delta.get("type") or "").lower() == "text_delta":
            return [SemanticEvent(kind="text", text=str(delta.get("text") or ""), raw=raw)]
        if event_type == "content_block_delta" and str(delta.get("type") or "").lower() == "thinking_delta":
            return [SemanticEvent(kind="thinking", text=str(delta.get("thinking") or ""), raw=raw)]
        if event_type in {"message_delta", "text_delta"}:
            text = raw.get("text") or raw.get("delta")
            return [SemanticEvent(kind="text", text=str(text or ""), raw=raw)] if text else []
        for block in _content_blocks(raw):
            events.extend(_semantic_from_content_block(block, raw))
        if events:
            return events
        if event_type == "result":
            text = raw.get("result") or raw.get("summary") or raw.get("message")
            return [SemanticEvent(kind="final", text=str(text or ""), raw=raw)]
        if event_type in {"message_start", "content_block_start", "content_block_stop", "message_stop"}:
            return [SemanticEvent(kind="metadata", raw=raw)]
        return GenericEventAdapter().extract(raw)


class CodexEventAdapter:
    def extract(self, raw: dict[str, Any]) -> list[SemanticEvent]:
        method = str(raw.get("method") or raw.get("type") or "").lower()
        params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
        text = params.get("delta") or params.get("text") or params.get("message")
        if any(token in method for token in ("delta", "message")) and isinstance(text, str):
            return [SemanticEvent(kind="text", text=text, raw=raw)]
        tool_name = params.get("name") or params.get("toolName") or params.get("tool_name")
        if "tool" in method and any(token in method for token in ("call", "start", "begin", "use")):
            return [
                SemanticEvent(
                    kind="tool_use",
                    tool_call_id=_first_string(params, "callId", "toolCallId", "tool_use_id", "id"),
                    tool_name=str(tool_name or "Tool"),
                    tool_input=_first_dict(params, "input", "arguments", "args"),
                    raw=raw,
                )
            ]
        if "tool" in method and any(token in method for token in ("result", "output", "end", "complete")):
            return [
                SemanticEvent(
                    kind="tool_result",
                    tool_call_id=_first_string(params, "callId", "toolCallId", "tool_use_id", "id"),
                    tool_name=str(tool_name or "Tool"),
                    tool_result=params.get("output") if "output" in params else params.get("result"),
                    is_error=bool(params.get("error") or params.get("is_error")),
                    raw=raw,
                )
            ]
        if any(token in method for token in ("complete", "done", "finish")) or str(params.get("type") or "").lower() in {"completed", "done"}:
            return [SemanticEvent(kind="final", text=str(params.get("summary") or params.get("result") or ""), raw=raw)]
        return GenericEventAdapter().extract(raw)


class GenericEventAdapter:
    def extract(self, raw: dict[str, Any]) -> list[SemanticEvent]:
        events: list[SemanticEvent] = []
        for block in _content_blocks(raw):
            events.extend(_semantic_from_content_block(block, raw))
        if events:
            return events
        for key in ("text", "delta", "message"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return [SemanticEvent(kind="text", text=value, raw=raw)]
        token = str(raw.get("type") or raw.get("method") or raw.get("event") or "").lower()
        if token in {"ping", "heartbeat", "message_start", "content_block_start", "content_block_stop", "message_stop"}:
            return [SemanticEvent(kind="metadata", raw=raw)]
        if "result" in token or "complete" in token or "done" in token:
            value = raw.get("result") or raw.get("summary") or ""
            return [SemanticEvent(kind="final", text=str(value), raw=raw)]
        return [SemanticEvent(kind="unknown", raw=raw)]


class SpanBuilder:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.message_parts: list[str] = []
        self.message_raw_events: list[dict[str, Any]] = []
        self.thinking_parts: list[str] = []
        self.thinking_raw_events: list[dict[str, Any]] = []
        self.pending_tools: dict[str, dict[str, Any]] = {}
        self.pending_order: list[str] = []

    def ingest(self, event: SemanticEvent) -> None:
        if event.kind == "text":
            if event.text:
                self.message_parts.append(event.text)
                self.message_raw_events.append(event.raw)
            return
        if event.kind == "thinking":
            if event.text:
                self.thinking_parts.append(event.text)
                self.thinking_raw_events.append(event.raw)
            return
        if event.kind == "tool_use":
            self.flush_message()
            self._start_tool(event)
            return
        if event.kind == "tool_result":
            self.flush_message()
            self._finish_tool(event)
            return
        if event.kind == "final":
            self.flush_message()
            self._flush_pending_tools()
            if event.text:
                self.steps.append(
                    {
                        "step_type": "agent.final",
                        "title": "Agent final",
                        "summary": _compact(event.text, 240),
                        "detail": {"text": event.text, "raw_event_count": 1},
                        "raw_event": {"events": [event.raw]},
                    }
                )
            return
        if event.kind in {"metadata", "unknown"}:
            return

    def finish(self) -> list[dict[str, Any]]:
        self.flush_message()
        self._flush_pending_tools()
        return self.steps

    def flush_message(self) -> None:
        text = "".join(self.message_parts).strip()
        thinking = "".join(self.thinking_parts).strip()
        if not text and not thinking:
            self.message_parts = []
            self.message_raw_events = []
            self.thinking_parts = []
            self.thinking_raw_events = []
            return
        summary_text = text or thinking
        self.steps.append(
            {
                "step_type": "agent.message",
                "title": "Agent message",
                "summary": _compact(summary_text, 240),
                "detail": {
                    "text": text,
                    "thinking": thinking,
                    "raw_event_count": len(self.message_raw_events) + len(self.thinking_raw_events),
                    "thinking_event_count": len(self.thinking_raw_events),
                },
                "raw_event": {"events": [*self.thinking_raw_events, *self.message_raw_events]},
            }
        )
        self.message_parts = []
        self.message_raw_events = []
        self.thinking_parts = []
        self.thinking_raw_events = []

    def _start_tool(self, event: SemanticEvent) -> None:
        call_id = event.tool_call_id or f"tool_{len(self.pending_order) + len(self.steps) + 1}"
        self.pending_tools[call_id] = {
            "tool_call_id": call_id,
            "tool_name": event.tool_name or "Tool",
            "input": event.tool_input or {},
            "result": None,
            "status": "pending_result",
            "is_error": False,
            "raw_events": [event.raw],
        }
        self.pending_order.append(call_id)

    def _finish_tool(self, event: SemanticEvent) -> None:
        call_id = event.tool_call_id or self._last_pending_tool_id()
        if not call_id or call_id not in self.pending_tools:
            call_id = event.tool_call_id or f"orphan_tool_{len(self.pending_order) + len(self.steps) + 1}"
            self.pending_tools[call_id] = {
                "tool_call_id": call_id,
                "tool_name": event.tool_name or "Tool",
                "input": {},
                "raw_events": [],
            }
            self.pending_order.append(call_id)
        span = self.pending_tools[call_id]
        if event.tool_name and span.get("tool_name") == "Tool":
            span["tool_name"] = event.tool_name
        span["result"] = event.tool_result
        span["status"] = "failed" if event.is_error else "completed"
        span["is_error"] = event.is_error
        span.setdefault("raw_events", []).append(event.raw)
        self._emit_tool(call_id)

    def _last_pending_tool_id(self) -> str | None:
        for call_id in reversed(self.pending_order):
            if call_id in self.pending_tools:
                return call_id
        return None

    def _flush_pending_tools(self) -> None:
        for call_id in list(self.pending_order):
            if call_id in self.pending_tools:
                self._emit_tool(call_id)

    def _emit_tool(self, call_id: str) -> None:
        span = self.pending_tools.pop(call_id)
        if call_id in self.pending_order:
            self.pending_order.remove(call_id)
        status = str(span.get("status") or "pending_result")
        tool_name = str(span.get("tool_name") or "Tool")
        tool_input = span.get("input") if isinstance(span.get("input"), dict) else {}
        result = span.get("result")
        summary = _tool_summary(tool_name, tool_input, result, status)
        self.steps.append(
            {
                "step_type": "tool.span",
                "title": tool_name,
                "status": status,
                "summary": summary,
                "detail": {
                    "tool_name": tool_name,
                    "tool_call_id": span.get("tool_call_id"),
                    "input": tool_input,
                    "result": result,
                    "status": status,
                    "is_error": bool(span.get("is_error")),
                    "raw_event_count": len(span.get("raw_events") or []),
                },
                "raw_event": {"events": span.get("raw_events") or []},
            }
        )


def _semantic_from_content_block(block: dict[str, Any], raw: dict[str, Any]) -> list[SemanticEvent]:
    block_type = str(block.get("type") or "").lower()
    if block_type in {"text", "text_delta"}:
        return [SemanticEvent(kind="text", text=str(block.get("text") or ""), raw=raw)]
    if block_type == "thinking":
        return [SemanticEvent(kind="thinking", text=str(block.get("thinking") or block.get("text") or ""), raw=raw)]
    if block_type == "tool_use":
        return [
            SemanticEvent(
                kind="tool_use",
                tool_call_id=_first_string(block, "id", "tool_use_id", "call_id", "callId"),
                tool_name=str(block.get("name") or block.get("tool_name") or "Tool"),
                tool_input=block.get("input") if isinstance(block.get("input"), dict) else {},
                raw=raw,
            )
        ]
    if block_type == "tool_result":
        return [
            SemanticEvent(
                kind="tool_result",
                tool_call_id=_first_string(block, "tool_use_id", "id", "call_id", "callId"),
                tool_name=str(block.get("name") or block.get("tool_name") or "Tool"),
                tool_result=block.get("content") if "content" in block else block.get("result"),
                is_error=bool(block.get("is_error") or block.get("error")),
                raw=raw,
            )
        ]
    return []


def _content_blocks(raw: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for candidate in (raw.get("content"), raw.get("message"), raw.get("params")):
        if isinstance(candidate, dict):
            content = candidate.get("content")
            if isinstance(content, list):
                blocks.extend(block for block in content if isinstance(block, dict))
        elif isinstance(candidate, list):
            blocks.extend(block for block in candidate if isinstance(block, dict))
    return blocks


def _unwrap_event(raw: dict[str, Any]) -> dict[str, Any]:
    current = raw
    seen = 0
    while seen < 5 and isinstance(current, dict):
        nested = None
        if current.get("type") == "stream_event" and isinstance(current.get("event"), dict):
            nested = current["event"]
        elif isinstance(current.get("event"), dict) and _looks_like_envelope(current):
            nested = current["event"]
        if not isinstance(nested, dict):
            break
        current = nested
        seen += 1
    return current


def _looks_like_envelope(value: dict[str, Any]) -> bool:
    keys = set(value)
    return bool(keys & {"session_id", "parent_tool_use_id", "uuid"}) or keys <= {"event", "type"}


def _first_string(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _first_dict(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _tool_summary(tool_name: str, tool_input: dict[str, Any], result: Any, status: str) -> str:
    input_bits = " ".join(f"{key}={value}" for key, value in list(tool_input.items())[:4])
    result_text = _result_text(result)
    parts = [tool_name]
    if input_bits:
        parts.append(input_bits)
    parts.append(status)
    if result_text:
        parts.append(_compact(result_text, 120))
    return _compact(" · ".join(parts), 240)


def _result_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return " ".join(str(item.get("text") if isinstance(item, dict) else item) for item in result).strip()
    return json.dumps(result, ensure_ascii=False)


def _compact(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "..."
