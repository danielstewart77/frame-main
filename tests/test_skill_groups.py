"""Skill groups: per-user named sets, and per-session mounting of just that set."""

from __future__ import annotations

import pytest

import skills as skills_mod


@pytest.fixture
def user_id(manager):
    return manager.resolve_user("web", "sg-user", "Ada")


# --- registry --------------------------------------------------------------


def test_skill_group_round_trip(registry):
    uid = registry.create_user("Ada")["user_id"]
    assert registry.list_skill_groups(uid) == []
    registry.set_skill_group(uid, "ado", ["get-story", "create-pull-request"])
    assert registry.get_skill_group(uid, "ado") == ["get-story", "create-pull-request"]
    registry.set_skill_group(uid, "ado", ["get-story"])  # replace
    assert registry.get_skill_group(uid, "ado") == ["get-story"]
    assert [g["name"] for g in registry.list_skill_groups(uid)] == ["ado"]
    registry.delete_skill_group(uid, "ado")
    assert registry.get_skill_group(uid, "ado") is None


def test_session_stores_its_skill_selection(registry):
    uid = registry.create_user("Ada")["user_id"]
    s = registry.create_session(uid, "claude", "opus", skills=["get-story"])
    import json
    assert json.loads(registry.get_session(s["id"])["skills"]) == ["get-story"]
    # default: no selection recorded
    s2 = registry.create_session(uid, "claude", "opus")
    assert registry.get_session(s2["id"])["skills"] is None


# --- mounts + available ----------------------------------------------------


def test_available_skills_lists_only_skill_dirs(tmp_path):
    root = tmp_path
    (root / "claude-skills" / "get-story").mkdir(parents=True)
    (root / "claude-skills" / "get-story" / "SKILL.md").write_text("x")
    (root / "claude-skills" / ".git").mkdir()  # not a skill
    (root / "claude-skills" / "README.md").write_text("x")  # a file, not a skill
    (root / "codex-skills" / "open-story").mkdir(parents=True)
    (root / "codex-skills" / "open-story" / "SKILL.md").write_text("x")
    avail = skills_mod.available_skills(root)
    assert avail["claude"] == ["get-story"]
    assert avail["codex"] == ["open-story"]


def test_selection_mounts_only_chosen_skill_folders(tmp_path):
    for name in ("get-story", "code-genius"):
        d = tmp_path / "claude-skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("x")
    # whole library when no selection
    whole = skills_mod.skill_mounts(tmp_path)
    assert (str(tmp_path / "claude-skills"), "/workspace/.claude/skills") in whole
    # only the chosen folder when selected, at its own subpath
    picked = skills_mod.skill_mounts(tmp_path, ["get-story", "nonexistent"])
    assert (str(tmp_path / "claude-skills" / "get-story"),
            "/workspace/.claude/skills/get-story") in picked
    assert not any(h.endswith("code-genius") for h, _ in picked)  # not selected
    assert not any(c == "/workspace/.claude/skills" for _, c in picked)  # not the whole repo


@pytest.mark.asyncio
async def test_session_mounts_only_its_group_skills(manager, user_id, provisioner, tmp_path):
    from dataclasses import replace

    for name in ("get-story", "code-genius"):
        d = tmp_path / "claude-skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("x")
    manager.settings = replace(manager.settings, skills_root=tmp_path)
    session = manager.create(user_id, skills=["get-story"])
    await manager.ensure_running(session["id"])
    mounts = [tuple(m) for m in provisioner.skill_mounts[-1]]
    assert (str(tmp_path / "claude-skills" / "get-story"),
            "/workspace/.claude/skills/get-story") in mounts
    assert not any("code-genius" in h for h, _ in mounts)


# --- routes ----------------------------------------------------------------


def test_skill_group_routes_are_owner_scoped(logged_in):
    uid = logged_in.user_id
    assert logged_in.get(f"/users/{uid}/skill-groups").json() == []
    put = logged_in.put(f"/users/{uid}/skill-groups/ado", json={"skills": ["get-story"]})
    assert put.status_code == 200 and put.json()["skills"] == ["get-story"]
    assert [g["name"] for g in logged_in.get(f"/users/{uid}/skill-groups").json()] == ["ado"]
    # can't touch someone else's groups
    assert logged_in.get("/users/someone-else/skill-groups").status_code == 403
    assert logged_in.put("/users/someone-else/skill-groups/x", json={"skills": []}).status_code == 403
    assert logged_in.delete(f"/users/{uid}/skill-groups/ado").status_code == 204


def test_spawning_with_a_group_records_its_skills(logged_in):
    uid = logged_in.user_id
    logged_in.put(f"/users/{uid}/skill-groups/ado", json={"skills": ["get-story", "open-story"]})
    session = logged_in.post(f"/users/{uid}/sessions", json={"skill_group": "ado"}).json()
    got = logged_in.get(f"/sessions/{session['id']}").json()
    import json
    assert json.loads(got["skills"]) == ["get-story", "open-story"]


def test_spawning_with_an_unknown_group_is_404(logged_in):
    uid = logged_in.user_id
    assert logged_in.post(f"/users/{uid}/sessions", json={"skill_group": "nope"}).status_code == 404
