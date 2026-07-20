import re
from pathlib import Path

import pytest

CONSOLE = Path(__file__).resolve().parent.parent / "console"


@pytest.fixture
def user_id(client):
    return client.post(
        "/identities", json={"surface": "web", "external_id": "42", "display_name": "Daniel"}
    ).json()["user_id"]


# --- the shell -------------------------------------------------------------


def test_console_page_is_served(anon_client):
    """The shell is public — it is only the login form until a token is in hand."""
    response = anon_client.get("/console")
    assert response.status_code == 200
    assert "/console/static/console.js" in response.text


def test_console_static_assets_are_served(anon_client):
    for name in ("console.js", "console.css"):
        assert anon_client.get(f"/console/static/{name}").status_code == 200


# --- bootstrap -------------------------------------------------------------


def test_bootstrap_requires_a_login(anon_client):
    assert anon_client.get("/console/bootstrap").status_code == 401


def test_bootstrap_refuses_the_service_token(client):
    """The console is for user logins; the service token has no console."""
    assert client.get("/console/bootstrap").status_code == 403


def test_bootstrap_reports_identity_layout_and_harnesses(logged_in):
    body = logged_in.get("/console/bootstrap").json()
    assert body["user_id"] == logged_in.user_id
    assert body["external_id"] == logged_in.user_id
    assert body["username"] == "daniel"
    assert body["sidebar_collapsed"] is False
    assert body["frames"] == []
    assert set(body["harnesses"]) == {"claude", "codex"}
    assert body["default_harness"] == "claude"


def test_bootstrap_returns_the_open_frames_to_restore(logged_in):
    user = logged_in.user_id
    session = logged_in.post(f"/users/{user}/sessions", json={"title": "keep"}).json()
    logged_in.patch(f"/sessions/{session['id']}", json={"frame_state": "docked"})

    frames = logged_in.get("/console/bootstrap").json()["frames"]
    assert [f["id"] for f in frames] == [session["id"]]
    assert frames[0]["frame_state"] == "docked"


def test_bootstrap_carries_the_collapsed_sidebar_back(logged_in):
    user = logged_in.user_id
    logged_in.patch(f"/surfaces/web/{user}/layout", json={"sidebar_collapsed": True})
    assert logged_in.get("/console/bootstrap").json()["sidebar_collapsed"] is True


# --- interrupt -------------------------------------------------------------


def test_interrupt_reports_false_when_nothing_is_running(client, user_id):
    session = client.post(f"/users/{user_id}/sessions", json={}).json()
    body = client.post(f"/sessions/{session['id']}/interrupt").json()
    assert body["interrupted"] is False


def test_interrupt_hits_the_provisioner_for_a_live_session(client, user_id, provisioner):
    session = client.post(f"/users/{user_id}/sessions", json={}).json()
    client.post(f"/sessions/{session['id']}/start")
    body = client.post(f"/sessions/{session['id']}/interrupt").json()
    assert body["interrupted"] is True
    assert provisioner.interrupted == [session["id"]]


def test_interrupt_for_an_unknown_session_is_404(client):
    assert client.post("/sessions/ghost/interrupt").status_code == 404


# --- the asset itself ------------------------------------------------------


def test_console_js_has_no_control_characters():
    """A stray NUL truncates the file for every tool that reads it."""
    source = CONSOLE.joinpath("console.js").read_text(encoding="utf-8")
    stray = [c for c in source if ord(c) < 32 and c not in "\t\n"]
    assert stray == []


def test_console_js_persists_layout_on_the_server_not_in_localstorage():
    source = CONSOLE.joinpath("console.js").read_text(encoding="utf-8")
    assert "localStorage." not in source
    assert "frame_state" in source
    assert "sidebar_collapsed" in source


def test_console_js_only_calls_routes_the_server_serves(client):
    """Catch a pane wired to an endpoint that was never built."""
    source = CONSOLE.joinpath("console.js").read_text(encoding="utf-8")
    served = set()
    for route in client.app.routes:
        served.add(getattr(route, "path", ""))

    referenced = set(re.findall(r'"(/(?:sessions|users|surfaces|voice|console|auth)[^"]*)"', source))
    assert referenced, "no API paths found — the regex has drifted"
    for path in referenced:
        # JS builds paths by concatenation, so only the leading segment is
        # literal; a query string may be glued on the end of a fragment.
        stem = path.split("?")[0].strip("/").split("/")[0]
        assert any(r.startswith("/" + stem) for r in served), path
