import httpx
import pytest

import proxy as proxy_mod


@pytest.fixture
def user_id(client):
    response = client.post(
        "/identities", json={"surface": "web", "external_id": "7", "display_name": "Daniel"}
    )
    return response.json()["user_id"]


# --- unit ------------------------------------------------------------------


def test_target_url_carries_the_query_string():
    assert proxy_mod.target_url(9601, "/static/app.js", "v=2") == (
        "http://127.0.0.1:9601/static/app.js?v=2"
    )


def test_target_url_without_query():
    assert proxy_mod.target_url(9601, "") == "http://127.0.0.1:9601/"


def test_hop_by_hop_and_host_headers_are_stripped():
    kept = proxy_mod.forwardable(
        {
            "Host": "console.local",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
            "Content-Length": "12",
            "Accept": "text/html",
        }
    )
    assert kept == {"Accept": "text/html"}


def test_base_tag_goes_just_inside_head():
    out = proxy_mod.inject_base(b"<html><head><title>x</title></head>", "/sessions/s1/app/")
    assert out == b'<html><head><base href="/sessions/s1/app/"><title>x</title></head>'


def test_base_tag_is_prepended_when_there_is_no_head():
    out = proxy_mod.inject_base(b"<p>hi</p>", "/sessions/s1/app/")
    assert out.startswith(b'<base href="/sessions/s1/app/">')


def test_html_detection_ignores_charset_and_case():
    assert proxy_mod.is_html("text/html; charset=utf-8")
    assert proxy_mod.is_html("TEXT/HTML")
    assert not proxy_mod.is_html("application/json")


# --- through the route -----------------------------------------------------


def make_client(settings, registry, provisioner, voice, handler):
    from fastapi.testclient import TestClient

    from server import create_app

    app = create_app(
        settings=settings,
        registry=registry,
        provisioner=provisioner,
        voice=voice,
        proxy_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return TestClient(app)


def new_session(test_client, user_id):
    return test_client.post(f"/users/{user_id}/sessions", json={}).json()


def test_browser_pane_proxies_to_the_sessions_app_port(
    settings, registry, provisioner, voice, user_id, client
):
    session = new_session(client, user_id)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    with make_client(settings, registry, provisioner, voice, handler) as proxied:
        response = proxied.get(f"/sessions/{session['id']}/app/hello?x=1")

    assert response.status_code == 200
    assert response.text == "ok"
    port = registry.get_session(session["id"])["app_port"]
    assert seen["url"] == f"http://127.0.0.1:{port}/hello?x=1"


def test_browser_pane_injects_a_base_tag_into_html(
    settings, registry, provisioner, voice, user_id, client
):
    session = new_session(client, user_id)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<html><head></head><body>app</body></html>",
            headers={"content-type": "text/html"},
        )

    with make_client(settings, registry, provisioner, voice, handler) as proxied:
        response = proxied.get(f"/sessions/{session['id']}/app/")

    assert f'<base href="/sessions/{session["id"]}/app/">' in response.text


def test_browser_pane_forwards_the_request_body_and_method(
    settings, registry, provisioner, voice, user_id, client
):
    session = new_session(client, user_id)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = request.content
        return httpx.Response(201, text="created", headers={"content-type": "text/plain"})

    with make_client(settings, registry, provisioner, voice, handler) as proxied:
        response = proxied.post(f"/sessions/{session['id']}/app/submit", content=b"payload")

    assert response.status_code == 201
    assert seen["method"] == "POST"
    assert seen["body"] == b"payload"


def test_browser_pane_reports_an_unreachable_app_as_502(
    settings, registry, provisioner, voice, user_id, client
):
    session = new_session(client, user_id)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with make_client(settings, registry, provisioner, voice, handler) as proxied:
        response = proxied.get(f"/sessions/{session['id']}/app/")

    assert response.status_code == 502


def test_browser_pane_for_an_unknown_session_is_404(
    settings, registry, provisioner, voice, client
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    with make_client(settings, registry, provisioner, voice, handler) as proxied:
        assert proxied.get("/sessions/ghost/app/").status_code == 404
