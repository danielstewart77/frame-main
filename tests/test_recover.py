"""Restart recovery: the session table must be made true again on boot.

frame-main restarting does not stop its containers, and a host reboot does not
spare them; either way `recover()` reconciles what the table claims against
what docker is actually running. Nothing here may delete a session — a
stranded container is reconciled, never a reason to lose someone's work.
"""

import pytest

from sessions import SessionManager


def restarted(registry, settings, provisioner) -> SessionManager:
    """A fresh manager over the same registry and containers — as a redeploy
    leaves things: the process is new, the containers and rows are not."""
    return SessionManager(registry, settings, provisioner)


@pytest.mark.asyncio
async def test_a_live_container_is_re_adopted(registry, settings, provisioner):
    manager = SessionManager(registry, settings, provisioner)
    user_id = manager.resolve_user("web", "1")
    session = manager.create(user_id)
    running = await manager.ensure_running(session["id"])
    container_id = running["container_id"]
    assert container_id

    result = await restarted(registry, settings, provisioner).recover()

    assert result["adopted"] == [session["id"]]
    assert result["cleared"] == []
    assert registry.get_session(session["id"])["container_id"] == container_id


@pytest.mark.asyncio
async def test_a_dead_container_is_cleared_for_a_fresh_respawn(registry, settings, provisioner):
    manager = SessionManager(registry, settings, provisioner)
    user_id = manager.resolve_user("web", "1")
    session = manager.create(user_id)
    running = await manager.ensure_running(session["id"])
    # The container did not survive whatever took frame-main down.
    await provisioner.stop(running["container_id"])

    result = await restarted(registry, settings, provisioner).recover()

    assert result["cleared"] == [session["id"]]
    assert result["adopted"] == []
    row = registry.get_session(session["id"])
    assert row["container_id"] is None
    assert row["app_port"] is None
    # And the session is not lost — the next turn simply re-provisions it.
    revived = await manager.ensure_running(session["id"])
    assert revived["container_id"]


@pytest.mark.asyncio
async def test_an_orphan_container_is_removed(registry, settings, provisioner):
    manager = SessionManager(registry, settings, provisioner)
    user_id = manager.resolve_user("web", "1")
    session = manager.create(user_id)
    running = await manager.ensure_running(session["id"])
    # The row is gone but the container lived on — the classic strand.
    registry.delete_session(session["id"])

    result = await restarted(registry, settings, provisioner).recover()

    assert result["orphaned"] == [session["id"]]
    assert running["container_id"] in provisioner.removed
    assert await provisioner.live_sessions() == set()


@pytest.mark.asyncio
async def test_recovery_on_a_fresh_box_finds_nothing(registry, settings, provisioner):
    result = await SessionManager(registry, settings, provisioner).recover()
    assert result == {"adopted": [], "cleared": [], "orphaned": []}
