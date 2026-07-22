"""Container provisioning — the only place that touches the Docker socket.

The agent never provisions anything; the control plane does. Two
implementations behind one interface:

  DockerProvisioner  real `docker run` / `docker exec` / `docker rm`
  FakeProvisioner    no daemon, deterministic synthetic stream

`FRAME_PROVISIONER=fake` is what the offline box runs, so every layer above
this file is exercised end to end without Docker or a provider account.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import shlex
import struct
import termios
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Protocol

import harness as harness_mod
from sandbox.harness_process import HarnessProcess, HarnessProcessError


class ProvisionError(RuntimeError):
    pass


@dataclass
class Container:
    container_id: str
    app_port: int | None


class Tty(Protocol):
    """An interactive shell attached to a session's container."""

    async def read(self) -> bytes: ...

    async def write(self, data: bytes) -> None: ...

    def resize(self, rows: int, cols: int) -> None: ...

    async def close(self) -> None: ...


class Provisioner(Protocol):
    # Set by the SessionManager: where output nobody requested is published.
    on_unsolicited: Callable[[str, dict[str, Any]], None] | None

    async def provision(
        self, session: dict[str, Any], workspace: Any, env: dict[str, str]
    ) -> Container: ...

    async def run_turn(
        self,
        session: dict[str, Any],
        prompt: str,
        system_prompt: str = "",
        channel_config: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def attach_tty(self, container_id: str) -> Tty: ...

    async def interrupt(self, session_id: str) -> bool: ...

    async def stop(self, container_id: str) -> None: ...

    async def remove(self, container_id: str) -> None: ...

    async def live_sessions(self) -> set[str]: ...

    async def remove_session(self, session_id: str) -> None: ...


def allocate_port(used: set[int], port_range: tuple[int, int]) -> int | None:
    low, high = port_range
    for port in range(low, high + 1):
        if port not in used:
            return port
    return None


# --- real ------------------------------------------------------------------


class DockerProvisioner:
    """One pristine container per session, from the base sandbox image."""

    def __init__(self, image: str, port_range: tuple[int, int] = (9600, 9699)):
        self.image = image
        self.port_range = port_range
        # Run the container as the host user the control plane runs as, so work
        # pushed into the mounted bare repo is owned by the operator, not root.
        self._user = f"{os.getuid()}:{os.getgid()}"
        # The turn currently executing per session, so it can be interrupted.
        # Only used by the one-shot path; stdin-driven harnesses interrupt in-band.
        self._running: dict[str, asyncio.subprocess.Process] = {}
        # The long-lived harness per session, for harnesses that read stdin.
        self._harnesses: dict[str, HarnessProcess] = {}
        self._harness_containers: dict[str, str] = {}
        self.on_unsolicited: Callable[[str, dict[str, Any]], None] | None = None

    async def provision(
        self, session: dict[str, Any], workspace: Any, env: dict[str, str]
    ) -> Container:
        app_port = env.pop("_app_port", None)
        app_port = int(app_port) if app_port else None
        name = f"frame-{session['id'][:12]}"

        argv = [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            # Run as the host user so pushed work is not root-owned on the host.
            "--user",
            self._user,
            # Stamp the session id on the container so a restart can reconcile
            # the table against what docker actually still has running.
            "--label",
            f"frame.session={session['id']}",
            # so the channel shim can call FRAME_CHANNEL_URL back on the host.
            # (The inference proxy is reached by its own DNS name, not this.)
            "--add-host",
            "host.docker.internal:host-gateway",
            "-v",
            f"{workspace.origin}:/origin.git",
            "-v",
            f"{workspace.memory_db}:/workspace/memory.db",
            "-v",
            f"{workspace.identity}:/workspace/identity.md:ro",
            "-v",
            f"{workspace.transcripts}:/workspace/transcripts",
        ]
        if app_port:
            argv += ["-p", f"127.0.0.1:{app_port}:3000"]
        for key, value in env.items():
            argv += ["-e", f"{key}={value}"]
        argv += [self.image]

        code, out, err = await _run(argv)
        if code != 0:
            raise ProvisionError(f"docker run failed: {err.strip() or out.strip()}")
        return Container(container_id=out.strip(), app_port=app_port)

    async def run_turn(
        self,
        session: dict[str, Any],
        prompt: str,
        system_prompt: str = "",
        channel_config: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        container_id = session.get("container_id")
        if not container_id:
            raise ProvisionError("session has no container")
        if harness_mod.supports_stdin(session["harness"]):
            process = await self._harness(session, system_prompt, channel_config)
            try:
                async for event in process.turn(prompt):
                    yield event
            except HarnessProcessError as exc:
                self._harnesses.pop(session["id"], None)
                yield {"kind": "error", "text": str(exc)}
            return
        argv = harness_mod.build_argv(
            session["harness"],
            prompt,
            session["model"],
            resume_id=session.get("resume_id"),
            system_prompt=system_prompt,
            channel_config=channel_config,
        )
        command = " ".join(shlex.quote(a) for a in argv)
        docker_argv = [
            "docker", "exec", "-u", self._user, "-w", "/workspace/repo",
            container_id, "bash", "-lc", command,
        ]

        process = await asyncio.create_subprocess_exec(
            *docker_argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        self._running[session["id"]] = process
        try:
            async for line in process.stdout:
                event = harness_mod.parse_line(session["harness"], line.decode("utf-8", "replace"))
                if event is not None:
                    yield event
            await process.wait()
        finally:
            self._running.pop(session["id"], None)
        if process.returncode not in (0, -15):
            stderr = b""
            if process.stderr is not None:
                stderr = await process.stderr.read()
            yield {
                "kind": "error",
                "text": stderr.decode("utf-8", "replace").strip() or "harness exited nonzero",
            }

    async def _harness(
        self,
        session: dict[str, Any],
        system_prompt: str,
        channel_config: str | None,
    ) -> HarnessProcess:
        """The session's long-lived harness, spawning it the first time."""
        existing = self._harnesses.get(session["id"])
        if existing is not None and existing.alive:
            return existing
        if existing is not None:
            await existing.close()

        container_id = session["container_id"]
        argv = harness_mod.build_argv(
            session["harness"],
            None,
            session["model"],
            resume_id=session.get("resume_id"),
            system_prompt=system_prompt,
            channel_config=channel_config,
        )
        command = " ".join(shlex.quote(a) for a in argv)
        docker_argv = [
            "docker", "exec", "-i", "-u", self._user, "-w", "/workspace/repo",
            container_id, "bash", "-lc", command,
        ]

        async def spawn() -> asyncio.subprocess.Process:
            return await asyncio.create_subprocess_exec(
                *docker_argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        session_id = session["id"]

        def unsolicited(event: dict[str, Any]) -> None:
            if self.on_unsolicited is not None:
                self.on_unsolicited(session_id, event)

        process = HarnessProcess(session_id, session["harness"], spawn, unsolicited)
        await process.start()
        self._harnesses[session_id] = process
        self._harness_containers[session_id] = container_id
        return process

    async def interrupt(self, session_id: str) -> bool:
        """Cut the in-flight turn short. False if nothing was running."""
        harness_process = self._harnesses.get(session_id)
        if harness_process is not None:
            return await harness_process.interrupt()
        process = self._running.get(session_id)
        if process is None or process.returncode is not None:
            return False
        process.terminate()
        return True

    async def attach_tty(self, container_id: str) -> Tty:
        """A real pty into the container, so TUIs and slash-commands work."""
        return await DockerTty.open(container_id, user=self._user)

    async def stop(self, container_id: str) -> None:
        await self._close_harness_for(container_id)
        await _run(["docker", "stop", container_id])

    async def remove(self, container_id: str) -> None:
        await self._close_harness_for(container_id)
        await _run(["docker", "rm", "-f", container_id])

    async def live_sessions(self) -> set[str]:
        """The session ids docker is still running a container for.

        Read off the `frame.session` label, so it survives frame-main losing
        the container ids it held in memory across a restart.
        """
        code, out, _ = await _run(
            ["docker", "ps", "--filter", "label=frame.session",
             "--format", '{{.Label "frame.session"}}']
        )
        if code != 0:
            return set()
        return {line.strip() for line in out.splitlines() if line.strip()}

    async def remove_session(self, session_id: str) -> None:
        """Kill and delete a session's container by its label, id unknown."""
        code, out, _ = await _run(
            ["docker", "ps", "-aq", "--filter", f"label=frame.session={session_id}"]
        )
        if code != 0:
            return
        for container_id in (line.strip() for line in out.splitlines() if line.strip()):
            await self.remove(container_id)

    async def _close_harness_for(self, container_id: str) -> None:
        """A harness outlives its turns but not its container."""
        for session_id, container in list(self._harness_containers.items()):
            if container != container_id:
                continue
            self._harness_containers.pop(session_id, None)
            process = self._harnesses.pop(session_id, None)
            if process is not None:
                await process.close()


class DockerTty:
    """`docker exec -it` behind a pty pair, pumped over asyncio.

    A pipe isn't enough: the harness's TUI and any curses program need a real
    terminal to size themselves against and to switch to raw mode.
    """

    def __init__(self, process: asyncio.subprocess.Process, master_fd: int) -> None:
        self.process = process
        self.master_fd = master_fd
        self._loop = asyncio.get_event_loop()
        self._closed = False

    @classmethod
    async def open(
        cls, container_id: str, command: str = "bash -l", user: str | None = None
    ) -> "DockerTty":
        master_fd, slave_fd = pty.openpty()
        user_flag = ["-u", user] if user else []
        try:
            process = await asyncio.create_subprocess_exec(
                "docker", "exec", "-it", *user_flag, container_id, "bash", "-lc", command,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
            )
        except OSError as exc:
            os.close(master_fd)
            os.close(slave_fd)
            raise ProvisionError(f"tty attach failed: {exc}") from exc
        os.close(slave_fd)
        os.set_blocking(master_fd, False)
        return cls(process, master_fd)

    async def read(self) -> bytes:
        """Next chunk of terminal output; empty bytes once the shell is gone."""
        future: asyncio.Future[bytes] = self._loop.create_future()

        def on_readable() -> None:
            if future.done():
                return
            try:
                data = os.read(self.master_fd, 65536)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            self._loop.remove_reader(self.master_fd)
            future.set_result(data)

        if self._closed:
            return b""
        self._loop.add_reader(self.master_fd, on_readable)
        try:
            return await future
        finally:
            if not self._closed:
                self._loop.remove_reader(self.master_fd)

    async def write(self, data: bytes) -> None:
        if not self._closed:
            os.write(self.master_fd, data)

    def resize(self, rows: int, cols: int) -> None:
        if self._closed:
            return
        fcntl.ioctl(
            self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._loop.remove_reader(self.master_fd)
        except (ValueError, OSError):
            pass
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
        os.close(self.master_fd)


# --- fake ------------------------------------------------------------------


class FakeProvisioner:
    """In-process stand-in. Records calls so tests can assert on lifecycle."""

    def __init__(self, port_range: tuple[int, int] = (9600, 9699)):
        self.port_range = port_range
        self.provisioned: dict[str, Container] = {}
        self.stopped: list[str] = []
        self.removed: list[str] = []
        self.turns: list[tuple[str, str]] = []
        self.channel_configs: list[str | None] = []
        self.ttys: list["FakeTty"] = []
        self.interrupted: list[str] = []
        self.on_unsolicited: Callable[[str, dict[str, Any]], None] | None = None
        self._counter = 0
        # container_id -> session_id for containers docker would still list.
        # Survives a new SessionManager over the same provisioner, the way a
        # real container survives frame-main restarting; a test drops an entry
        # to model a container that died while frame-main was down.
        self._live: dict[str, str] = {}

    def emit_unsolicited(self, session_id: str, event: dict[str, Any]) -> None:
        """Stand in for the harness speaking with nobody having prompted it."""
        if self.on_unsolicited is not None:
            self.on_unsolicited(session_id, event)

    async def provision(
        self, session: dict[str, Any], workspace: Any, env: dict[str, str]
    ) -> Container:
        workspace.ensure()
        self._counter += 1
        app_port = env.pop("_app_port", None)
        container = Container(
            container_id=f"fake-{session['id'][:8]}-{self._counter}",
            app_port=int(app_port) if app_port else None,
        )
        self.provisioned[session["id"]] = container
        self._live[container.container_id] = session["id"]
        return container

    async def run_turn(
        self,
        session: dict[str, Any],
        prompt: str,
        system_prompt: str = "",
        channel_config: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        if not session.get("container_id"):
            raise ProvisionError("session has no container")
        self.turns.append((session["id"], prompt))
        self.channel_configs.append(channel_config)
        if not session.get("resume_id"):
            yield {"kind": "session", "resume_id": f"resume-{session['id'][:8]}"}
        yield {"kind": "text", "text": f"[fake {session['harness']}] "}
        yield {"kind": "text", "text": prompt}
        yield {"kind": "result", "text": f"[fake {session['harness']}] {prompt}"}

    async def attach_tty(self, container_id: str) -> Tty:
        tty = FakeTty(container_id)
        self.ttys.append(tty)
        return tty

    async def interrupt(self, session_id: str) -> bool:
        self.interrupted.append(session_id)
        return session_id in self.provisioned

    async def stop(self, container_id: str) -> None:
        self.stopped.append(container_id)
        self._live.pop(container_id, None)

    async def remove(self, container_id: str) -> None:
        self.removed.append(container_id)
        self._live.pop(container_id, None)

    async def live_sessions(self) -> set[str]:
        return set(self._live.values())

    async def remove_session(self, session_id: str) -> None:
        for container_id in [c for c, s in self._live.items() if s == session_id]:
            await self.remove(container_id)


class FakeTty:
    """Echoing stand-in for a container shell — no Docker, no pty."""

    def __init__(self, container_id: str) -> None:
        self.container_id = container_id
        self.size: tuple[int, int] | None = None
        self.closed = False
        self._outbox: asyncio.Queue[bytes] = asyncio.Queue()
        self._outbox.put_nowait(f"[fake tty {container_id}]\r\n$ ".encode())

    async def read(self) -> bytes:
        if self.closed:
            return b""
        return await self._outbox.get()

    async def write(self, data: bytes) -> None:
        if not self.closed:
            await self._outbox.put(data + b"\r\n$ ")

    def resize(self, rows: int, cols: int) -> None:
        self.size = (rows, cols)

    async def close(self) -> None:
        self.closed = True
        await self._outbox.put(b"")


def get_provisioner(kind: str, image: str, port_range: tuple[int, int]) -> Provisioner:
    if kind == "docker":
        return DockerProvisioner(image, port_range)
    if kind == "fake":
        return FakeProvisioner(port_range)
    raise ValueError(f"unknown provisioner: {kind!r}")


async def _run(argv: list[str]) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await process.communicate()
    return process.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def sandbox_dir() -> Path:
    return Path(__file__).resolve().parent
