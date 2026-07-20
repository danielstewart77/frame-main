"""Surface-agnostic engage/disengage logic for a chat remote.

A chat is a thin remote that attaches to exactly one session at a time. The
attachment is the `surface_bindings` row, not bot process state, so a restart
or a `/switch` from the web console stays consistent.

`ChatRouter` is pure: it takes a command and returns a `Reply` describing what
the bot should render. All state changes go through a `Client`, of which there
are two — `LocalClient` (same process as the control plane) and `HttpClient`
(the agent-server's HTTP API). Telegram-specific IO lives in the bot entrypoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

ACTIVE = "active"
ARCHIVED = "archived"


@dataclass
class Button:
    label: str
    action: str          # 'attach'
    session_id: str = ""


@dataclass
class Reply:
    text: str
    buttons: list[Button] = field(default_factory=list)
    prompt: str | None = None       # set when the message should run a turn
    session_id: str | None = None


HELP = (
    "/agents — list your active sessions\n"
    "/archived — list archived sessions\n"
    "/new [harness] [model] — create a session and attach\n"
    "/switch <id> — attach this chat to a session\n"
    "/detach — leave the chat idle\n"
    "/whoami — which session this chat is attached to"
)


def label_for(session: dict[str, Any]) -> str:
    return session["title"] or f"{session['harness']}:{session['id'][:8]}"


class Client(Protocol):
    def resolve_user(self, surface: str, external_id: str, display_name: str | None) -> str: ...
    def list_sessions(self, user_id: str, status: str) -> list[dict[str, Any]]: ...
    def create_session(self, user_id: str, harness: str | None, model: str | None) -> dict[str, Any]: ...
    def get_session(self, session_id: str) -> dict[str, Any] | None: ...
    def attach(self, surface: str, external_id: str, session_id: str) -> dict[str, Any]: ...
    def attached(self, surface: str, external_id: str) -> dict[str, Any] | None: ...
    def detach(self, surface: str, external_id: str) -> None: ...


class LocalClient:
    """Backed by a SessionManager in this process."""

    def __init__(self, manager):
        self.manager = manager

    def resolve_user(self, surface, external_id, display_name=None):
        return self.manager.resolve_user(surface, external_id, display_name)

    def list_sessions(self, user_id, status=ACTIVE):
        return self.manager.registry.list_sessions(user_id, status)

    def create_session(self, user_id, harness=None, model=None):
        return self.manager.create(user_id, harness=harness, model=model)

    def get_session(self, session_id):
        return self.manager.registry.get_session(session_id)

    def attach(self, surface, external_id, session_id):
        return self.manager.attach(surface, external_id, session_id)

    def attached(self, surface, external_id):
        return self.manager.attached(surface, external_id)

    def detach(self, surface, external_id):
        self.manager.detach(surface, external_id)


class HttpClient:
    """Backed by the agent-server HTTP API — for a bot in its own process.

    A surface is a service principal: it carries `FRAME_SERVICE_TOKEN` on every
    request and thereby acts for whichever user a chat identity resolves to.
    Without the token the control plane answers 401 and the bot is inert, which
    is the intended failure — a surface with no credential should do nothing.
    """

    def __init__(self, base_url: str, timeout: float = 30.0, service_token: str = ""):
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {service_token}"} if service_token else {}
        self.http = httpx.Client(base_url=self.base_url, timeout=timeout, headers=headers)

    def resolve_user(self, surface, external_id, display_name=None):
        response = self.http.post(
            "/identities",
            json={"surface": surface, "external_id": str(external_id), "display_name": display_name},
        )
        response.raise_for_status()
        return response.json()["user_id"]

    def list_sessions(self, user_id, status=ACTIVE):
        response = self.http.get(f"/users/{user_id}/sessions", params={"status": status})
        response.raise_for_status()
        return response.json()

    def create_session(self, user_id, harness=None, model=None):
        response = self.http.post(
            f"/users/{user_id}/sessions", json={"harness": harness, "model": model}
        )
        response.raise_for_status()
        return response.json()

    def get_session(self, session_id):
        response = self.http.get(f"/sessions/{session_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def attach(self, surface, external_id, session_id):
        response = self.http.post(
            f"/surfaces/{surface}/{external_id}/attach", json={"session_id": session_id}
        )
        response.raise_for_status()
        return response.json()

    def attached(self, surface, external_id):
        response = self.http.get(f"/surfaces/{surface}/{external_id}/attach")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def detach(self, surface, external_id):
        self.http.delete(f"/surfaces/{surface}/{external_id}/attach").raise_for_status()

    def close(self) -> None:
        self.http.close()


class ChatRouter:
    def __init__(self, client: Client, surface: str = "telegram"):
        self.client = client
        self.surface = surface

    def handle(self, external_id: str, text: str, display_name: str | None = None) -> Reply:
        user_id = self.client.resolve_user(self.surface, external_id, display_name)
        text = (text or "").strip()

        if not text.startswith("/"):
            return self._route_to_session(external_id, text)

        command, _, rest = text.partition(" ")
        rest = rest.strip()

        if command in {"/start", "/help"}:
            return Reply(text=HELP)
        if command == "/agents":
            return self._list(user_id, ACTIVE)
        if command == "/archived":
            return self._list(user_id, ARCHIVED)
        if command == "/new":
            return self._new(user_id, external_id, rest)
        if command == "/switch":
            return self._switch(external_id, rest)
        if command == "/detach":
            self.client.detach(self.surface, external_id)
            return Reply(text="Detached. This chat is idle.")
        if command == "/whoami":
            session = self.client.attached(self.surface, external_id)
            if not session:
                return Reply(text="Not attached to any session.")
            return Reply(text=f"Attached to {label_for(session)}.", session_id=session["id"])
        return Reply(text=f"Unknown command {command}.\n\n{HELP}")

    def tap(self, external_id: str, session_id: str) -> Reply:
        """A session button was tapped — repoint the binding."""
        session = self.client.attach(self.surface, external_id, session_id)
        return Reply(text=f"Attached to {label_for(session)}.", session_id=session["id"])

    # --- internals ---------------------------------------------------------

    def _list(self, user_id: str, status: str) -> Reply:
        sessions = self.client.list_sessions(user_id, status)
        if not sessions:
            word = "active" if status == ACTIVE else status
            return Reply(text=f"No {word} sessions. /new to create one.")
        buttons = [Button(label_for(s), "attach", s["id"]) for s in sessions]
        return Reply(text=f"Your {status} sessions:", buttons=buttons)

    def _new(self, user_id: str, external_id: str, rest: str) -> Reply:
        parts = rest.split()
        harness = parts[0] if parts else None
        model = parts[1] if len(parts) > 1 else None
        session = self.client.create_session(user_id, harness, model)
        self.client.attach(self.surface, external_id, session["id"])
        return Reply(
            text=f"Created and attached to {label_for(session)} "
                 f"({session['harness']} / {session['model']}).",
            session_id=session["id"],
        )

    def _switch(self, external_id: str, session_id: str) -> Reply:
        if not session_id:
            return Reply(text="Usage: /switch <session-id>")
        session = self.client.get_session(session_id)
        if not session:
            return Reply(text=f"No such session: {session_id}")
        self.client.attach(self.surface, external_id, session_id)
        return Reply(text=f"Attached to {label_for(session)}.", session_id=session_id)

    def _route_to_session(self, external_id: str, text: str) -> Reply:
        session = self.client.attached(self.surface, external_id)
        if not session:
            return Reply(text="Not attached to a session. /agents to pick one, /new to create one.")
        if not text:
            return Reply(text="Nothing to send.", session_id=session["id"])
        return Reply(text="", prompt=text, session_id=session["id"])
