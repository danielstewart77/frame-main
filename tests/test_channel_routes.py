"""The inbound wake path, end to end through the manager and the HTTP API.

Covers what the inversion bought: an event delivered with no requester present
still reaches a surface that is merely watching the session.
"""

from __future__ import annotations

import asyncio

import pytest

from sessions import SessionError


@pytest.fixture
def user_id(manager):
    return manager.resolve_user("telegram", "2002", "Daniel")


@pytest.fixture
def session(manager, user_id):
    return manager.create(user_id)


async def drain(subscription, count: int, timeout: float = 2.0) -> list[dict]:
    events: list[dict] = []

    async def collect() -> None:
        async for event in subscription:
            events.append(event)
            if len(events) >= count:
                return

    await asyncio.wait_for(collect(), timeout)
    return events


# --- delivery --------------------------------------------------------------


async def test_deliver_queues_for_the_shim(manager, session):
    manager.deliver(session["id"], "build failed", {"run_id": "7"})

    events = await manager.channel_events(session["id"], timeout=1)

    assert events == [{"content": "build failed", "meta": {"run_id": "7"}}]


async def test_channel_events_times_out_empty(manager, session):
    assert await manager.channel_events(session["id"], timeout=0.05) == []


async def test_deliver_to_an_archived_session_is_refused(manager, session):
    await manager.archive(session["id"])

    with pytest.raises(SessionError, match="archived"):
        manager.deliver(session["id"], "too late")


def test_deliver_to_an_unknown_session_is_refused(manager):
    with pytest.raises(SessionError):
        manager.deliver("no-such-session", "hello")


# --- the inversion ---------------------------------------------------------


async def test_a_watcher_sees_a_turn_it_did_not_start(manager, session):
    subscription = manager.subscribe(session["id"])

    manager.run_turn_in_background(session["id"], "from a webhook")
    events = await drain(subscription, 1)

    # FakeProvisioner emits `session` first on a session with no resume_id.
    assert events[0]["kind"] == "session"


async def test_background_turn_reaches_the_bus_in_full(manager, session):
    subscription = manager.subscribe(session["id"])

    task = manager.run_turn_in_background(session["id"], "hello")
    await asyncio.wait_for(task, 5)
    events = await drain(subscription, 4)

    assert [event["kind"] for event in events] == ["session", "text", "text", "result"]


async def test_a_directly_iterated_turn_also_publishes(manager, session):
    subscription = manager.subscribe(session["id"])

    consumed = [event async for event in manager.turn(session["id"], "hi")]
    watched = await drain(subscription, len(consumed))

    assert watched == consumed


async def test_two_watchers_both_see_the_turn(manager, session):
    first = manager.subscribe(session["id"])
    second = manager.subscribe(session["id"])

    await asyncio.wait_for(manager.run_turn_in_background(session["id"], "hi"), 5)

    assert await drain(first, 4) == await drain(second, 4)


async def test_unsolicited_harness_output_reaches_watchers(manager, session):
    """The persistent harness speaks with no turn outstanding — a wake landing."""
    subscription = manager.subscribe(session["id"])

    manager.provisioner.emit_unsolicited(session["id"], {"kind": "text", "text": "awake"})

    assert (await drain(subscription, 1))[0]["text"] == "awake"


async def test_unsolicited_session_event_persists_the_resume_id(manager, session):
    manager.provisioner.emit_unsolicited(
        session["id"], {"kind": "session", "resume_id": "sess-42"}
    )

    assert manager.get(session["id"])["resume_id"] == "sess-42"


async def test_a_woken_session_counts_as_active(manager, session):
    """Otherwise the reaper stops a container that is doing channel-driven work."""
    before = manager.get(session["id"])["last_active"]

    manager.provisioner.emit_unsolicited(session["id"], {"kind": "result", "text": "done"})

    assert manager.get(session["id"])["last_active"] >= before


async def test_channel_reply_is_published_to_watchers(manager, session):
    subscription = manager.subscribe(session["id"])

    manager.channel_reply(session["id"], "chat-7", "all done")

    assert await drain(subscription, 1) == [
        {"kind": "reply", "chat_id": "chat-7", "text": "all done", "seq": 1}
    ]


def test_subscribing_to_an_unknown_session_is_refused(manager):
    with pytest.raises(SessionError):
        manager.subscribe("no-such-session")


async def test_background_turn_publishes_its_error(manager, session):
    # A turn nobody is holding open has no caller to raise to, so the failure
    # has to reach watchers as an event or it is lost entirely.
    await manager.archive(session["id"])
    subscription = manager.subscribe(session["id"])

    await asyncio.wait_for(manager.run_turn_in_background(session["id"], "hi"), 5)

    events = await drain(subscription, 1)
    assert events[0]["kind"] == "error"
    assert "archived" in events[0]["text"]


