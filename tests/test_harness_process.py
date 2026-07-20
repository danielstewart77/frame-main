"""The long-lived, stdin-driven harness process.

Driven against a real subprocess rather than a stubbed stream: the whole point
of this class is process wiring, and a fake would prove nothing about it.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from sandbox.harness_process import HarnessProcess, HarnessProcessError

# A stand-in harness speaking Claude's stream-json in both directions.
FAKE_HARNESS = """
import json, sys
print(json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"}), flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    if message.get("type") == "control_request":
        print(json.dumps({"type": "result", "result": "interrupted"}), flush=True)
        continue
    text = message["message"]["content"][0]["text"]
    if text == "boom":
        sys.stderr.write("harness fell over\\n")
        sys.exit(3)
    print(json.dumps({
        "type": "stream_event",
        "event": {"delta": {"type": "text_delta", "text": text.upper()}},
    }), flush=True)
    print(json.dumps({"type": "result", "result": text.upper()}), flush=True)
"""


def make_process(script: str = FAKE_HARNESS, sink=None) -> HarnessProcess:
    spawns: list[int] = []

    async def spawn() -> asyncio.subprocess.Process:
        spawns.append(1)
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    process = HarnessProcess("session-1", "claude", spawn, sink)
    process.spawns = spawns  # type: ignore[attr-defined]
    return process


async def collect(process: HarnessProcess, prompt: str, timeout: float = 10.0) -> list[dict]:
    async def run() -> list[dict]:
        return [event async for event in process.turn(prompt)]

    return await asyncio.wait_for(run(), timeout)


async def until(predicate, timeout: float = 10.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


# --- turns -----------------------------------------------------------------


async def test_a_turn_yields_its_events_and_ends_at_the_result():
    process = make_process()
    try:
        events = await collect(process, "hello")
    finally:
        await process.close()

    assert [event["kind"] for event in events][-2:] == ["text", "result"]
    assert events[-1]["text"] == "HELLO"


async def test_successive_turns_reuse_the_one_process():
    process = make_process()
    try:
        first = await collect(process, "one")
        second = await collect(process, "two")
    finally:
        await process.close()

    assert first[-1]["text"] == "ONE"
    assert second[-1]["text"] == "TWO"
    assert len(process.spawns) == 1  # type: ignore[attr-defined]


async def test_the_process_survives_a_finished_turn():
    process = make_process()
    try:
        await collect(process, "hello")
        assert process.alive
    finally:
        await process.close()


# --- unsolicited output ----------------------------------------------------


async def test_output_with_no_turn_outstanding_goes_to_the_sink():
    """The wake path: the harness speaks and nobody prompted it."""
    seen: list[dict] = []
    process = make_process(sink=seen.append)
    try:
        await process.start()
        # The init line lands before any prompt, exactly as a real harness does.
        await until(lambda: any(event["kind"] == "session" for event in seen))
    finally:
        await process.close()

    assert seen[0] == {"kind": "session", "resume_id": "sess-1"}
    assert process.resume_id == "sess-1"


async def test_a_turn_takes_precedence_over_the_sink():
    seen: list[dict] = []
    process = make_process(sink=seen.append)
    try:
        await process.start()
        await until(lambda: bool(seen))
        events = await collect(process, "hello")
    finally:
        await process.close()

    assert [event["kind"] for event in events] == ["text", "result"]
    assert not [event for event in seen if event["kind"] in ("text", "result")]


# --- interrupt and death ---------------------------------------------------


async def test_interrupt_ends_the_turn_without_killing_the_process():
    """Signalling the process would take the session's context with it."""
    process = make_process(script=FAKE_HARNESS.replace('"type": "stream_event"', '"type": "noop"'))
    try:
        await process.start()
        stream = process.turn("hello").__aiter__()
        await asyncio.wait_for(stream.__anext__(), 10)  # the raw noop event

        assert await process.interrupt() is True
        assert process.alive
    finally:
        await process.close()


async def test_interrupt_with_no_turn_running_is_false():
    process = make_process()
    try:
        await process.start()
        assert await process.interrupt() is False
    finally:
        await process.close()


async def test_a_harness_that_dies_says_why():
    seen: list[dict] = []
    process = make_process(sink=seen.append)
    try:
        events = await collect(process, "boom")
    finally:
        await process.close()

    assert events[-1]["kind"] == "error"
    assert "harness fell over" in events[-1]["text"]


async def test_a_turn_after_the_process_died_is_refused():
    process = make_process()
    try:
        await collect(process, "boom")
        with pytest.raises(HarnessProcessError):
            await collect(process, "again")
    finally:
        await process.close()


async def test_close_is_idempotent():
    process = make_process()
    await process.start()
    await process.close()
    await process.close()
    assert not process.alive


async def test_a_harness_that_cannot_read_stdin_is_refused():
    async def spawn() -> asyncio.subprocess.Process:  # pragma: no cover - never called
        raise AssertionError("should not spawn")

    with pytest.raises(HarnessProcessError):
        HarnessProcess("session-1", "codex", spawn)
