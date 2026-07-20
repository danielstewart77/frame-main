"""Channel relay — the stdio shim Claude Code spawns inside a session container.

A channel is an MCP server the harness spawns as a subprocess and speaks to over
stdio, so a remotely-hosted server cannot be one. This file exists only to bridge
that constraint: it pulls events the control plane has queued for this session
and emits them as `notifications/claude/channel`, and hands the agent's `reply`
calls straight back over HTTP. Sender allowlisting, surface fan-out and routing
all stay in the control plane, which is the only place that knows about users.

Two layers, so the relay is testable without an MCP peer or a live server:

  Transport     how to reach the control plane (`HttpTransport`, or a fake)
  ChannelRelay  the pump and the reply path — no MCP imports
  main()        the MCP wiring, imported lazily because `mcp` ships in the
                sandbox image rather than in the control plane's own venv
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from typing import Any, Awaitable, Callable, Protocol

CHANNEL_NOTIFICATION = "notifications/claude/channel"
CHANNEL_CAPABILITY = "claude/channel"

# Claude Code turns each meta entry into an attribute on the <channel> tag and
# silently drops keys that aren't identifiers. Drop them here instead, so a
# mis-keyed attribute fails visibly in our tests rather than vanishing at runtime.
META_KEY_RE = re.compile(r"^[A-Za-z0-9_]+$")

# A control-plane blip must not kill the channel: back off and keep pumping.
BACKOFF_INITIAL_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 30.0


def sanitize_meta(meta: dict[str, Any] | None) -> dict[str, str]:
    """Keep only identifier-keyed entries, stringified."""
    if not meta:
        return {}
    return {k: str(v) for k, v in meta.items() if META_KEY_RE.match(str(k))}


class Transport(Protocol):
    """How the shim reaches the control plane."""

    async def poll(self) -> list[dict[str, Any]]:
        """Queued events for this session. Blocks until at least one, or empty on timeout."""
        ...

    async def reply(self, chat_id: str, text: str) -> None: ...


class TransportError(RuntimeError):
    pass


class ChannelRelay:
    """Pumps control-plane events into the session and replies back out."""

    def __init__(
        self,
        transport: Transport,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.transport = transport
        self._sleep = sleep
        self._running = False
        # Surfaced for tests and for the operator: a channel that is failing to
        # reach the control plane is invisible from inside the session otherwise.
        self.consecutive_failures = 0

    async def pump(
        self,
        emit: Callable[[str, dict[str, str]], Awaitable[None]],
        iterations: int | None = None,
    ) -> None:
        """Poll for events and emit each one. Runs until `stop()`.

        `iterations` bounds the loop for tests; production passes None.
        """
        self._running = True
        backoff = BACKOFF_INITIAL_SECONDS
        count = 0
        while self._running and (iterations is None or count < iterations):
            count += 1
            try:
                events = await self.transport.poll()
            except Exception:
                self.consecutive_failures += 1
                await self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
                continue
            self.consecutive_failures = 0
            backoff = BACKOFF_INITIAL_SECONDS
            for event in events:
                content = str(event.get("content", ""))
                if not content:
                    continue
                await emit(content, sanitize_meta(event.get("meta")))

    def stop(self) -> None:
        self._running = False

    async def reply(self, chat_id: str, text: str) -> str:
        """Route an agent reply back to the surface it came from."""
        try:
            await self.transport.reply(chat_id, text)
        except Exception as exc:
            raise TransportError(f"reply failed: {exc}") from exc
        return "sent"


class HttpTransport:
    """Long-polls the control plane over the container's host gateway."""

    def __init__(
        self,
        base_url: str,
        session_id: str,
        token: str = "",
        client: Any | None = None,
        poll_timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id
        self.token = token
        self.poll_timeout = poll_timeout
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self.poll_timeout + 10)
        return self._client

    async def poll(self) -> list[dict[str, Any]]:
        client = await self._http()
        response = await client.get(
            f"{self.base_url}/sessions/{self.session_id}/channel/events",
            params={"timeout": self.poll_timeout},
            headers=self._headers(),
        )
        response.raise_for_status()
        payload = response.json()
        events = payload.get("events", []) if isinstance(payload, dict) else payload
        return list(events or [])

    async def reply(self, chat_id: str, text: str) -> None:
        client = await self._http()
        response = await client.post(
            f"{self.base_url}/sessions/{self.session_id}/channel/reply",
            json={"chat_id": chat_id, "text": text},
            headers=self._headers(),
        )
        response.raise_for_status()


INSTRUCTIONS = (
    'Messages arrive as <channel source="frame" chat_id="...">. They come from a '
    "surface outside this terminal — a user on another device, or an automated "
    "event. Reply with the frame reply tool, passing the chat_id from the tag."
)

REPLY_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "chat_id": {"type": "string", "description": "The conversation to reply in"},
        "text": {"type": "string", "description": "The message to send"},
    },
    "required": ["chat_id", "text"],
}


def channel_payload(content: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """The wire form of one channel event, meta sanitised.

    Kept pure and separate from the MCP models so the shape is asserted in tests
    rather than discovered when a container fails to deliver an event.
    """
    return {
        "method": CHANNEL_NOTIFICATION,
        "params": {"content": content, "meta": sanitize_meta(meta)},
    }


async def main() -> None:  # pragma: no cover - exercised by the stdio smoke test
    """Wire ChannelRelay to stdio MCP. Runs only inside a session container."""
    import mcp.types as types
    from mcp.server.lowlevel import NotificationOptions, Server
    from mcp.server.stdio import stdio_server
    from mcp.shared.message import SessionMessage

    base_url = os.environ["FRAME_CHANNEL_URL"]
    session_id = os.environ["FRAME_SESSION_ID"]
    token = os.getenv("FRAME_CHANNEL_TOKEN", "")

    relay = ChannelRelay(HttpTransport(base_url, session_id, token))
    server: Server = Server("frame", instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="reply",
                description="Send a message back over this channel",
                inputSchema=REPLY_TOOL_SCHEMA,
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        if name != "reply":
            raise ValueError(f"unknown tool: {name}")
        result = await relay.reply(str(arguments["chat_id"]), str(arguments["text"]))
        return [types.TextContent(type="text", text=result)]

    options = server.create_initialization_options(
        notification_options=NotificationOptions(),
        experimental_capabilities={CHANNEL_CAPABILITY: {}},
    )

    async with stdio_server() as (read_stream, write_stream):
        # Written straight to the transport rather than through the session:
        # `notifications/claude/channel` is a Claude Code extension the SDK's
        # typed ServerNotification union doesn't carry, and the pump runs
        # outside any request so there is no request context to send from.
        async def emit(content: str, meta: dict[str, str]) -> None:
            payload = channel_payload(content, meta)
            notification = types.JSONRPCNotification(
                jsonrpc="2.0", method=payload["method"], params=payload["params"]
            )
            await write_stream.send(SessionMessage(types.JSONRPCMessage(notification)))

        # The pump starts alongside the server: Claude Code initialises within
        # milliseconds of spawning us and the first poll is a long-poll round
        # trip, so there is no practical window for an event to precede it.
        pump = asyncio.create_task(relay.pump(emit))
        pump.add_done_callback(_report_pump_exit)
        try:
            await server.run(read_stream, write_stream, options)
        finally:
            relay.stop()
            pump.cancel()


def _report_pump_exit(task: "asyncio.Task[None]") -> None:  # pragma: no cover
    """A pump that dies silently makes the channel look merely quiet."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print(f"frame channel pump died: {exc!r}", file=sys.stderr, flush=True)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
