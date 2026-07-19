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
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

import harness as harness_mod


class ProvisionError(RuntimeError):
    pass


@dataclass
class Container:
    container_id: str
    app_port: int | None


class Provisioner(Protocol):
    async def provision(
        self, session: dict[str, Any], workspace: Any, env: dict[str, str]
    ) -> Container: ...

    async def run_turn(
        self,
        session: dict[str, Any],
        prompt: str,
        system_prompt: str = "",
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def stop(self, container_id: str) -> None: ...

    async def remove(self, container_id: str) -> None: ...


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
        self, session: dict[str, Any], prompt: str, system_prompt: str = ""
    ) -> AsyncIterator[dict[str, Any]]:
        container_id = session.get("container_id")
        if not container_id:
            raise ProvisionError("session has no container")
        argv = harness_mod.build_argv(
            session["harness"],
            prompt,
            session["model"],
            resume_id=session.get("resume_id"),
            system_prompt=system_prompt,
        )
        command = " ".join(shlex.quote(a) for a in argv)
        docker_argv = ["docker", "exec", "-w", "/workspace/repo", container_id, "bash", "-lc", command]

        process = await asyncio.create_subprocess_exec(
            *docker_argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        async for line in process.stdout:
            event = harness_mod.parse_line(session["harness"], line.decode("utf-8", "replace"))
            if event is not None:
                yield event
        await process.wait()
        if process.returncode != 0:
            stderr = b""
            if process.stderr is not None:
                stderr = await process.stderr.read()
            yield {
                "kind": "error",
                "text": stderr.decode("utf-8", "replace").strip() or "harness exited nonzero",
            }

    async def stop(self, container_id: str) -> None:
        await _run(["docker", "stop", container_id])

    async def remove(self, container_id: str) -> None:
        await _run(["docker", "rm", "-f", container_id])


# --- fake ------------------------------------------------------------------


class FakeProvisioner:
    """In-process stand-in. Records calls so tests can assert on lifecycle."""

    def __init__(self, port_range: tuple[int, int] = (9600, 9699)):
        self.port_range = port_range
        self.provisioned: dict[str, Container] = {}
        self.stopped: list[str] = []
        self.removed: list[str] = []
        self.turns: list[tuple[str, str]] = []
        self._counter = 0

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
        return container

    async def run_turn(
        self, session: dict[str, Any], prompt: str, system_prompt: str = ""
    ) -> AsyncIterator[dict[str, Any]]:
        if not session.get("container_id"):
            raise ProvisionError("session has no container")
        self.turns.append((session["id"], prompt))
        if not session.get("resume_id"):
            yield {"kind": "session", "resume_id": f"resume-{session['id'][:8]}"}
        yield {"kind": "text", "text": f"[fake {session['harness']}] "}
        yield {"kind": "text", "text": prompt}
        yield {"kind": "result", "text": f"[fake {session['harness']}] {prompt}"}

    async def stop(self, container_id: str) -> None:
        self.stopped.append(container_id)

    async def remove(self, container_id: str) -> None:
        self.removed.append(container_id)


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
