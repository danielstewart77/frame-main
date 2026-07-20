import pytest

from sandbox.provision import FakeTty


@pytest.fixture
def user_id(client):
    response = client.post(
        "/identities", json={"surface": "web", "external_id": "9", "display_name": "Daniel"}
    )
    return response.json()["user_id"]


@pytest.fixture
def session(client, user_id):
    return client.post(f"/users/{user_id}/sessions", json={}).json()


# --- the fake tty itself ---------------------------------------------------


async def test_fake_tty_greets_then_echoes():
    tty = FakeTty("fake-abc")
    assert b"[fake tty fake-abc]" in await tty.read()
    await tty.write(b"ls")
    assert await tty.read() == b"ls\r\n$ "


async def test_fake_tty_records_resize_and_stops_reading_once_closed():
    tty = FakeTty("fake-abc")
    tty.resize(40, 120)
    assert tty.size == (40, 120)
    await tty.close()
    assert tty.closed
    assert await tty.read() == b""


async def test_attaching_a_tty_provisions_the_container(manager, registry, user):
    session = manager.create(user["user_id"])
    tty = await manager.attach_tty(session["id"])
    assert registry.get_session(session["id"])["container_id"]
    assert isinstance(tty, FakeTty)


# --- through the socket ----------------------------------------------------


def test_tui_socket_streams_the_shell_banner(client, session):
    with client.websocket_connect(f"/sessions/{session['id']}/tui") as socket:
        assert "[fake tty" in socket.receive_text()


def test_tui_socket_forwards_keystrokes_and_returns_output(client, session):
    with client.websocket_connect(f"/sessions/{session['id']}/tui") as socket:
        socket.receive_text()  # banner
        socket.send_json({"data": "whoami"})
        assert socket.receive_text() == "whoami\r\n$ "


def test_tui_socket_applies_a_resize(client, session, provisioner):
    with client.websocket_connect(f"/sessions/{session['id']}/tui") as socket:
        socket.receive_text()
        socket.send_json({"resize": {"rows": 50, "cols": 200}})
        socket.send_json({"data": "x"})
        socket.receive_text()
    assert provisioner.ttys[-1].size == (50, 200)


def test_tui_socket_closes_the_tty_on_disconnect(client, session, provisioner):
    with client.websocket_connect(f"/sessions/{session['id']}/tui") as socket:
        socket.receive_text()
    assert provisioner.ttys[-1].closed


def test_tui_socket_rejects_an_unknown_session(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/sessions/ghost/tui") as socket:
            socket.receive_text()
    assert excinfo.value.code == 4404
