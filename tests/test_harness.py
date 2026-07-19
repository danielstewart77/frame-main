import json

import pytest

import harness


def test_claude_argv_streams_json_and_carries_model():
    argv = harness.build_argv("claude", "do the thing", "opus")
    assert argv[:3] == ["claude", "-p", "do the thing"]
    assert "--output-format" in argv and "stream-json" in argv
    assert argv[argv.index("--model") + 1] == "opus"
    assert "--resume" not in argv


def test_claude_argv_resumes_and_appends_system_prompt():
    argv = harness.build_argv("claude", "go", "opus", resume_id="sess-9", system_prompt="be brief")
    assert argv[argv.index("--resume") + 1] == "sess-9"
    assert argv[argv.index("--append-system-prompt") + 1] == "be brief"


def test_codex_argv_is_json_and_ends_with_the_prompt():
    argv = harness.build_argv("codex", "go", "gpt-5")
    assert argv[:2] == ["codex", "exec"]
    assert "--json" in argv
    assert argv[-1] == "go"


def test_unknown_harness_is_rejected():
    with pytest.raises(harness.UnknownHarness):
        harness.build_argv("hal9000", "open the door", "opus")


def test_claude_init_event_yields_the_resume_id():
    line = json.dumps({"type": "system", "subtype": "init", "session_id": "abc-123"})
    assert harness.parse_line("claude", line) == {"kind": "session", "resume_id": "abc-123"}


def test_claude_text_delta_becomes_a_text_event():
    line = json.dumps(
        {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "hi"}}}
    )
    assert harness.parse_line("claude", line) == {"kind": "text", "text": "hi"}


def test_claude_tool_use_becomes_a_tool_event():
    line = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit"}]}}
    )
    assert harness.parse_line("claude", line) == {"kind": "tool", "name": "Edit"}


def test_claude_result_and_error_are_distinguished():
    ok = json.dumps({"type": "result", "result": "done"})
    bad = json.dumps({"type": "result", "is_error": True, "result": "boom"})
    assert harness.parse_line("claude", ok) == {"kind": "result", "text": "done"}
    assert harness.parse_line("claude", bad) == {"kind": "error", "text": "boom"}


def test_codex_session_and_completion_events():
    created = json.dumps({"type": "session.created", "session_id": "cx-1"})
    done = json.dumps({"type": "turn.completed", "last_agent_message": "finished"})
    assert harness.parse_line("codex", created) == {"kind": "session", "resume_id": "cx-1"}
    assert harness.parse_line("codex", done) == {"kind": "result", "text": "finished"}


def test_blank_lines_are_dropped_and_garbage_passes_through_as_text():
    assert harness.parse_line("claude", "   ") is None
    assert harness.parse_line("claude", "not json") == {"kind": "text", "text": "not json"}


def test_collect_text_prefers_the_result_over_deltas():
    events = [
        {"kind": "text", "text": "par"},
        {"kind": "text", "text": "tial"},
        {"kind": "result", "text": "final answer"},
    ]
    assert harness.collect_text(events) == "final answer"


def test_collect_text_falls_back_to_joined_deltas():
    events = [{"kind": "text", "text": "par"}, {"kind": "text", "text": "tial"}]
    assert harness.collect_text(events) == "partial"


def test_parse_stream_skips_blanks():
    lines = ["", json.dumps({"type": "result", "result": "ok"}), "  "]
    assert list(harness.parse_stream("claude", lines)) == [{"kind": "result", "text": "ok"}]


def test_api_retry_becomes_a_status_event():
    line = json.dumps(
        {"type": "system", "subtype": "api_retry", "attempt": 3, "max_retries": 10}
    )
    assert harness.parse_line("claude", line) == {
        "kind": "status",
        "text": "retrying provider (3/10)",
    }


def test_system_status_becomes_a_status_event():
    line = json.dumps({"type": "system", "subtype": "status", "status": "requesting"})
    assert harness.parse_line("claude", line) == {"kind": "status", "text": "requesting"}


def test_status_events_do_not_pollute_collected_text():
    events = [
        {"kind": "status", "text": "requesting"},
        {"kind": "text", "text": "answer"},
    ]
    assert harness.collect_text(events) == "answer"


def test_unknown_system_subtype_still_passes_through_as_raw():
    line = json.dumps({"type": "system", "subtype": "compact_boundary"})
    assert harness.parse_line("claude", line)["kind"] == "raw"
