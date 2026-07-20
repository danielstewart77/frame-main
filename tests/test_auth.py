"""Authentication: the credential primitives, and the routes that enforce them.

The control plane runs sessions with approvals off, so who may open a turn or
push a wake event into one is a real boundary, not a formality. These cover the
primitives in `auth.py` and the enforcement in `server.py` end to end.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import auth
from conftest import SERVICE_TOKEN


# --- primitives ------------------------------------------------------------


def test_password_round_trips():
    encoded = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", encoded)


def test_wrong_password_is_rejected():
    encoded = auth.hash_password("hunter2")
    assert not auth.verify_password("hunter3", encoded)


def test_the_same_password_hashes_differently_each_time():
    """A per-password salt, so two users with one password are not obvious."""
    assert auth.hash_password("same") != auth.hash_password("same")


def test_an_empty_password_will_not_hash():
    with pytest.raises(ValueError):
        auth.hash_password("")


def test_a_garbage_digest_verifies_false_without_raising():
    assert not auth.verify_password("anything", "not-a-real-digest")
    assert not auth.verify_password("anything", "md5$deadbeef")


def test_a_token_is_matched_by_constant_time_compare():
    token = auth.new_token()
    assert auth.tokens_match(token, token)
    assert not auth.tokens_match(token, auth.new_token())
    assert not auth.tokens_match("", "")


def test_a_token_digest_is_not_the_token():
    token = auth.new_token()
    assert auth.token_digest(token) != token
    assert auth.token_digest(token) == auth.token_digest(token)


def test_bearer_pulls_the_token_out_of_the_header():
    assert auth.bearer("Bearer abc.def") == "abc.def"
    assert auth.bearer("bearer abc") == "abc"
    assert auth.bearer("Basic abc") is None
    assert auth.bearer("Bearer ") is None
    assert auth.bearer(None) is None


def test_a_service_owns_everyone_a_user_owns_only_themselves():
    service = auth.Principal(auth.SERVICE)
    user = auth.Principal(auth.USER, "u1")
    assert service.owns("u1") and service.owns("u2")
    assert user.owns("u1")
    assert not user.owns("u2")


# --- registry token store --------------------------------------------------


def test_a_stored_token_resolves_to_its_user(registry):
    user = registry.create_user("Ada")
    token = auth.new_token()
    registry.store_token(auth.token_digest(token), user["user_id"], ttl_hours=1)
    assert registry.user_for_token(auth.token_digest(token)) == user["user_id"]


def test_an_expired_token_resolves_to_nobody_and_is_dropped(registry):
    user = registry.create_user("Ada")
    token = auth.new_token()
    registry.store_token(auth.token_digest(token), user["user_id"], ttl_hours=-1)
    assert registry.user_for_token(auth.token_digest(token)) is None
    # And it was purged on the way out.
    assert registry.purge_expired_tokens() == 0


def test_a_password_change_can_log_a_user_out_everywhere(registry):
    user = registry.create_user("Ada")
    token = auth.new_token()
    registry.store_token(auth.token_digest(token), user["user_id"], ttl_hours=1)
    registry.delete_user_tokens(user["user_id"])
    assert registry.user_for_token(auth.token_digest(token)) is None


def test_a_channel_token_is_scoped_to_one_session(registry, user):
    session = registry.create_session(user["user_id"], "claude", "opus")
    token = registry.rotate_channel_token(session["id"])
    assert registry.session_for_channel_token(token) == session["id"]
    # Rotating supersedes the old one rather than accumulating.
    fresh = registry.rotate_channel_token(session["id"])
    assert registry.session_for_channel_token(token) is None
    assert registry.session_for_channel_token(fresh) == session["id"]


# --- registration ----------------------------------------------------------


def test_the_first_account_can_be_claimed_without_credentials(anon_client):
    response = anon_client.post(
        "/auth/register", json={"username": "daniel", "password": "a good password"}
    )
    assert response.status_code == 201
    assert response.json()["username"] == "daniel"


def test_registration_closes_once_an_account_exists(anon_client):
    anon_client.post(
        "/auth/register", json={"username": "first", "password": "a good password"}
    )
    second = anon_client.post(
        "/auth/register", json={"username": "second", "password": "a good password"}
    )
    assert second.status_code == 403


def test_the_service_token_can_still_register_after_the_first(anon_client):
    anon_client.post(
        "/auth/register", json={"username": "first", "password": "a good password"}
    )
    response = anon_client.post(
        "/auth/register",
        json={"username": "second", "password": "a good password"},
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    assert response.status_code == 201


def test_a_duplicate_username_is_refused(anon_client):
    anon_client.post(
        "/auth/register", json={"username": "dup", "password": "a good password"}
    )
    again = anon_client.post(
        "/auth/register",
        json={"username": "dup", "password": "another password"},
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    assert again.status_code == 409


def test_a_short_password_is_refused(anon_client):
    response = anon_client.post(
        "/auth/register", json={"username": "daniel", "password": "short"}
    )
    assert response.status_code == 422


# --- login / logout / me ---------------------------------------------------


def test_login_issues_a_token_and_a_cookie(anon_client):
    anon_client.post(
        "/auth/register", json={"username": "daniel", "password": "a good password"}
    )
    response = anon_client.post(
        "/auth/login", json={"username": "daniel", "password": "a good password"}
    )
    assert response.status_code == 200
    assert response.json()["token"]
    assert response.cookies.get("frame_auth")


def test_login_with_a_bad_password_is_401(anon_client):
    anon_client.post(
        "/auth/register", json={"username": "daniel", "password": "a good password"}
    )
    response = anon_client.post(
        "/auth/login", json={"username": "daniel", "password": "wrong password"}
    )
    assert response.status_code == 401


def test_login_with_an_unknown_username_is_401(anon_client):
    response = anon_client.post(
        "/auth/login", json={"username": "ghost", "password": "whatever now"}
    )
    assert response.status_code == 401


def test_me_reports_the_logged_in_user(logged_in):
    body = logged_in.get("/auth/me").json()
    assert body["kind"] == "user"
    assert body["username"] == "daniel"
    assert body["user_id"] == logged_in.user_id


def test_me_reports_the_service_principal(client):
    body = client.get("/auth/me").json()
    assert body["kind"] == "service"
    assert body["user_id"] is None


def test_me_without_credentials_is_401(anon_client):
    assert anon_client.get("/auth/me").status_code == 401


def test_logout_kills_the_token(app):
    with TestClient(app) as c:
        c.post("/auth/register", json={"username": "daniel", "password": "a good password"})
        login = c.post(
            "/auth/login", json={"username": "daniel", "password": "a good password"}
        ).json()
        token = login["token"]
        c.post("/auth/logout")
        # The cookie is cleared and the token no longer resolves.
        again = c.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert again.status_code == 401


# --- enforcement -----------------------------------------------------------


def test_protected_routes_refuse_an_anonymous_caller(anon_client):
    assert anon_client.get("/users").status_code == 401
    assert anon_client.post("/users/whoever/sessions", json={}).status_code == 401
    assert anon_client.get("/sessions/whatever").status_code == 401


def test_minting_users_is_service_only(logged_in):
    """A logged-in user is not the fleet operator and cannot mint accounts."""
    assert logged_in.get("/users").status_code == 403
    assert logged_in.post("/users", json={"display_name": "x"}).status_code == 403
    assert (
        logged_in.post(
            "/identities",
            json={"surface": "telegram", "external_id": "9", "display_name": "x"},
        ).status_code
        == 403
    )


def test_a_user_cannot_see_another_users_session(app):
    """The whole point of tying a session to a user: isolation between them."""
    with TestClient(app) as owner, TestClient(app) as intruder:
        owner.post("/auth/register", json={"username": "owner", "password": "a good password"})
        owner_login = owner.post(
            "/auth/login", json={"username": "owner", "password": "a good password"}
        ).json()
        session = owner.post(
            f"/users/{owner_login['user_id']}/sessions", json={"title": "secret"}
        ).json()

        # A second account, registered through the service token now that the
        # box is claimed, logging in on its own client.
        intruder.post(
            "/auth/register",
            json={"username": "intruder", "password": "a good password"},
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        intruder.post(
            "/auth/login", json={"username": "intruder", "password": "a good password"}
        )

        # Every session-scoped route reports it as simply absent — a 404, not a
        # 403, so existence itself does not leak across accounts.
        assert intruder.get(f"/sessions/{session['id']}").status_code == 404
        assert intruder.post(f"/sessions/{session['id']}/turn", json={"prompt": "hi"}).status_code == 404
        assert intruder.get(f"/sessions/{session['id']}/events").status_code == 404
        assert intruder.get(f"/sessions/{session['id']}/diff").status_code == 404
        assert intruder.post(
            f"/sessions/{session['id']}/channel/deliver", json={"content": "hi"}
        ).status_code == 404


def test_a_user_can_reach_their_own_session(logged_in):
    session = logged_in.post(
        f"/users/{logged_in.user_id}/sessions", json={"title": "mine"}
    ).json()
    assert logged_in.get(f"/sessions/{session['id']}").status_code == 200
    assert logged_in.get(f"/sessions/{session['id']}/events").status_code == 200


def test_a_user_cannot_list_another_users_sessions(logged_in):
    assert logged_in.get("/users/someone-else/sessions").status_code == 403


def test_the_wake_path_refuses_an_anonymous_caller(anon_client, client, user):
    """channel/deliver writes into a session running with approvals off, so it
    takes the owner's authority — never an open door."""
    session = client.post(f"/users/{user['user_id']}/sessions", json={}).json()
    response = anon_client.post(
        f"/sessions/{session['id']}/channel/deliver", json={"content": "pwn"}
    )
    assert response.status_code == 401
