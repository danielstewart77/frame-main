"""Channel relay — the stdio shim's pump, reply path and wire format.

No MCP peer and no control plane: `FakeTransport` stands in for both, so these
run on the offline box alongside the rest of the suite.
"""

from __future__ import annotations

from typing import Any

import pytest

from sandbox.channel import (
    CHANNEL_NOTIFICATION,
    BACKOFF_INITIAL_SECONDS,
    ChannelRelay,
    HttpTransport,
    TransportError,
    channel_payload,
    sanitize_meta,
)


class FakeTransport:
    """Serves queued poll batches, records replies, optionally raises."""

    def __init__(self, batches: list[Any] | None = None) -> None:
        self.batches = list(batches or [])
        self.replies: list[tuple[str, str]] = []
        self.polls = 0
        self.reply_error: Exception | None = None

    async def poll(self) -> list[dict[str, Any]]:
        self.polls += 1
        if not self.batches:
            return []
        batch = self.batches.pop(0)
        if isinstance(batch, Exception):
            raise batch
        return batch

    async def reply(self, chat_id: str, text: str) -> None:
        if self.reply_error is not None:
            raise self.reply_error
        self.replies.append((chat_id, text))


def collector() -> tuple[list[tuple[str, dict[str, str]]], Any]:
    emitted: list[tuple[str, dict[str, str]]] = []

    async def emit(content: str, meta: dict[str, str]) -> None:
        emitted.append((content, meta))

    return emitted, emit


# --- meta sanitising -------------------------------------------------------


def test_sanitize_meta_keeps_identifier_keys():
    assert sanitize_meta({"chat_id": "7", "severity": "high"}) == {
        "chat_id": "7",
        "severity": "high",
    }


def test_sanitize_meta_drops_non_identifier_keys():
    # Claude Code drops hyphenated keys silently; do it here where it's visible.
    assert sanitize_meta({"chat-id": "7", "ok": "1", "a.b": "2"}) == {"ok": "1"}


def test_sanitize_meta_stringifies_values():
    assert sanitize_meta({"run_id": 1234}) == {"run_id": "1234"}


def test_sanitize_meta_handles_none():
    assert sanitize_meta(None) == {}


# --- wire format -----------------------------------------------------------


def test_channel_payload_shape():
    # meta is a nested object on params, not flattened into it.
    assert channel_payload("build failed", {"run_id": "1234"}) == {
        "method": CHANNEL_NOTIFICATION,
        "params": {"content": "build failed", "meta": {"run_id": "1234"}},
    }


def test_channel_payload_sanitises_meta():
    payload = channel_payload("x", {"bad-key": "1", "good": "2"})
    assert payload["params"]["meta"] == {"good": "2"}


# --- pump ------------------------------------------------------------------


async def test_pump_emits_each_event():
    transport = FakeTransport([[{"content": "one"}, {"content": "two"}]])
    emitted, emit = collector()

    await ChannelRelay(transport).pump(emit, iterations=1)

    assert [content for content, _ in emitted] == ["one", "two"]


async def test_pump_passes_sanitised_meta():
    transport = FakeTransport([[{"content": "hi", "meta": {"chat_id": 9, "no-go": "x"}}]])
    emitted, emit = collector()

    await ChannelRelay(transport).pump(emit, iterations=1)

    assert emitted == [("hi", {"chat_id": "9"})]


async def test_pump_skips_empty_content():
    # An event with no body would render as an empty <channel> tag; drop it.
    transport = FakeTransport([[{"content": ""}, {"meta": {"a": "b"}}, {"content": "real"}]])
    emitted, emit = collector()

    await ChannelRelay(transport).pump(emit, iterations=1)

    assert [content for content, _ in emitted] == ["real"]


async def test_pump_survives_transport_failure_and_backs_off():
    transport = FakeTransport([RuntimeError("control plane down"), [{"content": "back"}]])
    emitted, emit = collector()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    relay = ChannelRelay(transport, sleep=sleep)
    await relay.pump(emit, iterations=2)

    assert slept == [BACKOFF_INITIAL_SECONDS]
    assert [content for content, _ in emitted] == ["back"]
    assert relay.consecutive_failures == 0


