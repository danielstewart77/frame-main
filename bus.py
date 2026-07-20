"""Per-session event fan-out and the inbound channel queue.

A turn used to be request-scoped: a surface POSTed a prompt and read that turn's
events back off its own response. Channels break that assumption — an event
pushed in by a webhook or a finished background job opens a turn with no
requester to stream to. So a session's normalised events go to a `SessionBus`
that any number of subscribers read, and surfaces subscribe rather than drive.

`ChannelQueue` is the other direction: events the control plane has accepted for
a session, waiting for the container's stdio shim to long-poll them out.

Both are bounded. A surface that stops reading, or a container that dies mid-poll,
must not grow the control plane's memory without limit.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

# One slow subscriber shouldn't stall the others or grow without bound; past this
# the oldest events are dropped and the subscriber is told what it missed.
SUBSCRIBER_QUEUE_MAX = 512
# Undelivered inbound events for a session whose container is gone or wedged.
CHANNEL_QUEUE_MAX = 256


class Subscription:
    """One reader's view of a session's event stream."""

    def __init__(self, bus: "SessionBus", maxsize: int = SUBSCRIBER_QUEUE_MAX) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def _put(self, event: dict[str, Any] | None) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest rather than the newest: on a live stream the tail
            # is what the surface actually needs to render.
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - queue was just full
                pass
            self.dropped += 1
            self._queue.put_nowait(event)

    async def __aenter__(self) -> "Subscription":
        return self

    async def __aexit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._bus.unsubscribe(self)

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            event = await self._queue.get()
            if event is None:  # the bus closed
                return
            yield event


class SessionBus:
    """Fan-out of one session's events to every attached surface."""

    def __init__(self) -> None:
        self._subscribers: list[Subscription] = []
        self.closed = False

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> Subscription:
        subscription = Subscription(self)
        self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        if subscription in self._subscribers:
            self._subscribers.remove(subscription)

    def publish(self, event: dict[str, Any]) -> None:
        """Hand one event to every current subscriber. Never blocks."""
        if self.closed:
            return
        for subscription in list(self._subscribers):
            subscription._put(event)

    def close(self) -> None:
        self.closed = True
        for subscription in list(self._subscribers):
            subscription._put(None)
        self._subscribers.clear()


class ChannelQueue:
    """Inbound events for one session, drained by the container's shim."""

    def __init__(self, maxsize: int = CHANNEL_QUEUE_MAX) -> None:
        self._pending: list[dict[str, Any]] = []
        self._arrived = asyncio.Event()
        self._maxsize = maxsize
        self.dropped = 0

    @property
    def depth(self) -> int:
        return len(self._pending)

    def put(self, content: str, meta: dict[str, Any] | None = None) -> None:
        if len(self._pending) >= self._maxsize:
            self._pending.pop(0)
            self.dropped += 1
        self._pending.append({"content": content, "meta": dict(meta or {})})
        self._arrived.set()

    async def take(self, timeout: float) -> list[dict[str, Any]]:
        """Every pending event, waiting up to `timeout` for the first.

        Returns empty on timeout so the shim's long poll completes and reissues
        rather than holding a connection open indefinitely.
        """
        if not self._pending:
            try:
                await asyncio.wait_for(self._arrived.wait(), timeout)
            except asyncio.TimeoutError:
                return []
        batch = list(self._pending)
        self._pending.clear()
        self._arrived.clear()
        return batch


class SessionStreams:
    """The buses and channel queues, keyed by session id."""

    def __init__(self) -> None:
        self._buses: dict[str, SessionBus] = {}
        self._channels: dict[str, ChannelQueue] = {}

    def bus(self, session_id: str) -> SessionBus:
        bus = self._buses.get(session_id)
        if bus is None or bus.closed:
            bus = SessionBus()
            self._buses[session_id] = bus
        return bus

    def channel(self, session_id: str) -> ChannelQueue:
        queue = self._channels.get(session_id)
        if queue is None:
            queue = ChannelQueue()
            self._channels[session_id] = queue
        return queue

    def publish(self, session_id: str, event: dict[str, Any]) -> None:
        self.bus(session_id).publish(event)

    def discard(self, session_id: str) -> None:
        """Drop a session's streams — its container is gone for good."""
        bus = self._buses.pop(session_id, None)
        if bus is not None:
            bus.close()
        self._channels.pop(session_id, None)
