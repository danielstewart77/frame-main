"""Skills: the host clone/pull, the read-only mounts, and the admin gate.

Uses a local git repo as the "remote" so nothing here touches ADO or the
network — the clone/pull path is real git, just against a file:// source.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace

import pytest

import skills as skills_mod
from conftest import SERVICE_TOKEN


def _make_source_repo(path):
    """A tiny git repo with one skill folder, to clone from."""
    path.mkdir(parents=True)
    (path / "hello-skill").mkdir()
    (path / "hello-skill" / "SKILL.md").write_text("# hello\n")
    env = {"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin"}
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(path), *a],
        check=True, capture_output=True,
        env={**env, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    run("add", "-A")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "seed")


def test_skill_mounts_only_includes_present_repos(tmp_path):
    (tmp_path / "claude-skills").mkdir()
    # codex-skills intentionally absent
    mounts = skills_mod.skill_mounts(tmp_path)
    assert (str(tmp_path / "claude-skills"), "/workspace/.claude/skills") in mounts
    assert all(host.endswith("claude-skills") for host, _ in mounts)


def test_sync_clones_then_fast_forwards(settings, tmp_path):
    source = tmp_path / "src"
    _make_source_repo(source)
    cfg = replace(
        settings,
        skills_root=tmp_path / "skills",
        claude_skills_repo=str(source),
        codex_skills_repo="",  # unconfigured — should be skipped, not errored
    )

    first = {r["name"]: r for r in skills_mod.sync(cfg)}
    assert first["claude-skills"]["ok"] and first["claude-skills"]["action"] == "clone"
    assert first["codex-skills"]["action"] == "skip"
    assert (tmp_path / "skills" / "claude-skills" / "hello-skill" / "SKILL.md").exists()

    # A second sync fast-forwards the existing clone rather than re-cloning.
    second = {r["name"]: r for r in skills_mod.sync(cfg)}
    assert second["claude-skills"]["ok"] and second["claude-skills"]["action"] == "pull"

    st = {r["name"]: r for r in skills_mod.status(cfg)}
    assert st["claude-skills"]["present"] and st["claude-skills"]["head"]
    assert st["codex-skills"]["configured"] is False


def test_skills_routes_require_admin(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as admin, TestClient(app) as peon, TestClient(app) as anon:
        # first account is admin; second (via service token) is a plain user
        admin.post("/auth/register", json={"username": "boss", "password": "a good password"})
        admin.post("/auth/login", json={"username": "boss", "password": "a good password"})
        peon.post("/auth/register", json={"username": "peon", "password": "a good password"},
                  headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
        peon.post("/auth/login", json={"username": "peon", "password": "a good password"})

        got = admin.get("/admin/skills")
        assert got.status_code == 200
        assert {r["name"] for r in got.json()["repos"]} == {"claude-skills", "codex-skills"}
        assert peon.get("/admin/skills").status_code == 403
        assert anon.get("/admin/skills").status_code == 401
        assert anon.post("/admin/skills/sync").status_code == 401
