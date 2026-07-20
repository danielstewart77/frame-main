"""Durable record of what a session said.

The bus fans events out to whoever is attached and keeps a bounded in-memory
tail for reconnects. Neither survives the process, and a session running
unattended has nobody attached in the first place — so without this, a session
can work all night and leave nothing to read in the morning.

Text arrives a token at a time. Persisting one row per token would be one row
per token, so contiguous text is coalesced into a single row, stamped with the
`seq` of the run's first event to keep transcript order and bus order the same.
"""

from __future__ import annotations

from typing import Any

# Kinds whose payload is prose the reader wants to see as one block.
TEXT_KINDS = {"text"}
# Kinds that end a turn, and so decide how the session is doing.
TERMINAL_KINDS = {"result", "error"}

_OUTCOMES = {"result": "ok", "error": "error"}


class TranscriptWriter:
    """Writes a session's stream to the registry, coalescing text runs."""

    def __init__(self, registry: Any):
        self.registry = registry
        # session_id -> (first seq of the run, accumulated text)
        self._pending: dict[str, tuple[int, list[str]]] = {}

    def write(self, session_id: str, event: dict[str, Any]) -> None:
        seq = event.get("seq")
        if not isinstance(seq, int):
            # Unstamped events never went through the bus, so they have no
            # place in an ordering the reader can trust.
            return
        kind = event.get("kind", "raw")

        if kind in TEXT_KINDS:
            first, chunks = self._pending.setdefault(session_id, (seq, []))
            chunks.append(str(event.get("text", "")))
            return

        self.flush(session_id)
        text = event.get("text")
        data = {k: v for k, v in event.items() if k not in ("kind", "text", "seq")}
        self.registry.append_event(
            session_id, seq, kind, text if text is None else str(text), data
        )
        if kind in TERMINAL_KINDS:
            self.registry.set_outcome(session_id, _OUTCOMES[kind])

    def flush(self, session_id: str) -> None:
        """Commit the open text run, if any. Safe to call when there isn't one."""
        pending = self._pending.pop(session_id, None)
        if pending is None:
            return
        first, chunks = pending
        joined = "".join(chunks)
        if joined:
            self.registry.append_event(session_id, first, "text", joined, {})

    def discard(self, session_id: str) -> None:
        """Forget a session's buffered run without writing it."""
        self._pending.pop(session_id, None)
