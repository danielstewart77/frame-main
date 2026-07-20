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

A subscriber that falls too far behind is dropped rather than quietly starved:
every published event carries a monotonic `seq`, the bus keeps a replay buffer of
the recent tail, and a reconnecting surface passes the last `seq` it rendered to
get the gap backfilled. Silently discarding events would leave a frame showing a
conversation with a hole in it and no way to know; disconnecting turns that into
a reconnect that comes back whole.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, AsyncIterator, Callable

# One slow subscriber shouldn't stall the others or grow without bound; past this
# it is disconnected and expected to reconnect with the `seq` it got to.
SUBSCRIBER_QUEUE_MAX = 512
# How much of a session's tail is retained for reconnect backfill. Larger than a
# subscriber queue so a surface that overflows can always be made whole again.
REPLAY_BUFFER_MAX = 2048
# Undelivered inbound events for a session whose container is gone or wedged.
CHANNEL_QUEUE_MAX = 256


class Subscription:
    """One reader's view of a session's event stream."""

    def __init__(self, bus: "SessionBus", maxsize: int = SUBSCRIBER_QUEUE_MAX) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=maxsize)
        self.overflowed = False
        self.last_seq = 0

    def _put(self, event: dict[str, Any] | None) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._overflow()

    def _overflow(self) -> None:
        """This reader is too far behind to be made whole in place — end it."""
        if self.overflowed:
            return
        self.overflowed = True
        # Evict one event to make room for the terminator. What's lost here is
        # replayed on reconnect from `last_seq`, which is the point.
        try:
            self._queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - queue was just full
            pass
        self._queue.put_nowait(None)
        self._bus.unsubscribe(self)

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
            if event is None:  # the bus closed, or this reader overflowed
                return
            seq = event.get("seq")
            if isinstance(seq, int):
                self.last_seq = seq
            yield event


class SessionBus:
    """Fan-out of one session's events to every attached surface."""

    def __init__(self, replay_max: int = REPLAY_BUFFER_MAX) -> None:
        self._subscribers: list[Subscription] = []
        self._history: deque[dict[str, Any]] = deque(maxlen=replay_max)
        self._seq = 0
        self.closed = False
        # Called with every stamped event, for anyone who needs the stream to
        # outlive this process. Set by `SessionStreams`; None keeps the bus
        # standalone and testable.
        self.on_publish: Callable[[dict[str, Any]], None] | None = None

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def last_seq(self) -> int:
        return self._seq

    def subscribe(self, since: int | None = None) -> Subscription:
        """Attach a reader, optionally backfilling everything after `since`.

        A reconnecting surface passes the last `seq` it rendered. If that point
        has already aged out of the replay buffer the backfill opens with a
        `gap` event naming the range, so the surface can refetch rather than
        render a hole it doesn't know about.
        """
        subscription = Subscription(self)
        if since is not None:
            subscription.last_seq = since
            for event in self._replay_from(since):
                subscription._put(event)
        self._subscribers.append(subscription)
        return subscription

    def _replay_from(self, since: int) -> list[dict[str, Any]]:
        missed = [event for event in self._history if event["seq"] > since]
        if not self._history:
            return missed
        earliest = self._history[0]["seq"]
        if since < earliest - 1:
            gap = {"kind": "gap", "from_seq": since + 1, "to_seq": earliest - 1}
            return [gap, *missed]
        return missed

    def unsubscribe(self, subscription: Subscription) -> None:
        if subscription in self._subscribers:
            self._subscribers.remove(subscription)

    def publish(self, event: dict[str, Any]) -> dict[str, Any]:
        """Hand one event to every current subscriber. Never blocks."""
        if self.closed:
            return event
        self._seq += 1
        stamped = {**event, "seq": self._seq}
        self._history.append(stamped)
        if self.on_publish is not None:
            # A sink that fails must not cost a live surface its event.
            try:
                self.on_publish(stamped)
            except Exception:  # pragma: no cover - defensive
                logging.getLogger(__name__).exception("event sink failed")
        for subscription in list(self._subscribers):
            subscription._put(stamped)
        return stamped

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
        # Set by the session manager to persist what goes past. Wired onto each
        # bus as it is created, so it catches events published straight to a bus
        # as well as those going through `publish` here.
        self.on_publish: Callable[[str, dict[str, Any]], None] | None = None

    def bus(self, session_id: str) -> SessionBus:
        bus = self._buses.get(session_id)
        if bus is None or bus.closed:
            bus = SessionBus()
            if self.on_publish is not None:
                sink = self.on_publish
                bus.on_publish = lambda event: sink(session_id, event)
            self._buses[session_id] = bus
        return bus

    def channel(self, session_id: str) -> ChannelQueue:
        queue = self._channels.get(session_id)
        if queue is None:
            queue = ChannelQueue()
            self._channels[session_id] = queue
        return queue

    def publish(self, session_id: str, event: dict[str, Any]) -> dict[str, Any]:
        return self.bus(session_id).publish(event)

    def discard(self, session_id: str) -> None:
        """Drop a session's streams — its container is gone for good."""
        bus = self._buses.pop(session_id, None)
        if bus is not None:
            bus.close()
        self._channels.pop(session_id, None)
