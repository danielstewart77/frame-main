import pytest

import registry as registry_mod
from surfaces.chat import ChatRouter, LocalClient


@pytest.fixture
def router(manager):
    return ChatRouter(LocalClient(manager), surface="telegram")


def test_help_lists_the_commands(router):
    assert "/agents" in router.handle("42", "/help").text


def test_new_creates_and_attaches(router, manager):
    reply = router.handle("42", "/new")
    assert reply.session_id
    assert manager.attached("telegram", "42")["id"] == reply.session_id


def test_new_accepts_a_harness_and_model(router, manager):
    reply = router.handle("42", "/new codex gpt-5")
    session = manager.get(reply.session_id)
    assert (session["harness"], session["model"]) == ("codex", "gpt-5")


def test_agents_lists_active_sessions_as_buttons(router):
    router.handle("42", "/new")
    router.handle("42", "/new")
    reply = router.handle("42", "/agents")
    assert len(reply.buttons) == 2
    assert all(b.action == "attach" for b in reply.buttons)


def test_agents_is_empty_before_any_session(router):
    assert router.handle("42", "/agents").buttons == []


def test_archived_lists_only_archived_sessions(router, manager):
    active = router.handle("42", "/new").session_id
    archived = router.handle("42", "/new").session_id
    manager.registry.update_session(archived, status=registry_mod.ARCHIVED)

    assert [b.session_id for b in router.handle("42", "/agents").buttons] == [active]
    assert [b.session_id for b in router.handle("42", "/archived").buttons] == [archived]


def test_plain_text_routes_to_the_attached_session(router):
    session_id = router.handle("42", "/new").session_id
    reply = router.handle("42", "run the tests")
    assert reply.prompt == "run the tests"
    assert reply.session_id == session_id


def test_plain_text_with_no_attachment_prompts_for_one(router):
    reply = router.handle("42", "hello?")
    assert reply.prompt is None
    assert "/agents" in reply.text


def test_listing_does_not_lose_the_attachment(router, manager):
    session_id = router.handle("42", "/new").session_id
    router.handle("42", "/agents")
    assert manager.attached("telegram", "42")["id"] == session_id


def test_tapping_a_button_repoints_the_binding(router, manager):
    first = router.handle("42", "/new").session_id
    second = router.handle("42", "/new").session_id
    assert manager.attached("telegram", "42")["id"] == second

    router.tap("42", first)
    assert manager.attached("telegram", "42")["id"] == first


def test_switch_repoints_and_rejects_unknown_ids(router, manager):
    first = router.handle("42", "/new").session_id
    router.handle("42", "/new")
    router.handle("42", f"/switch {first}")
    assert manager.attached("telegram", "42")["id"] == first
    assert "No such session" in router.handle("42", "/switch ghost").text
    assert "Usage" in router.handle("42", "/switch").text


def test_detach_leaves_the_chat_idle(router, manager):
    router.handle("42", "/new")
    router.handle("42", "/detach")
    assert manager.attached("telegram", "42") is None


def test_whoami_reports_the_attachment(router):
    assert "Not attached" in router.handle("42", "/whoami").text
    router.handle("42", "/new")
    assert "Attached to" in router.handle("42", "/whoami").text


def test_two_chats_are_independent(router, manager):
    one = router.handle("42", "/new").session_id
    two = router.handle("77", "/new").session_id
    assert one != two
    assert manager.attached("telegram", "42")["id"] == one
    assert manager.attached("telegram", "77")["id"] == two


def test_attachment_survives_a_new_router_instance(manager):
    first = ChatRouter(LocalClient(manager))
    session_id = first.handle("42", "/new").session_id

    restarted = ChatRouter(LocalClient(manager))
    assert restarted.handle("42", "/whoami").session_id == session_id


def test_unknown_command_shows_help(router):
    assert "Unknown command" in router.handle("42", "/banana").text


def test_labels_prefer_the_title(router, manager):
    session_id = router.handle("42", "/new").session_id
    manager.registry.update_session(session_id, title="refactor the parser")
    assert router.handle("42", "/agents").buttons[0].label == "refactor the parser"
