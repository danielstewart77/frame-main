"""Tool-approval requests in flight, and the surfaces that answer them.

A containerised session has nobody at a terminal to answer a permission prompt,
and the alternative is `--dangerously-skip-permissions`, which is not an
alternative. So the prompt goes out to whatever surface is watching the session
and the answer comes back in.

The harness blocks on its request either way, so the shim's call blocks here too
and takes the verdict as its response. That keeps the whole exchange in one
request with no queue, no expiry sweep, and no way for a verdict to be minted
for a request that has already gone away. A prompt nobody answers denies itself
when the wait runs out — silence is not consent.

Request ids are five letters because a person reads one off a screen and types
it back ("yes wKtpq"), so they are short and unambiguous rather than uuids.
"""

from __future__ import annotations

import asyncio
import secrets
import string
from dataclasses import dataclass, field
from typing import Any

# Unambiguous when read aloud or off a small screen: no l/1, no O/0.
ID_ALPHABET = "".join(sorted(set(string.ascii_lowercase + string.ascii_uppercase) - set("lIO")))
ID_LENGTH = 5

# How long a prompt waits for a surface before denying itself.
DEFAULT_TIMEOUT_SECONDS = 300.0


@dataclass
class PermissionRequest:
    id: str
    session_id: str
    tool: str
    tool_input: dict[str, Any]
    allow: bool | None = None
    reason: str = ""
    _answered: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def as_event(self) -> dict[str, Any]:
        """The form a surface renders to prompt the user."""
        return {
            "kind": "permission",
            "request_id": self.id,
            "tool": self.tool,
            "input": self.tool_input,
        }


class PermissionBroker:
    """Open prompts, keyed by request id, across every session."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout
        self._pending: dict[str, PermissionRequest] = {}

    def pending(self, session_id: str | None = None) -> list[PermissionRequest]:
        """Open prompts, so a surface attaching mid-prompt still sees them."""
        return [
            request
            for request in self._pending.values()
            if session_id is None or request.session_id == session_id
        ]

    def open(
        self, session_id: str, tool: str, tool_input: dict[str, Any] | None = None
    ) -> PermissionRequest:
        request = PermissionRequest(
            id=self._mint_id(),
            session_id=session_id,
            tool=tool,
            tool_input=dict(tool_input or {}),
        )
        self._pending[request.id] = request
        return request

    def resolve(self, request_id: str, allow: bool, reason: str = "") -> PermissionRequest | None:
        """Answer a prompt. None if it has already been answered or timed out."""
        request = self._pending.get(request_id)
        if request is None:
            return None
        request.allow = allow
        request.reason = reason
        request._answered.set()
        return request

    async def wait(self, request: PermissionRequest, timeout: float | None = None) -> bool:
        """Block until a surface answers, or deny when the wait runs out."""
        try:
            await asyncio.wait_for(request._answered.wait(), timeout or self.timeout)
        except asyncio.TimeoutError:
            request.allow = False
            request.reason = "no surface answered in time"
        finally:
            self._pending.pop(request.id, None)
        return bool(request.allow)

    def _mint_id(self) -> str:
        while True:
            candidate = "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LENGTH))
            if candidate not in self._pending:
                return candidate
