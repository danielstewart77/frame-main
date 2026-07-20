import pytest

import registry as registry_mod


def test_create_user_and_resolve_identity(registry):
    user = registry.create_user("Daniel")
    registry.link_identity("telegram", "12345", user["user_id"])
    assert registry.resolve_identity("telegram", "12345") == user["user_id"]
    assert registry.resolve_identity("telegram", "99999") is None


def test_resolve_or_create_user_is_idempotent(registry):
    first = registry.resolve_or_create_user("telegram", "555")
    second = registry.resolve_or_create_user("telegram", "555")
    assert first == second
    assert len(registry.list_users()) == 1


def test_session_carries_its_own_branch_harness_and_model(registry, user):
    session = registry.create_session(user["user_id"], "claude", "opus", title="refactor")
    assert session["branch"] == f"session/{session['id']}"
    assert session["harness"] == "claude"
    assert session["model"] == "opus"
    assert session["status"] == registry_mod.ACTIVE
    assert session["resume_id"] is None


def test_resume_id_persists_across_registry_instances(registry, user, settings):
    session = registry.create_session(user["user_id"], "claude", "opus")
    registry.update_session(session["id"], resume_id="abc-123")
    registry.close()

    reopened = registry_mod.Registry(settings.db_path)
    assert reopened.get_session(session["id"])["resume_id"] == "abc-123"
    reopened.close()


def test_update_session_rejects_unknown_and_bad_fields(registry, user):
    session = registry.create_session(user["user_id"], "claude", "opus")
    with pytest.raises(ValueError):
        registry.update_session(session["id"], nonsense="x")
    with pytest.raises(ValueError):
        registry.update_session(session["id"], status="banana")
    with pytest.raises(ValueError):
        registry.update_session(session["id"], frame_state="floating")


def test_list_sessions_filters_by_status(registry, user):
    active = registry.create_session(user["user_id"], "claude", "opus")
    archived = registry.create_session(user["user_id"], "codex", "gpt-5")
    registry.update_session(archived["id"], status=registry_mod.ARCHIVED)

    assert [s["id"] for s in registry.list_sessions(user["user_id"])] == [active["id"]]
    assert [
        s["id"] for s in registry.list_sessions(user["user_id"], registry_mod.ARCHIVED)
    ] == [archived["id"]]


def test_binding_is_repointable_and_unique_per_chat(registry, user):
    first = registry.create_session(user["user_id"], "claude", "opus")
    second = registry.create_session(user["user_id"], "claude", "opus")
    registry.bind_surface("telegram", "42", first["id"])
    assert registry.bound_session("telegram", "42") == first["id"]

    registry.bind_surface("telegram", "42", second["id"])
    assert registry.bound_session("telegram", "42") == second["id"]

    registry.unbind_surface("telegram", "42")
    assert registry.bound_session("telegram", "42") is None


def test_delete_session_clears_its_bindings(registry, user):
    session = registry.create_session(user["user_id"], "claude", "opus")
    registry.bind_surface("telegram", "7", session["id"])
    registry.delete_session(session["id"])
    assert registry.get_session(session["id"]) is None
    assert registry.bound_session("telegram", "7") is None


def test_open_frames_returns_only_non_closed_active_sessions(registry, user):
    docked = registry.create_session(user["user_id"], "claude", "opus")
    minimized = registry.create_session(user["user_id"], "claude", "opus")
    registry.create_session(user["user_id"], "claude", "opus")  # closed
    registry.update_session(docked["id"], frame_state=registry_mod.FRAME_DOCKED)
    registry.update_session(minimized["id"], frame_state=registry_mod.FRAME_MINIMIZED)

    open_ids = {s["id"] for s in registry.open_frames(user["user_id"])}
    assert open_ids == {docked["id"], minimized["id"]}


def test_archiving_removes_a_session_from_open_frames(registry, user):
    session = registry.create_session(user["user_id"], "claude", "opus")
    registry.update_session(session["id"], frame_state=registry_mod.FRAME_DOCKED)
    registry.update_session(session["id"], status=registry_mod.ARCHIVED)
    assert registry.open_frames(user["user_id"]) == []


def test_sidebar_collapsed_defaults_false_and_persists(registry):
    assert registry.sidebar_collapsed("web", "u1") is False
    registry.set_sidebar_collapsed("web", "u1", True)
    assert registry.sidebar_collapsed("web", "u1") is True
    registry.set_sidebar_collapsed("web", "u1", False)
    assert registry.sidebar_collapsed("web", "u1") is False


def test_used_app_ports_reports_allocations(registry, user):
    session = registry.create_session(user["user_id"], "claude", "opus")
    assert registry.used_app_ports() == set()
    registry.update_session(session["id"], app_port=9601)
    assert registry.used_app_ports() == {9601}


# --- telegram bots ---------------------------------------------------------


def test_set_and_get_telegram_bot(registry, user):
    assert registry.get_telegram_bot(user["user_id"]) is None
    registry.set_telegram_bot(user["user_id"], "bot-token-1")
    bot = registry.get_telegram_bot(user["user_id"])
    assert bot["bot_token"] == "bot-token-1"
    assert bot["owner_chat_id"] is None
    assert bot["enabled"] == 1


def test_owner_chat_locks_in_then_survives_a_re_save(registry, user):
    registry.set_telegram_bot(user["user_id"], "bot-token-1")
    registry.set_telegram_owner_chat(user["user_id"], "555")
    # Re-saving the same token must not evict the enrolled owner.
    registry.set_telegram_bot(user["user_id"], "bot-token-1")
    assert registry.get_telegram_bot(user["user_id"])["owner_chat_id"] == "555"


def test_changing_the_token_resets_the_owner(registry, user):
    registry.set_telegram_bot(user["user_id"], "bot-token-1")
    registry.set_telegram_owner_chat(user["user_id"], "555")
    registry.set_telegram_bot(user["user_id"], "bot-token-2")
    bot = registry.get_telegram_bot(user["user_id"])
    assert bot["bot_token"] == "bot-token-2"
    assert bot["owner_chat_id"] is None
    assert bot["enabled"] == 1


def test_clear_telegram_bot_removes_the_row(registry, user):
    registry.set_telegram_bot(user["user_id"], "bot-token-1")
    registry.clear_telegram_bot(user["user_id"])
    assert registry.get_telegram_bot(user["user_id"]) is None


def test_list_telegram_bots_returns_only_enabled(registry):
    one = registry.create_user("One")
    two = registry.create_user("Two")
    registry.set_telegram_bot(one["user_id"], "token-one")
    registry.set_telegram_bot(two["user_id"], "token-two")
    registry.conn.execute(
        "UPDATE telegram_bots SET enabled=0 WHERE user_id=?", (two["user_id"],)
    )
    registry.conn.commit()

    listed = registry.list_telegram_bots()
    assert [b["user_id"] for b in listed] == [one["user_id"]]
    assert listed[0]["bot_token"] == "token-one"
