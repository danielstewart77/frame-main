"""Event fan-out and the inbound channel queue."""

from __future__ import annotations

import asyncio

import pytest

from bus import (
    CHANNEL_QUEUE_MAX,
    ChannelQueue,
    SessionBus,
    SessionStreams,
)


async def drain(subscription, count: int, timeout: float = 2.0) -> list[dict]:
    """The next `count` events, or fail rather than hang the suite."""
    events: list[dict] = []

    async def collect() -> None:
        async for event in subscription:
            events.append(event)
            if len(events) >= count:
                return

    await asyncio.wait_for(collect(), timeout)
    return events


# --- fan-out ---------------------------------------------------------------


async def test_every_subscriber_sees_every_event():
    bus = SessionBus()
    first, second = bus.subscribe(), bus.subscribe()

    bus.publish({"kind": "text", "text": "hello"})

    assert await drain(first, 1) == [{"kind": "text", "text": "hello", "seq": 1}]
    assert await drain(second, 1) == [{"kind": "text", "text": "hello", "seq": 1}]


async def test_every_published_event_gets_a_monotonic_seq():
    bus = SessionBus()
    subscription = bus.subscribe()

    for index in range(3):
        bus.publish({"kind": "text", "text": str(index)})

    assert [event["seq"] for event in await drain(subscription, 3)] == [1, 2, 3]
    assert bus.last_seq == 3


async def test_publish_never_blocks_without_subscribers():
    bus = SessionBus()
    bus.publish({"kind": "text", "text": "into the void"})
    assert bus.subscriber_count == 0


async def test_unsubscribe_stops_delivery():
    bus = SessionBus()
    subscription = bus.subscribe()
    subscription.close()

    bus.publish({"kind": "text", "text": "after"})

    assert bus.subscriber_count == 0


async def test_close_ends_subscriber_iteration():
    bus = SessionBus()
    subscription = bus.subscribe()
    bus.close()

    received = [event async for event in subscription]

    assert received == []


async def test_publish_after_close_is_a_no_op():
    bus = SessionBus()
    bus.close()
    bus.publish({"kind": "text", "text": "too late"})  # must not raise


async def test_slow_subscriber_is_disconnected_rather_than_starved():
    bus = SessionBus()
    subscription = bus.subscribe()
    subscription._queue = asyncio.Queue(maxsize=2)

    for index in range(4):
        bus.publish({"kind": "text", "text": str(index)})

    assert subscription.overflowed
    assert bus.subscriber_count == 0
    # Iteration ends instead of resuming mid-stream: the surface must reconnect.
    received = [event async for event in subscription]
    assert [event["text"] for event in received] == ["1"]


async def test_reconnect_replays_the_tail_after_the_last_seen_seq():
    bus = SessionBus()
    for index in range(4):
        bus.publish({"kind": "text", "text": str(index)})

    subscription = bus.subscribe(since=2)

    assert [event["text"] for event in await drain(subscription, 2)] == ["2", "3"]


async def test_reconnect_past_the_replay_buffer_reports_the_gap():
    bus = SessionBus(replay_max=2)
    for index in range(4):
        bus.publish({"kind": "text", "text": str(index)})

    subscription = bus.subscribe(since=0)
    received = await drain(subscription, 3)

    assert received[0] == {"kind": "gap", "from_seq": 1, "to_seq": 2}
    assert [event["text"] for event in received[1:]] == ["2", "3"]


async def test_reconnect_at_the_head_replays_nothing():
    bus = SessionBus()
    bus.publish({"kind": "text", "text": "0"})

    subscription = bus.subscribe(since=1)
    bus.publish({"kind": "text", "text": "1"})

    assert [event["text"] for event in await drain(subscription, 1)] == ["1"]


async def test_subscription_tracks_the_last_seq_it_yielded():
    bus = SessionBus()
    subscription = bus.subscribe()
    bus.publish({"kind": "text", "text": "0"})
    bus.publish({"kind": "text", "text": "1"})

    await drain(subscription, 2)

    assert subscription.last_seq == 2


# --- channel queue ---------------------------------------------------------


async def test_channel_queue_returns_pending_events():
    queue = ChannelQueue()
    queue.put("ci failed", {"run_id": "7"})

    assert await queue.take(timeout=1) == [{"content": "ci failed", "meta": {"run_id": "7"}}]


async def test_channel_queue_drains_completely():
    queue = ChannelQueue()
    queue.put("one")
    queue.put("two")

    assert len(await queue.take(timeout=1)) == 2
    assert queue.depth == 0


async def test_channel_queue_returns_empty_on_timeout():
    queue = ChannelQueue()
    assert await queue.take(timeout=0.05) == []


async def test_channel_queue_wakes_a_waiting_poll():
    queue = ChannelQueue()
    poll = asyncio.create_task(queue.take(timeout=5))
    await asyncio.sleep(0)
    queue.put("late arrival")

    assert await asyncio.wait_for(poll, 2) == [{"content": "late arrival", "meta": {}}]


async def test_channel_queue_is_bounded():
    queue = ChannelQueue(maxsize=3)
    for index in range(5):
        queue.put(str(index))

    assert queue.depth == 3
    assert queue.dropped == 2
    assert [event["content"] for event in await queue.take(timeout=1)] == ["2", "3", "4"]


def test_channel_queue_default_bound_is_sane():
    assert ChannelQueue()._maxsize == CHANNEL_QUEUE_MAX


# --- registry --------------------------------------------------------------


def test_streams_are_stable_per_session():
    streams = SessionStreams()
    assert streams.bus("a") is streams.bus("a")
    assert streams.channel("a") is streams.channel("a")
    assert streams.bus("a") is not streams.bus("b")


async def test_discard_closes_the_bus_and_drops_the_queue():
    streams = SessionStreams()
    subscription = streams.bus("a").subscribe()
    streams.channel("a").put("pending")

    streams.discard("a")

    assert [event async for event in subscription] == []
    assert streams.channel("a").depth == 0


def test_bus_is_replaced_after_discard():
    streams = SessionStreams()
    original = streams.bus("a")
    streams.discard("a")

    assert streams.bus("a") is not original
    assert streams.bus("a").closed is False
