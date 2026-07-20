"""Telegram surface: one bot per user, supervised in-process.

There is no shared bot and no shared token. Each user pastes the token
BotFather gave them (via `PUT /users/{id}/telegram`), and `TelegramSupervisor`
runs one `BotPoller` long-poll loop per enabled row, reconciling on a timer as
tokens are added, changed, or removed. A personal bot answers only its owner:
the first chat to message it is enrolled and locked in, and every other chat is
ignored in silence.

Routing logic lives in `surfaces/chat.py`; this file is Telegram IO only —
long-poll updates, owner binding, forwarding to the attached session, and
streaming the reply back with debounced `editMessageText`. The Telegram API for
one bot sits behind `TelegramTransport`, a seam tests replace with a fake so
nothing leaves the process.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import suppress
from typing import Any, Callable

import httpx

import voice as voice_mod
from surfaces.chat import ChatRouter, LocalClient, Reply

EDIT_INTERVAL = 1.2  # seconds between editMessageText calls while streaming

_log = logging.getLogger(__name__)


class TelegramTransport:
    """The Telegram Bot API for one bot, over httpx.

    High-level methods, not raw URLs, so a test can drive a `BotPoller` with a
    fake that queues updates and captures what was sent instead of reaching
    api.telegram.org.
    """

    def __init__(self, token: str, http: httpx.AsyncClient | None = None):
        self.api = f"https://api.telegram.org/bot{token}"
        self.file_api = f"https://api.telegram.org/file/bot{token}"
        self.http = http or httpx.AsyncClient(timeout=70.0)

    async def get_updates(self, offset: int, timeout: int = 60) -> list[dict[str, Any]]:
        response = await self.http.get(
            f"{self.api}/getUpdates", params={"offset": offset, "timeout": timeout}
        )
        return response.json().get("result", [])

    async def send_message(
        self, chat_id: str, text: str, reply_markup: dict | None = None
    ) -> int:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text or "..."}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        response = await self.http.post(f"{self.api}/sendMessage", json=payload)
        return response.json()["result"]["message_id"]

    async def edit_message_text(self, chat_id: str, message_id: int, text: str) -> None:
        await self.http.post(
            f"{self.api}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id, "text": text[-4000:]},
        )

    async def answer_callback_query(self, callback_query_id: str) -> None:
        await self.http.post(
            f"{self.api}/answerCallbackQuery", json={"callback_query_id": callback_query_id}
        )

    async def download_file(self, file_id: str) -> bytes:
        info = await self.http.get(f"{self.api}/getFile", params={"file_id": file_id})
        path = info.json()["result"]["file_path"]
        blob = await self.http.get(f"{self.file_api}/{path}")
        return blob.content

    async def close(self) -> None:
        await self.http.aclose()


class BotPoller:
    """The long-poll loop for one user's bot.

    Owner binding is the whole of the access policy: the first chat to message a
    fresh bot is enrolled as its owner and locked in; every other chat is
    dropped. Routing is delegated to `ChatRouter` with the user already known —
    a bot's messages all belong to the one user who configured it.
    """

    def __init__(
        self,
        token: str,
        user_id: str,
        manager: Any,
        registry: Any,
        settings: Any,
        transport: TelegramTransport | None = None,
        voice: voice_mod.VoiceService | None = None,
    ):
        self.token = token
        self.user_id = user_id
        self.manager = manager
        self.registry = registry
        self.settings = settings
        self.transport = transport or TelegramTransport(token)
        self.voice = voice or voice_mod.get_voice(settings.voice, settings)
        self.router = ChatRouter(LocalClient(manager), surface="telegram")

    async def run(self) -> None:
        offset = 0
        while True:
            try:
                updates = await self.transport.get_updates(offset)
            except httpx.HTTPError as exc:
                print(f"telegram poll failed for {self.user_id}: {exc}", file=sys.stderr)
                await asyncio.sleep(3)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    await self.dispatch(update)
                except Exception as exc:  # one bad update must not kill the loop
                    print(
                        f"update {update.get('update_id')} failed: {exc}", file=sys.stderr
                    )

    async def dispatch(self, update: dict) -> None:
        if "callback_query" in update:
            query = update["callback_query"]
            chat_id = str(query["message"]["chat"]["id"])
            if not self._authorize(chat_id):
                return
            await self.transport.answer_callback_query(query["id"])
            session_id = query.get("data", "")
            reply = await asyncio.to_thread(self.router.tap, chat_id, session_id)
            await self.render(chat_id, reply)
            return

        message = update.get("message")
        if not message:
            return
        chat_id = str(message["chat"]["id"])
        if not self._authorize(chat_id):
            return

        text = message.get("text", "")
        if "voice" in message or "audio" in message:
            text = await self.transcribe(message.get("voice") or message["audio"])

        reply = await asyncio.to_thread(self.router.handle, self.user_id, chat_id, text)
        await self.render(chat_id, reply)

    def _authorize(self, chat_id: str) -> bool:
        """Enforce the owner binding, enrolling the first chat that reaches the bot.

        A NULL `owner_chat_id` means the bot is fresh (or its token was just
        changed): this chat claims it. Once set, only that chat is answered.
        """
        bot = self.registry.get_telegram_bot(self.user_id)
        if not bot:
            return False
        owner = bot["owner_chat_id"]
        if owner is None:
            self.registry.set_telegram_owner_chat(self.user_id, chat_id)
            return True
        return owner == chat_id

    async def transcribe(self, audio_meta: dict) -> str:
        blob = await self.transport.download_file(audio_meta["file_id"])
        result = await self.voice.transcribe(
            blob, audio_meta.get("mime_type", "audio/ogg")
        )
        return result.text

    async def render(self, chat_id: str, reply: Reply) -> None:
        if reply.prompt and reply.session_id:
            await self.stream_turn(chat_id, reply.session_id, reply.prompt)
            return
        reply_markup = None
        if reply.buttons:
            reply_markup = {
                "inline_keyboard": [
                    [{"text": b.label, "callback_data": b.session_id}] for b in reply.buttons
                ]
            }
        await self.transport.send_message(chat_id, reply.text or "...", reply_markup)

    async def stream_turn(self, chat_id: str, session_id: str, prompt: str) -> None:
        message_id = await self.transport.send_message(chat_id, "...")
        buffer, last_edit, shown = "", 0.0, ""

        async for event in self.manager.turn(session_id, prompt):
            if event["kind"] == "text":
                buffer += event["text"] or ""
            elif event["kind"] in {"result", "error"}:
                buffer = event["text"] or buffer
            elif event["kind"] == "tool":
                buffer += f"\n[{event.get('name', 'tool')}]\n"
            now = time.monotonic()
            if buffer and buffer != shown and now - last_edit > EDIT_INTERVAL:
                await self.transport.edit_message_text(chat_id, message_id, buffer)
                shown, last_edit = buffer, now

        if buffer and buffer != shown:
            await self.transport.edit_message_text(chat_id, message_id, buffer)

    async def close(self) -> None:
        with suppress(Exception):
            await self.transport.close()


class TelegramSupervisor:
    """Runs one `BotPoller` per enabled `telegram_bots` row, kept in sync on a timer.

    Each `reconcile` diffs the desired set of bots against the pollers actually
    running: it starts one for a new row, cancels and restarts one whose token
    changed, and stops one whose row was removed. A poller that crashes leaves
    its task done rather than taking the supervisor with it, so the next
    reconcile simply starts it again.
    """

    def __init__(
        self,
        manager: Any,
        registry: Any,
        settings: Any,
        transport_factory: Callable[[str, str], TelegramTransport] | None = None,
    ):
        self.manager = manager
        self.registry = registry
        self.settings = settings
        # (token, user_id) -> transport. The default hits api.telegram.org; a
        # test injects a fake so a poller can be driven without a network.
        self._transport_factory = transport_factory
        self._pollers: dict[str, dict[str, Any]] = {}

    async def run(self) -> None:
        try:
            while True:
                await self.reconcile()
                await asyncio.sleep(self.settings.telegram_reconcile_seconds)
        except asyncio.CancelledError:
            await self._shutdown()
            raise

    async def reconcile(self) -> None:
        desired = {row["user_id"]: row["bot_token"] for row in self.registry.list_telegram_bots()}

        # Stop pollers whose row vanished or whose token was replaced.
        for user_id in list(self._pollers):
            if user_id not in desired or desired[user_id] != self._pollers[user_id]["token"]:
                await self._stop(user_id)

        # (Re)start a poller for every desired bot without a live one — a
        # crashed poller's task is done, so it is restarted here too.
        for user_id, token in desired.items():
            existing = self._pollers.get(user_id)
            if existing and not existing["task"].done():
                continue
            self._start(user_id, token)

    def _start(self, user_id: str, token: str) -> None:
        transport = (
            self._transport_factory(token, user_id) if self._transport_factory else None
        )
        poller = BotPoller(
            token, user_id, self.manager, self.registry, self.settings, transport=transport
        )
        task = asyncio.create_task(self._supervise(user_id, poller))
        self._pollers[user_id] = {"token": token, "task": task, "poller": poller}

    async def _supervise(self, user_id: str, poller: BotPoller) -> None:
        try:
            await poller.run()
        except asyncio.CancelledError:
            raise
        except Exception:  # a crashed poller is restarted on the next reconcile
            _log.exception("telegram poller for %s crashed", user_id)

    async def _stop(self, user_id: str) -> None:
        entry = self._pollers.pop(user_id, None)
        if not entry:
            return
        entry["task"].cancel()
        with suppress(asyncio.CancelledError):
            await entry["task"]
        await entry["poller"].close()

    async def _shutdown(self) -> None:
        for user_id in list(self._pollers):
            await self._stop(user_id)