async def test_pump_backoff_grows_then_resets():
    transport = FakeTransport(
        [RuntimeError("down"), RuntimeError("still down"), [{"content": "recovered"}]]
    )
    emitted, emit = collector()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    relay = ChannelRelay(transport, sleep=sleep)
    await relay.pump(emit, iterations=3)

    assert slept == [BACKOFF_INITIAL_SECONDS, BACKOFF_INITIAL_SECONDS * 2]
    assert [content for content, _ in emitted] == ["recovered"]


async def test_pump_counts_consecutive_failures():
    transport = FakeTransport([RuntimeError("a"), RuntimeError("b")])
    _, emit = collector()

    async def sleep(_: float) -> None:
        return None

    relay = ChannelRelay(transport, sleep=sleep)
    await relay.pump(emit, iterations=2)

    assert relay.consecutive_failures == 2


async def test_stop_ends_the_pump():
    transport = FakeTransport([[{"content": "one"}], [{"content": "two"}]])
    emitted: list[str] = []
    relay = ChannelRelay(transport)

    async def emit(content: str, _meta: dict[str, str]) -> None:
        emitted.append(content)
        relay.stop()

    await relay.pump(emit, iterations=10)

    assert emitted == ["one"]


# --- reply -----------------------------------------------------------------


async def test_reply_routes_to_transport():
    transport = FakeTransport()
    result = await ChannelRelay(transport).reply("7", "done")

    assert transport.replies == [("7", "done")]
    assert result == "sent"


async def test_reply_wraps_transport_errors():
    transport = FakeTransport()
    transport.reply_error = RuntimeError("connection refused")

    with pytest.raises(TransportError, match="reply failed"):
        await ChannelRelay(transport).reply("7", "done")


# --- http transport --------------------------------------------------------


class FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class FakeClient:
    def __init__(self, payload: Any = None) -> None:
        self.payload = payload if payload is not None else {"events": []}
        self.gets: list[tuple[str, dict[str, Any]]] = []
        self.posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def get(self, url: str, params=None, headers=None) -> FakeResponse:
        self.gets.append((url, dict(headers or {})))
        return FakeResponse(self.payload)

    async def post(self, url: str, json=None, headers=None) -> FakeResponse:
        self.posts.append((url, json, dict(headers or {})))
        return FakeResponse({})


async def test_http_transport_polls_session_scoped_url():
    client = FakeClient({"events": [{"content": "hello"}]})
    transport = HttpTransport("http://host.docker.internal:8500/", "sess-1", client=client)

    events = await transport.poll()

    assert events == [{"content": "hello"}]
    url, _ = client.gets[0]
    assert url == "http://host.docker.internal:8500/sessions/sess-1/channel/events"


async def test_http_transport_sends_bearer_when_configured():
    client = FakeClient()
    transport = HttpTransport("http://x:8500", "sess-1", token="secret", client=client)

    await transport.poll()

    assert client.gets[0][1]["Authorization"] == "Bearer secret"


async def test_http_transport_omits_bearer_when_unset():
    client = FakeClient()
    transport = HttpTransport("http://x:8500", "sess-1", client=client)

    await transport.poll()

    assert "Authorization" not in client.gets[0][1]


async def test_http_transport_accepts_bare_list_payload():
    client = FakeClient([{"content": "hello"}])
    transport = HttpTransport("http://x:8500", "sess-1", client=client)

    assert await transport.poll() == [{"content": "hello"}]


async def test_http_transport_tolerates_null_events():
    client = FakeClient({"events": None})
    transport = HttpTransport("http://x:8500", "sess-1", client=client)

    assert await transport.poll() == []


async def test_http_transport_posts_reply():
    client = FakeClient()
    transport = HttpTransport("http://x:8500", "sess-1", client=client)

    await transport.reply("7", "done")

    url, body, _ = client.posts[0]
    assert url == "http://x:8500/sessions/sess-1/channel/reply"
    assert body == {"chat_id": "7", "text": "done"}
