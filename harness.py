"""Harness adapters — argv construction and stream-json event normalisation.

The harness *is* the agent. Nothing here implements a turn loop; it builds the
command line, then translates each harness's own stream format into one small
event vocabulary the surfaces can render.

Normalised event kinds:
  session   {'kind': 'session', 'resume_id': str}   -- first event, id to persist
  text      {'kind': 'text', 'text': str}           -- assistant output delta
  tool      {'kind': 'tool', 'name': str}           -- a tool call started
  status    {'kind': 'status', 'text': str}         -- liveness (requesting, retrying)
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

# The MCP server name in the sandbox image's mcp.json. Codex has no channel
# equivalent, so channel_config is ignored for that harness.
CHANNEL_SERVER_NAME = "frame"


class UnknownHarness(ValueError):
    pass


def supports_stdin(harness: str) -> bool:
    """Whether the harness can be driven as one long-lived stdin-fed process.

    Claude reads turns off stdin as stream-json and stays up between them, which
    is what a channel needs: a session that is already running when an event
    arrives. `codex exec` is one-shot, so it stays on the per-turn path.
    """
    return harness == CLAUDE


def build_argv(
    harness: str,
    prompt: str | None,
    model: str,
    resume_id: str | None = None,
    system_prompt: str = "",
    channel_config: str | None = None,
) -> list[str]:
    """The exact command the container entrypoint execs.

    A `prompt` of `None` builds the persistent form: the harness takes its turns
    off stdin as stream-json instead of carrying one on the command line, and
    stays up between them.

    `channel_config` is the path to the MCP config declaring the frame channel.
    Passing it registers the channel so the control plane can push events into a
    running session; the flag is `--dangerously-load-development-channels`
    because channels are a research preview and only Anthropic-allowlisted
    plugins register without it.

    Both harnesses spawn with approvals off. A session runs unattended inside a
    container with nobody at a terminal, so a prompt has no one to answer it: it
    would hang the turn until something timed out, and a fleet of sessions would
    come back mostly stopped rather than mostly finished. The container is the
    sandbox boundary — the harness does not need a second one inside it.
    """
    if harness == CLAUDE:
        argv = ["claude", "-p"]
        if prompt is not None:
            argv.append(prompt)
        else:
            argv += ["--input-format", "stream-json"]
        argv += [
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--dangerously-skip-permissions",
            "--model",
            model,
        ]
        if resume_id:
            argv += ["--resume", resume_id]
        if system_prompt:
            argv += ["--append-system-prompt", system_prompt]
        if channel_config:
            argv += [
                "--mcp-config",
                channel_config,
                "--dangerously-load-development-channels",
                f"server:{CHANNEL_SERVER_NAME}",
            ]
        return argv

    if harness == CODEX:
        if prompt is None:
            raise UnknownHarness("codex has no stdin-driven form")
        argv = [
            "codex",
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            model,
        ]
        if resume_id:
            argv += ["resume", resume_id]
        if system_prompt:
            argv += ["--config", f"instructions={json.dumps(system_prompt)}"]
        argv += [prompt]
        return argv

    raise UnknownHarness(f"unsupported harness: {harness!r}")


def encode_turn(harness: str, prompt: str) -> str:
    """One prompt as a line for a stdin-driven harness."""
    if harness != CLAUDE:
        raise UnknownHarness(f"{harness!r} is not stdin-driven")
    message = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
    }
    return json.dumps(message) + "\n"


def encode_interrupt(harness: str, request_id: str) -> str:
    """Cut the in-flight turn short without ending the process.

    Signalling the process would take the session's context down with it, which
    is the whole thing the persistent form exists to keep.
    """
    if harness != CLAUDE:
        raise UnknownHarness(f"{harness!r} is not stdin-driven")
    message = {
        "type": "control_request",
        "request_id": request_id,
        "request": {"subtype": "interrupt"},
    }
    return json.dumps(message) + "\n"


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

    if kind == "system":
        subtype = event.get("subtype")
        if subtype == "init":
            return {"kind": "session", "resume_id": event.get("session_id", "")}
        # A stalled provider retries silently for minutes; surface it as liveness
        # so a frame never looks dead.
        if subtype == "api_retry":
            attempt = event.get("attempt", "?")
            maximum = event.get("max_retries", "?")
            return {"kind": "status", "text": f"retrying provider ({attempt}/{maximum})"}
        if subtype == "status":
            return {"kind": "status", "text": str(event.get("status", ""))}

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
