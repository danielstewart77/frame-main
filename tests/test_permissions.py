"""Tool approval relayed out to a surface and answered back in.

A containerised session has nobody at a terminal, and the alternative to this
path is `--dangerously-skip-permissions`, so the interesting cases are the ones
where nothing answers.
"""

from __future__ import annotations

import asyncio

import pytest

from permissions import ID_LENGTH, PermissionBroker
from sessions import SessionError, UnknownSession


@pytest.fixture
def user_id(manager):
    return manager.resolve_user("telegram", "3003", "Daniel")


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


async def next_pending(manager, session_id: str, timeout: float = 2.0):
    """Wait for the prompt to be opened by a concurrently-running request."""

    async def wait():
        while True:
            pending = manager.pending_permissions(session_id)
            if pending:
                return pending[0]
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(wait(), timeout)


# --- the broker ------------------------------------------------------------


def test_request_ids_are_short_and_unambiguous():
    broker = PermissionBroker()
    request = broker.open("session-1", "Bash", {"command": "rm -rf /"})

    assert len(request.id) == ID_LENGTH
    # A person reads this off a screen and types it back.
    assert not set(request.id) & set("lIO")


def test_ids_do_not_collide_across_open_requests():
    broker = PermissionBroker()
    ids = {broker.open("session-1", "Bash").id for _ in range(200)}

    assert len(ids) == 200


async def test_resolving_releases_the_waiter():
    broker = PermissionBroker()
    request = broker.open("session-1", "Bash")

    waiter = asyncio.create_task(broker.wait(request))
    await asyncio.sleep(0)
    broker.resolve(request.id, True)

    assert await asyncio.wait_for(waiter, 2) is True


async def test_an_unanswered_prompt_denies_itself():
    """Silence is not consent."""
    broker = PermissionBroker()
    request = broker.open("session-1", "Bash")

    assert await broker.wait(request, timeout=0.05) is False
    assert "in time" in request.reason


async def test_an_answered_prompt_is_no_longer_pending():
    broker = PermissionBroker()
    request = broker.open("session-1", "Bash")
    broker.resolve(request.id, True)
    await broker.wait(request, timeout=1)

    assert broker.pending("session-1") == []
    # No verdict can be minted for a request that has already gone away.
    assert broker.resolve(request.id, False) is None


def test_pending_is_scoped_to_one_session():
    broker = PermissionBroker()
    broker.open("session-1", "Bash")
    broker.open("session-2", "Write")

    assert [r.session_id for r in broker.pending("session-1")] == ["session-1"]


# --- through the manager ---------------------------------------------------


async def test_a_prompt_reaches_a_watching_surface(manager, session):
    subscription = manager.subscribe(session["id"])
    asking = asyncio.create_task(
        manager.request_permission(session["id"], "Bash", {"command": "ls"})
    )

    event = (await drain(subscription, 1))[0]
    manager.answer_permission(session["id"], event["request_id"], True)
    await asyncio.wait_for(asking, 2)

    assert event["kind"] == "permission"
    assert event["tool"] == "Bash"
    assert event["input"] == {"command": "ls"}


async def test_the_verdict_is_published_so_every_surface_clears_the_prompt(manager, session):
    subscription = manager.subscribe(session["id"])
    asking = asyncio.create_task(manager.request_permission(session["id"], "Bash"))

    request = await next_pending(manager, session["id"])
    manager.answer_permission(session["id"], request.id, False, "not that one")
    await asyncio.wait_for(asking, 2)

    events = await drain(subscription, 2)
    assert events[1]["kind"] == "permission_resolved"
    assert events[1]["allow"] is False
    assert events[1]["reason"] == "not that one"


async def test_a_prompt_nobody_answers_comes_back_denied(manager, session):
    request = await manager.request_permission(session["id"], "Bash", timeout=0.05)

    assert request.allow is False


async def test_answering_an_unknown_request_is_refused(manager, session):
    with pytest.raises(UnknownSession):
        manager.answer_permission(session["id"], "zzzzz", True)


async def test_a_prompt_cannot_be_answered_through_another_session(manager, user_id, session):
    other = manager.create(user_id)
    asking = asyncio.create_task(manager.request_permission(session["id"], "Bash"))
    request = await next_pending(manager, session["id"])

    with pytest.raises(UnknownSession):
        manager.answer_permission(other["id"], request.id, True)

    manager.answer_permission(session["id"], request.id, True)
    await asyncio.wait_for(asking, 2)


async def test_an_archived_session_takes_no_prompts(manager, session):
    await manager.archive(session["id"])

    with pytest.raises(SessionError, match="archived"):
        await manager.request_permission(session["id"], "Bash")


# --- over HTTP -------------------------------------------------------------


def test_permission_round_trip_over_http(client, user):
    """The shim's call blocks; a surface answers it from another connection."""
    created = client.post(f"/users/{user['user_id']}/sessions", json={}).json()
    session_id = created["id"]

    with client.websocket_connect(f"/sessions/{session_id}/stream") as socket:
        import threading

        answered: list[dict] = []

        def answer() -> None:
            event = socket.receive_json()
            answered.append(
                client.post(
                    f"/sessions/{session_id}/permissions/{event['request_id']}",
                    json={"allow": True},
                ).json()
            )

        worker = threading.Thread(target=answer)
        worker.start()
        response = client.post(
            f"/sessions/{session_id}/channel/permission",
            json={"tool": "Bash", "input": {"command": "ls"}, "timeout": 10},
        )
        worker.join(timeout=10)

    assert response.status_code == 200
    assert response.json()["allow"] is True
    assert answered[0]["allow"] is True


def test_an_unanswered_prompt_returns_denied_over_http(client, user):
    created = client.post(f"/users/{user['user_id']}/sessions", json={}).json()

    response = client.post(
        f"/sessions/{created['id']}/channel/permission",
        json={"tool": "Bash", "timeout": 0.05},
    )

    assert response.status_code == 200
    assert response.json()["allow"] is False


def test_answering_an_unknown_request_over_http_is_404(client, user):
    created = client.post(f"/users/{user['user_id']}/sessions", json={}).json()

    response = client.post(
        f"/sessions/{created['id']}/permissions/zzzzz", json={"allow": True}
    )

    assert response.status_code == 404


def test_pending_prompts_are_listed_for_a_late_surface(client, user):
    created = client.post(f"/users/{user['user_id']}/sessions", json={}).json()
    session_id = created["id"]
    import threading

    listed: list[dict] = []

    def watch() -> None:
        while True:
            payload = client.get(f"/sessions/{session_id}/permissions").json()
            if payload["pending"]:
                listed.append(payload["pending"][0])
                client.post(
                    f"/sessions/{session_id}/permissions/{payload['pending'][0]['request_id']}",
                    json={"allow": False},
                )
                return

    worker = threading.Thread(target=watch, daemon=True)
    worker.start()
    client.post(
        f"/sessions/{session_id}/channel/permission",
        json={"tool": "Write", "timeout": 10},
    )
    worker.join(timeout=10)

    assert listed[0]["tool"] == "Write"


def test_a_prompt_for_an_unknown_session_is_404(client):
    response = client.post("/sessions/nope/channel/permission", json={"tool": "Bash"})
    assert response.status_code == 404
