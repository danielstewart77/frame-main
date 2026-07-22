"""SQLite registry: users, surface identities, sessions, surface bindings.

The registry is the source of truth for session state. `resume_id` in
particular lives here and never only in process memory — a restart has to be
able to find and resume every session.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import auth

SCHEMA_PATH = Path(__file__).resolve().parent / "db" / "schema.sql"

ACTIVE = "active"
DONE = "done"
ARCHIVED = "archived"

ROLE_ADMIN = "admin"
ROLE_USER = "user"
_ROLES = {ROLE_ADMIN, ROLE_USER}

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

    # Columns added after the first release. `CREATE TABLE IF NOT EXISTS` leaves
    # an existing table alone, so a column added to the schema never reaches a
    # database that predates it unless it is also listed here.
    _ADDED_COLUMNS = {
        "sessions": {"outcome": "TEXT"},
        "users": {
            "role": "TEXT NOT NULL DEFAULT 'user'",
            "disabled": "INTEGER NOT NULL DEFAULT 0",
            "must_change_pw": "INTEGER NOT NULL DEFAULT 0",
            "last_login_at": "TEXT",
        },
    }

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text())
        for table, wanted in self._ADDED_COLUMNS.items():
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for column, decl in wanted.items():
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
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

    # --- user administration -----------------------------------------------

    def set_role(self, user_id: str, role: str) -> None:
        if role not in _ROLES:
            raise ValueError(f"bad role: {role}")
        self.conn.execute("UPDATE users SET role=? WHERE user_id=?", (role, user_id))
        self.conn.commit()

    def set_disabled(self, user_id: str, disabled: bool) -> None:
        self.conn.execute(
            "UPDATE users SET disabled=? WHERE user_id=?", (1 if disabled else 0, user_id)
        )
        self.conn.commit()

    def set_must_change_pw(self, user_id: str, must: bool) -> None:
        self.conn.execute(
            "UPDATE users SET must_change_pw=? WHERE user_id=?", (1 if must else 0, user_id)
        )
        self.conn.commit()

    def update_last_login(self, user_id: str) -> None:
        self.conn.execute("UPDATE users SET last_login_at=? WHERE user_id=?", (now(), user_id))
        self.conn.commit()

    def admin_count(self) -> int:
        """How many enabled admins exist — the last one must not be removed."""
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role=? AND disabled=0",
                (ROLE_ADMIN,),
            ).fetchone()["n"]
        )

    def first_credentialed_user(self) -> str | None:
        """The earliest user that has a console login — the bootstrap admin pick."""
        row = _one(
            self.conn.execute(
                """SELECT u.user_id FROM users u JOIN credentials c ON c.user_id = u.user_id
                   ORDER BY u.created_at LIMIT 1"""
            )
        )
        return row["user_id"] if row else None

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

    # --- telegram bots -----------------------------------------------------

    def set_telegram_bot(self, user_id: str, bot_token: str) -> None:
        """Give a user a bot, or replace the token on the one they have.

        A new token value re-opens owner enrollment: `owner_chat_id` drops to
        NULL and `enabled` back to 1, so the first chat to message the fresh
        bot claims it. Re-saving the same token leaves the existing owner in
        place — it is not a way to hand the bot to a different chat.
        """
        existing = self.get_telegram_bot(user_id)
        if existing and existing["bot_token"] == bot_token:
            return
        self.conn.execute(
            """INSERT INTO telegram_bots (user_id, bot_token, owner_chat_id, enabled, created_at)
               VALUES (?,?,NULL,1,?)
               ON CONFLICT(user_id) DO UPDATE
                 SET bot_token=excluded.bot_token, owner_chat_id=NULL, enabled=1""",
            (user_id, bot_token, now()),
        )
        self.conn.commit()

    def clear_telegram_bot(self, user_id: str) -> None:
        self.conn.execute("DELETE FROM telegram_bots WHERE user_id=?", (user_id,))
        self.conn.commit()

    def get_telegram_bot(self, user_id: str) -> dict[str, Any] | None:
        return _one(
            self.conn.execute("SELECT * FROM telegram_bots WHERE user_id=?", (user_id,))
        )

    def list_telegram_bots(self) -> list[dict[str, Any]]:
        """Every enabled bot the supervisor should be running a poller for."""
        return _all(
            self.conn.execute(
                "SELECT user_id, bot_token, owner_chat_id FROM telegram_bots WHERE enabled=1"
            )
        )

    def set_telegram_owner_chat(self, user_id: str, chat_id: str) -> None:
        """Lock a bot to the chat that first messaged it."""
        self.conn.execute(
            "UPDATE telegram_bots SET owner_chat_id=? WHERE user_id=?",
            (str(chat_id), user_id),
        )
        self.conn.commit()

    # --- per-user proxy keys -----------------------------------------------

    def set_proxy_key(self, user_id: str, api_key: str) -> None:
        """Set (or replace) a user's inference-proxy API key."""
        self.conn.execute(
            """INSERT INTO proxy_keys (user_id, api_key, created_at) VALUES (?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET api_key=excluded.api_key""",
            (user_id, api_key, now()),
        )
        self.conn.commit()

    def get_proxy_key(self, user_id: str | None) -> str | None:
        """The user's own proxy key, or None to fall back to the box-wide token."""
        if not user_id:
            return None
        row = _one(
            self.conn.execute("SELECT api_key FROM proxy_keys WHERE user_id=?", (user_id,))
        )
        return row["api_key"] if row else None

    def clear_proxy_key(self, user_id: str) -> None:
        self.conn.execute("DELETE FROM proxy_keys WHERE user_id=?", (user_id,))
        self.conn.commit()

    def has_proxy_key(self, user_id: str) -> bool:
        return self.get_proxy_key(user_id) is not None

    # --- credentials + tokens ----------------------------------------------

    def set_credential(self, user_id: str, username: str, password_hash: str) -> None:
        """Give a user a console login, or replace the one they have."""
        self.conn.execute(
            """INSERT INTO credentials (user_id, username, password_hash, created_at)
               VALUES (?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE
                 SET username=excluded.username, password_hash=excluded.password_hash""",
            (user_id, username, password_hash, now()),
        )
        self.conn.commit()

    def credential_by_username(self, username: str) -> dict[str, Any] | None:
        return _one(
            self.conn.execute("SELECT * FROM credentials WHERE username=?", (username,))
        )

    def credential_for(self, user_id: str) -> dict[str, Any] | None:
        return _one(self.conn.execute("SELECT * FROM credentials WHERE user_id=?", (user_id,)))

    def count_credentials(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM credentials").fetchone()["n"])

    def store_token(self, token_hash: str, user_id: str, ttl_hours: int) -> dict[str, Any]:
        issued = datetime.now(timezone.utc)
        expires = issued + timedelta(hours=ttl_hours)
        self.conn.execute(
            """INSERT OR REPLACE INTO auth_tokens
                 (token_hash, user_id, created_at, expires_at, last_used)
               VALUES (?,?,?,?,?)""",
            (
                token_hash,
                user_id,
                issued.isoformat(timespec="seconds"),
                expires.isoformat(timespec="seconds"),
                None,
            ),
        )
        self.conn.commit()
        return {"user_id": user_id, "expires_at": expires.isoformat(timespec="seconds")}

    def user_for_token(self, token_hash: str) -> str | None:
        """Resolve a live token to its user, dropping it if it has expired."""
        row = _one(
            self.conn.execute(
                "SELECT user_id, expires_at FROM auth_tokens WHERE token_hash=?", (token_hash,)
            )
        )
        if not row:
            return None
        if row["expires_at"] <= now():
            self.delete_token(token_hash)
            return None
        self.conn.execute(
            "UPDATE auth_tokens SET last_used=? WHERE token_hash=?", (now(), token_hash)
        )
        self.conn.commit()
        return row["user_id"]

    def delete_token(self, token_hash: str) -> None:
        self.conn.execute("DELETE FROM auth_tokens WHERE token_hash=?", (token_hash,))
        self.conn.commit()

    def delete_user_tokens(self, user_id: str) -> None:
        """Log a user out everywhere — used when their password changes."""
        self.conn.execute("DELETE FROM auth_tokens WHERE user_id=?", (user_id,))
        self.conn.commit()

    def purge_expired_tokens(self) -> int:
        cursor = self.conn.execute("DELETE FROM auth_tokens WHERE expires_at <= ?", (now(),))
        self.conn.commit()
        return cursor.rowcount

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

    # --- session channel tokens --------------------------------------------

    def rotate_channel_token(self, session_id: str) -> str:
        """Mint a fresh channel bearer for the session and return the plaintext.

        Called each time a container is provisioned. Only the sha256 is kept, so
        the old token dies with the old container and the database never holds a
        usable one. The plaintext is returned exactly here, to go into the
        container's env, and cannot be read back.
        """
        token = auth.new_token()
        self.conn.execute(
            """INSERT INTO session_tokens (session_id, token_hash) VALUES (?,?)
               ON CONFLICT(session_id) DO UPDATE SET token_hash=excluded.token_hash""",
            (session_id, auth.token_digest(token)),
        )
        self.conn.commit()
        return token

    def session_for_channel_token(self, token: str) -> str | None:
        """Which session a presented channel bearer speaks for, if any."""
        row = _one(
            self.conn.execute(
                "SELECT session_id FROM session_tokens WHERE token_hash=?",
                (auth.token_digest(token),),
            )
        )
        return row["session_id"] if row else None

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
