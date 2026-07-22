import json

import pytest


@pytest.fixture
def user_id(client):
    response = client.post(
        "/identities", json={"surface": "telegram", "external_id": "42", "display_name": "Daniel"}
    )
    assert response.status_code == 200
    return response.json()["user_id"]


def make_session(client, user_id, **body):
    response = client.post(f"/users/{user_id}/sessions", json=body)
    assert response.status_code == 201
    return response.json()


def test_health_reports_the_backends_in_use(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["provisioner"] == "fake"
    assert body["voice"] == "fake"


def test_identity_resolution_is_stable(client, user_id):
    again = client.post("/identities", json={"surface": "telegram", "external_id": "42"})
    assert again.json()["user_id"] == user_id


def test_unknown_surface_is_rejected(client):
    response = client.post("/identities", json={"surface": "carrier-pigeon", "external_id": "1"})
    assert response.status_code == 400


def test_create_and_list_sessions(client, user_id):
    session = make_session(client, user_id, title="refactor", color="#ff8800")
    listed = client.get(f"/users/{user_id}/sessions").json()
    assert [s["id"] for s in listed] == [session["id"]]
    assert listed[0]["title"] == "refactor"
    assert listed[0]["color"] == "#ff8800"


def test_create_session_for_unknown_user_is_404(client):
    assert client.post("/users/ghost/sessions", json={}).status_code == 404


def test_get_unknown_session_is_404(client):
    assert client.get("/sessions/nope").status_code == 404


def test_rename_and_recolor_a_session(client, user_id):
    session = make_session(client, user_id)
    patched = client.patch(
        f"/sessions/{session['id']}", json={"title": "new name", "color": "#00ccff"}
    ).json()
    assert patched["title"] == "new name"
    assert patched["color"] == "#00ccff"


def test_patch_rejects_a_bad_frame_state(client, user_id):
    session = make_session(client, user_id)
    response = client.patch(f"/sessions/{session['id']}", json={"frame_state": "floating"})
    assert response.status_code == 400


def test_frame_state_and_speaker_persist_for_layout_restore(client, user_id):
    docked = make_session(client, user_id)
    minimized = make_session(client, user_id)
    make_session(client, user_id)  # never opened

    client.patch(f"/sessions/{docked['id']}", json={"frame_state": "docked", "speaker": True})
    client.patch(f"/sessions/{minimized['id']}", json={"frame_state": "minimized"})

    frames = {f["id"]: f for f in client.get(f"/users/{user_id}/frames").json()}
    assert set(frames) == {docked["id"], minimized["id"]}
    assert frames[docked["id"]]["frame_state"] == "docked"
    assert frames[docked["id"]]["speaker"] == 1
    assert frames[minimized["id"]]["frame_state"] == "minimized"


def test_sidebar_collapsed_state_round_trips(client):
    assert client.get("/surfaces/web/42/layout").json()["sidebar_collapsed"] is False
    client.patch("/surfaces/web/42/layout", json={"sidebar_collapsed": True})
    assert client.get("/surfaces/web/42/layout").json()["sidebar_collapsed"] is True


def test_turn_streams_ndjson_events_and_persists_resume_id(client, user_id):
    session = make_session(client, user_id)
    with client.stream("POST", f"/sessions/{session['id']}/turn", json={"prompt": "hello"}) as r:
        assert r.status_code == 200
        events = [json.loads(line) for line in r.iter_lines() if line.strip()]

    assert events[0]["kind"] == "session"
    assert events[-1]["kind"] == "result"
    assert "hello" in events[-1]["text"]
    assert client.get(f"/sessions/{session['id']}").json()["resume_id"] == events[0]["resume_id"]


def test_events_route_reads_back_a_finished_turn(client, user_id):
    """Nobody was attached while it ran; this is how you find out what happened."""
    session = make_session(client, user_id)
    with client.stream("POST", f"/sessions/{session['id']}/turn", json={"prompt": "hello"}) as r:
        [line for line in r.iter_lines()]

    body = client.get(f"/sessions/{session['id']}/events").json()
    assert body["outcome"] == "ok"
    assert body["running"] is False
    kinds = [event["kind"] for event in body["events"]]
    assert "result" in kinds
    assert body["last_seq"] == body["events"][-1]["seq"]


def test_events_route_pages_from_a_seq(client, user_id):
    session = make_session(client, user_id)
    with client.stream("POST", f"/sessions/{session['id']}/turn", json={"prompt": "hello"}) as r:
        [line for line in r.iter_lines()]

    everything = client.get(f"/sessions/{session['id']}/events").json()
    tail = client.get(
        f"/sessions/{session['id']}/events", params={"after_seq": everything["events"][0]["seq"]}
    ).json()
    assert [event["seq"] for event in tail["events"]] == [
        event["seq"] for event in everything["events"][1:]
    ]


def test_events_on_an_unknown_session_is_404(client):
    assert client.get("/sessions/ghost/events").status_code == 404


def test_deleting_a_session_takes_its_transcript_with_it(client, user_id):
    session = make_session(client, user_id)
    with client.stream("POST", f"/sessions/{session['id']}/turn", json={"prompt": "hello"}) as r:
        [line for line in r.iter_lines()]
    assert client.get(f"/sessions/{session['id']}/events").json()["events"]

    assert client.delete(f"/sessions/{session['id']}").status_code in (200, 204)
    assert client.get(f"/sessions/{session['id']}/events").status_code == 404


def test_turn_requires_a_nonempty_prompt(client, user_id):
    session = make_session(client, user_id)
    assert client.post(f"/sessions/{session['id']}/turn", json={"prompt": ""}).status_code == 422


def test_turn_on_an_unknown_session_is_404(client):
    assert client.post("/sessions/ghost/turn", json={"prompt": "hi"}).status_code == 404


def test_start_stop_archive_lifecycle(client, user_id):
    session = make_session(client, user_id)
    started = client.post(f"/sessions/{session['id']}/start").json()
    assert started["container_id"]

    stopped = client.post(f"/sessions/{session['id']}/stop").json()
    assert stopped["container_id"] is None
    assert stopped["status"] == "active"

    archived = client.post(f"/sessions/{session['id']}/archive").json()
    assert archived["status"] == "archived"
    assert client.get(f"/users/{user_id}/sessions", params={"status": "archived"}).json()


def test_starting_an_archived_session_is_409(client, user_id):
    session = make_session(client, user_id)
    client.post(f"/sessions/{session['id']}/archive")
    assert client.post(f"/sessions/{session['id']}/start").status_code == 409


def test_delete_session(client, user_id):
    session = make_session(client, user_id)
    assert client.delete(f"/sessions/{session['id']}").status_code == 204
    assert client.get(f"/sessions/{session['id']}").status_code == 404


def test_clone_url_hands_back_a_runnable_git_command(client, user_id):
    session = make_session(client, user_id)
    body = client.get(f"/sessions/{session['id']}/clone-url").json()
    assert body["branch"] == session["branch"]
    assert body["command"].startswith("git clone -b ")
    assert body["clone_url"].endswith("origin.git")


def test_diff_is_empty_before_any_push(client, user_id):
    session = make_session(client, user_id)
    body = client.get(f"/sessions/{session['id']}/diff").json()
    assert body["diff"] == ""
    assert body["branch"] == session["branch"]


def test_attach_switch_and_detach_a_chat(client, user_id):
    first = make_session(client, user_id)
    second = make_session(client, user_id)

    client.post("/surfaces/telegram/42/attach", json={"session_id": first["id"]})
    assert client.get("/surfaces/telegram/42/attach").json()["id"] == first["id"]

    client.post("/surfaces/telegram/42/attach", json={"session_id": second["id"]})
    assert client.get("/surfaces/telegram/42/attach").json()["id"] == second["id"]

    assert client.delete("/surfaces/telegram/42/attach").status_code == 204
    assert client.get("/surfaces/telegram/42/attach").status_code == 404


def test_attaching_to_an_unknown_session_is_404(client):
    assert client.post(
        "/surfaces/telegram/42/attach", json={"session_id": "ghost"}
    ).status_code == 404


def test_websocket_stream_returns_turn_events(client, user_id):
    session = make_session(client, user_id)
    with client.websocket_connect(f"/sessions/{session['id']}/stream") as socket:
        socket.send_json({"prompt": "ping"})
        events = []
        while True:
            event = socket.receive_json()
            events.append(event)
            if event["kind"] == "result":
                break
    assert events[0]["kind"] == "session"
    assert "ping" in events[-1]["text"]


def test_websocket_rejects_an_empty_prompt(client, user_id):
    session = make_session(client, user_id)
    with client.websocket_connect(f"/sessions/{session['id']}/stream") as socket:
        socket.send_json({"prompt": ""})
        assert socket.receive_json() == {"kind": "error", "text": "empty prompt"}


def test_voice_round_trip_through_the_fake_backend(client):
    spoken = client.post("/voice/speak", json={"text": "hold my beer"})
    assert spoken.status_code == 200
    assert spoken.headers["content-type"] == "audio/mpeg"

    transcribed = client.post(
        "/voice/transcribe", files={"file": ("voice.ogg", spoken.content, "audio/ogg")}
    )
    assert transcribed.json()["text"] == "hold my beer"


def test_models_falls_back_to_default_without_a_proxy(client):
    """No proxy configured in tests, so /models degrades to the default model
    rather than erroring — the picker still works offline."""
    body = client.get("/models?harness=claude").json()
    assert body["source"] == "fallback"
    assert body["default"] == "opus"
    assert body["models"] == [{"id": "opus"}]


def test_models_requires_authentication(anon_client):
    assert anon_client.get("/models").status_code == 401
