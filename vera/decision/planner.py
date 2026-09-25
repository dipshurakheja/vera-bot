"""Tick planner: which of the available triggers deserve a message right now, and in what order.

Gates (a trigger is skipped with a logged reason when any fails):
  unknown trigger / missing merchant / missing category / missing customer (customer scope)
  suppression key already sent (unless the trigger materially changed)
  merchant or customer opted out; merchant in a quiet window (unless urgency 5)
  conversation for this trigger already in flight
  another merchant-facing conversation still active (cooldown; urgency 5 bypasses)
  trigger not relevant to the merchant's category
  customer consent does not cover the purpose
  expired (only when VERA_ENFORCE_TRIGGER_EXPIRY=true; otherwise it just lowers priority)
Then: rank by priority, keep one message per recipient per tick, cap at 20.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..composer.engine import derive_suppression_key
from ..context.views import CategoryView, CustomerView, MerchantView, TriggerView
from ..decision.policy import category_fit, customer_consent, is_expired, priority
from ..decision.signals import extract
from ..store.context_store import ContextRecord, ContextStore
from ..store.state_store import EngineState


@dataclass
class Candidate:
    trigger_id: str
    record: ContextRecord
    trigger: dict
    merchant: dict
    category: dict
    customer: Optional[dict]
    merchant_id: str
    customer_id: Optional[str]
    recipient: str
    suppression_key: str
    score: float
    expired: bool
    notes: list[str] = field(default_factory=list)


@dataclass
class Skip:
    trigger_id: str
    reason: str


def evaluate(store: ContextStore, state: EngineState, trigger_ids: list[str], now: datetime,
             enforce_expiry: bool, cooldown_minutes: int = 30) -> tuple[list[Candidate], list[Skip]]:
    candidates: list[Candidate] = []
    skips: list[Skip] = []
    seen: set[str] = set()
    for tid in trigger_ids:
        if not isinstance(tid, str) or not tid or tid in seen:
            continue
        seen.add(tid)
        rec = store.get_record("trigger", tid)
        if rec is None:
            skips.append(Skip(tid, "unknown_trigger"))
            continue
        tv = TriggerView(rec.payload, tid)
        mid = tv.merchant_id
        merchant = store.get("merchant", mid)
        if merchant is None:
            skips.append(Skip(tid, "merchant_context_missing"))
            continue
        mv = MerchantView(merchant, mid)
        slug = mv.category_slug or str(tv.p("category", "") or "")
        category = store.get("category", slug)
        if category is None:
            skips.append(Skip(tid, "category_context_missing"))
            continue
        cv = CategoryView(category, slug)
        customer, cid = None, None
        if tv.scope == "customer" or tv.customer_id:
            cid = tv.customer_id
            customer = store.get("customer", cid)
            if customer is None:
                skips.append(Skip(tid, "customer_context_missing"))
                continue
        key = derive_suppression_key(tv)
        if state.is_suppressed(key, rec.version, rec.digest, now):
            skips.append(Skip(tid, "suppressed"))
            continue
        m_state = state.recipient(mid)
        if m_state.opted_out_until and now < m_state.opted_out_until:
            skips.append(Skip(tid, "merchant_opted_out"))
            continue
        recipient = cid or mid
        if cid:
            c_state = state.recipient(cid)
            if c_state.opted_out_until and now < c_state.opted_out_until:
                skips.append(Skip(tid, "customer_opted_out"))
                continue
        if not cid and m_state.quiet_until and now < m_state.quiet_until and tv.urgency < 5:
            skips.append(Skip(tid, "merchant_quiet_window"))
            continue
        in_flight = state.open_conversation_for(tid, recipient)
        if in_flight and int(in_flight.meta.get("trigger_version", 0)) >= rec.version:
            skips.append(Skip(tid, "conversation_in_flight"))
            continue
        if not cid and tv.urgency < 5 and state.active_merchant_conversation(mid, now, cooldown_minutes):
            skips.append(Skip(tid, "merchant_conversation_active_cooldown"))
            continue
        fit = category_fit(tv, cv)
        if not fit.ok:
            skips.append(Skip(tid, fit.reason))
            continue
        if customer is not None:
            verdict = customer_consent(CustomerView(customer, cid), tv, mv)
            if not verdict.ok:
                skips.append(Skip(tid, f"consent:{verdict.reason}"))
                continue
        expired = is_expired(tv, now)
        if expired and enforce_expiry:
            skips.append(Skip(tid, "expired"))
            continue
        score = priority(tv, extract(mv, cv), expired)
        candidates.append(Candidate(tid, rec, rec.payload, merchant, category, customer, mid, cid, recipient, key,
                                    score, expired, ["past_expiry_but_listed_active"] if expired else []))
    return candidates, skips


def select(candidates: list[Candidate], cap: int) -> tuple[list[Candidate], list[Skip]]:
    """Highest priority first; one message per recipient per tick; hard cap."""
    ordered = sorted(candidates, key=lambda c: (-c.score, c.trigger_id))
    chosen, deferred, taken = [], [], set()
    for c in ordered:
        if c.recipient in taken:
            deferred.append(Skip(c.trigger_id, "deferred_one_message_per_recipient_per_tick"))
            continue
        if len(chosen) >= cap:
            deferred.append(Skip(c.trigger_id, "deferred_action_cap"))
            continue
        taken.add(c.recipient)
        chosen.append(c)
    return chosen, deferred
