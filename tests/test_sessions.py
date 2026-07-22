from datetime import datetime, timedelta, timezone

import pytest

import registry as registry_mod
from sandbox.provision import ProvisionError, allocate_port
from sessions import SessionError, UnknownSession


@pytest.fixture
def user_id(manager):
    return manager.resolve_user("telegram", "1001", "Daniel")


def test_resolve_user_creates_the_workspace(manager, settings):
    user_id = manager.resolve_user("telegram", "1001")
    assert manager.workspace(user_id).exists()


def test_resolve_user_is_stable_for_the_same_chat(manager):
    assert manager.resolve_user("telegram", "1001") == manager.resolve_user("telegram", "1001")


def test_create_uses_configured_defaults(manager, user_id, settings):
    session = manager.create(user_id)
    assert session["harness"] == settings.default_harness
    assert session["model"] == settings.default_model


def test_create_rejects_an_unknown_user(manager):
    with pytest.raises(UnknownSession):
        manager.create("no-such-user")


@pytest.mark.asyncio
async def test_present_skills_are_mounted_read_only_at_spawn(manager, user_id, provisioner, tmp_path):
    """A cloned skills repo is shared into the container; nothing is mounted
    when none are present."""
    from dataclasses import replace

    root = tmp_path / "sk"
    (root / "claude-skills").mkdir(parents=True)
    manager.settings = replace(manager.settings, skills_root=root)
    session = manager.create(user_id)
    await manager.ensure_running(session["id"])
    mounts = [tuple(m) for m in provisioner.skill_mounts[-1]]
    assert (str(root / "claude-skills"), "/workspace/.claude/skills") in mounts
    assert not any(host.endswith("codex-skills") for host, _ in mounts)


@pytest.mark.asyncio
async def test_per_session_state_is_mounted_for_resume(manager, user_id, provisioner):
    """The harness conversation store is mounted from a per-session host dir so
    it survives container teardown — the difference between resuming work and
    resuming the train of thought."""
    session = manager.create(user_id)
    await manager.ensure_running(session["id"])
    mounts = {c: h for h, c in (tuple(m) for m in provisioner.state_mounts[-1])}
    assert "/workspace/.claude/projects" in mounts
    assert "/workspace/.codex/sessions" in mounts
    # backed by a per-session dir on the host, and actually created
    from pathlib import Path
    assert session["id"] in mounts["/workspace/.claude/projects"]
    assert Path(mounts["/workspace/.claude/projects"]).is_dir()


@pytest.mark.asyncio
async def test_deleting_a_session_removes_its_state(manager, user_id, provisioner):
    from pathlib import Path

    session = manager.create(user_id)
    await manager.ensure_running(session["id"])
    state = Path(manager.workspace(user_id).path) / "sessions" / session["id"]
    assert state.is_dir()
    await manager.delete(session["id"])
    assert not state.exists()


@pytest.mark.asyncio
async def test_ensure_running_provisions_once_and_allocates_a_port(manager, user_id, provisioner):
    session = manager.create(user_id)
    started = await manager.ensure_running(session["id"])
    assert started["container_id"]
    assert started["app_port"] in range(*manager.settings.app_port_range)

    again = await manager.ensure_running(session["id"])
    assert again["container_id"] == started["container_id"]
    assert len(provisioner.provisioned) == 1


@pytest.mark.asyncio
async def test_parallel_sessions_get_distinct_containers_and_ports(manager, user_id):
    first = await manager.ensure_running(manager.create(user_id)["id"])
    second = await manager.ensure_running(manager.create(user_id)["id"])
    assert first["container_id"] != second["container_id"]
    assert first["app_port"] != second["app_port"]


@pytest.mark.asyncio
async def test_turn_persists_the_resume_id_on_the_first_turn(manager, user_id, registry):
    session = manager.create(user_id)
    events = [event async for event in manager.turn(session["id"], "hello")]

    assert events[0]["kind"] == "session"
    assert registry.get_session(session["id"])["resume_id"] == events[0]["resume_id"]
    assert events[-1]["kind"] == "result"


@pytest.mark.asyncio
async def test_second_turn_reuses_the_stored_resume_id(manager, user_id):
    session = manager.create(user_id)
    [e async for e in manager.turn(session["id"], "first")]
    events = [e async for e in manager.turn(session["id"], "second")]
    assert not any(e["kind"] == "session" for e in events)


