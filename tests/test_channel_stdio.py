"""The shim's MCP wiring, spoken over real stdio.

`test_channel.py` covers the relay's logic with the MCP layer stubbed out. That
left the wiring itself untested, and it was wrong: the first implementation sent
notifications through `server.request_context`, which doesn't exist outside a
request handler, so every event vanished silently. These tests spawn
`sandbox/channel.py` as a subprocess and speak JSON-RPC to it, which is exactly
what Claude Code does.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHIM = ROOT / "sandbox" / "channel.py"
READ_TIMEOUT = 20.0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeControlPlane:
    """Serves one batch of queued events, then nothing. Records replies."""

    def __init__(self, events: list[dict]) -> None:
        self.port = free_port()
        self.replies: list[dict] = []
        self._events = events
        self._served = False
        plane = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                batch = [] if plane._served else plane._events
                plane._served = True
                self._respond({"events": batch})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                plane.replies.append(json.loads(self.rfile.read(length) or b"{}"))
                self._respond({"ok": True})

            def _respond(self, payload: dict) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args) -> None:
                return None

        self._server = HTTPServer(("127.0.0.1", self.port), Handler)

    def __enter__(self) -> "FakeControlPlane":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()


class Shim:
    """The channel subprocess, with line-oriented JSON-RPC helpers."""

    def __init__(self, port: int) -> None:
        env = dict(
            os.environ,
            FRAME_CHANNEL_URL=f"http://127.0.0.1:{port}",
            FRAME_SESSION_ID="sess-test",
        )
        self.process = subprocess.Popen(
            [sys.executable, str(SHIM)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
            cwd=str(ROOT),
        )
        self._lines: list[str] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            line = line.strip()
            if line:
                with self._lock:
                    self._lines.append(line)

    def send(self, message: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def await_message(self, match) -> dict:
        """Next stdout message satisfying `match`, or fail the test."""
        deadline = threading.Event()
        timer = threading.Timer(READ_TIMEOUT, deadline.set)
        timer.start()
        try:
            seen = 0
            while not deadline.is_set():
                with self._lock:
                    pending = self._lines[seen:]
                    seen = len(self._lines)
                for line in pending:
                    message = json.loads(line)
                    if match(message):
                        return message
                deadline.wait(0.05)
        finally:
            timer.cancel()
        raise AssertionError(f"no matching message within {READ_TIMEOUT}s; saw {self._lines}")

    def initialize(self) -> dict:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
        )
        result = self.await_message(lambda m: m.get("id") == 1)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


@pytest.fixture
def shim():
    started: list[Shim] = []

    def start(events: list[dict] | None = None):
        plane = FakeControlPlane(events or [])
        plane.__enter__()
        instance = Shim(plane.port)
        started.append(instance)
        return instance, plane

    try:
        yield start
    finally:
        for instance in started:
            instance.close()


def test_shim_declares_the_channel_capability(shim):
    instance, plane = shim()
    with plane:
        result = instance.initialize()

    capabilities = result["result"]["capabilities"]
    assert capabilities["experimental"] == {"claude/channel": {}}


def test_shim_advertises_the_reply_tool(shim):
    instance, plane = shim()
    with plane:
        instance.initialize()
        instance.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        result = instance.await_message(lambda m: m.get("id") == 2)

    names = [tool["name"] for tool in result["result"]["tools"]]
    assert names == ["reply"]


def test_shim_emits_queued_events_as_channel_notifications(shim):
    instance, plane = shim([{"content": "ci failed", "meta": {"run_id": "7"}}])
    with plane:
        instance.initialize()
        message = instance.await_message(
            lambda m: m.get("method") == "notifications/claude/channel"
        )

    # The shape Claude Code parses into a <channel> tag.
    assert message["params"] == {"content": "ci failed", "meta": {"run_id": "7"}}


def test_shim_drops_non_identifier_meta_keys_on_the_wire(shim):
    instance, plane = shim([{"content": "hi", "meta": {"chat-id": "1", "chat_id": "2"}}])
    with plane:
        instance.initialize()
        message = instance.await_message(
            lambda m: m.get("method") == "notifications/claude/channel"
        )

    assert message["params"]["meta"] == {"chat_id": "2"}


def test_shim_reply_tool_posts_to_the_control_plane(shim):
    instance, plane = shim()
    with plane:
        instance.initialize()
        instance.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "reply", "arguments": {"chat_id": "7", "text": "done"}},
            }
        )
        result = instance.await_message(lambda m: m.get("id") == 3)

        assert plane.replies == [{"chat_id": "7", "text": "done"}]

    assert result["result"]["content"][0]["text"] == "sent"
