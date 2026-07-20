"""One long-lived harness process per session, fed turns over stdin.

A turn used to be a process: `docker exec` the harness with the prompt on the
command line, read its output, watch it exit. That model can't be woken. A
channel event arrives for a session with nothing running, and there is nothing
to deliver it to — the notification listener only exists while the harness is
up.

So the harness stays up. One `docker exec -i` per session, prompts written to
its stdin as stream-json lines, output read continuously by one reader task.
Anything the harness emits while no turn is outstanding is unsolicited — a
channel wake, a background job reporting in — and goes to the session's bus
through `on_unsolicited` rather than being attributed to a requester that
doesn't exist.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any, AsyncIterator, Awaitable, Callable

import harness as harness_mod

# Kinds that close out a turn. `session` and `text` are mid-turn; these are not.
TERMINAL_KINDS = frozenset({"result", "error"})

# How long to wait for the process to exit after stdin closes before killing it.
SHUTDOWN_GRACE_SECONDS = 5.0

Spawn = Callable[[], Awaitable[asyncio.subprocess.Process]]
Sink = Callable[[dict[str, Any]], None]


class HarnessProcessError(RuntimeError):
    pass


class Turn:
    """Events belonging to one outstanding request, in order, until it ends."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.done = False

    def _put(self, event: dict[str, Any] | None) -> None:
        self._queue.put_nowait(event)

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event


class HarnessProcess:
    """The session's harness: spawned once, fed many turns."""

    def __init__(
        self,
        session_id: str,
        harness: str,
        spawn: Spawn,
        on_unsolicited: Sink | None = None,
    ) -> None:
        if not harness_mod.supports_stdin(harness):
            raise HarnessProcessError(f"{harness!r} cannot be driven over stdin")
        self.session_id = session_id
        self.harness = harness
        self._spawn = spawn
        self._on_unsolicited = on_unsolicited
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        # Only one requester at a time: the harness runs turns in sequence, so a
        # second prompt written now would have its output interleaved with the
        # first turn's and be indistinguishable from it.
        self._send_lock = asyncio.Lock()
        self._turn: Turn | None = None
        self._interrupts = itertools.count(1)
        self.resume_id: str | None = None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        if self._process is not None:
            return
        self._process = await self._spawn()
        if self._process.stdin is None or self._process.stdout is None:
            raise HarnessProcessError("harness process needs piped stdin and stdout")
        self._reader = asyncio.create_task(self._pump())

    async def turn(self, prompt: str) -> AsyncIterator[dict[str, Any]]:
        """Send a prompt and yield that turn's events until it finishes."""
        async with self._send_lock:
            await self.start()
            if not self.alive:
                raise HarnessProcessError("harness process is not running")
            turn = Turn()
            self._turn = turn
            assert self._process is not None and self._process.stdin is not None
            try:
                self._process.stdin.write(harness_mod.encode_turn(self.harness, prompt).encode())
                await self._process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                self._turn = None
                raise HarnessProcessError("harness process closed its stdin") from exc
            try:
                async for event in turn:
                    yield event
            finally:
                self._turn = None

    async def interrupt(self) -> bool:
        """Cut the in-flight turn short, leaving the process and its context up."""
        if self._turn is None or not self.alive:
            return False
        assert self._process is not None and self._process.stdin is not None
        line = harness_mod.encode_interrupt(self.harness, f"int-{next(self._interrupts)}")
        try:
            self._process.stdin.write(line.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return False
        return True

    async def close(self) -> None:
        """Shut the harness down: stdin first, then signals if it won't go."""
        process, self._process = self._process, None
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if process is None:
            return
        if process.returncode is None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            try:
                await asyncio.wait_for(process.wait(), SHUTDOWN_GRACE_SECONDS)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        self._deliver({"kind": "status", "text": "harness stopped"}, terminal=True)

    # --- reader ------------------------------------------------------------

    async def _pump(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            async for raw in process.stdout:
                event = harness_mod.parse_line(self.harness, raw.decode("utf-8", "replace"))
                if event is None:
                    continue
                if event["kind"] == "session" and event.get("resume_id"):
                    self.resume_id = event["resume_id"]
                self._deliver(event, terminal=event["kind"] in TERMINAL_KINDS)
        except asyncio.CancelledError:
            raise
        finally:
            await self._on_exit(process)

    async def _on_exit(self, process: asyncio.subprocess.Process) -> None:
        """The harness died on its own — say why rather than just going quiet."""
        if self._process is not process:  # a deliberate close(), already reported
            return
        await process.wait()
        if process.returncode in (0, -15):
            self._deliver({"kind": "status", "text": "harness exited"}, terminal=True)
            return
        stderr = b""
        if process.stderr is not None:
            stderr = await process.stderr.read()
        text = stderr.decode("utf-8", "replace").strip()
        self._deliver(
            {"kind": "error", "text": text or f"harness exited {process.returncode}"},
            terminal=True,
        )

    def _deliver(self, event: dict[str, Any], terminal: bool = False) -> None:
        turn = self._turn
        if turn is None:
            # Nobody asked for this — a channel woke the session up.
            if self._on_unsolicited is not None:
                self._on_unsolicited(event)
            return
        turn._put(event)
        if terminal:
            turn.done = True
            turn._put(None)
            self._turn = None
