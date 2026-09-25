"""compose(category, merchant, trigger, customer?) — the single composition entry point.

Flow: views -> signals -> consent/scope routing -> kind strategy -> validation
(grounding, taboo, CTA, URL) -> (optional LLM polish, validated) -> result dict.
Any strategy exception or validation failure degrades to a minimal, always-valid
message instead of breaking the API.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Callable, Optional

from ..context.views import CategoryView, CustomerView, MerchantView, TriggerView
from ..decision.policy import customer_consent
from ..decision.signals import Signals, extract
from ..utils.logging import get_logger, log_event
from ..utils.text import humanize
from ..utils.timeutil import parse_iso
from ..validation.output import allowed_numbers, validate_message
from . import customer as C
from . import merchant as M
from .base import ComposeInput, Draft, merchant_opener, rationale, yes_cta

log = get_logger("composer")

MERCHANT_STRATEGIES: dict[str, Callable[[ComposeInput], Draft]] = {
    "research_digest": M.research_digest,
    "research_digest_release": M.research_digest,
    "category_research_digest_release": M.research_digest,
    "regulation_change": M.regulation_change,
    "compliance_update": M.regulation_change,
    "cde_opportunity": M.cde_opportunity,
    "perf_dip": M.perf_dip,
    "perf_spike": M.perf_spike,
    "seasonal_perf_dip": M.seasonal_perf_dip,
    "milestone_reached": M.milestone_reached,
    "renewal_due": M.renewal_due,
    "winback_eligible": M.winback_eligible,
    "dormant_with_vera": M.dormant_with_vera,
    "gbp_unverified": M.gbp_unverified,
    "competitor_opened": M.competitor_opened,
    "festival_upcoming": M.festival_upcoming,
    "ipl_match_today": M.ipl_match_today,
    "category_seasonal": M.category_seasonal,
    "supply_alert": M.supply_alert,
    "review_theme_emerged": M.review_theme_emerged,
    "curious_ask_due": M.curious_ask,
    "scheduled_recurring": M.curious_ask,
    "active_planning_intent": M.active_planning_intent,
    "category_trend_movement": M.trend_movement,
    "weather_heatwave": M.external_event,
    "local_news_event": M.external_event,
}

CUSTOMER_STRATEGIES: dict[str, Callable[[ComposeInput], Draft]] = {
    "recall_due": C.recall_due,
    "appointment_tomorrow": C.appointment_tomorrow,
    "chronic_refill_due": C.chronic_refill_due,
    "customer_lapsed_soft": C.customer_lapsed,
    "customer_lapsed_hard": C.customer_lapsed,
    "winback": C.customer_lapsed,
    "trial_followup": C.trial_followup,
    "wedding_package_followup": C.wedding_followup,
    "bridal_followup": C.wedding_followup,
    "unplanned_slot_open": C.slot_open,
}

_KEYWORD_ROUTES_MERCHANT = [
    (("weather", "news", "event", "heatwave", "rain", "strike", "closure"), M.external_event),
    (("trend",), M.trend_movement),
    (("dip", "drop", "decline"), M.perf_dip),
    (("spike", "surge"), M.perf_spike),
    (("research", "digest", "journal"), M.research_digest),
    (("regulat", "compliance", "circular"), M.regulation_change),
    (("festival", "holiday"), M.festival_upcoming),
    (("match", "ipl", "sport"), M.ipl_match_today),
    (("review",), M.review_theme_emerged),
    (("competitor",), M.competitor_opened),
    (("renewal", "subscription", "expir"), M.renewal_due),
    (("dormant", "inactive"), M.dormant_with_vera),
    (("milestone",), M.milestone_reached),
    (("ask", "curious", "question"), M.curious_ask),
]
_KEYWORD_ROUTES_CUSTOMER = [
    (("refill", "medicine"), C.chronic_refill_due),
    (("appointment", "booking"), C.appointment_tomorrow),
    (("lapsed", "winback", "churn"), C.customer_lapsed),
    (("recall", "due", "checkup"), C.recall_due),
    (("trial",), C.trial_followup),
    (("wedding", "bridal"), C.wedding_followup),
    (("slot",), C.slot_open),
]


def pick_strategy(kind: str, customer_facing: bool) -> Callable[[ComposeInput], Draft]:
    table = CUSTOMER_STRATEGIES if customer_facing else MERCHANT_STRATEGIES
    if kind in table:
        return table[kind]
    for words, fn in (_KEYWORD_ROUTES_CUSTOMER if customer_facing else _KEYWORD_ROUTES_MERCHANT):
        if any(w in kind for w in words):
            return fn
    return C.generic_customer if customer_facing else M.generic


def derive_suppression_key(trigger: TriggerView) -> str:
    if trigger.suppression_key:
        return trigger.suppression_key
    blob = f"{trigger.kind}|{trigger.merchant_id}|{trigger.customer_id}|{sorted(trigger.payload.items())!r}"
    return f"{trigger.kind}:{trigger.merchant_id or '-'}:{trigger.customer_id or '-'}:" + \
        hashlib.sha256(blob.encode("utf-8")).hexdigest()[:10]


# ------------------------------------------------------------------ fallbacks

_BLOCK_TEXT = {
    "no_consent_on_record": "there's no WhatsApp opt-in on record for them",
    "no_contactable_channel": "we don't have a reachable WhatsApp number for them",
    "reminders_opted_out": "they've switched off reminders",
    "customer_belongs_to_other_merchant": "they aren't on your customer list",
}


_KIND_NOUN = {
    "recall_due": "recall reminder",
    "appointment_tomorrow": "reminder for tomorrow's appointment",
    "chronic_refill_due": "refill reminder",
    "trial_followup": "trial follow-up",
    "customer_lapsed_soft": "win-back message",
    "customer_lapsed_hard": "win-back message",
    "wedding_package_followup": "bridal follow-up",
}


def upper_a(noun: str) -> str:
    return "An" if noun[:1].lower() in "aeiou" else "A"


def consent_blocked(ci: ComposeInput, reason: str) -> Draft:
    c, m = ci.customer, ci.merchant
    who = c.first_name or "this customer"
    why = _BLOCK_TEXT.get(reason, "their opt-in doesn't cover this kind of message")
    d = Draft(merchant_opener(ci), [], "", "binary_yes_no", "vera_consent_check_v1", "")
    noun = _KIND_NOUN.get(ci.trigger.kind, f"{humanize(ci.trigger.kind)} message")
    d.lines.append(f"{upper_a(noun)} {noun} for {who} is due, but I haven't sent it because {why}.")
    d.cta = yes_cta(ci, "Want me to add it to your front-desk list so the team can ask in person?")
    d.next_action = {"type": "summary", "label": "the front-desk note"}
    d.rationale = rationale(ci.trigger.kind, f"customer-facing send blocked by consent policy ({reason}); "
                                             f"routed to the merchant instead", ["compliance", "single YES CTA"])
    return d


def safe_minimal(ci: ComposeInput, why: str) -> Draft:
    """Last-resort message: no numbers, no claims, always valid."""
    m = ci.merchant
    kind_h = humanize(ci.trigger.kind) or "account"
    if ci.customer is not None and ci.trigger.scope == "customer":
        d = Draft(C.opener(ci), [f"{C.intro(ci)}.", "We have a quick update for you."], "", "binary_yes_no",
                  "merchant_generic_v1", "", send_as="merchant_on_behalf")
        d.cta = "Reply YES and we'll share the details."
    else:
        d = Draft(merchant_opener(ci), [f"there's a new {kind_h} update for {m.name}."], "", "binary_yes_no",
                  "vera_generic_v1", "")
        d.cta = yes_cta(ci, "Want me to send you the details and the one action I'd take?")
    d.next_action = {"type": "summary", "label": "the details"}
    d.rationale = rationale(ci.trigger.kind, f"safe fallback ({why})", ["single YES CTA"])
    return d


# ------------------------------------------------------------------ public API

def build_input(category: Optional[dict], merchant: Optional[dict], trigger: Optional[dict],
                customer: Optional[dict] = None, now: Any = None, used_digest_ids: Any = ()) -> ComposeInput:
    mv = MerchantView(merchant)
    tv = TriggerView(trigger)
    cv = CategoryView(category, slug_hint=mv.category_slug or str(tv.p("category", "")))
    cust = CustomerView(customer) if customer else None
    now_dt = parse_iso(now) if not isinstance(now, datetime) else now
    try:
        signals = extract(mv, cv)
    except Exception as exc:  # malformed merchant/category data must not abort composition
        log_event(log, "signal_extraction_failed", error_type=type(exc).__name__)
        signals = Signals()
    return ComposeInput(category=cv, merchant=mv, trigger=tv, customer=cust, signals=signals, now=now_dt,
                        used_digest_ids=frozenset(used_digest_ids or ()))


def compose_draft(ci: ComposeInput) -> tuple[Draft, str]:
    """Pick and run the strategy. Returns (draft, route) where route explains the choice."""
    t = ci.trigger
    customer_facing = ci.customer is not None and (t.scope == "customer" or bool(t.customer_id))
    if customer_facing:
        verdict = customer_consent(ci.customer, t, ci.merchant)
        if not verdict.ok:
            return consent_blocked(ci, verdict.reason), f"consent_blocked:{verdict.reason}"
    strategy = pick_strategy(t.kind, customer_facing)
    try:
        return strategy(ci), strategy.__name__
    except Exception as exc:  # a strategy bug must never break the endpoint
        log_event(log, "strategy_error", kind=t.kind, strategy=strategy.__name__, error_type=type(exc).__name__,
                  error=str(exc)[:200])
        return safe_minimal(ci, f"strategy_error:{type(exc).__name__}"), "safe_minimal"


def finalize(ci: ComposeInput, draft: Draft, route: str) -> dict:
    t = ci.trigger
    msg = {
        "body": draft.body,
        "cta": draft.cta_type,
        "send_as": draft.send_as,
        "suppression_key": derive_suppression_key(t),
        "rationale": draft.rationale,
        "template_name": draft.template,
        "template_params": draft.template_params,
    }
    allowed = allowed_numbers(ci.category.raw, ci.merchant.raw, t.raw, ci.customer.raw if ci.customer else None,
                              extra=draft.extra_numbers)
    errors = validate_message(msg, ci.category.taboos, allowed)
    if errors:
        log_event(log, "composition_invalid", kind=t.kind, route=route, errors=errors)
        fallback = safe_minimal(ci, "validation:" + ";".join(errors)[:80])
        msg.update(body=fallback.body, cta=fallback.cta_type, send_as=fallback.send_as, rationale=fallback.rationale,
                   template_name=fallback.template, template_params=fallback.template_params)
        draft = fallback
        route = "safe_minimal"
    msg["_internal"] = {
        "route": route,
        "next_action": draft.next_action,
        "meta": draft.meta,
        "levers": draft.levers,
        "validation_errors": errors,
        "facts": {"merchant": ci.merchant.name, "kind": t.kind, "category": ci.category.slug},
    }
    return msg


def compose(category: Optional[dict], merchant: Optional[dict], trigger: Optional[dict],
            customer: Optional[dict] = None, now: Any = None, used_digest_ids: Any = ()) -> dict:
    """Public, deterministic composition. Returns body/cta/send_as/suppression_key/rationale (+ template info)."""
    ci = build_input(category, merchant, trigger, customer, now, used_digest_ids)
    draft, route = compose_draft(ci)
    return finalize(ci, draft, route)


def public_view(msg: dict) -> dict:
    return {k: v for k, v in msg.items() if not k.startswith("_")}
