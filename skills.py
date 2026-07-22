"""Agent skills — shared, read-only, host-managed.

Both harnesses take skills the same way: a directory of skill folders that the
CLI auto-discovers under its home. Claude reads `~/.claude/skills`, codex
`~/.codex/skills`. We keep one clone of each skills repo on the host and bind
it read-only into every container, so:

  * containers stay pristine and disposable — skills are never copied in per
    spawn, and the agent cannot mutate the shared set (its own work goes to git);
  * the ADO PAT never enters a container — the host account's git credential
    helper authenticates the clone/pull, exactly like cloning any other repo;
  * updating skills is a `git pull` in one place (the admin "sync" button), not
    a rebuild and not N containers.

Nothing here is account-specific: `skills_root` is whatever path the control
plane is configured with, owned by whatever account runs it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

# repo dir under skills_root  ->  where the harness looks for it in the container
REPOS: tuple[tuple[str, str], ...] = (
    ("claude-skills", "/workspace/.claude/skills"),
    ("codex-skills", "/workspace/.codex/skills"),
)


def _repo_urls(settings: Any) -> dict[str, str]:
    return {
        "claude-skills": settings.claude_skills_repo,
        "codex-skills": settings.codex_skills_repo,
    }


def _git(*args: str, cwd: Path | None = None) -> tuple[int, str]:
    """Run git non-interactively (a missing credential fails fast, never hangs)."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        env={"GIT_TERMINAL_PROMPT": "0", "PATH": _PATH},
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def _head(dest: Path) -> str | None:
    code, out = _git("-C", str(dest), "log", "-1", "--format=%h %s")
    return out if code == 0 else None


def status(settings: Any) -> list[dict[str, Any]]:
    """Per-repo view for the admin panel: is it configured, cloned, and at what commit."""
    root = Path(settings.skills_root)
    urls = _repo_urls(settings)
    out: list[dict[str, Any]] = []
    for name, mount in REPOS:
        dest = root / name
        present = (dest / ".git").is_dir()
        out.append(
            {
                "name": name,
                "mount": mount,
                "configured": bool(urls[name]),
                "present": present,
                "head": _head(dest) if present else None,
            }
        )
    return out


def sync(settings: Any) -> list[dict[str, Any]]:
    """Clone each configured skills repo, or fast-forward it if already cloned.

    Authentication is whatever git resolves for the URL (the host account's
    credential helper / PAT) — we never touch the credential ourselves.
    """
    root = Path(settings.skills_root)
    root.mkdir(parents=True, exist_ok=True)
    urls = _repo_urls(settings)
    results: list[dict[str, Any]] = []
    for name, _mount in REPOS:
        url = urls[name]
        dest = root / name
        if not url:
            results.append({"name": name, "ok": False, "action": "skip",
                            "detail": "no repo configured"})
            continue
        if (dest / ".git").is_dir():
            code, out = _git("-C", str(dest), "pull", "--ff-only")
            action = "pull"
        else:
            code, out = _git("clone", "--depth", "1", url, str(dest))
            action = "clone"
        results.append({
            "name": name,
            "ok": code == 0,
            "action": action,
            "detail": out[-500:] if code != 0 else (_head(dest) or "ok"),
        })
    return results


def skill_mounts(skills_root: Path | str) -> list[tuple[str, str]]:
    """(host_path, container_path) read-only mounts for skills repos on disk.

    Only repos actually cloned are mounted, so an unconfigured or offline box
    simply spawns without skills rather than failing."""
    root = Path(skills_root)
    mounts: list[tuple[str, str]] = []
    for name, mount in REPOS:
        dest = root / name
        if dest.is_dir():
            mounts.append((str(dest), mount))
    return mounts


# PATH for the git subprocess — kept explicit so the env is minimal but git and
# its credential helper are still found.
_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