@pytest.mark.asyncio
async def test_turn_updates_last_active(manager, user_id, registry):
    session = manager.create(user_id)
    registry.conn.execute(
        "UPDATE sessions SET last_active=? WHERE id=?", ("2000-01-01T00:00:00+00:00", session["id"])
    )
    registry.conn.commit()
    [e async for e in manager.turn(session["id"], "hi")]
    assert registry.get_session(session["id"])["last_active"] > "2001"


def test_spawn_env_maps_one_proxy_credential_onto_both_harnesses(manager, user_id):
    """One base URL + one token reach the container under each harness's own
    env var names — ANTHROPIC_* for claude, OPENAI_* for codex."""
    from dataclasses import replace

    manager.settings = replace(
        manager.settings,
        anthropic_base_url="https://ulmaiproxy.utsystem.edu",
        ulmaiproxy_auth_token="secret-token",
    )
    session = manager.create(user_id)
    env = manager._spawn_env(session, manager.workspace(user_id), "chan-token")

    assert env["ANTHROPIC_BASE_URL"] == "https://ulmaiproxy.utsystem.edu"
    assert env["OPENAI_BASE_URL"] == "https://ulmaiproxy.utsystem.edu"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "secret-token"
    assert env["OPENAI_API_KEY"] == "secret-token"


def test_spawn_env_prefers_the_users_own_proxy_key(manager, user_id, registry):
    """A user's own proxy key wins over the box-wide token, scoping usage and
    model access to them; the base URL stays shared."""
    from dataclasses import replace

    manager.settings = replace(
        manager.settings,
        anthropic_base_url="https://ulmaiproxy.utsystem.edu",
        ulmaiproxy_auth_token="box-wide-token",
    )
    registry.set_proxy_key(user_id, "the-users-own-key")
    session = manager.create(user_id)
    env = manager._spawn_env(session, manager.workspace(user_id), "chan-token")
    assert env["ANTHROPIC_AUTH_TOKEN"] == "the-users-own-key"
    assert env["OPENAI_API_KEY"] == "the-users-own-key"
    assert env["ANTHROPIC_BASE_URL"] == "https://ulmaiproxy.utsystem.edu"


def test_spawn_env_falls_back_to_the_box_token_without_a_user_key(manager, user_id):
    from dataclasses import replace

    manager.settings = replace(manager.settings, ulmaiproxy_auth_token="box-wide-token")
    session = manager.create(user_id)
    env = manager._spawn_env(session, manager.workspace(user_id), "chan-token")
    assert env["ANTHROPIC_AUTH_TOKEN"] == "box-wide-token"


def test_proxy_key_registry_round_trip(registry):
    uid = registry.create_user("Ada")["user_id"]
    assert registry.get_proxy_key(uid) is None and registry.has_proxy_key(uid) is False
    registry.set_proxy_key(uid, "sk-abc")
    assert registry.get_proxy_key(uid) == "sk-abc" and registry.has_proxy_key(uid) is True
    registry.set_proxy_key(uid, "sk-def")  # replaces
    assert registry.get_proxy_key(uid) == "sk-def"
    registry.clear_proxy_key(uid)
    assert registry.get_proxy_key(uid) is None
    assert registry.get_proxy_key(None) is None  # service principal has no key


def test_spawn_env_omits_proxy_creds_when_unset(manager, user_id):
    """Offline, nothing provider-shaped is injected — the harness talks to no one."""
    from dataclasses import replace

    manager.settings = replace(
        manager.settings, anthropic_base_url="", ulmaiproxy_auth_token=""
    )
    session = manager.create(user_id)
    env = manager._spawn_env(session, manager.workspace(user_id), "chan-token")
    for key in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        assert key not in env


@pytest.mark.asyncio
async def test_system_prompt_carries_identity_and_commit_discipline(manager, user_id):
    manager.workspace(user_id).set_identity("Daniel is the operator.")
    session = manager.create(user_id)
    prompt = manager.system_prompt(session)
    assert "Daniel is the operator." in prompt
    assert "commit" in prompt.lower()


@pytest.mark.asyncio
async def test_stop_clears_the_container_but_keeps_the_session(manager, user_id, provisioner):
    session = await manager.ensure_running(manager.create(user_id)["id"])
    stopped = await manager.stop(session["id"])
    assert stopped["container_id"] is None
    assert stopped["status"] == registry_mod.ACTIVE
    assert provisioner.stopped == [session["container_id"]]


