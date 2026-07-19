"""Real-Docker integration: provisioning, branch clone, and push durability.

Skipped unless the sandbox image is built. This is the layer the fakes cannot
prove — that a pristine container clones its session branch off the mounted bare
repo and that the Stop hook pushes work back to the host.

Build first: docker build -f sandbox/Dockerfile -t frame-main-sandbox:latest .
"""

import asyncio
import shutil
import subprocess

import pytest

from sandbox.provision import Container, DockerProvisioner
from workspace import Workspace

IMAGE = "frame-main-sandbox:latest"


def _image_present() -> bool:
    if not shutil.which("docker"):
        return False
    result = subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True, text=True
    )
    return result.returncode == 0


pytestmark = pytest.mark.skipif(
    not _image_present(), reason=f"{IMAGE} not built; see docs/vpn-cutover.md"
)


async def _exec(container_id: str, command: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        "docker", "exec", "-w", "/workspace/repo", container_id, "bash", "-lc", command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await process.communicate()
    return process.returncode, out.decode()


@pytest.fixture
async def container(tmp_path):
    workspace = Workspace(tmp_path, "user-1").ensure()
    session = {
        "id": "integration01",
        "user_id": "user-1",
        "harness": "claude",
        "model": "opus",
        "branch": "session/integration01",
    }
    provisioner = DockerProvisioner(IMAGE)
    env = {
        "FRAME_BRANCH": session["branch"],
        "FRAME_SESSION_ID": session["id"],
        "GIT_ORIGIN": "/origin.git",
    }
    handle: Container = await provisioner.provision(session, workspace, env)
    for _ in range(40):  # entrypoint clones before it idles
        code, _ = await _exec(handle.container_id, "test -d /workspace/repo/.git")
        if code == 0:
            break
        await asyncio.sleep(0.25)
    try:
        yield handle, workspace, session
    finally:
        await provisioner.remove(handle.container_id)


@pytest.mark.asyncio
async def test_container_starts_on_the_session_branch(container):
    handle, _, session = container
    code, out = await _exec(handle.container_id, "git branch --show-current")
    assert code == 0
    assert out.strip() == session["branch"]


@pytest.mark.asyncio
async def test_both_harness_clis_are_installed(container):
    handle, _, _ = container
    code, out = await _exec(handle.container_id, "which claude codex")
    assert code == 0
    assert "claude" in out and "codex" in out


@pytest.mark.asyncio
async def test_the_stop_hook_pushes_work_to_the_host_bare_repo(container):
    handle, workspace, session = container
    await _exec(handle.container_id, "echo 'agent output' > note.txt")
    code, out = await _exec(handle.container_id, "/root/.claude/hooks/stop-commit.sh")
    assert code == 0, out

    assert session["branch"] in workspace.branches()
    assert "agent output" in workspace.diff(session["branch"])


@pytest.mark.asyncio
async def test_the_stop_hook_is_a_no_op_when_nothing_changed(container):
    handle, workspace, session = container
    code, out = await _exec(handle.container_id, "/root/.claude/hooks/stop-commit.sh")
    assert code == 0, out
    assert workspace.diff(session["branch"]) == ""


@pytest.mark.asyncio
async def test_work_survives_the_container_being_destroyed(tmp_path):
    """The point of pushing every turn: a thrown-away container loses nothing."""
    workspace = Workspace(tmp_path, "user-2").ensure()
    session = {
        "id": "integration02",
        "user_id": "user-2",
        "harness": "claude",
        "model": "opus",
        "branch": "session/integration02",
    }
    provisioner = DockerProvisioner(IMAGE)
    env = {"FRAME_BRANCH": session["branch"], "FRAME_SESSION_ID": session["id"]}

    first = await provisioner.provision(session, workspace, dict(env))
    for _ in range(40):
        code, _ = await _exec(first.container_id, "test -d /workspace/repo/.git")
        if code == 0:
            break
        await asyncio.sleep(0.25)
    await _exec(first.container_id, "echo 'turn one' > work.txt")
    await _exec(first.container_id, "/root/.claude/hooks/stop-commit.sh")
    await provisioner.remove(first.container_id)

    second = await provisioner.provision(session, workspace, dict(env))
    try:
        for _ in range(40):
            code, out = await _exec(second.container_id, "cat work.txt")
            if code == 0:
                break
            await asyncio.sleep(0.25)
        assert "turn one" in out
    finally:
        await provisioner.remove(second.container_id)
