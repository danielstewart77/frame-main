"""The agent-server HTTP API — the control plane's only surface contract.

Importable so tests and surfaces can build the app directly; the runnable
entrypoint is `agent-server.py`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

import harness as harness_mod
import proxy as proxy_mod
import registry as registry_mod
import voice as voice_mod
from config import Settings, load
from sandbox.provision import ProvisionError, get_provisioner
from sessions import SessionError, SessionManager, UnknownSession

SURFACES = {"telegram", "web"}
CONSOLE_DIR = Path(__file__).resolve().parent / "console"
CONSOLE_COOKIE = "frame_console_id"


# --- request bodies ---------------------------------------------------------


class UserCreate(BaseModel):
    display_name: str


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


class AttachRequest(BaseModel):
    session_id: str


class LayoutPatch(BaseModel):
    sidebar_collapsed: bool


class SpeakRequest(BaseModel):
    text: str
    voice: str | None = None


# --- app --------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
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
    def create_user(body: UserCreate) -> dict[str, Any]:
        return registry.create_user(body.display_name)

    @app.get("/users")
    def list_users() -> list[dict[str, Any]]:
        return registry.list_users()

    @app.post("/identities")
    def resolve_identity(
        body: IdentityLink, manager: SessionManager = Depends(get_manager)
    ) -> dict[str, Any]:
        _check_surface(body.surface)
        user_id = manager.resolve_user(body.surface, body.external_id, body.display_name)
        return {"user_id": user_id}

    # --- sessions ----------------------------------------------------------

    @app.post("/users/{user_id}/sessions", status_code=201)
    def create_session(
        user_id: str, body: SessionCreate, manager: SessionManager = Depends(get_manager)
    ) -> dict[str, Any]:
        try:
            return manager.create(
                user_id, body.harness, body.model, body.title, body.color
            )
        except UnknownSession as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/users/{user_id}/sessions")
    def list_sessions(user_id: str, status: str = registry_mod.ACTIVE) -> list[dict[str, Any]]:
        return registry.list_sessions(user_id, status)

    @app.get("/users/{user_id}/frames")
    def open_frames(user_id: str) -> list[dict[str, Any]]:
        """Console layout restore: which sessions are open and in what state."""
        return registry.open_frames(user_id)

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str, manager: SessionManager = Depends(get_manager)):
        return _resolve(manager, session_id)

    @app.patch("/sessions/{session_id}")
    def patch_session(
        session_id: str, body: SessionPatch, manager: SessionManager = Depends(get_manager)
    ) -> dict[str, Any]:
        _resolve(manager, session_id)
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        if not fields:
            return manager.get(session_id)
        try:
            return registry.update_session(session_id, **fields)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/sessions/{session_id}/turn")
    async def turn(
        session_id: str, body: TurnRequest, manager: SessionManager = Depends(get_manager)
    ) -> StreamingResponse:
        _resolve(manager, session_id)

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for event in manager.turn(session_id, body.prompt):
                    yield (json.dumps(event) + "\n").encode()
            except SessionError as exc:
                yield (json.dumps({"kind": "error", "text": str(exc)}) + "\n").encode()

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @app.websocket("/sessions/{session_id}/stream")
    async def stream_socket(websocket: WebSocket, session_id: str) -> None:
        """The frame's default live stream: send a prompt, receive turn events."""
        manager: SessionManager = websocket.app.state.manager
        await websocket.accept()
        try:
            while True:
                message = await websocket.receive_json()
                prompt = (message or {}).get("prompt", "")
                if not prompt:
                    await websocket.send_json({"kind": "error", "text": "empty prompt"})
                    continue
                try:
                    async for event in manager.turn(session_id, prompt):
                        await websocket.send_json(event)
                except SessionError as exc:
                    await websocket.send_json({"kind": "error", "text": str(exc)})
        except WebSocketDisconnect:
            return

    @app.post("/sessions/{session_id}/interrupt")
    async def interrupt_session(session_id: str, manager: SessionManager = Depends(get_manager)):
        _resolve(manager, session_id)
        return {"interrupted": await manager.interrupt(session_id)}

    @app.post("/sessions/{session_id}/start")
    async def start_session(session_id: str, manager: SessionManager = Depends(get_manager)):
        _resolve(manager, session_id)
        try:
            return await manager.ensure_running(session_id)
        except SessionError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/sessions/{session_id}/stop")
    async def stop_session(session_id: str, manager: SessionManager = Depends(get_manager)):
        _resolve(manager, session_id)
        return await manager.stop(session_id)

    @app.post("/sessions/{session_id}/archive")
    async def archive_session(session_id: str, manager: SessionManager = Depends(get_manager)):
        _resolve(manager, session_id)
        return await manager.archive(session_id)

    @app.delete("/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str, manager: SessionManager = Depends(get_manager)):
        _resolve(manager, session_id)
        await manager.delete(session_id)
        return Response(status_code=204)

    # --- pull down and browse ----------------------------------------------

    @app.get("/sessions/{session_id}/diff")
    def session_diff(session_id: str, manager: SessionManager = Depends(get_manager)):
        session = _resolve(manager, session_id)
        workspace = manager.workspace(session["user_id"])
        return {"session_id": session_id, "branch": session["branch"],
                "diff": workspace.diff(session["branch"])}

    @app.get("/sessions/{session_id}/clone-url")
    def session_clone_url(session_id: str, manager: SessionManager = Depends(get_manager)):
        session = _resolve(manager, session_id)
        workspace = manager.workspace(session["user_id"])
        return {
            "clone_url": workspace.clone_url(),
            "branch": session["branch"],
            "command": f"git clone -b {session['branch']} {workspace.clone_url()}",
        }

    # --- the web console -----------------------------------------------------

    @app.get("/console")
    def console_page(request: Request) -> Response:
        """The console shell. Issues the surface identity cookie on first visit."""
        response = FileResponse(CONSOLE_DIR / "index.html")
        if not request.cookies.get(CONSOLE_COOKIE):
            response.set_cookie(
                CONSOLE_COOKIE,
                uuid.uuid4().hex,
                max_age=60 * 60 * 24 * 365,
                httponly=True,
                samesite="lax",
            )
        return response

    @app.get("/console/bootstrap")
    def console_bootstrap(
        request: Request, manager: SessionManager = Depends(get_manager)
    ) -> dict[str, Any]:
        """Everything the console needs to restore itself: identity + layout."""
        external_id = request.cookies.get(CONSOLE_COOKIE)
        if not external_id:
            raise HTTPException(400, "no console identity cookie; reload /console")
        user_id = manager.resolve_user("web", external_id, "console")
        return {
            "user_id": user_id,
            "external_id": external_id,
            "sidebar_collapsed": registry.sidebar_collapsed("web", external_id),
            "frames": registry.open_frames(user_id),
            "harnesses": [harness_mod.CLAUDE, harness_mod.CODEX],
            "default_harness": settings.default_harness,
            "default_model": settings.default_model,
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
    ) -> Response:
        """Browser pane: the session's own app, reverse-proxied off its app_port."""
        _resolve(manager, session_id)
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
        try:
            manager.get(session_id)
        except UnknownSession as exc:
            await websocket.close(code=4404, reason=str(exc))
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
    ):
        _check_surface(surface)
        _resolve(manager, body.session_id)
        return manager.attach(surface, external_id, body.session_id)

    @app.get("/surfaces/{surface}/{external_id}/attach")
    def attached(surface: str, external_id: str, manager: SessionManager = Depends(get_manager)):
        _check_surface(surface)
        session = manager.attached(surface, external_id)
        if not session:
            raise HTTPException(404, "no session attached")
        return session

    @app.delete("/surfaces/{surface}/{external_id}/attach", status_code=204)
    def detach(surface: str, external_id: str, manager: SessionManager = Depends(get_manager)):
        _check_surface(surface)
        manager.detach(surface, external_id)
        return Response(status_code=204)

    @app.get("/surfaces/{surface}/{external_id}/layout")
    def get_layout(surface: str, external_id: str):
        _check_surface(surface)
        return {"sidebar_collapsed": registry.sidebar_collapsed(surface, external_id)}

    @app.patch("/surfaces/{surface}/{external_id}/layout")
    def patch_layout(surface: str, external_id: str, body: LayoutPatch):
        _check_surface(surface)
        registry.set_sidebar_collapsed(surface, external_id, body.sidebar_collapsed)
        return {"sidebar_collapsed": body.sidebar_collapsed}

    # --- voice --------------------------------------------------------------

    @app.post("/voice/transcribe")
    async def transcribe(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
        audio = await file.read()
        try:
            result = await request.app.state.voice.transcribe(
                audio, file.content_type or "audio/ogg"
            )
        except voice_mod.VoiceError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"text": result.text}

    @app.post("/voice/speak")
    async def speak(request: Request, body: SpeakRequest) -> Response:
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
