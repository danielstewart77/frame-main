"""A turn must not hang forever when the provider is unreachable.

The real `claude` CLI retries a dead endpoint ten times with backoff, emitting
only `api_retry` lines. Without a ceiling the frame streams nothing and the
session's semaphore slot is held indefinitely.
"""

import asyncio

import pytest

from sessions import SessionManager


class StallingProvisioner:
    """Emits one event, then never yields again."""

    def __init__(self):
        self.closed = False

    async def provision(self, session, workspace, env):
        from sandbox.provision import Container

        workspace.ensure()
        return Container(container_id="stalled", app_port=None)

    async def run_turn(self, session, prompt, system_prompt="", channel_config=None):
        try:
            yield {"kind": "status", "text": "retrying provider (1/10)"}
            await asyncio.sleep(3600)
            yield {"kind": "result", "text": "never"}
        finally:
            self.closed = True

    async def stop(self, container_id):
        pass

    async def remove(self, container_id):
        pass


@pytest.fixture
def stalling_manager(registry, settings):
    from dataclasses import replace

    provisioner = StallingProvisioner()
    manager = SessionManager(registry, replace(settings, turn_timeout_seconds=1), provisioner)
    return manager, provisioner


@pytest.mark.asyncio
async def test_a_stalled_turn_ends_with_an_error_event(stalling_manager):
    manager, provisioner = stalling_manager
    user_id = manager.resolve_user("web", "1")
    session = manager.create(user_id)

    events = [event async for event in manager.turn(session["id"], "hello")]

    assert events[0]["kind"] == "status"
    assert events[-1]["kind"] == "error"
    assert "timed out" in events[-1]["text"]
    assert provisioner.closed


@pytest.mark.asyncio
async def test_a_stalled_turn_releases_its_semaphore_slot(stalling_manager):
    manager, _ = stalling_manager
    user_id = manager.resolve_user("web", "1")
    session = manager.create(user_id)

    [e async for e in manager.turn(session["id"], "one")]
    assert manager.semaphore._value == manager.settings.max_concurrent_sessions


@pytest.mark.asyncio
async def test_the_timeout_is_configurable(settings):
    from dataclasses import replace

    assert replace(settings, turn_timeout_seconds=5).turn_timeout_seconds == 5
