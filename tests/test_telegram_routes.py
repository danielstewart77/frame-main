"""The per-user Telegram routes: set/read/clear a bot, and the bootstrap summary.

Telegram is optional and per-user now — a user manages their own bot token and
nobody else's, and the token is write-only: it goes in over the API and never
comes back out.
"""

from __future__ import annotations


def test_telegram_starts_unconfigured(logged_in):
    body = logged_in.get(f"/users/{logged_in.user_id}/telegram").json()
    assert body == {"configured": False, "enabled": False, "owner_chat_id": None}


def test_saving_a_token_reports_configured_but_never_echoes_it(logged_in):
    uid = logged_in.user_id
    saved = logged_in.put(f"/users/{uid}/telegram", json={"bot_token": "super-secret-token"})
    assert saved.status_code == 200
    assert saved.json() == {"configured": True, "enabled": True, "owner_chat_id": None}
    assert "super-secret-token" not in saved.text
    # And it is not readable back out on a subsequent GET either.
    got = logged_in.get(f"/users/{uid}/telegram")
    assert got.json()["configured"] is True
    assert "super-secret-token" not in got.text


def test_disconnect_clears_the_bot(logged_in):
    uid = logged_in.user_id
    logged_in.put(f"/users/{uid}/telegram", json={"bot_token": "tok"})
    assert logged_in.delete(f"/users/{uid}/telegram").status_code == 204
    assert logged_in.get(f"/users/{uid}/telegram").json()["configured"] is False


def test_a_blank_token_is_refused(logged_in):
    uid = logged_in.user_id
    assert logged_in.put(f"/users/{uid}/telegram", json={"bot_token": ""}).status_code == 422
    assert logged_in.put(f"/users/{uid}/telegram", json={"bot_token": "   "}).status_code == 400


def test_a_user_cannot_touch_another_users_bot(logged_in):
    """The bot is scoped to its owner — a wrong user_id is a 403, not a peek."""
    assert logged_in.get("/users/someone-else/telegram").status_code == 403
    assert (
        logged_in.put("/users/someone-else/telegram", json={"bot_token": "t"}).status_code
        == 403
    )
    assert logged_in.delete("/users/someone-else/telegram").status_code == 403


def test_the_routes_refuse_an_anonymous_caller(anon_client):
    assert anon_client.get("/users/x/telegram").status_code == 401
    assert anon_client.put("/users/x/telegram", json={"bot_token": "t"}).status_code == 401
    assert anon_client.delete("/users/x/telegram").status_code == 401


def test_bootstrap_carries_the_telegram_summary(logged_in):
    uid = logged_in.user_id
    assert logged_in.get("/console/bootstrap").json()["telegram"] == {
        "configured": False,
        "enabled": False,
        "owner_chat_id": None,
    }
    logged_in.put(f"/users/{uid}/telegram", json={"bot_token": "boot-token"})
    boot = logged_in.get("/console/bootstrap")
    assert boot.json()["telegram"]["configured"] is True
    assert "boot-token" not in boot.text
