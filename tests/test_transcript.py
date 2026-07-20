"""What a session said, readable after the process that heard it is gone.

The bus serves whoever is attached while a turn happens. For an unattended run
that is nobody, so these tests are about the copy that outlives it.
"""

import pytest

from transcript import TranscriptWriter


@pytest.fixture
def user_id(manager):
    return manager.resolve_user("telegram", "1001", "Daniel")


@pytest.fixture
def session_id(manager, user_id):
    return manager.create(user_id)["id"]


@pytest.fixture
def writer(registry):
    return TranscriptWriter(registry)


@pytest.mark.asyncio
async def test_a_turn_is_readable_after_the_bus_is_gone(manager, user_id):
    """The whole point: the record survives losing the bus."""
    session = await manager.ensure_running(manager.create(user_id)["id"])
    async for _ in manager.turn(session["id"], "hello"):
        pass
    manager.streams.discard(session["id"])  # the bus and its replay buffer, gone

    events = manager.registry.session_events(session["id"])
    assert events, "a completed turn left no transcript"
    assert manager.registry.get_session(session["id"])["outcome"] == "ok"


def test_contiguous_text_is_one_row_not_one_row_per_token(writer, registry, session_id):
    for seq, chunk in enumerate(["Hel", "lo ", "there"], start=1):
        writer.write(session_id, {"kind": "text", "text": chunk, "seq": seq})
    writer.write(session_id, {"kind": "result", "text": "done", "seq": 4})

    rows = registry.session_events(session_id)
    assert [(row["kind"], row["text"]) for row in rows] == [
        ("text", "Hello there"),
        ("result", "done"),
    ]
    # The run is stamped with its first seq, so transcript order matches bus order.
    assert rows[0]["seq"] == 1


def test_a_text_run_interrupted_by_a_tool_call_keeps_its_order(writer, registry, session_id):
    writer.write(session_id, {"kind": "text", "text": "let me look", "seq": 1})
    writer.write(session_id, {"kind": "tool", "name": "Read", "seq": 2})
    writer.write(session_id, {"kind": "text", "text": "found it", "seq": 3})
    writer.flush(session_id)

    rows = registry.session_events(session_id)
    assert [row["kind"] for row in rows] == ["text", "tool", "text"]
    assert [row["seq"] for row in rows] == [1, 2, 3]
    assert rows[1]["data"]["name"] == "Read"


def test_an_unflushed_text_run_is_not_lost_on_flush(writer, registry, session_id):
    writer.write(session_id, {"kind": "text", "text": "half a thought", "seq": 1})
    assert registry.session_events(session_id) == []
    writer.flush(session_id)
    assert registry.session_events(session_id)[0]["text"] == "half a thought"


def test_an_error_marks_the_session_so_a_list_can_show_it(writer, registry, session_id):
    writer.write(session_id, {"kind": "error", "text": "harness fell over", "seq": 1})
    assert registry.get_session(session_id)["outcome"] == "error"


def test_unstamped_events_are_refused(writer, registry, session_id):
    """An event that never went through the bus has no place in the ordering."""
    writer.write(session_id, {"kind": "text", "text": "from nowhere"})
    writer.flush(session_id)
    assert registry.session_events(session_id) == []


def test_a_replayed_write_does_not_double_the_transcript(registry, session_id):
    registry.append_event(session_id, 1, "text", "once", {})
    registry.append_event(session_id, 1, "text", "once", {})
    assert len(registry.session_events(session_id)) == 1


def test_events_page_from_a_seq(registry, session_id):
    for seq in range(1, 6):
        registry.append_event(session_id, seq, "text", f"line {seq}", {})
    assert [row["seq"] for row in registry.session_events(session_id, after_seq=3)] == [4, 5]
    assert [row["seq"] for row in registry.session_events(session_id, limit=2)] == [1, 2]


def test_a_sink_that_throws_does_not_cost_a_surface_its_event():
    from bus import SessionBus

    bus = SessionBus()

    def explode(event):
        raise RuntimeError("disk full")

    bus.on_publish = explode
    subscription = bus.subscribe()
    stamped = bus.publish({"kind": "text", "text": "still delivered"})
    assert stamped["seq"] == 1
    subscription.close()
