"""Per-user host state — the isolation boundary that outlives containers.

    users/<user_id>/
      origin.git/   bare repo; sessions push their branches here
      memory.db     per-user memory, shared across that user's sessions
      identity.md   this user's soul seed (asked, never inferred)

Containers are pristine and disposable; everything durable lives here.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

IDENTITY_PLACEHOLDER = (
    "# identity\n\n"
    "_Not yet set. A user's identity is asked for, never inferred._\n"
)


class Workspace:
    def __init__(self, root: Path | str, user_id: str):
        self.user_id = user_id
        self.path = Path(root) / user_id

    # --- paths -------------------------------------------------------------

    @property
    def origin(self) -> Path:
        return self.path / "origin.git"

    @property
    def memory_db(self) -> Path:
        return self.path / "memory.db"

    @property
    def identity(self) -> Path:
        return self.path / "identity.md"

    @property
    def transcripts(self) -> Path:
        return self.path / "transcripts"

    def exists(self) -> bool:
        return self.origin.exists() and self.memory_db.exists() and self.identity.exists()

    # --- lifecycle ---------------------------------------------------------

    def ensure(self) -> "Workspace":
        """Create anything missing. Idempotent — safe to call every turn."""
        self.path.mkdir(parents=True, exist_ok=True)
        self.transcripts.mkdir(exist_ok=True)
        self._ensure_origin()
        self._ensure_memory()
        if not self.identity.exists():
            self.identity.write_text(IDENTITY_PLACEHOLDER)
        return self

    def _ensure_origin(self) -> None:
        if (self.origin / "HEAD").exists():
            return
        self.origin.mkdir(parents=True, exist_ok=True)
        _git("init", "--bare", "--initial-branch=main", str(self.origin))

    def _ensure_memory(self) -> None:
        if self.memory_db.exists():
            return
        conn = sqlite3.connect(str(self.memory_db))
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                  id         INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT,
                  kind       TEXT NOT NULL,
                  content    TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS memories_by_kind ON memories(kind, created_at);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def destroy(self) -> None:
        if self.path.exists():
            shutil.rmtree(self.path)

    # --- git ---------------------------------------------------------------

    def branches(self) -> list[str]:
        if not (self.origin / "HEAD").exists():
            return []
        out = _git("--git-dir", str(self.origin), "branch", "--format=%(refname:short)")
        return [line.strip() for line in out.splitlines() if line.strip()]

    def diff(self, branch: str, base: str = "main") -> str:
        """Rendered `git diff` for a session branch, read straight off the bare repo."""
        known = self.branches()
        if branch not in known:
            return ""
        if base not in known:
            base = _EMPTY_TREE
        return _git("--git-dir", str(self.origin), "diff", f"{base}..{branch}")

    def clone_url(self) -> str:
        """What a laptop runs `git clone` against to pull the work down."""
        return str(self.origin)

    def identity_text(self) -> str:
        return self.identity.read_text() if self.identity.exists() else ""

    def set_identity(self, text: str) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        self.identity.write_text(text)


# git's empty tree — lets a first-ever branch diff against "nothing".
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0 and not result.stdout:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout
