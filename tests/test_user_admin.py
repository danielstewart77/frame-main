"""User administration: roles, the admin routes, self-service password change,
the forced-change flow, and disabling. Mirrors the enforcement style in
test_auth.py — 401 for anonymous, 403 for a non-admin, guards on the last admin.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import registry as registry_mod
from conftest import SERVICE_TOKEN


# --- registry-level --------------------------------------------------------


def test_new_user_defaults_to_a_plain_enabled_user(registry):
    row = registry.get_user(registry.create_user("Ada")["user_id"])
    assert row["role"] == registry_mod.ROLE_USER
    assert row["disabled"] == 0
    assert row["must_change_pw"] == 0


def test_role_helper_validates_and_persists(registry):
    uid = registry.create_user("Ada")["user_id"]
    registry.set_role(uid, registry_mod.ROLE_ADMIN)
    assert registry.get_user(uid)["role"] == "admin"
    with pytest.raises(ValueError):
        registry.set_role(uid, "superuser")


def test_flag_helpers_reflect_in_get_user(registry):
    uid = registry.create_user("Ada")["user_id"]
    registry.set_disabled(uid, True)
    registry.set_must_change_pw(uid, True)
    row = registry.get_user(uid)
    assert row["disabled"] == 1 and row["must_change_pw"] == 1


def test_admin_count_ignores_disabled_admins(registry):
    a = registry.create_user("A")["user_id"]
    b = registry.create_user("B")["user_id"]
    registry.set_role(a, registry_mod.ROLE_ADMIN)
    registry.set_role(b, registry_mod.ROLE_ADMIN)
    assert registry.admin_count() == 2
    registry.set_disabled(b, True)
    assert registry.admin_count() == 1


def test_first_credentialed_user_is_the_earliest_with_a_login(registry):
    first = registry.create_user("First")["user_id"]
    registry.set_credential(first, "first", "hash")
    registry.create_user("NoLogin")  # no credential — skipped
    assert registry.first_credentialed_user() == first


# --- route helpers ---------------------------------------------------------


def _login(client: TestClient, username: str, password: str, service: bool = False) -> None:
    """Register (first account is open; later ones need the service token) + log in."""
    headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"} if service else {}
    client.post(
        "/auth/register", json={"username": username, "password": password}, headers=headers
    ).raise_for_status()
    client.post("/auth/login", json={"username": username, "password": password}).raise_for_status()


# --- roles + admin gate ----------------------------------------------------


def test_first_registrant_is_admin_others_are_plain_users(app):
    with TestClient(app) as boss, TestClient(app) as peon:
        _login(boss, "boss", "a good password")
        _login(peon, "peon", "a good password", service=True)
        assert boss.get("/auth/me").json()["is_admin"] is True
        assert peon.get("/auth/me").json()["is_admin"] is False


def test_admin_routes_require_admin(app):
    with TestClient(app) as boss, TestClient(app) as peon, TestClient(app) as anon:
        _login(boss, "boss", "a good password")
        _login(peon, "peon", "a good password", service=True)
        assert boss.get("/admin/users").status_code == 200
        assert peon.get("/admin/users").status_code == 403
        assert anon.get("/admin/users").status_code == 401


# --- admin creates / resets ------------------------------------------------


def test_admin_creates_a_user_with_a_one_time_password(app):
    with TestClient(app) as boss, TestClient(app) as newbie:
        _login(boss, "boss", "a good password")
        created = boss.post("/admin/users", json={"username": "newbie", "role": "user"})
        assert created.status_code == 201
        body = created.json()
        assert body["temp_password"] and body["must_change_pw"] is True and body["role"] == "user"
        # the temp password actually logs in, and the response says a change is due
        login = newbie.post(
            "/auth/login", json={"username": "newbie", "password": body["temp_password"]}
        ).json()
        assert login["must_change_pw"] is True


def test_admin_reset_password_issues_a_new_temp_and_kills_old_sessions(app):
    with TestClient(app) as boss, TestClient(app) as victim:
        _login(boss, "boss", "a good password")
        created = boss.post("/admin/users", json={"username": "victim"}).json()
        uid, old_temp = created["user_id"], created["temp_password"]
        victim.post("/auth/login", json={"username": "victim", "password": old_temp})
        assert victim.get("/auth/me").status_code == 200

        reset = boss.post(f"/admin/users/{uid}/reset-password")
        assert reset.status_code == 200 and reset.json()["temp_password"] != old_temp
        # the victim's live session is gone, and the old temp no longer works
        assert victim.get("/auth/me").status_code == 401
        assert victim.post(
            "/auth/login", json={"username": "victim", "password": old_temp}
        ).status_code == 401


# --- self-service password change ------------------------------------------


def test_self_password_change_logs_out_everywhere_and_clears_must_change(app):
    with TestClient(app) as boss, TestClient(app) as newbie:
        _login(boss, "boss", "a good password")
        temp = boss.post("/admin/users", json={"username": "newbie"}).json()["temp_password"]
        newbie.post("/auth/login", json={"username": "newbie", "password": temp})

        changed = newbie.post(
            "/auth/password", json={"current_password": temp, "new_password": "brand new password"}
        )
        assert changed.status_code == 204
        assert newbie.get("/auth/me").status_code == 401  # every token invalidated

        login = newbie.post(
            "/auth/login", json={"username": "newbie", "password": "brand new password"}
        ).json()
        assert login["must_change_pw"] is False


def test_self_password_change_rejects_a_wrong_current_password(app):
    with TestClient(app) as boss:
        _login(boss, "boss", "a good password")
        r = boss.post(
            "/auth/password", json={"current_password": "nope", "new_password": "another good one"}
        )
        assert r.status_code == 403


# --- disable + guards ------------------------------------------------------


def test_disabling_a_user_blocks_login_and_revokes_live_tokens(app):
    with TestClient(app) as boss, TestClient(app) as dave:
        _login(boss, "boss", "a good password")
        created = boss.post("/admin/users", json={"username": "dave"}).json()
        uid, temp = created["user_id"], created["temp_password"]
        dave.post("/auth/login", json={"username": "dave", "password": temp})
        assert dave.get("/auth/me").status_code == 200

        boss.post(f"/admin/users/{uid}/disable").raise_for_status()
        assert dave.get("/auth/me").status_code == 401  # live token revoked
        assert dave.post(
            "/auth/login", json={"username": "dave", "password": temp}
        ).status_code == 403  # and cannot log back in

        boss.post(f"/admin/users/{uid}/enable").raise_for_status()
        assert dave.post(
            "/auth/login", json={"username": "dave", "password": temp}
        ).status_code == 200


def test_admin_cannot_disable_or_demote_themselves_as_the_last_admin(app):
    with TestClient(app) as boss:
        _login(boss, "boss", "a good password")
        me = boss.get("/auth/me").json()["user_id"]
        assert boss.post(f"/admin/users/{me}/disable").status_code == 400
        assert boss.post(f"/admin/users/{me}/role", json={"role": "user"}).status_code == 400
