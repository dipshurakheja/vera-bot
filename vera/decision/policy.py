"""Decision policy: may we contact this recipient about this trigger, and how important is it?

Consent model (customer-facing sends):
  * hard blocks  — no opt-in on record, no contactable channel, customer belongs to another merchant
  * reminders    — blocked when preferences.reminder_opt_in is False
  * scope        — the consent scope must cover the trigger's purpose;
                   `promotional_offers` is the broad marketing consent and covers
                   service nudges, but health-specific refill content needs refill consent
                   (enforced inside the refill composer by genericising molecules).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..context.views import CategoryView, CustomerView, MerchantView, TriggerView
from ..decision.signals import Signals
from ..utils.timeutil import parse_iso

REMINDER, TRANSACTIONAL, PROMOTIONAL = "reminder", "transactional", "promotional"

CONSENT_RULES: dict[str, tuple[str, Optional[set[str]]]] = {
    "recall_due": (REMINDER, {"recall_reminders", "appointment_reminders", "treatment_followup", "promotional_offers"}),
    "appointment_tomorrow": (TRANSACTIONAL, None),
    "chronic_refill_due": (REMINDER, {"refill_reminders", "recall_reminders", "recall_alerts", "appointment_reminders",
                                      "promotional_offers"}),
    "customer_lapsed_soft": (PROMOTIONAL, {"winback_offers", "promotional_offers", "renewal_reminders", "recall_reminders"}),
    "customer_lapsed_hard": (PROMOTIONAL, {"winback_offers", "promotional_offers", "renewal_reminders", "recall_reminders"}),
    "winback": (PROMOTIONAL, {"winback_offers", "promotional_offers", "renewal_reminders"}),
    "trial_followup": (REMINDER, {"kids_program_updates", "program_updates", "appointment_reminders", "promotional_offers",
                                  "trial_followup"}),
    "wedding_package_followup": (REMINDER, {"bridal_package_followup", "appointment_reminders", "promotional_offers"}),
    "bridal_followup": (REMINDER, {"bridal_package_followup", "appointment_reminders", "promotional_offers"}),
    "unplanned_slot_open": (PROMOTIONAL, {"promotional_offers", "appointment_reminders", "winback_offers"}),
}
DEFAULT_RULE = (PROMOTIONAL, {"promotional_offers"})

KIND_WEIGHT = {
    "supply_alert": 9, "active_planning_intent": 9, "regulation_change": 8, "chronic_refill_due": 8,
    "appointment_tomorrow": 8, "recall_due": 7, "perf_dip": 7, "renewal_due": 6, "competitor_opened": 6,
    "review_theme_emerged": 6, "ipl_match_today": 6, "trial_followup": 6, "wedding_package_followup": 6,
    "customer_lapsed_hard": 5, "customer_lapsed_soft": 5, "winback_eligible": 5, "gbp_unverified": 5,
    "perf_spike": 5, "seasonal_perf_dip": 5, "category_seasonal": 5, "research_digest": 4, "cde_opportunity": 4,
    "milestone_reached": 4, "festival_upcoming": 4, "dormant_with_vera": 3, "curious_ask_due": 3,
}


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""


def customer_consent(customer: CustomerView, trigger: TriggerView, merchant: MerchantView) -> Verdict:
    if customer.merchant_id and merchant.merchant_id and customer.merchant_id != merchant.merchant_id:
        return Verdict(False, "customer_belongs_to_other_merchant")
    if not customer.opted_in:
        return Verdict(False, "no_consent_on_record")
    if not customer.contactable:
        return Verdict(False, "no_contactable_channel")
    purpose, scopes = CONSENT_RULES.get(trigger.kind, DEFAULT_RULE)
    if purpose in {REMINDER, TRANSACTIONAL} and customer.reminder_opt_in is False:
        return Verdict(False, "reminders_opted_out")
    if scopes is not None and not (customer.consent_scope & scopes):
        return Verdict(False, f"consent_scope_excludes_{purpose}")
    return Verdict(True)


def category_fit(trigger: TriggerView, category: CategoryView) -> Verdict:
    relevance = trigger.p("category_relevance")
    if isinstance(relevance, list) and relevance and category.slug and category.slug not in {str(r).lower() for r in relevance}:
        return Verdict(False, "trigger_not_relevant_to_category")
    payload_cat = str(trigger.p("category", "") or "").lower()
    if payload_cat and category.slug and payload_cat != category.slug:
        return Verdict(False, "trigger_category_mismatch")
    return Verdict(True)


def is_expired(trigger: TriggerView, now: Optional[datetime]) -> bool:
    exp = parse_iso(trigger.expires_at)
    return bool(exp and now and now > exp)


def priority(trigger: TriggerView, sig: Signals, expired: bool) -> float:
    score = trigger.urgency * 10.0 + KIND_WEIGHT.get(trigger.kind, 3)
    delta = trigger.p("delta_pct")
    if isinstance(delta, (int, float)):
        score += min(5.0, abs(float(delta)) * 10.0)
    if trigger.is_placeholder:
        score -= 3.0
    if trigger.scope == "merchant" and sig.pending_intent and trigger.kind == "active_planning_intent":
        score += 5.0
    if expired:
        score -= 8.0
    return round(score, 2)
