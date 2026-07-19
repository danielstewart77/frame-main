"""Session lifecycle — provision, run, idle, teardown.

The session is the unit. A row carries its own harness, model, and container;
there is no long-lived agent object that owns sessions. Spawn is a new row plus
a container, ditch is status `archived` plus the container removed, return is a
resume.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import registry as registry_mod
from config import Settings
from sandbox.provision import Provisioner, allocate_port
from workspace import Workspace


class SessionError(RuntimeError):
    pass


class UnknownSession(SessionError):
    pass


class SessionManager:
    def __init__(
        self,
        registry: registry_mod.Registry,
        settings: Settings,
        provisioner: Provisioner,
    ):
        self.registry = registry
        self.settings = settings
        self.provisioner = provisioner
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_sessions)

    # --- users -------------------------------------------------------------

    def workspace(self, user_id: str) -> Workspace:
        return Workspace(self.settings.users_root, user_id)

    def resolve_user(self, surface: str, external_id: str, display_name: str | None = None) -> str:
        user_id = self.registry.resolve_or_create_user(surface, external_id, display_name)
        self.workspace(user_id).ensure()
        return user_id

    # --- lifecycle ---------------------------------------------------------

    def create(
        self,
        user_id: str,
        harness: str | None = None,
        model: str | None = None,
        title: str | None = None,
        color: str | None = None,
    ) -> dict[str, Any]:
        if not self.registry.get_user(user_id):
            raise UnknownSession(f"no such user: {user_id}")
        self.workspace(user_id).ensure()
        return self.registry.create_session(
            user_id=user_id,
            harness=harness or self.settings.default_harness,
            model=model or self.settings.default_model,
            title=title,
            color=color,
        )

    def get(self, session_id: str) -> dict[str, Any]:
        session = self.registry.get_session(session_id)
        if not session:
            raise UnknownSession(f"no such session: {session_id}")
        return session

    async def ensure_running(self, session_id: str) -> dict[str, Any]:
        """Provision a container for the session if it does not already have one."""
        session = self.get(session_id)
        if session["status"] == registry_mod.ARCHIVED:
            raise SessionError("cannot run an archived session")
        if session["container_id"]:
            return session

        workspace = self.workspace(session["user_id"]).ensure()
        app_port = session["app_port"] or allocate_port(
            self.registry.used_app_ports(), self.settings.app_port_range
        )
        env = self._spawn_env(session, workspace)
        if app_port:
            env["_app_port"] = str(app_port)

        async with self.semaphore:
            container = await self.provisioner.provision(session, workspace, env)

        return self.registry.update_session(
            session_id,
            container_id=container.container_id,
            app_port=container.app_port,
        )

    def _spawn_env(self, session: dict[str, Any], workspace: Workspace) -> dict[str, str]:
        """Per-container env — creds are injected at spawn time, never baked in."""
        env = {
            "FRAME_USER_ID": session["user_id"],
            "FRAME_SESSION_ID": session["id"],
            "FRAME_BRANCH": session["branch"],
            "FRAME_HARNESS": session["harness"],
            "FRAME_MODEL": session["model"],
            "GIT_ORIGIN": "/origin.git",
        }
        if self.settings.anthropic_base_url:
            env["ANTHROPIC_BASE_URL"] = self.settings.anthropic_base_url
        if self.settings.anthropic_auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = self.settings.anthropic_auth_token
        return env

    def system_prompt(self, session: dict[str, Any]) -> str:
        """Identity + memory blocks appended to the harness's own system prompt."""
        workspace = self.workspace(session["user_id"])
        blocks = []
        identity = workspace.identity_text().strip()
        if identity:
            blocks.append(f"<identity>\n{identity}\n</identity>")
        blocks.append(
            "<commit-discipline>\n"
            "Commit logically as you work — each meaningful step is its own commit, "
            "not one blob at the end of the turn. Your work is pushed to a host repo "
            "after every turn, so committed work survives this container.\n"
            "</commit-discipline>"
        )
        return "\n\n".join(blocks)

    async def turn(self, session_id: str, prompt: str) -> AsyncIterator[dict[str, Any]]:
        """Run one turn, streaming normalised events and persisting `resume_id`.

        Bounded by `turn_timeout_seconds` — an unreachable provider otherwise
        keeps the harness retrying and the frame streaming nothing.
        """
        session = await self.ensure_running(session_id)
        timeout = self.settings.turn_timeout_seconds
        async with self.semaphore:
            stream = self.provisioner.run_turn(session, prompt, self.system_prompt(session))
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(stream.__anext__(), timeout)
                    except StopAsyncIteration:
                        break
                    if event["kind"] == "session" and event.get("resume_id"):
                        self.registry.update_session(session_id, resume_id=event["resume_id"])
                    yield event
            except asyncio.TimeoutError:
                yield {
                    "kind": "error",
                    "text": f"turn timed out after {timeout}s with no output",
                }
            finally:
                await stream.aclose()
        self.registry.touch(session_id)

    async def stop(self, session_id: str) -> dict[str, Any]:
        """Stop the container; state persists in origin.git, a resume re-provisions."""
        session = self.get(session_id)
        if session["container_id"]:
            await self.provisioner.stop(session["container_id"])
            session = self.registry.update_session(session_id, container_id=None)
        return session

    async def archive(self, session_id: str) -> dict[str, Any]:
        """Remove the container for good. The branch stays."""
        session = self.get(session_id)
        if session["container_id"]:
            await self.provisioner.remove(session["container_id"])
        return self.registry.update_session(
            session_id,
            container_id=None,
            app_port=None,
            status=registry_mod.ARCHIVED,
            frame_state=registry_mod.FRAME_CLOSED,
        )

    async def delete(self, session_id: str) -> None:
        session = self.get(session_id)
        if session["container_id"]:
            await self.provisioner.remove(session["container_id"])
        self.registry.delete_session(session_id)

    async def reap_idle(self, now: datetime | None = None) -> list[str]:
        """Stop containers idle past the timeout. Returns the sessions reaped."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=self.settings.idle_timeout_minutes)
        reaped = []
        for session in self.registry.running_sessions():
            last = datetime.fromisoformat(session["last_active"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last < cutoff:
                await self.stop(session["id"])
                reaped.append(session["id"])
        return reaped

    # --- surface bindings --------------------------------------------------

    def attach(self, surface: str, external_id: str, session_id: str) -> dict[str, Any]:
        session = self.get(session_id)
        self.registry.bind_surface(surface, external_id, session_id)
        return session

    def attached(self, surface: str, external_id: str) -> dict[str, Any] | None:
        session_id = self.registry.bound_session(surface, external_id)
        if not session_id:
            return None
        return self.registry.get_session(session_id)

    def detach(self, surface: str, external_id: str) -> None:
        self.registry.unbind_surface(surface, external_id)
