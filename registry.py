"""SQLite registry: users, surface identities, sessions, surface bindings.

The registry is the source of truth for session state. `resume_id` in
particular lives here and never only in process memory — a restart has to be
able to find and resume every session.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_PATH = Path(__file__).resolve().parent / "db" / "schema.sql"

ACTIVE = "active"
DONE = "done"
ARCHIVED = "archived"

FRAME_CLOSED = "closed"
FRAME_DOCKED = "docked"
FRAME_MINIMIZED = "minimized"

_FRAME_STATES = {FRAME_CLOSED, FRAME_DOCKED, FRAME_MINIMIZED}
_STATUSES = {ACTIVE, DONE, ARCHIVED}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


class Registry:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text())
        # `CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so a
        # column added to the schema never reaches a database that predates it.
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(sessions)")}
        if "outcome" not in columns:
            self.conn.execute("ALTER TABLE sessions ADD COLUMN outcome TEXT")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- users + identities ------------------------------------------------

    def create_user(self, display_name: str, user_id: str | None = None) -> dict[str, Any]:
        user_id = user_id or new_id()
        self.conn.execute(
            "INSERT INTO users (user_id, display_name, created_at, status) VALUES (?,?,?,?)",
            (user_id, display_name, now(), ACTIVE),
        )
        self.conn.commit()
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        return _one(self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)))

    def list_users(self) -> list[dict[str, Any]]:
        return _all(self.conn.execute("SELECT * FROM users ORDER BY created_at"))

    def link_identity(self, surface: str, external_id: str, user_id: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO identities (surface, external_id, user_id) VALUES (?,?,?)",
            (surface, str(external_id), user_id),
        )
        self.conn.commit()

    def resolve_identity(self, surface: str, external_id: str) -> str | None:
        row = _one(
            self.conn.execute(
                "SELECT user_id FROM identities WHERE surface=? AND external_id=?",
                (surface, str(external_id)),
            )
        )
        return row["user_id"] if row else None

    def resolve_or_create_user(
        self, surface: str, external_id: str, display_name: str | None = None
    ) -> str:
        """Map a surface identity to a user, minting one on first contact."""
        existing = self.resolve_identity(surface, external_id)
        if existing:
            return existing
        user = self.create_user(display_name or f"{surface}:{external_id}")
        self.link_identity(surface, external_id, user["user_id"])
        return user["user_id"]

    # --- sessions ----------------------------------------------------------

    def create_session(
        self,
        user_id: str,
        harness: str,
        model: str,
        title: str | None = None,
        color: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        session_id = session_id or new_id()
        stamp = now()
        branch = f"session/{session_id}"
        self.conn.execute(
            """INSERT INTO sessions
               (id, user_id, title, color, harness, model, branch, status,
                frame_state, speaker, created_at, last_active)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id,
                user_id,
                title,
                color,
                harness,
                model,
                branch,
                ACTIVE,
                FRAME_CLOSED,
                0,
                stamp,
                stamp,
            ),
        )
        self.conn.commit()
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return _one(self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)))

    def list_sessions(self, user_id: str, status: str = ACTIVE) -> list[dict[str, Any]]:
        return _all(
            self.conn.execute(
                "SELECT * FROM sessions WHERE user_id=? AND status=? ORDER BY last_active DESC",
                (user_id, status),
            )
        )

    def open_frames(self, user_id: str) -> list[dict[str, Any]]:
        """Sessions the console should restore as frames on reload."""
        return _all(
            self.conn.execute(
                """SELECT * FROM sessions
                   WHERE user_id=? AND status=? AND frame_state != ?
                   ORDER BY last_active DESC""",
                (user_id, ACTIVE, FRAME_CLOSED),
            )
        )

    _UPDATABLE = {
        "title",
        "color",
        "harness",
        "model",
        "container_id",
        "app_port",
        "resume_id",
        "transcript",
        "status",
        "frame_state",
        "speaker",
    }

    def update_session(self, session_id: str, **fields: Any) -> dict[str, Any] | None:
        unknown = set(fields) - self._UPDATABLE
        if unknown:
            raise ValueError(f"not updatable: {sorted(unknown)}")
        if "status" in fields and fields["status"] not in _STATUSES:
            raise ValueError(f"bad status: {fields['status']}")
        if "frame_state" in fields and fields["frame_state"] not in _FRAME_STATES:
            raise ValueError(f"bad frame_state: {fields['frame_state']}")
        if "speaker" in fields:
            fields["speaker"] = 1 if fields["speaker"] else 0
        if not fields:
            return self.get_session(session_id)
        fields["last_active"] = now()
        assignments = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE sessions SET {assignments} WHERE id=?",
            (*fields.values(), session_id),
        )
        self.conn.commit()
        return self.get_session(session_id)

    # --- session events ----------------------------------------------------

    def append_event(
        self, session_id: str, seq: int, kind: str, text: str | None, data: dict[str, Any]
    ) -> None:
        """Record one event. Idempotent on (session_id, seq) so a replayed
        write cannot double up the transcript."""
        self.conn.execute(
            """INSERT OR IGNORE INTO session_events
                 (session_id, seq, kind, text, data, created_at)
               VALUES (?,?,?,?,?,?)""",
            (session_id, seq, kind, text, json.dumps(data, default=str), now()),
        )
        self.conn.commit()

    def session_events(
        self, session_id: str, after_seq: int = 0, limit: int = 1000
    ) -> list[dict[str, Any]]:
        rows = _all(
            self.conn.execute(
                """SELECT seq, kind, text, data, created_at FROM session_events
                   WHERE session_id=? AND seq > ? ORDER BY seq LIMIT ?""",
                (session_id, after_seq, limit),
            )
        )
        for row in rows:
            row["data"] = json.loads(row["data"]) if row["data"] else {}
        return rows

    def delete_session_events(self, session_id: str) -> None:
        self.conn.execute("DELETE FROM session_events WHERE session_id=?", (session_id,))
        self.conn.commit()

    def set_outcome(self, session_id: str, outcome: str | None) -> None:
        self.conn.execute(
            "UPDATE sessions SET outcome=? WHERE id=?", (outcome, session_id)
        )
        self.conn.commit()

    def touch(self, session_id: str) -> None:
        self.conn.execute("UPDATE sessions SET last_active=? WHERE id=?", (now(), session_id))
        self.conn.commit()

    def delete_session(self, session_id: str) -> None:
        """Drop the row and any bindings pointing at it. The branch survives."""
        self.conn.execute("DELETE FROM surface_bindings WHERE session_id=?", (session_id,))
        self.conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        self.conn.commit()

    def running_sessions(self) -> list[dict[str, Any]]:
        return _all(self.conn.execute("SELECT * FROM sessions WHERE container_id IS NOT NULL"))

    def used_app_ports(self) -> set[int]:
        rows = self.conn.execute(
            "SELECT app_port FROM sessions WHERE app_port IS NOT NULL"
        ).fetchall()
        return {r["app_port"] for r in rows}

    # --- surface bindings + layout -----------------------------------------

    def bind_surface(self, surface: str, external_id: str, session_id: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO surface_bindings (surface, external_id, session_id) VALUES (?,?,?)",
            (surface, str(external_id), session_id),
        )
        self.conn.commit()

    def bound_session(self, surface: str, external_id: str) -> str | None:
        row = _one(
            self.conn.execute(
                "SELECT session_id FROM surface_bindings WHERE surface=? AND external_id=?",
                (surface, str(external_id)),
            )
        )
        return row["session_id"] if row else None

    def unbind_surface(self, surface: str, external_id: str) -> None:
        self.conn.execute(
            "DELETE FROM surface_bindings WHERE surface=? AND external_id=?",
            (surface, str(external_id)),
        )
        self.conn.commit()

    def set_sidebar_collapsed(self, surface: str, external_id: str, collapsed: bool) -> None:
        self.conn.execute(
            """INSERT INTO surface_layout (surface, external_id, sidebar_collapsed)
               VALUES (?,?,?)
               ON CONFLICT(surface, external_id)
               DO UPDATE SET sidebar_collapsed=excluded.sidebar_collapsed""",
            (surface, str(external_id), 1 if collapsed else 0),
        )
        self.conn.commit()

    def sidebar_collapsed(self, surface: str, external_id: str) -> bool:
        row = _one(
            self.conn.execute(
                "SELECT sidebar_collapsed FROM surface_layout WHERE surface=? AND external_id=?",
                (surface, str(external_id)),
            )
        )
        return bool(row["sidebar_collapsed"]) if row else False


def _one(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    row = cursor.fetchone()
    return dict(row) if row else None


def _all(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cursor.fetchall()]


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]