@pytest.mark.asyncio
async def test_a_stopped_session_re_provisions_on_resume(manager, user_id):
    session = await manager.ensure_running(manager.create(user_id)["id"])
    await manager.stop(session["id"])
    resumed = await manager.ensure_running(session["id"])
    assert resumed["container_id"] and resumed["container_id"] != session["container_id"]
    assert resumed["resume_id"] == session["resume_id"]


@pytest.mark.asyncio
async def test_archive_removes_the_container_and_closes_the_frame(manager, user_id, provisioner):
    session = await manager.ensure_running(manager.create(user_id)["id"])
    archived = await manager.archive(session["id"])
    assert archived["status"] == registry_mod.ARCHIVED
    assert archived["container_id"] is None
    assert archived["app_port"] is None
    assert archived["frame_state"] == registry_mod.FRAME_CLOSED
    assert provisioner.removed == [session["container_id"]]


@pytest.mark.asyncio
async def test_an_archived_session_cannot_be_run(manager, user_id):
    session = manager.create(user_id)
    await manager.archive(session["id"])
    with pytest.raises(SessionError):
        await manager.ensure_running(session["id"])


@pytest.mark.asyncio
async def test_archiving_keeps_the_branch(manager, user_id):
    session = manager.create(user_id)
    branch = session["branch"]
    archived = await manager.archive(session["id"])
    assert archived["branch"] == branch


@pytest.mark.asyncio
async def test_delete_removes_the_container_and_the_row(manager, user_id, provisioner):
    session = await manager.ensure_running(manager.create(user_id)["id"])
    await manager.delete(session["id"])
    assert manager.registry.get_session(session["id"]) is None
    assert provisioner.removed == [session["container_id"]]


@pytest.mark.asyncio
async def test_reap_idle_stops_only_stale_containers(manager, user_id, registry):
    fresh = await manager.ensure_running(manager.create(user_id)["id"])
    stale = await manager.ensure_running(manager.create(user_id)["id"])
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    registry.conn.execute("UPDATE sessions SET last_active=? WHERE id=?", (old, stale["id"]))
    registry.conn.commit()

    reaped = await manager.reap_idle()
    assert reaped == [stale["id"]]
    assert registry.get_session(stale["id"])["container_id"] is None
    assert registry.get_session(fresh["id"])["container_id"] is not None


@pytest.mark.asyncio
async def test_reap_spares_a_session_that_is_mid_turn(manager, user_id, registry):
    # `last_active` is only stamped when a turn ends, so a long unattended turn
    # looks stale while it is working. Reaping it would kill the work.
    session = await manager.ensure_running(manager.create(user_id)["id"])
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    registry.conn.execute("UPDATE sessions SET last_active=? WHERE id=?", (old, session["id"]))
    registry.conn.commit()

    manager._in_flight.add(session["id"])
    assert await manager.reap_idle() == []
    assert registry.get_session(session["id"])["container_id"] is not None

    manager._in_flight.discard(session["id"])
    assert await manager.reap_idle() == [session["id"]]


@pytest.mark.asyncio
async def test_a_turn_marks_itself_in_flight_and_clears_it(manager, user_id):
    session = await manager.ensure_running(manager.create(user_id)["id"])
    seen = []
    async for _ in manager.turn(session["id"], "hi"):
        seen.append(manager._in_flight & {session["id"]})
    assert seen and all(marked for marked in seen)
    assert session["id"] not in manager._in_flight


@pytest.mark.asyncio
async def test_a_turn_without_a_container_is_an_error(manager, user_id, provisioner):
    session = manager.create(user_id)
    with pytest.raises(ProvisionError):
        async for _ in provisioner.run_turn(session, "hi"):
            pass


def test_attach_detach_round_trip(manager, user_id):
    session = manager.create(user_id)
    manager.attach("telegram", "1001", session["id"])
    assert manager.attached("telegram", "1001")["id"] == session["id"]
    manager.detach("telegram", "1001")
    assert manager.attached("telegram", "1001") is None


def test_allocate_port_skips_used_and_returns_none_when_exhausted():
    assert allocate_port({9600}, (9600, 9602)) == 9601
    assert allocate_port({9600, 9601, 9602}, (9600, 9602)) is None
