import pytest

import registry as registry_mod
from surfaces.chat import ChatRouter, LocalClient


@pytest.fixture
def user_id(manager):
    """The one user every chat in these tests belongs to.

    The router no longer resolves the user — the surface (the Telegram poller)
    knows whose bot a message arrived on and passes the user in. These tests
    stand in for that by resolving a user once and handing it to `send`.
    """
    return manager.resolve_user("telegram", "owner")


@pytest.fixture
def router(manager):
    return ChatRouter(LocalClient(manager), surface="telegram")


@pytest.fixture
def send(router, user_id):
    def _send(external_id, text):
        return router.handle(user_id, external_id, text)

    return _send


def test_help_lists_the_commands(send):
    assert "/agents" in send("42", "/help").text


def test_new_creates_and_attaches(send, manager):
    reply = send("42", "/new")
    assert reply.session_id
    assert manager.attached("telegram", "42")["id"] == reply.session_id


def test_new_accepts_a_harness_and_model(send, manager):
    reply = send("42", "/new codex gpt-5")
    session = manager.get(reply.session_id)
    assert (session["harness"], session["model"]) == ("codex", "gpt-5")


def test_agents_lists_active_sessions_as_buttons(send):
    send("42", "/new")
    send("42", "/new")
    reply = send("42", "/agents")
    assert len(reply.buttons) == 2
    assert all(b.action == "attach" for b in reply.buttons)


def test_agents_is_empty_before_any_session(send):
    assert send("42", "/agents").buttons == []


def test_archived_lists_only_archived_sessions(send, manager):
    active = send("42", "/new").session_id
    archived = send("42", "/new").session_id
    manager.registry.update_session(archived, status=registry_mod.ARCHIVED)

    assert [b.session_id for b in send("42", "/agents").buttons] == [active]
    assert [b.session_id for b in send("42", "/archived").buttons] == [archived]


def test_plain_text_routes_to_the_attached_session(send):
    session_id = send("42", "/new").session_id
    reply = send("42", "run the tests")
    assert reply.prompt == "run the tests"
    assert reply.session_id == session_id


def test_plain_text_with_no_attachment_prompts_for_one(send):
    reply = send("42", "hello?")
    assert reply.prompt is None
    assert "/agents" in reply.text


def test_listing_does_not_lose_the_attachment(send, manager):
    session_id = send("42", "/new").session_id
    send("42", "/agents")
    assert manager.attached("telegram", "42")["id"] == session_id


def test_tapping_a_button_repoints_the_binding(send, router, manager):
    first = send("42", "/new").session_id
    second = send("42", "/new").session_id
    assert manager.attached("telegram", "42")["id"] == second

    router.tap("42", first)
    assert manager.attached("telegram", "42")["id"] == first


def test_switch_repoints_and_rejects_unknown_ids(send, manager):
    first = send("42", "/new").session_id
    send("42", "/new")
    send("42", f"/switch {first}")
    assert manager.attached("telegram", "42")["id"] == first
    assert "No such session" in send("42", "/switch ghost").text
    assert "Usage" in send("42", "/switch").text


def test_detach_leaves_the_chat_idle(send, manager):
    send("42", "/new")
    send("42", "/detach")
    assert manager.attached("telegram", "42") is None


def test_whoami_reports_the_attachment(send):
    assert "Not attached" in send("42", "/whoami").text
    send("42", "/new")
    assert "Attached to" in send("42", "/whoami").text


def test_two_chats_are_independent(send, manager):
    one = send("42", "/new").session_id
    two = send("77", "/new").session_id
    assert one != two
    assert manager.attached("telegram", "42")["id"] == one
    assert manager.attached("telegram", "77")["id"] == two


def test_attachment_survives_a_new_router_instance(manager, user_id):
    first = ChatRouter(LocalClient(manager))
    session_id = first.handle(user_id, "42", "/new").session_id

    restarted = ChatRouter(LocalClient(manager))
    assert restarted.handle(user_id, "42", "/whoami").session_id == session_id


def test_unknown_command_shows_help(send):
    assert "Unknown command" in send("42", "/banana").text


def test_labels_prefer_the_title(send, manager):
    session_id = send("42", "/new").session_id
    manager.registry.update_session(session_id, title="refactor the parser")
    assert send("42", "/agents").buttons[0].label == "refactor the parser"
