"""Mutable engine state: suppression ledger, conversations, per-recipient engagement.

All access goes through EngineState methods guarded by one re-entrant lock;
critical sections are tiny (dict ops), so contention is negligible at 10 rps.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterator, Optional

from ..utils.timeutil import iso_now, parse_iso


@dataclass
class Turn:
    role: str            # "bot" | "merchant" | "customer"
    body: str
    ts: str
    intent: str = ""
    action: str = ""     # bot turns: send | wait | end


@dataclass
class Conversation:
    conversation_id: str
    merchant_id: Optional[str]
    customer_id: Optional[str]
    trigger_id: Optional[str]
    kind: str
    send_as: str
    created_at: str
    origin: str = "tick"                     # tick | inbound
    status: str = "open"                     # open | waiting | ended
    stage: str = "pitched"                   # pitched | delivered | executed | closed
    next_action: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    turns: list[Turn] = field(default_factory=list)
    sent_bodies: set[str] = field(default_factory=set)
    auto_reply_streak: int = 0
    hostile_count: int = 0
    clarify_count: int = 0
    wait_until: Optional[str] = None
    bot_sends: int = 0
    end_reason: str = ""

    def last_bot_body(self) -> str:
        for t in reversed(self.turns):
            if t.role == "bot" and t.body:
                return t.body
        return ""


@dataclass
class RecipientState:
    """Engagement state for a merchant (or a customer, keyed separately)."""
    opted_out_until: Optional[datetime] = None
    quiet_until: Optional[datetime] = None
    auto_reply_count: int = 0
    message_counts: dict[str, int] = field(default_factory=dict)
    digest_items_sent: set[str] = field(default_factory=set)
    last_proactive_at: Optional[datetime] = None


@dataclass
class SuppressionEntry:
    key: str
    trigger_id: str
    trigger_version: int
    payload_digest: str
    conversation_id: str
    sent_at: str
    expires_at: Optional[datetime]


class EngineState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._suppressed: dict[str, SuppressionEntry] = {}
        self._conversations: dict[str, Conversation] = {}
        self._recipients: dict[str, RecipientState] = {}

    # ---------------- suppression ----------------
    def suppression(self, key: str) -> Optional[SuppressionEntry]:
        with self.lock:
            return self._suppressed.get(key)

    def is_suppressed(self, key: str, trigger_version: int, payload_digest: str, now: Optional[datetime]) -> bool:
        """Suppressed unless the ledger entry expired or the trigger materially changed (newer version, new payload)."""
        with self.lock:
            entry = self._suppressed.get(key)
            if entry is None:
                return False
            if entry.expires_at is not None and now is not None and now > entry.expires_at:
                return False
            if trigger_version > entry.trigger_version and payload_digest != entry.payload_digest:
                return False
            return True

    def suppress(self, key: str, trigger_id: str, trigger_version: int, payload_digest: str,
                 conversation_id: str, expires_at: Any = None) -> None:
        with self.lock:
            self._suppressed[key] = SuppressionEntry(
                key=key, trigger_id=trigger_id, trigger_version=trigger_version,
                payload_digest=payload_digest, conversation_id=conversation_id,
                sent_at=iso_now(), expires_at=parse_iso(expires_at),
            )

    # ---------------- conversations ----------------
    def conversation(self, conversation_id: str) -> Optional[Conversation]:
        with self.lock:
            return self._conversations.get(conversation_id)

    def has_conversation(self, conversation_id: str) -> bool:
        with self.lock:
            return conversation_id in self._conversations

    def put_conversation(self, conv: Conversation) -> None:
        with self.lock:
            self._conversations[conv.conversation_id] = conv

    def conversations(self) -> Iterator[Conversation]:
        with self.lock:
            return iter(list(self._conversations.values()))

    def open_conversation_for(self, trigger_id: str, recipient_key: str) -> Optional[Conversation]:
        with self.lock:
            for c in self._conversations.values():
                if c.trigger_id == trigger_id and c.status != "ended" and \
                        (c.customer_id or c.merchant_id) == recipient_key:
                    return c
        return None

    def active_merchant_conversation(self, merchant_id: str, now: datetime, minutes: int) -> Optional[Conversation]:
        """A merchant-facing conversation still in play (open, bot spoke last, recent activity)."""
        if minutes <= 0:
            return None
        cutoff = now - timedelta(minutes=minutes)
        with self.lock:
            for c in self._conversations.values():
                if c.merchant_id != merchant_id or c.send_as != "vera" or c.status != "open" or not c.turns:
                    continue
                last = c.turns[-1]
                ts = parse_iso(last.ts)
                if last.role == "bot" and ts is not None and ts >= cutoff:
                    return c
        return None

    # ---------------- recipients ----------------
    def recipient(self, key: str) -> RecipientState:
        with self.lock:
            st = self._recipients.get(key)
            if st is None:
                st = RecipientState()
                self._recipients[key] = st
            return st

    def opt_out(self, key: str, now: datetime, days: int) -> None:
        with self.lock:
            self.recipient(key).opted_out_until = now + timedelta(days=days)

    def set_quiet(self, key: str, until: datetime) -> None:
        with self.lock:
            st = self.recipient(key)
            if st.quiet_until is None or until > st.quiet_until:
                st.quiet_until = until

    def counts(self) -> dict[str, int]:
        with self.lock:
            return {
                "conversations": len(self._conversations),
                "suppressed_keys": len(self._suppressed),
            }

    def clear(self) -> None:
        with self.lock:
            self._suppressed.clear()
            self._conversations.clear()
            self._recipients.clear()
