"""Harness adapters — argv construction and stream-json event normalisation.

The harness *is* the agent. Nothing here implements a turn loop; it builds the
command line, then translates each harness's own stream format into one small
event vocabulary the surfaces can render.

Normalised event kinds:
  session   {'kind': 'session', 'resume_id': str}   -- first event, id to persist
  text      {'kind': 'text', 'text': str}           -- assistant output delta
  tool      {'kind': 'tool', 'name': str}           -- a tool call started
  result    {'kind': 'result', 'text': str}         -- turn finished
  error     {'kind': 'error', 'text': str}
  raw       {'kind': 'raw', 'event': dict}          -- unrecognised, passed through
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator

CLAUDE = "claude"
CODEX = "codex"
SUPPORTED = (CLAUDE, CODEX)


class UnknownHarness(ValueError):
    pass


def build_argv(
    harness: str,
    prompt: str,
    model: str,
    resume_id: str | None = None,
    system_prompt: str = "",
) -> list[str]:
    """The exact command the container entrypoint execs."""
    if harness == CLAUDE:
        argv = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--model",
            model,
        ]
        if resume_id:
            argv += ["--resume", resume_id]
        if system_prompt:
            argv += ["--append-system-prompt", system_prompt]
        return argv

    if harness == CODEX:
        argv = ["codex", "exec", "--json", "--model", model]
        if resume_id:
            argv += ["resume", resume_id]
        if system_prompt:
            argv += ["--config", f"instructions={json.dumps(system_prompt)}"]
        argv += [prompt]
        return argv

    raise UnknownHarness(f"unsupported harness: {harness!r}")


def parse_line(harness: str, line: str) -> dict[str, Any] | None:
    """Translate one stream-json line into a normalised event."""
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return {"kind": "text", "text": line}
    if not isinstance(event, dict):
        return {"kind": "raw", "event": {"value": event}}
    if harness == CLAUDE:
        return _parse_claude(event)
    if harness == CODEX:
        return _parse_codex(event)
    raise UnknownHarness(f"unsupported harness: {harness!r}")


def parse_stream(harness: str, lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    for line in lines:
        event = parse_line(harness, line)
        if event is not None:
            yield event


def _parse_claude(event: dict[str, Any]) -> dict[str, Any]:
    kind = event.get("type")

    if kind == "system" and event.get("subtype") == "init":
        return {"kind": "session", "resume_id": event.get("session_id", "")}

    if kind == "result":
        if event.get("is_error"):
            return {"kind": "error", "text": str(event.get("result", "harness error"))}
        return {"kind": "result", "text": str(event.get("result", ""))}

    if kind == "stream_event":
        delta = (event.get("event") or {}).get("delta") or {}
        if delta.get("type") == "text_delta":
            return {"kind": "text", "text": delta.get("text", "")}
        return {"kind": "raw", "event": event}

    if kind == "assistant":
        parts = (event.get("message") or {}).get("content") or []
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        for part in parts:
            if part.get("type") == "tool_use":
                return {"kind": "tool", "name": part.get("name", "tool")}
        if text:
            return {"kind": "text", "text": text}

    return {"kind": "raw", "event": event}


def _parse_codex(event: dict[str, Any]) -> dict[str, Any]:
    kind = event.get("type") or event.get("msg", {}).get("type")

    if kind in {"session.created", "session_configured"}:
        resume_id = event.get("session_id") or event.get("msg", {}).get("session_id", "")
        return {"kind": "session", "resume_id": resume_id}

    if kind in {"item.completed", "agent_message"}:
        item = event.get("item") or event.get("msg") or {}
        if item.get("item_type") == "command_execution" or kind == "exec_command_begin":
            return {"kind": "tool", "name": item.get("command", "exec")}
        text = item.get("text") or item.get("message") or ""
        if text:
            return {"kind": "text", "text": text}

    if kind in {"agent_message_delta", "item.delta"}:
        return {"kind": "text", "text": event.get("delta", "")}

    if kind in {"turn.completed", "task_complete"}:
        return {"kind": "result", "text": event.get("last_agent_message", "")}

    if kind in {"error", "turn.failed"}:
        return {"kind": "error", "text": str(event.get("message", "harness error"))}

    return {"kind": "raw", "event": event}


def collect_text(events: Iterable[dict[str, Any]]) -> str:
    """Fold a normalised stream down to the assistant's final text."""
    result: str | None = None
    chunks: list[str] = []
    for event in events:
        if event["kind"] == "text":
            chunks.append(event["text"])
        elif event["kind"] == "result":
            result = event["text"]
    if result:
        return result
    return "".join(chunks)
