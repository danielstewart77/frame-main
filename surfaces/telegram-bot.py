#!/usr/bin/env python3
"""Telegram surface: one bot per user. Entrypoint — never imported.

Routing logic lives in `surfaces/chat.py`; this file is Telegram IO only:
long-poll updates, forward text/voice to the attached session, and stream the
reply back with debounced `editMessageText`.
"""

import asyncio
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import load  # noqa: E402
from surfaces.chat import ChatRouter, HttpClient, Reply  # noqa: E402

EDIT_INTERVAL = 1.2  # seconds between editMessageText calls while streaming


class TelegramBot:
    def __init__(self, token: str, server_url: str):
        self.token = token
        self.api = f"https://api.telegram.org/bot{token}"
        self.file_api = f"https://api.telegram.org/file/bot{token}"
        self.server_url = server_url.rstrip("/")
        self.router = ChatRouter(HttpClient(server_url), surface="telegram")
        self.http = httpx.AsyncClient(timeout=70.0)

    async def run(self) -> None:
        offset = 0
        while True:
            try:
                response = await self.http.get(
                    f"{self.api}/getUpdates", params={"offset": offset, "timeout": 60}
                )
                updates = response.json().get("result", [])
            except httpx.HTTPError as exc:
                print(f"telegram poll failed: {exc}", file=sys.stderr)
                await asyncio.sleep(3)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    await self.dispatch(update)
                except Exception as exc:  # one bad update must not kill the bot
                    print(f"update {update['update_id']} failed: {exc}", file=sys.stderr)

    async def dispatch(self, update: dict) -> None:
        if "callback_query" in update:
            query = update["callback_query"]
            chat_id = str(query["message"]["chat"]["id"])
            await self.http.post(
                f"{self.api}/answerCallbackQuery", json={"callback_query_id": query["id"]}
            )
            session_id = query.get("data", "")
            reply = await asyncio.to_thread(self.router.tap, chat_id, session_id)
            await self.render(chat_id, reply)
            return

        message = update.get("message")
        if not message:
            return
        chat_id = str(message["chat"]["id"])
        name = message["chat"].get("username") or message["chat"].get("first_name")

        text = message.get("text", "")
        if "voice" in message or "audio" in message:
            text = await self.transcribe(message.get("voice") or message["audio"])

        reply = await asyncio.to_thread(self.router.handle, chat_id, text, name)
        await self.render(chat_id, reply)

    async def transcribe(self, audio_meta: dict) -> str:
        info = await self.http.get(f"{self.api}/getFile", params={"file_id": audio_meta["file_id"]})
        path = info.json()["result"]["file_path"]
        blob = await self.http.get(f"{self.file_api}/{path}")
        response = await self.http.post(
            f"{self.server_url}/voice/transcribe",
            files={"file": ("voice.ogg", blob.content, audio_meta.get("mime_type", "audio/ogg"))},
        )
        response.raise_for_status()
        return response.json()["text"]

    async def render(self, chat_id: str, reply: Reply) -> None:
        if reply.prompt and reply.session_id:
            await self.stream_turn(chat_id, reply.session_id, reply.prompt)
            return
        payload = {"chat_id": chat_id, "text": reply.text or "..."}
        if reply.buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": b.label, "callback_data": b.session_id}] for b in reply.buttons
                ]
            }
        await self.http.post(f"{self.api}/sendMessage", json=payload)

    async def stream_turn(self, chat_id: str, session_id: str, prompt: str) -> None:
        sent = await self.http.post(
            f"{self.api}/sendMessage", json={"chat_id": chat_id, "text": "..."}
        )
        message_id = sent.json()["result"]["message_id"]
        buffer, last_edit, shown = "", 0.0, ""

        async with self.http.stream(
            "POST", f"{self.server_url}/sessions/{session_id}/turn",
            json={"prompt": prompt}, timeout=None,
        ) as response:
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                event = httpx.Response(200, text=line).json()
                if event["kind"] == "text":
                    buffer += event["text"]
                elif event["kind"] in {"result", "error"}:
                    buffer = event["text"] or buffer
                elif event["kind"] == "tool":
                    buffer += f"\n[{event['name']}]\n"
                now = time.monotonic()
                if buffer and buffer != shown and now - last_edit > EDIT_INTERVAL:
                    await self.edit(chat_id, message_id, buffer)
                    shown, last_edit = buffer, now

        if buffer and buffer != shown:
            await self.edit(chat_id, message_id, buffer)

    async def edit(self, chat_id: str, message_id: int, text: str) -> None:
        await self.http.post(
            f"{self.api}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id, "text": text[-4000:]},
        )


def main() -> None:
    settings = load()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    asyncio.run(TelegramBot(settings.telegram_bot_token, settings.server_url).run())


if __name__ == "__main__":
    main()
