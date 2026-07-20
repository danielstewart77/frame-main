"""The in-process Telegram supervisor and its per-bot poller.

Driven through a fake transport so nothing reaches api.telegram.org: the fake
hands the poller a queued batch of updates and captures every message it sends
back. That is enough to prove owner binding, routing, and the supervisor's
reconcile diff without a network.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from surfaces.telegram import BotPoller, TelegramSupervisor


class FakeTransport:
    """Stands in for the Telegram Bot API of one bot."""

    def __init__(self):
        self.updates = []           # getUpdates delivers these once, by offset
        self.sent = []              # (chat_id, text)
        self.edits = []             # (chat_id, message_id, text)
        self.answered = []          # callback_query ids
        self._next_message_id = 100

    async def get_updates(self, offset, timeout=60):
        batch = [u for u in self.updates if u["update_id"] >= offset]
        if batch:
            return batch
        await asyncio.sleep(0.01)
        return []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((str(chat_id), text))
        self._next_message_id += 1
        return self._next_message_id

    async def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((str(chat_id), message_id, text))

    async def answer_callback_query(self, callback_query_id):
        self.answered.append(callback_query_id)

    async def download_file(self, file_id):
        return b""

    async def close(self):
        pass


def message(update_id, chat_id, text):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


async def run_until(coro_fn, cond, timeout=1.0):
    """Run a poller until `cond()` holds, then cancel it cleanly."""
    task = asyncio.create_task(coro_fn())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while not cond() and loop.time() < deadline:
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


# --- owner binding ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_chat_enrolls_as_owner_and_is_answered(manager, registry, settings):
    user = registry.create_user("Owner")
    registry.set_telegram_bot(user["user_id"], "tok")
    transport = FakeTransport()
    transport.updates = [message(1, "555", "/help")]
    poller = BotPoller("tok", user["user_id"], manager, registry, settings, transport=transport)

    await run_until(poller.run, lambda: transport.sent)

    assert registry.get_telegram_bot(user["user_id"])["owner_chat_id"] == "555"
    assert "/agents" in transport.sent[0][1]


@pytest.mark.asyncio
async def test_a_non_owner_chat_is_ignored(manager, registry, settings):
    user = registry.create_user("Owner")
    registry.set_telegram_bot(user["user_id"], "tok")
    registry.set_telegram_owner_chat(user["user_id"], "555")
    transport = FakeTransport()
    transport.updates = [message(1, "999", "/help")]
    poller = BotPoller("tok", user["user_id"], manager, registry, settings, transport=transport)

    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert transport.sent == []
    # The owner binding is untouched by the interloper.
    assert registry.get_telegram_bot(user["user_id"])["owner_chat_id"] == "555"


@pytest.mark.asyncio
async def test_an_owner_message_routes_to_the_session_and_streams_a_reply(
    manager, registry, settings
):
    user = registry.create_user("Owner")
    registry.set_telegram_bot(user["user_id"], "tok")
    registry.set_telegram_owner_chat(user["user_id"], "555")
    session = manager.create(user["user_id"])
    manager.attach("telegram", "555", session["id"])

    transport = FakeTransport()
    transport.updates = [message(1, "555", "do the thing")]
    poller = BotPoller("tok", user["user_id"], manager, registry, settings, transport=transport)

    await run_until(poller.run, lambda: transport.edits)

    # The fake harness echoes the prompt; it must have ridden back out as an edit.
    assert any("do the thing" in edit[2] for edit in transport.edits)


# --- supervisor reconcile --------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_starts_stops_and_restarts_on_token_change(manager, registry, settings):
    user = registry.create_user("Owner")

    def factory(token, user_id):
        return FakeTransport()

    supervisor = TelegramSupervisor(manager, registry, settings, transport_factory=factory)
    try:
        await supervisor.reconcile()
        assert supervisor._pollers == {}

        # A new row starts a poller.
        registry.set_telegram_bot(user["user_id"], "tok-1")
        await supervisor.reconcile()
        assert supervisor._pollers[user["user_id"]]["token"] == "tok-1"
        first = supervisor._pollers[user["user_id"]]["task"]

        # A changed token cancels the old poller and starts a fresh one.
        registry.set_telegram_bot(user["user_id"], "tok-2")
        await supervisor.reconcile()
        assert supervisor._pollers[user["user_id"]]["token"] == "tok-2"
        assert first.cancelled() or first.done()

        # A removed row stops the poller.
        registry.clear_telegram_bot(user["user_id"])
        await supervisor.reconcile()
        assert supervisor._pollers == {}
    finally:
        await supervisor._shutdown()


@pytest.mark.asyncio
async def test_a_crashed_poller_is_restarted_on_the_next_reconcile(manager, registry, settings):
    user = registry.create_user("Owner")
    registry.set_telegram_bot(user["user_id"], "tok")

    class Boom(FakeTransport):
        async def get_updates(self, offset, timeout=60):
            raise RuntimeError("boom")

    made = []

    def factory(token, user_id):
        # First poller crashes; the restart gets a well-behaved transport.
        transport = Boom() if not made else FakeTransport()
        made.append(transport)
        return transport

    supervisor = TelegramSupervisor(manager, registry, settings, transport_factory=factory)
    try:
        await supervisor.reconcile()
        crashed = supervisor._pollers[user["user_id"]]["task"]
        # Let the poller run and fall over.
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(crashed, timeout=1.0)
        assert crashed.done()

        await supervisor.reconcile()
        assert not supervisor._pollers[user["user_id"]]["task"].done()
        assert len(made) == 2
    finally:
        await supervisor._shutdown()
