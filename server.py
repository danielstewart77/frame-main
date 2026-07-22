"""The agent-server HTTP API — the control plane's only surface contract.

Importable so tests and surfaces can build the app directly; the runnable
entrypoint is `agent-server.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
)
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

import auth as auth_mod
import harness as harness_mod
import proxy as proxy_mod
import registry as registry_mod
import voice as voice_mod
from config import Settings, load
from sandbox.provision import ProvisionError, get_provisioner
from sessions import SessionError, SessionManager, UnknownSession
from surfaces.telegram import TelegramSupervisor

SURFACES = {"telegram", "web"}
CONSOLE_DIR = Path(__file__).resolve().parent / "console"
AUTH_COOKIE = "frame_auth"


# --- request bodies ---------------------------------------------------------


class UserCreate(BaseModel):
    display_name: str


class Register(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8)
    display_name: str | None = None


class Login(BaseModel):
    username: str
    password: str


class IdentityLink(BaseModel):
    surface: str
    external_id: str
    display_name: str | None = None


class SessionCreate(BaseModel):
    harness: str | None = None
    model: str | None = None
    title: str | None = None
    color: str | None = None


class SessionPatch(BaseModel):
    title: str | None = None
    color: str | None = None
    status: str | None = None
    frame_state: str | None = None
    speaker: bool | None = None


class TurnRequest(BaseModel):
    prompt: str = Field(min_length=1)


class ChannelEvent(BaseModel):
    content: str = Field(min_length=1)
    meta: dict[str, str] = Field(default_factory=dict)


class ChannelReply(BaseModel):
    chat_id: str
    text: str


class AttachRequest(BaseModel):
    session_id: str


class LayoutPatch(BaseModel):
    sidebar_collapsed: bool


class SpeakRequest(BaseModel):
    text: str
    voice: str | None = None


class TelegramConfig(BaseModel):
    bot_token: str = Field(min_length=1)


class ProxyKeyConfig(BaseModel):
    api_key: str = Field(min_length=1)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class AdminUserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    display_name: str | None = None
    role: str = "user"


class RoleChange(BaseModel):
    role: str


# --- app --------------------------------------------------------------------


async def _reap_loop(app: FastAPI) -> None:
    """Stop idle containers on a timer.

    `reap_idle` is the whole of the container lifecycle policy, and without
    something calling it the fleet only ever grows: a box running sessions
    unattended exhausts its memory or its app-port range and takes every live
    session down with it. A reap that raises must not kill the loop, or the
    first transient docker error silently disables reaping for the process.
    """
    interval = app.state.settings.reap_interval_seconds
    manager = app.state.manager
    while True:
        await asyncio.sleep(interval)
        try:
            await manager.reap_idle()
            # Same timer clears out login tokens that have aged past their TTL,
            # so the table does not grow without bound over a long-lived process.
            manager.registry.purge_expired_tokens()
        except Exception:  # pragma: no cover - defensive, logged not raised
            logging.getLogger(__name__).exception("idle reap failed")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Reconcile the session table against the containers docker actually has
    # before anything can touch a stale container_id. A recovery that raises
    # must not stop the server coming up — a fresh box has nothing to recover.
    try:
        recovered = await app.state.manager.recover()
        logging.getLogger(__name__).info("recovered sessions: %s", recovered)
    except Exception:  # pragma: no cover - defensive, logged not raised
        logging.getLogger(__name__).exception("session recovery failed")
    # Ensure the box always has an admin. On a fresh box the first registrant
    # becomes admin; on a box that predates roles (every account defaulted to
    # 'user'), promote the earliest credentialed account so administration is
    # reachable without the service token. Idempotent once an admin exists.
    registry = app.state.registry
    if registry.count_credentials() and registry.admin_count() == 0:
        oldest = registry.first_credentialed_user()
        if oldest:
            registry.set_role(oldest, registry_mod.ROLE_ADMIN)
            logging.getLogger(__name__).info("bootstrapped admin: %s", oldest)
    reaper = asyncio.create_task(_reap_loop(app))
    supervisor = TelegramSupervisor(
        app.state.manager, app.state.registry, app.state.settings
    )
    app.state.telegram = supervisor
    telegram = asyncio.create_task(supervisor.run())
    try:
        yield
    finally:
        for task in (reaper, telegram):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await app.state.proxy_client.aclose()


def create_app(
    settings: Settings | None = None,
    registry: registry_mod.Registry | None = None,
    provisioner: Any = None,
    voice: voice_mod.VoiceService | None = None,
    proxy_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or load()
    registry = registry or registry_mod.Registry(settings.db_path)
    provisioner = provisioner or get_provisioner(
        settings.provisioner, settings.sandbox_image, settings.app_port_range
    )
    voice = voice or voice_mod.get_voice(settings.voice, settings)
    manager = SessionManager(registry, settings, provisioner)

    app = FastAPI(title="frame-main agent-server", lifespan=_lifespan)
    app.state.settings = settings
    app.state.registry = registry
    app.state.manager = manager
    app.state.voice = voice
    app.state.proxy_client = proxy_client or httpx.AsyncClient(
        follow_redirects=False, timeout=30.0
    )

    def get_manager(request: Request) -> SessionManager:
        return request.app.state.manager

    # --- authentication ----------------------------------------------------

    def _identify(token: str | None) -> auth_mod.Principal | None:
        """Resolve a bearer token to whoever holds it, or None.

        A disabled user resolves to None — their live tokens stop working the
        moment an admin disables them, without waiting for expiry. The service
        token bypasses the DB and cannot be disabled this way.
        """
        if not token:
            return None
        if settings.service_token and auth_mod.tokens_match(token, settings.service_token):
            return auth_mod.Principal(auth_mod.SERVICE)
        user_id = registry.user_for_token(auth_mod.token_digest(token))
        if not user_id:
            return None
        user = registry.get_user(user_id)
        if not user or user["disabled"]:
            return None
        return auth_mod.Principal(auth_mod.USER, user_id, role=user["role"])

    def _presented(headers: Any, cookies: dict[str, str]) -> str | None:
        """A token from the Authorization header, else the console's cookie.

        The cookie exists because a browser cannot attach a header to a
        WebSocket handshake or an <img> load; it is `httponly` and `samesite`
        so it is not readable by script and does not ride a cross-site form.
        """
        return auth_mod.bearer(headers.get("authorization")) or cookies.get(AUTH_COOKIE)

    def principal(request: Request) -> auth_mod.Principal:
        who = _identify(_presented(request.headers, request.cookies))
        if not who:
            raise HTTPException(
                401, "authentication required", headers={"WWW-Authenticate": "Bearer"}
            )
        return who

    def service_only(who: auth_mod.Principal = Depends(principal)) -> auth_mod.Principal:
        """Fleet-wide routes: minting and listing users, resolving a chat to an account.

        A logged-in user is not the fleet operator and must not reach these;
        they take the service token, the operator/admin credential.
        """
        if not who.is_service:
            raise HTTPException(403, "service credentials required")
        return who

    def admin_only(who: auth_mod.Principal = Depends(principal)) -> auth_mod.Principal:
        """User administration: create users, reset passwords, roles, enable/disable.

        The service token is a superuser and qualifies; among logged-in users
        only the admin role does. A normal user gets 403.
        """
        if not who.is_admin:
            raise HTTPException(403, "admin credentials required")
        return who

    def socket_principal(websocket: WebSocket) -> auth_mod.Principal | None:
        token = auth_mod.bearer(
            websocket.headers.get("authorization")
        ) or websocket.cookies.get(AUTH_COOKIE) or websocket.query_params.get("token")
        return _identify(token)

    def owned(
        manager: SessionManager, who: auth_mod.Principal, session_id: str
    ) -> dict[str, Any]:
        """Resolve a session the caller is entitled to.

        A session belonging to someone else answers 404, not 403: confirming
        that an id exists is itself a leak across accounts.
        """
        session = _resolve(manager, session_id)
        if not who.owns(session["user_id"]):
            raise HTTPException(404, f"no such session: {session_id}")
        return session

    def owned_surface(who: auth_mod.Principal, surface: str, external_id: str) -> None:
        """A surface binding belongs to the account that identity maps to.

        The web console keys its surface on the user's own id (see
        `console_bootstrap`), so a user always owns `web/<their user_id>`; every
        other surface is owned via the identity row that maps it to an account.
        """
        _check_surface(surface)
        if who.is_service or external_id == who.user_id:
            return
        if registry.resolve_identity(surface, external_id) != who.user_id:
            raise HTTPException(403, "not your surface")

    def _own_user(who: auth_mod.Principal, user_id: str) -> None:
        if not who.owns(user_id):
            raise HTTPException(403, "not your account")

    def channel_caller(session_id: str, request: Request) -> None:
        """Authorize a call from a container's channel shim.

        The shim holds a bearer minted for one session; it may touch that
        session's channel and nothing else. A bad or mismatched token is a 401,
        never a 404, because the shim is not browsing for ids — it either has
        its own capability or it does not.
        """
        token = auth_mod.bearer(request.headers.get("authorization"))
        if not token or registry.session_for_channel_token(token) != session_id:
            raise HTTPException(
                401, "invalid channel token", headers={"WWW-Authenticate": "Bearer"}
            )

    @app.post("/auth/register", status_code=201)
    def register(request: Request, body: Register) -> dict[str, Any]:
        """Create an account with a console login.

        Open only while no account exists, so a fresh box can be claimed by the
        operator standing in front of it. After that it takes the service token,
        the operator/admin credential.
        """
        first_account = registry.count_credentials() == 0
        if not first_account:
            who = _identify(_presented(request.headers, request.cookies))
            if not who or not who.is_service:
                raise HTTPException(403, "registration is closed; use the service token")
        username = body.username.strip()
        if not username:
            raise HTTPException(400, "username must not be blank")
        if registry.credential_by_username(username):
            raise HTTPException(409, "username taken")
        user = registry.create_user(body.display_name or username)
        registry.set_credential(
            user["user_id"], username, auth_mod.hash_password(body.password)
        )
        # Whoever claims a fresh box is its admin — the same bootstrap the proxy
        # does from env. Later accounts default to plain users.
        if first_account:
            registry.set_role(user["user_id"], registry_mod.ROLE_ADMIN)
        manager.workspace(user["user_id"]).ensure()
        return {"user_id": user["user_id"], "username": username,
                "display_name": user["display_name"],
                "role": registry_mod.ROLE_ADMIN if first_account else registry_mod.ROLE_USER}

    @app.post("/auth/login")
    def login(body: Login, response: Response) -> dict[str, Any]:
        credential = registry.credential_by_username(body.username.strip())
        # Hash-compare even on a miss, so a wrong username and a wrong password
        # take the same time and the endpoint is not a username oracle.
        stored = credential["password_hash"] if credential else auth_mod.hash_password("_")
        if not auth_mod.verify_password(body.password, stored) or not credential:
            raise HTTPException(401, "bad username or password")
        user = registry.get_user(credential["user_id"])
        if user and user["disabled"]:
            raise HTTPException(403, "account disabled")
        token = auth_mod.new_token()
        issued = registry.store_token(
            auth_mod.token_digest(token), credential["user_id"], settings.auth_token_ttl_hours
        )
        registry.update_last_login(credential["user_id"])
        response.set_cookie(
            AUTH_COOKIE,
            token,
            max_age=settings.auth_token_ttl_hours * 3600,
            httponly=True,
            samesite="lax",
        )
        return {
            "token": token,
            "user_id": credential["user_id"],
            "username": credential["username"],
            "expires_at": issued["expires_at"],
            "must_change_pw": bool(user and user["must_change_pw"]),
        }

    @app.post("/auth/logout", status_code=204)
    def logout(request: Request, response: Response) -> Response:
        token = _presented(request.headers, request.cookies)
        if token:
            registry.delete_token(auth_mod.token_digest(token))
        response = Response(status_code=204)
        response.delete_cookie(AUTH_COOKIE)
        return response

    @app.get("/auth/me")
    def whoami(who: auth_mod.Principal = Depends(principal)) -> dict[str, Any]:
        if who.is_service:
            return {"kind": auth_mod.SERVICE, "user_id": None, "username": None,
                    "role": "service", "is_admin": True, "must_change_pw": False}
        user = registry.get_user(who.user_id) or {}
        credential = registry.credential_for(who.user_id) or {}
        return {
            "kind": auth_mod.USER,
            "user_id": who.user_id,
            "username": credential.get("username"),
            "display_name": user.get("display_name"),
            "role": user.get("role", registry_mod.ROLE_USER),
            "is_admin": who.is_admin,
            "must_change_pw": bool(user.get("must_change_pw")),
        }

    @app.post("/auth/password", status_code=204)
    def change_password(
        request: Request, body: PasswordChange, response: Response
    ) -> Response:
        """Change your own password: verify the current one, set the new one,
        then invalidate every live token so other sessions must log in again."""
        who = principal(request)
        if who.is_service:
            raise HTTPException(400, "the service token has no password")
        credential = registry.credential_for(who.user_id)
        if not credential or not auth_mod.verify_password(
            body.current_password, credential["password_hash"]
        ):
            raise HTTPException(403, "current password is incorrect")
        registry.set_credential(
            who.user_id, credential["username"], auth_mod.hash_password(body.new_password)
        )
        registry.set_must_change_pw(who.user_id, False)
        registry.delete_user_tokens(who.user_id)
        response = Response(status_code=204)
        response.delete_cookie(AUTH_COOKIE)
        return response

    # --- health ------------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "provisioner": settings.provisioner,
            "voice": settings.voice,
            "running": len(registry.running_sessions()),
        }

    # --- users + identities ------------------------------------------------

    @app.post("/users", status_code=201)
    def create_user(
        body: UserCreate, who: auth_mod.Principal = Depends(service_only)
    ) -> dict[str, Any]:
        return registry.create_user(body.display_name)

    @app.get("/users")
    def list_users(who: auth_mod.Principal = Depends(service_only)) -> list[dict[str, Any]]:
        return registry.list_users()

    @app.post("/identities")
    def resolve_identity(
        body: IdentityLink,
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(service_only),
    ) -> dict[str, Any]:
        _check_surface(body.surface)
        user_id = manager.resolve_user(body.surface, body.external_id, body.display_name)
        return {"user_id": user_id}

    # --- user administration (admin role or service token) -----------------

    def _user_view(user: dict[str, Any]) -> dict[str, Any]:
        """A user row for the admin panel, with its login name folded in."""
        credential = registry.credential_for(user["user_id"]) or {}
        return {
            "user_id": user["user_id"],
            "display_name": user["display_name"],
            "username": credential.get("username"),
            "role": user.get("role", registry_mod.ROLE_USER),
            "disabled": bool(user.get("disabled")),
            "must_change_pw": bool(user.get("must_change_pw")),
            "last_login_at": user.get("last_login_at"),
            "created_at": user.get("created_at"),
        }

    @app.get("/admin/users")
    def admin_list_users(
        who: auth_mod.Principal = Depends(admin_only),
    ) -> list[dict[str, Any]]:
        return [_user_view(u) for u in registry.list_users()]

    @app.post("/admin/users", status_code=201)
    def admin_create_user(
        body: AdminUserCreate, who: auth_mod.Principal = Depends(admin_only)
    ) -> dict[str, Any]:
        """Create a user with a console login and a one-time temporary password.

        The temp password is returned exactly once, here; the account is flagged
        `must_change_pw` so the user must set their own before doing anything."""
        if body.role not in registry_mod._ROLES:
            raise HTTPException(400, f"bad role: {body.role}")
        username = body.username.strip()
        if not username:
            raise HTTPException(400, "username must not be blank")
        if registry.credential_by_username(username):
            raise HTTPException(409, "username taken")
        temp = _temp_password()
        user = registry.create_user(body.display_name or username)
        registry.set_credential(user["user_id"], username, auth_mod.hash_password(temp))
        registry.set_role(user["user_id"], body.role)
        registry.set_must_change_pw(user["user_id"], True)
        manager.workspace(user["user_id"]).ensure()
        return {**_user_view(registry.get_user(user["user_id"])), "temp_password": temp}

    @app.post("/admin/users/{user_id}/reset-password")
    def admin_reset_password(
        user_id: str, who: auth_mod.Principal = Depends(admin_only)
    ) -> dict[str, Any]:
        """Reset a user's password to a fresh one-time value and force a change.
        Their live sessions are logged out."""
        target = registry.get_user(user_id)
        if not target:
            raise HTTPException(404, "no such user")
        credential = registry.credential_for(user_id)
        if not credential:
            raise HTTPException(400, "user has no console login to reset")
        temp = _temp_password()
        registry.set_credential(user_id, credential["username"], auth_mod.hash_password(temp))
        registry.set_must_change_pw(user_id, True)
        registry.delete_user_tokens(user_id)
        return {"user_id": user_id, "username": credential["username"], "temp_password": temp}

    @app.post("/admin/users/{user_id}/role")
    def admin_change_role(
        user_id: str, body: RoleChange, who: auth_mod.Principal = Depends(admin_only)
    ) -> dict[str, Any]:
        if body.role not in registry_mod._ROLES:
            raise HTTPException(400, f"bad role: {body.role}")
        target = registry.get_user(user_id)
        if not target:
            raise HTTPException(404, "no such user")
        # Don't strip the last admin, and don't let an admin demote themselves
        # into a room with no admins left.
        if (
            target["role"] == registry_mod.ROLE_ADMIN
            and body.role != registry_mod.ROLE_ADMIN
            and registry.admin_count() <= 1
        ):
            raise HTTPException(400, "cannot remove the last admin")
        registry.set_role(user_id, body.role)
        return _user_view(registry.get_user(user_id))

    @app.post("/admin/users/{user_id}/disable")
    def admin_disable_user(
        user_id: str, who: auth_mod.Principal = Depends(admin_only)
    ) -> dict[str, Any]:
        target = registry.get_user(user_id)
        if not target:
            raise HTTPException(404, "no such user")
        if who.user_id == user_id:
            raise HTTPException(400, "cannot disable your own account")
        if target["role"] == registry_mod.ROLE_ADMIN and registry.admin_count() <= 1:
            raise HTTPException(400, "cannot disable the last admin")
        registry.set_disabled(user_id, True)
        registry.delete_user_tokens(user_id)  # cut off live sessions immediately
        return _user_view(registry.get_user(user_id))

    @app.post("/admin/users/{user_id}/enable")
    def admin_enable_user(
        user_id: str, who: auth_mod.Principal = Depends(admin_only)
    ) -> dict[str, Any]:
        target = registry.get_user(user_id)
        if not target:
            raise HTTPException(404, "no such user")
        registry.set_disabled(user_id, False)
        return _user_view(registry.get_user(user_id))

    # --- sessions ----------------------------------------------------------

    @app.post("/users/{user_id}/sessions", status_code=201)
    def create_session(
        user_id: str,
        body: SessionCreate,
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(principal),
    ) -> dict[str, Any]:
        _own_user(who, user_id)
        try:
            return manager.create(
                user_id, body.harness, body.model, body.title, body.color
            )
        except UnknownSession as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/users/{user_id}/sessions")
    def list_sessions(
        user_id: str,
        status: str = registry_mod.ACTIVE,
        who: auth_mod.Principal = Depends(principal),
    ) -> list[dict[str, Any]]:
        _own_user(who, user_id)
        return registry.list_sessions(user_id, status)

    @app.get("/users/{user_id}/frames")
    def open_frames(
        user_id: str, who: auth_mod.Principal = Depends(principal)
    ) -> list[dict[str, Any]]:
        """Console layout restore: which sessions are open and in what state."""
        _own_user(who, user_id)
        return registry.open_frames(user_id)

    # --- per-user telegram bot ---------------------------------------------

    def _telegram_summary(user_id: str) -> dict[str, Any]:
        """The bot's state, minus the token — the token is write-only over the API."""
        bot = registry.get_telegram_bot(user_id)
        if not bot:
            return {"configured": False, "enabled": False, "owner_chat_id": None}
        return {
            "configured": True,
            "enabled": bool(bot["enabled"]),
            "owner_chat_id": bot["owner_chat_id"],
        }

    @app.get("/users/{user_id}/telegram")
    def get_telegram(
        user_id: str, who: auth_mod.Principal = Depends(principal)
    ) -> dict[str, Any]:
        _own_user(who, user_id)
        return _telegram_summary(user_id)

    @app.put("/users/{user_id}/telegram")
    def put_telegram(
        user_id: str,
        body: TelegramConfig,
        who: auth_mod.Principal = Depends(principal),
    ) -> dict[str, Any]:
        """Set (or replace) this user's bot token. The supervisor picks it up on
        its next reconcile; a changed token re-opens owner enrollment."""
        _own_user(who, user_id)
        token = body.bot_token.strip()
        if not token:
            raise HTTPException(400, "bot_token must not be blank")
        registry.set_telegram_bot(user_id, token)
        return _telegram_summary(user_id)

    @app.delete("/users/{user_id}/telegram", status_code=204)
    def delete_telegram(
        user_id: str, who: auth_mod.Principal = Depends(principal)
    ) -> Response:
        _own_user(who, user_id)
        registry.clear_telegram_bot(user_id)
        return Response(status_code=204)

    # --- per-user proxy key ------------------------------------------------

    @app.get("/users/{user_id}/proxy-key")
    def get_proxy_key(
        user_id: str, who: auth_mod.Principal = Depends(principal)
    ) -> dict[str, Any]:
        """Whether the user has a proxy key set. The key itself is write-only
        over the API — never returned once stored."""
        _own_user(who, user_id)
        return {"configured": registry.has_proxy_key(user_id)}

    @app.put("/users/{user_id}/proxy-key")
    async def put_proxy_key(
        user_id: str,
        body: ProxyKeyConfig,
        request: Request,
        who: auth_mod.Principal = Depends(principal),
    ) -> dict[str, Any]:
        """Set (or replace) the user's proxy key. Their sessions and model
        picker use it instead of the box-wide token. Best-effort validated
        against the proxy: a key the proxy rejects outright (401) is refused, but
        a transient/unreachable proxy does not block saving."""
        _own_user(who, user_id)
        key = body.api_key.strip()
        if not key:
            raise HTTPException(400, "api_key must not be blank")
        base = settings.anthropic_base_url.rstrip("/")
        if base:
            try:
                resp = await request.app.state.proxy_client.get(
                    base + "/v1/models", headers={"Authorization": f"Bearer {key}"}
                )
                if resp.status_code in (401, 403):
                    raise HTTPException(400, "the proxy rejected that key")
            except httpx.HTTPError:
                pass  # transient/unreachable — accept and let it prove out later
        registry.set_proxy_key(user_id, key)
        return {"configured": True}

    @app.delete("/users/{user_id}/proxy-key", status_code=204)
    def delete_proxy_key(
        user_id: str, who: auth_mod.Principal = Depends(principal)
    ) -> Response:
        _own_user(who, user_id)
        registry.clear_proxy_key(user_id)
        return Response(status_code=204)

    @app.get("/sessions/{session_id}")
    def get_session(
        session_id: str,
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(principal),
    ):
        return owned(manager, who, session_id)

    @app.patch("/sessions/{session_id}")
    def patch_session(
        session_id: str,
        body: SessionPatch,
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(principal),
    ) -> dict[str, Any]:
        owned(manager, who, session_id)
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        if not fields:
            return manager.get(session_id)
        try:
            return registry.update_session(session_id, **fields)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/sessions/{session_id}/turn")
    async def turn(
        session_id: str,
        body: TurnRequest,
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(principal),
    ) -> StreamingResponse:
        owned(manager, who, session_id)

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for event in manager.turn(session_id, body.prompt):
                    yield (json.dumps(event) + "\n").encode()
            except SessionError as exc:
                yield (json.dumps({"kind": "error", "text": str(exc)}) + "\n").encode()

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @app.websocket("/sessions/{session_id}/stream")
    async def stream_socket(websocket: WebSocket, session_id: str) -> None:
        """The frame's live stream: everything the session emits, whoever started it.

        The socket subscribes rather than drives. A prompt sent over it starts a
        turn in the background and the events come back the same way a
        channel-opened turn does, so a frame sees work it didn't initiate.

        Reconnect with `?since=<seq>` to have the missed tail replayed first. A
        socket that stops draining is closed with 4429 rather than silently
        starved, because a reconnect can be made whole and a hole can't.
        """
        manager: SessionManager = websocket.app.state.manager
        await websocket.accept()
        who = socket_principal(websocket)
        if not who:
            await websocket.close(code=4401, reason="authentication required")
            return
        # A session the caller may not see is reported as absent, same as the
        # HTTP side — the error event keeps the existing wire contract.
        session = manager.registry.get_session(session_id)
        if not session or not who.owns(session["user_id"]):
            await websocket.send_json({"kind": "error", "text": f"no such session: {session_id}"})
            await websocket.close()
            return
        raw_since = websocket.query_params.get("since")
        try:
            since = int(raw_since) if raw_since is not None else None
        except ValueError:
            since = None
        try:
            subscription = manager.subscribe(session_id, since)
        except SessionError as exc:
            await websocket.send_json({"kind": "error", "text": str(exc)})
            await websocket.close()
            return

        async def pump() -> None:
            async for event in subscription:
                await websocket.send_json(event)
            if subscription.overflowed:
                await websocket.close(code=4429, reason="stream fell behind; reconnect with since")

        pumping = asyncio.create_task(pump())
        try:
            while True:
                message = await websocket.receive_json()
                prompt = (message or {}).get("prompt", "")
                if not prompt:
                    await websocket.send_json({"kind": "error", "text": "empty prompt"})
                    continue
                manager.run_turn_in_background(session_id, prompt)
        except WebSocketDisconnect:
            return
        finally:
            subscription.close()
            pumping.cancel()

    @app.get("/sessions/{session_id}/channel/events")
    async def channel_events(
        session_id: str,
        request: Request,
        timeout: float = 60.0,
        manager: SessionManager = Depends(get_manager),
    ):
        """Long poll drained by the container's stdio shim."""
        channel_caller(session_id, request)
        _resolve(manager, session_id)
        return {"events": await manager.channel_events(session_id, timeout)}

    @app.post("/sessions/{session_id}/channel/reply")
    async def channel_reply(
        session_id: str,
        body: ChannelReply,
        request: Request,
        manager: SessionManager = Depends(get_manager),
    ):
        """A reply the agent routed back out through its channel."""
        channel_caller(session_id, request)
        _resolve(manager, session_id)
        return manager.channel_reply(session_id, body.chat_id, body.text)

    @app.post("/sessions/{session_id}/channel/deliver", status_code=202)
    async def channel_deliver(
        session_id: str,
        body: ChannelEvent,
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(principal),
    ):
        """Push an event into a running session — the inbound wake path.

        This is the seam a prompt-injection payload would love, because it
        writes straight into a session running with approvals off. So it takes
        the same authority as the session's owner: a service surface, or the
        user who owns it. The container never reaches this route — it drains
        the queue, it does not fill it.
        """
        owned(manager, who, session_id)
        try:
            return manager.deliver(session_id, body.content, body.meta)
        except SessionError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/sessions/{session_id}/interrupt")
    async def interrupt_session(
        session_id: str,
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(principal),
    ):
        owned(manager, who, session_id)
        return {"interrupted": await manager.interrupt(session_id)}

    @app.post("/sessions/{session_id}/start")
    async def start_session(
        session_id: str,
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(principal),
    ):
        owned(manager, who, session_id)
        try:
            return await manager.ensure_running(session_id)
        except SessionError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/sessions/{session_id}/stop")
    async def stop_session(
        session_id: str,
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(principal),
    ):
        owned(manager, who, session_id)
        return await manager.stop(session_id)

    @app.post("/sessions/{session_id}/archive")
    async def archive_session(
        session_id: str,
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(principal),
    ):
        owned(manager, who, session_id)
        return await manager.archive(session_id)

    @app.delete("/sessions/{session_id}", status_code=204)
    async def delete_session(
        session_id: str,
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(principal),
    ):
        owned(manager, who, session_id)
        await manager.delete(session_id)
        return Response(status_code=204)

    @app.get("/sessions/{session_id}/events")
    def session_events(
        session_id: str,
        after_seq: int = 0,
        limit: int = Query(default=1000, ge=1, le=5000),
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(principal),
    ):
        """What the session said, after the fact.

        The bus only serves whoever is attached while it happens, which for an
        unattended run is nobody. This reads the persisted copy, so a session
        that finished overnight can still be read in the morning.
        """
        session = owned(manager, who, session_id)
        # Commit any text run still open, so a read mid-turn is not missing the
        # sentence being written as it is read.
        manager.transcript.flush(session_id)
        events = manager.registry.session_events(session_id, after_seq, limit)
        return {
            "session_id": session_id,
            "status": session["status"],
            "outcome": session["outcome"],
            "running": session_id in manager._in_flight,
            "events": events,
            "last_seq": events[-1]["seq"] if events else after_seq,
        }

    # --- pull down and browse ----------------------------------------------

    @app.get("/sessions/{session_id}/diff")
    def session_diff(
        session_id: str,
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(principal),
    ):
        session = owned(manager, who, session_id)
        workspace = manager.workspace(session["user_id"])
        return {"session_id": session_id, "branch": session["branch"],
                "diff": workspace.diff(session["branch"])}

    @app.get("/sessions/{session_id}/clone-url")
    def session_clone_url(
        session_id: str,
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(principal),
    ):
        session = owned(manager, who, session_id)
        workspace = manager.workspace(session["user_id"])
        return {
            "clone_url": workspace.clone_url(),
            "branch": session["branch"],
            "command": f"git clone -b {session['branch']} {workspace.clone_url()}",
        }

    # --- the web console -----------------------------------------------------

    @app.get("/console")
    def console_page() -> Response:
        """The console shell. Public: the page itself is just the login form
        until a token is in hand. Everything it then fetches is authenticated."""
        return FileResponse(CONSOLE_DIR / "index.html")

    @app.get("/console/bootstrap")
    def console_bootstrap(
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(principal),
    ) -> dict[str, Any]:
        """Everything the console needs to restore itself: identity + layout.

        Keyed to the logged-in user, not an anonymous cookie: the web surface's
        `external_id` is the user's own id, so layout and bindings follow the
        account across browsers rather than living on one device's cookie.
        """
        if who.is_service:
            raise HTTPException(403, "console is for user logins, not the service token")
        user_id = who.user_id
        manager.workspace(user_id).ensure()
        credential = registry.credential_for(user_id) or {}
        user = registry.get_user(user_id) or {}
        return {
            "user_id": user_id,
            "external_id": user_id,
            "username": credential.get("username"),
            "role": user.get("role", registry_mod.ROLE_USER),
            "is_admin": who.is_admin,
            "must_change_pw": bool(user.get("must_change_pw")),
            "sidebar_collapsed": registry.sidebar_collapsed("web", user_id),
            "frames": registry.open_frames(user_id),
            "telegram": _telegram_summary(user_id),
            "proxy_key": {"configured": registry.has_proxy_key(user_id)},
            "harnesses": [harness_mod.CLAUDE, harness_mod.CODEX],
            "default_harness": settings.default_harness,
            "default_model": settings.default_model,
        }

    @app.get("/models")
    async def list_models(
        request: Request,
        harness: str = Query(harness_mod.CLAUDE),
        who: auth_mod.Principal = Depends(principal),
    ) -> dict[str, Any]:
        """Models the proxy offers for a harness, for the spawn picker.

        Proxied from the configured inference proxy using frame-main's proxy
        token — the proxy already hides admin-only deployments based on that
        key's role. Degrades to the configured default when no proxy is set or
        it can't be reached, so the picker still works offline."""
        default = settings.default_model
        base = settings.anthropic_base_url.rstrip("/")
        token = registry.get_proxy_key(who.user_id) or settings.ulmaiproxy_auth_token
        fallback = {
            "harness": harness,
            "default": default,
            "models": [{"id": default}] if default else [],
            "source": "fallback",
        }
        if not base or not token:
            return fallback
        # Claude speaks the Anthropic Messages API; codex the OpenAI one.
        path = "/v1/anthropic/models" if harness == harness_mod.CLAUDE else "/v1/models"
        try:
            resp = await request.app.state.proxy_client.get(
                base + path, headers={"Authorization": f"Bearer {token}"}
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
        except (httpx.HTTPError, ValueError):
            return fallback
        models = [
            {"id": m["id"], "label": m.get("label"), "description": m.get("description")}
            for m in data
            if isinstance(m, dict) and m.get("id")
        ]
        return {
            "harness": harness,
            "default": default,
            "models": models or fallback["models"],
            "source": "proxy",
        }

    # --- sidecar panes: browser + full terminal ------------------------------

    @app.api_route(
        "/sessions/{session_id}/app/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def session_app(
        session_id: str,
        path: str,
        request: Request,
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(principal),
    ) -> Response:
        """Browser pane: the session's own app, reverse-proxied off its app_port."""
        owned(manager, who, session_id)
        try:
            port = await manager.app_port(session_id)
        except SessionError as exc:
            raise HTTPException(409, str(exc)) from exc

        try:
            status, headers, content = await proxy_mod.forward(
                request.app.state.proxy_client,
                request.method,
                port,
                path,
                request.url.query,
                dict(request.headers),
                await request.body(),
                base=f"/sessions/{session_id}/app/",
            )
        except proxy_mod.ProxyError as exc:
            raise HTTPException(502, str(exc)) from exc
        return Response(content=content, status_code=status, headers=headers)

    @app.websocket("/sessions/{session_id}/tui")
    async def tui_socket(websocket: WebSocket, session_id: str) -> None:
        """Full-terminal pane: a real pty on the container, framed over the socket.

        Client sends `{"data": "..."}` keystrokes or `{"resize": {rows, cols}}`;
        the server sends terminal output as text frames.
        """
        manager: SessionManager = websocket.app.state.manager
        await websocket.accept()
        who = socket_principal(websocket)
        if not who:
            await websocket.close(code=4401, reason="authentication required")
            return
        try:
            session = manager.get(session_id)
        except UnknownSession as exc:
            await websocket.close(code=4404, reason=str(exc))
            return
        if not who.owns(session["user_id"]):
            await websocket.close(code=4404, reason="no such session")
            return
        try:
            tty = await manager.attach_tty(session_id)
        except (SessionError, ProvisionError) as exc:
            await websocket.close(code=4409, reason=str(exc))
            return

        async def pump_out() -> None:
            while True:
                chunk = await tty.read()
                if not chunk:
                    break
                await websocket.send_text(chunk.decode("utf-8", "replace"))

        pump = asyncio.create_task(pump_out())
        try:
            while True:
                message = await websocket.receive_json()
                if not isinstance(message, dict):
                    continue
                if "resize" in message:
                    size = message["resize"] or {}
                    tty.resize(int(size.get("rows", 24)), int(size.get("cols", 80)))
                data = message.get("data")
                if data:
                    await tty.write(data.encode())
        except WebSocketDisconnect:
            pass
        finally:
            pump.cancel()
            await tty.close()

    # --- surface bindings + layout -----------------------------------------

    @app.post("/surfaces/{surface}/{external_id}/attach")
    def attach(
        surface: str,
        external_id: str,
        body: AttachRequest,
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(principal),
    ):
        owned_surface(who, surface, external_id)
        owned(manager, who, body.session_id)
        return manager.attach(surface, external_id, body.session_id)

    @app.get("/surfaces/{surface}/{external_id}/attach")
    def attached(
        surface: str,
        external_id: str,
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(principal),
    ):
        owned_surface(who, surface, external_id)
        session = manager.attached(surface, external_id)
        if not session:
            raise HTTPException(404, "no session attached")
        return session

    @app.delete("/surfaces/{surface}/{external_id}/attach", status_code=204)
    def detach(
        surface: str,
        external_id: str,
        manager: SessionManager = Depends(get_manager),
        who: auth_mod.Principal = Depends(principal),
    ):
        owned_surface(who, surface, external_id)
        manager.detach(surface, external_id)
        return Response(status_code=204)

    @app.get("/surfaces/{surface}/{external_id}/layout")
    def get_layout(
        surface: str, external_id: str, who: auth_mod.Principal = Depends(principal)
    ):
        owned_surface(who, surface, external_id)
        return {"sidebar_collapsed": registry.sidebar_collapsed(surface, external_id)}

    @app.patch("/surfaces/{surface}/{external_id}/layout")
    def patch_layout(
        surface: str,
        external_id: str,
        body: LayoutPatch,
        who: auth_mod.Principal = Depends(principal),
    ):
        owned_surface(who, surface, external_id)
        registry.set_sidebar_collapsed(surface, external_id, body.sidebar_collapsed)
        return {"sidebar_collapsed": body.sidebar_collapsed}

    # --- voice --------------------------------------------------------------

    @app.post("/voice/transcribe")
    async def transcribe(
        request: Request,
        file: UploadFile = File(...),
        who: auth_mod.Principal = Depends(principal),
    ) -> dict[str, Any]:
        audio = await file.read()
        try:
            result = await request.app.state.voice.transcribe(
                audio, file.content_type or "audio/ogg"
            )
        except voice_mod.VoiceError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"text": result.text}

    @app.post("/voice/speak")
    async def speak(
        request: Request,
        body: SpeakRequest,
        who: auth_mod.Principal = Depends(principal),
    ) -> Response:
        try:
            audio = await request.app.state.voice.speak(body.text, body.voice)
        except voice_mod.VoiceError as exc:
            raise HTTPException(502, str(exc)) from exc
        return Response(content=audio, media_type="audio/mpeg")

    app.mount(
        "/console/static", StaticFiles(directory=CONSOLE_DIR), name="console-static"
    )
    return app


def _resolve(manager: SessionManager, session_id: str) -> dict[str, Any]:
    try:
        return manager.get(session_id)
    except UnknownSession as exc:
        raise HTTPException(404, str(exc)) from exc


def _check_surface(surface: str) -> None:
    if surface not in SURFACES:
        raise HTTPException(400, f"unknown surface: {surface}")


def _temp_password() -> str:
    """A one-time password shown once on admin create/reset (~16 chars)."""
    return secrets.token_urlsafe(12)