# --- http ------------------------------------------------------------------


def test_deliver_route_accepts_an_event(client, user, registry):
    created = client.post(f"/users/{user['user_id']}/sessions", json={}).json()

    response = client.post(
        f"/sessions/{created['id']}/channel/deliver",
        json={"content": "build failed", "meta": {"run_id": "7"}},
    )

    assert response.status_code == 202
    assert response.json() == {"queued": 1}


def test_delivered_event_is_returned_to_the_shim(client, user):
    created = client.post(f"/users/{user['user_id']}/sessions", json={}).json()
    client.post(f"/sessions/{created['id']}/channel/deliver", json={"content": "hello"})

    response = client.get(f"/sessions/{created['id']}/channel/events", params={"timeout": 1})

    assert response.json() == {"events": [{"content": "hello", "meta": {}}]}


def test_channel_events_route_times_out_empty(client, user):
    created = client.post(f"/users/{user['user_id']}/sessions", json={}).json()

    response = client.get(f"/sessions/{created['id']}/channel/events", params={"timeout": 0.05})

    assert response.json() == {"events": []}


def test_deliver_requires_content(client, user):
    created = client.post(f"/users/{user['user_id']}/sessions", json={}).json()

    response = client.post(f"/sessions/{created['id']}/channel/deliver", json={"content": ""})

    assert response.status_code == 422


def test_deliver_to_unknown_session_is_404(client):
    response = client.post("/sessions/nope/channel/deliver", json={"content": "hi"})
    assert response.status_code == 404


def test_deliver_to_archived_session_is_409(client, user):
    created = client.post(f"/users/{user['user_id']}/sessions", json={}).json()
    client.post(f"/sessions/{created['id']}/archive")

    response = client.post(f"/sessions/{created['id']}/channel/deliver", json={"content": "hi"})

    assert response.status_code == 409


def test_reply_route_returns_the_event(client, user):
    created = client.post(f"/users/{user['user_id']}/sessions", json={}).json()

    response = client.post(
        f"/sessions/{created['id']}/channel/reply",
        json={"chat_id": "chat-7", "text": "all done"},
    )

    assert response.json() == {"kind": "reply", "chat_id": "chat-7", "text": "all done"}


def test_reply_to_unknown_session_is_404(client):
    response = client.post("/sessions/nope/channel/reply", json={"chat_id": "1", "text": "x"})
    assert response.status_code == 404


def test_stream_socket_delivers_a_channel_opened_turn(client, user):
    """The whole point: no prompt on the socket, events arrive anyway."""
    created = client.post(f"/users/{user['user_id']}/sessions", json={}).json()

    with client.websocket_connect(f"/sessions/{created['id']}/stream") as socket:
        client.post(f"/sessions/{created['id']}/channel/reply", json={"chat_id": "7", "text": "hi"})
        assert socket.receive_json() == {"kind": "reply", "chat_id": "7", "text": "hi", "seq": 1}


def test_stream_socket_replays_what_a_reconnecting_surface_missed(client, user):
    """A surface that dropped off comes back whole, not from the live edge."""
    created = client.post(f"/users/{user['user_id']}/sessions", json={}).json()
    for index in range(3):
        client.post(
            f"/sessions/{created['id']}/channel/reply",
            json={"chat_id": "7", "text": str(index)},
        )

    with client.websocket_connect(f"/sessions/{created['id']}/stream?since=1") as socket:
        assert [socket.receive_json()["text"] for _ in range(2)] == ["1", "2"]


def test_stream_socket_still_runs_a_prompt(client, user):
    created = client.post(f"/users/{user['user_id']}/sessions", json={}).json()

    with client.websocket_connect(f"/sessions/{created['id']}/stream") as socket:
        socket.send_json({"prompt": "hello"})
        kinds = [socket.receive_json()["kind"] for _ in range(4)]

    assert kinds == ["session", "text", "text", "result"]


def test_stream_socket_rejects_an_empty_prompt(client, user):
    created = client.post(f"/users/{user['user_id']}/sessions", json={}).json()

    with client.websocket_connect(f"/sessions/{created['id']}/stream") as socket:
        socket.send_json({"prompt": ""})
        assert socket.receive_json() == {"kind": "error", "text": "empty prompt"}


def test_stream_socket_refuses_an_unknown_session(client):
    with client.websocket_connect("/sessions/nope/stream") as socket:
        assert socket.receive_json()["kind"] == "error"
