"""Session lifecycle — provision, run, idle, teardown.

The session is the unit. A row carries its own harness, model, and container;
there is no long-lived agent object that owns sessions. Spawn is a new row plus
a container, ditch is status `archived` plus the container removed, return is a
resume.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import registry as registry_mod
import skills as skills_mod
from bus import SessionStreams, Subscription
from config import Settings
from sandbox.provision import Provisioner, allocate_port
from transcript import TranscriptWriter
from workspace import Workspace

# How long the container's shim holds a poll open before reissuing it.
CHANNEL_POLL_TIMEOUT_SECONDS = 60.0


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
        # One provisioning at a time per session, so an eager `/start` and the
        # first turn racing don't spin up two containers for one session.
        self._provision_locks: dict[str, asyncio.Lock] = {}
        self.streams = SessionStreams()
        # Turns started by a channel event have no requester holding the
        # generator open, so the manager owns them until they finish.
        self._background: set[asyncio.Task[None]] = set()
        # `last_active` is only stamped when a turn ends, so a long unattended
        # turn looks idle while it is working. The reaper reads this to tell the
        # difference between a session doing nothing and one mid-thought.
        self._in_flight: set[str] = set()
        # The bus is live-only and in-memory; this is the copy you can read back.
        self.transcript = TranscriptWriter(registry)
        self.streams.on_publish = self.transcript.write
        # The harness stays up between turns, so it can also speak without being
        # prompted. That output belongs to the session, not to any requester.
        provisioner.on_unsolicited = self._publish_unsolicited

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

        # Serialize per session: a re-read inside the lock means a concurrent
        # caller that already provisioned wins and we don't double-spawn.
        lock = self._provision_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            session = self.get(session_id)
            if session["container_id"]:
                return session

            workspace = self.workspace(session["user_id"]).ensure()
            app_port = session["app_port"] or allocate_port(
                self.registry.used_app_ports(), self.settings.app_port_range
            )
            # A fresh channel bearer per container: the shim can call back for this
            # session and no other, and the previous container's token is now dead.
            channel_token = self.registry.rotate_channel_token(session_id)
            env = self._spawn_env(session, workspace, channel_token)
            if app_port:
                env["_app_port"] = str(app_port)

            async with self.semaphore:
                container = await self.provisioner.provision(session, workspace, env)

            return self.registry.update_session(
                session_id,
                container_id=container.container_id,
                app_port=container.app_port,
            )

    def _spawn_env(
        self, session: dict[str, Any], workspace: Workspace, channel_token: str
    ) -> dict[str, str]:
        """Per-container env — creds are injected at spawn time, never baked in."""
        env = {
            "FRAME_USER_ID": session["user_id"],
            "FRAME_SESSION_ID": session["id"],
            "FRAME_BRANCH": session["branch"],
            "FRAME_HARNESS": session["harness"],
            "FRAME_MODEL": session["model"],
            "GIT_ORIGIN": "/origin.git",
            # Where the channel shim calls back to reach the control plane, and
            # the bearer that scopes it to this one session.
            "FRAME_CHANNEL_URL": self.settings.channel_url,
            "FRAME_CHANNEL_TOKEN": channel_token,
        }
        # The proxy speaks both providers' protocols at one base URL with one
        # token, so map that single pair onto the env var names each harness
        # reads: claude honours ANTHROPIC_*, codex honours OPENAI_*. Injecting
        # both is harmless to whichever harness this session isn't running.
        #
        # The key is the session owner's own proxy key when they've set one, so
        # usage and model access are scoped to them; otherwise the box-wide
        # token. The base URL is shared infrastructure and stays global.
        base_url = self.settings.anthropic_base_url
        token = self.registry.get_proxy_key(session["user_id"]) or self.settings.ulmaiproxy_auth_token
        if base_url:
            env["ANTHROPIC_BASE_URL"] = base_url
            env["OPENAI_BASE_URL"] = base_url
        if token:
            env["ANTHROPIC_AUTH_TOKEN"] = token
            env["OPENAI_API_KEY"] = token
        # Shared skills, read-only. Passed alongside the env (like `_app_port`)
        # and turned into `-v …:ro` mounts by the provisioner; empty when no
        # skills are cloned, so an unconfigured box just spawns without them.
        env["_skill_mounts"] = json.dumps(skills_mod.skill_mounts(self.settings.skills_root))
        # Per-session harness state, read-write. Persists the conversation store
        # across container teardown so a resumed session continues its context,
        # not just its committed work.
        state = self.workspace(session["user_id"]).session_state_dir(session["id"])
        env["_state_mounts"] = json.dumps([
            (str(state / "claude-projects"), "/workspace/.claude/projects"),
            (str(state / "codex-sessions"), "/workspace/.codex/sessions"),
        ])
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

        Every event is also published to the session's bus, so a surface watching
        the session sees turns it didn't start — including ones a channel event
        opened. Bounded by `turn_timeout_seconds`: an unreachable provider
        otherwise keeps the harness retrying and the frame streaming nothing.
        """
        session = await self.ensure_running(session_id)
        timeout = self.settings.turn_timeout_seconds
        bus = self.streams.bus(session_id)
        async with self.semaphore:
            self._in_flight.add(session_id)
            # Null while working, so a session list can tell "still going" from
            # "finished" without inspecting the transcript.
            self.registry.set_outcome(session_id, None)
            stream = self.provisioner.run_turn(
                session,
                prompt,
                self.system_prompt(session),
                channel_config=self.settings.channel_config_path,
            )
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(stream.__anext__(), timeout)
                    except StopAsyncIteration:
                        break
                    if event["kind"] == "session" and event.get("resume_id"):
                        self.registry.update_session(session_id, resume_id=event["resume_id"])
                    yield bus.publish(event)
            except asyncio.TimeoutError:
                yield bus.publish(
                    {
                        "kind": "error",
                        "text": f"turn timed out after {timeout}s with no output",
                    }
                )
            finally:
                self._in_flight.discard(session_id)
                # A turn that died without a terminal event still has its last
                # text run buffered; commit it rather than lose it.
                self.transcript.flush(session_id)
                await stream.aclose()
        self.registry.touch(session_id)

    # --- streams -----------------------------------------------------------

    def subscribe(self, session_id: str, since: int | None = None) -> Subscription:
        """Watch everything a session emits, whoever started it.

        `since` is the last `seq` the surface rendered; the tail after it is
        replayed before the live stream so a reconnect comes back whole.
        """
        self.get(session_id)
        return self.streams.bus(session_id).subscribe(since)

    def _publish_unsolicited(self, session_id: str, event: dict[str, Any]) -> None:
        """Harness output nobody asked for — a wake, or a background job landing."""
        if event["kind"] == "session" and event.get("resume_id"):
            self.registry.update_session(session_id, resume_id=event["resume_id"])
        if event["kind"] in ("result", "error"):
            # A session being woken by channel events is working, even though no
            # surface prompted it; the reaper reads `last_active`.
            self.registry.touch(session_id)
        self.streams.publish(session_id, event)

    def run_turn_in_background(self, session_id: str, prompt: str) -> "asyncio.Task[None]":
        """Start a turn nobody is holding open, and fan it out to the bus."""

        async def drive() -> None:
            try:
                async for _ in self.turn(session_id, prompt):
                    pass
            except SessionError as exc:
                self.streams.publish(session_id, {"kind": "error", "text": str(exc)})

        task = asyncio.create_task(drive())
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    # --- channel -----------------------------------------------------------

    def deliver(
        self, session_id: str, content: str, meta: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Queue an inbound event for the session's shim to collect.

        The control plane is the only component that knows about users, so
        allowlisting happens before this call, never in the container.
        """
        session = self.get(session_id)
        if session["status"] == registry_mod.ARCHIVED:
            raise SessionError("cannot deliver to an archived session")
        queue = self.streams.channel(session_id)
        queue.put(content, meta)
        return {"queued": queue.depth}

    async def channel_events(
        self, session_id: str, timeout: float = CHANNEL_POLL_TIMEOUT_SECONDS
    ) -> list[dict[str, Any]]:
        """The shim's long poll. Empty on timeout so it reissues cleanly."""
        self.get(session_id)
        return await self.streams.channel(session_id).take(timeout)

    def channel_reply(self, session_id: str, chat_id: str, text: str) -> dict[str, Any]:
        """A reply the agent routed back out through the channel."""
        self.get(session_id)
        event = {"kind": "reply", "chat_id": chat_id, "text": text}
        self.streams.publish(session_id, event)
        return event

    async def attach_tty(self, session_id: str) -> Any:
        """An interactive shell on the session's container, for the TUI pane."""
        session = await self.ensure_running(session_id)
        if not session["container_id"]:
            raise SessionError("session has no container")
        return await self.provisioner.attach_tty(session["container_id"])

    async def interrupt(self, session_id: str) -> bool:
        """Cut an in-flight turn short. False if the session wasn't working."""
        self.get(session_id)
        return await self.provisioner.interrupt(session_id)

    async def app_port(self, session_id: str) -> int:
        """The session app's host port, provisioning the container if needed."""
        session = await self.ensure_running(session_id)
        port = session.get("app_port")
        if not port:
            raise SessionError("session has no app port")
        return int(port)

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
        # Nothing will ever drain this session's channel queue again. The
        # transcript stays: reading back an archived session is the point of it.
        self.transcript.flush(session_id)
        self.streams.discard(session_id)
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
        self.transcript.discard(session_id)
        self.streams.discard(session_id)
        self.registry.delete_session_events(session_id)
        self.workspace(session["user_id"]).remove_session_state(session_id)
        self.registry.delete_session(session_id)

    async def recover(self) -> dict[str, list[str]]:
        """Make the session table true again after frame-main restarts.

        frame-main can restart out from under its containers: a redeploy leaves
        them running, a host reboot takes them with it, and either way the
        `container_id` on a session row can no longer be trusted. A session
        whose container is still up is re-adopted as-is — the next turn re-execs
        its harness with `--resume`, so nothing is lost. A session whose
        container is gone has its `container_id` cleared, so the next turn
        re-provisions cleanly from `origin.git` rather than talking to a dead
        id. A live container with no session still claiming it is an orphan and
        is removed. Nothing here deletes a session: a stranded container is a
        resource to reconcile, never a reason to lose someone's work.
        """
        live = await self.provisioner.live_sessions()
        adopted: list[str] = []
        cleared: list[str] = []
        orphaned: list[str] = []
        claimed: set[str] = set()
        for session in self.registry.running_sessions():
            claimed.add(session["id"])
            if session["id"] in live:
                adopted.append(session["id"])
            else:
                self.registry.update_session(session["id"], container_id=None, app_port=None)
                cleared.append(session["id"])
        for session_id in live - claimed:
            await self.provisioner.remove_session(session_id)
            orphaned.append(session_id)
        return {"adopted": adopted, "cleared": cleared, "orphaned": orphaned}

    async def reap_idle(self, now: datetime | None = None) -> list[str]:
        """Stop containers idle past the timeout. Returns the sessions reaped."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=self.settings.idle_timeout_minutes)
        reaped = []
        for session in self.registry.running_sessions():
            if session["id"] in self._in_flight:
                continue
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
