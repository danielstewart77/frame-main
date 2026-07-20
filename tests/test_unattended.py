"""The properties a session needs in order to be left alone.

Nobody is watching a session that runs unattended, so the things that would
normally be caught by a human noticing — work never committed, containers never
reclaimed — have to be caught here instead.
"""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import registry as registry_mod
import voice as voice_mod
from config import Settings
from sandbox.provision import FakeProvisioner
from server import create_app

ROOT = Path(__file__).resolve().parent.parent


def test_entrypoint_declares_the_stop_hook_it_installs():
    """A hook script in ~/.claude/hooks is inert until settings.json names it.

    This is a static read of the entrypoint rather than a container test so it
    runs on a box with no image built — the failure it guards against (a whole
    unattended run committing nothing) is too expensive to leave to a suite
    that skips by default.
    """
    entrypoint = (ROOT / "sandbox" / "entrypoint.sh").read_text()
    assert "/root/.claude/settings.json" in entrypoint

    body = entrypoint.split("cat > /root/.claude/settings.json <<'JSON'")[1]
    declared = json.loads(body.split("\nJSON")[0])
    commands = [
        hook["command"]
        for matcher in declared["hooks"]["Stop"]
        for hook in matcher["hooks"]
    ]
    assert "/root/.claude/hooks/stop-commit.sh" in commands
    assert "install -m 755 /opt/frame/hooks/stop-commit.sh" in entrypoint


@pytest.mark.asyncio
async def test_the_app_reaps_idle_containers_on_a_timer(tmp_path):
    """`reap_idle` is the whole container lifecycle policy; nothing else calls it."""
    settings = Settings(
        db_path=tmp_path / "registry.db",
        users_root=tmp_path / "users",
        provisioner="fake",
        voice="fake",
        reap_interval_seconds=0.01,
    )
    reg = registry_mod.Registry(settings.db_path)
    app = create_app(
        settings=settings,
        registry=reg,
        provisioner=FakeProvisioner(),
        voice=voice_mod.FakeVoice(),
    )

    swept = asyncio.Event()
    app.state.manager.reap_idle = lambda *a, **k: (swept.set(), _done())[1]

    async def _done():
        return []

    with TestClient(app):
        await asyncio.wait_for(swept.wait(), timeout=2)
    reg.close()


@pytest.mark.asyncio
async def test_a_failing_reap_does_not_disable_reaping(tmp_path):
    """One transient docker error must not silently stop the sweep for good."""
    settings = Settings(
        db_path=tmp_path / "registry.db",
        users_root=tmp_path / "users",
        provisioner="fake",
        voice="fake",
        reap_interval_seconds=0.01,
    )
    reg = registry_mod.Registry(settings.db_path)
    app = create_app(
        settings=settings,
        registry=reg,
        provisioner=FakeProvisioner(),
        voice=voice_mod.FakeVoice(),
    )

    calls = []
    third = asyncio.Event()

    async def _flaky():
        calls.append(1)
        if len(calls) >= 3:
            third.set()
        raise RuntimeError("docker went away")

    app.state.manager.reap_idle = _flaky

    with TestClient(app):
        await asyncio.wait_for(third.wait(), timeout=2)
    reg.close()
