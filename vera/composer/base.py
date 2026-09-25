"""Composer primitives: the Draft being built, language profile, salutations, CTAs."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from ..context.views import CategoryView, CustomerView, MerchantView, TriggerView
from ..decision.signals import Signals
from ..utils.text import clean, ensure_period

_STARTERS = {
    "calls", "profile", "click-through", "direction", "leads", "google", "your", "it's", "it", "this", "the", "a",
    "an", "good", "heads-up", "quick", "compliance", "urgent", "update", "here's", "searches", "festive", "numbers",
    "milestone", "recent", "flagging", "right", "seasonal", "summer", "winter", "monsoon", "there's", "most",
    "one", "honest", "since", "what", "worth", "over", "reviews", "just",
}

CTA_TYPES = {"binary_yes_no", "binary_confirm_cancel", "open_ended", "multi_choice_slot", "none"}

CATEGORY_EMOJI = {
    "dentists": "🦷",
    "salons": "✨",
    "restaurants": "🍽️",
    "gyms": "💪",
    "pharmacies": "💊",
}


@dataclass
class ComposeInput:
    category: CategoryView
    merchant: MerchantView
    trigger: TriggerView
    customer: Optional[CustomerView]
    signals: Signals
    now: Optional[datetime] = None
    used_digest_ids: frozenset = frozenset()

    @property
    def lang(self) -> str:
        """Merchant-facing register: 'hinglish_light' when merchant reads Hindi and the category code-mixes."""
        if "hi" in self.merchant.languages and ("hindi" in self.category.code_mix or not self.category.code_mix):
            return "hinglish_light"
        return "en"

    def seed(self, salt: str = "") -> int:
        """Stable per-(merchant, trigger) integer for deterministic phrasing variety."""
        key = f"{self.merchant.merchant_id}|{self.trigger.trigger_id}|{self.trigger.kind}|{salt}"
        return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)

    def pick(self, options: list[str], salt: str = "") -> str:
        return options[self.seed(salt) % len(options)] if options else ""


@dataclass
class Draft:
    opener: str
    lines: list[str]
    cta: str
    cta_type: str
    template: str
    rationale: str
    send_as: str = "vera"
    next_action: dict = field(default_factory=dict)
    facts: list[str] = field(default_factory=list)
    extra_numbers: set = field(default_factory=set)
    levers: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def allow(self, *values: Any) -> "Draft":
        for v in values:
            if v is not None:
                self.extra_numbers.add(v)
        return self

    @property
    def body(self) -> str:
        opener = clean(self.opener)
        lines = [clean(x) for x in self.lines if clean(x)]
        if opener.endswith(",") and lines:
            m = re.match(r"[A-Za-z'-]+", lines[0])
            if m and m.group(0).lower() in _STARTERS and not (len(m.group(0)) > 1 and m.group(0).isupper()):
                lines[0] = lines[0][0].lower() + lines[0][1:]
        out, prev_bullet = opener, False
        for part in lines + [clean(self.cta)]:
            if not part:
                continue
            bullet = part.startswith("•")
            sep = "\n" if (bullet or prev_bullet) else " "
            out = f"{out}{sep}{part}" if out else part
            prev_bullet = bullet
        return out.strip()

    @property
    def template_params(self) -> list[str]:
        """WhatsApp template variables {{1}}..{{4}}: name, hook, supporting detail, CTA."""
        hook = clean(self.lines[0]) if self.lines else ""
        rest = " ".join(clean(x) for x in self.lines[1:] if clean(x))
        name = clean(self.opener).rstrip(",:—- ")
        return [p for p in (name, hook, rest, clean(self.cta)) if p]


# ---------------------------------------------------------------- salutations

def merchant_name_for_greeting(ci: ComposeInput) -> str:
    m = ci.merchant
    first = m.owner_first
    owner_raw = clean(m.identity.get("owner_first_name")).lower()
    if first and (ci.category.slug == "dentists" or owner_raw.startswith("dr")):
        return f"Dr. {first}"
    if first:
        return first
    return f"{m.name} team" if m.has_name else ("Doctor" if ci.category.slug == "dentists" else "")


def merchant_opener(ci: ComposeInput, style: str = "name") -> str:
    """style: 'name' -> 'Dr. Meera,' ; 'hi' -> 'Hi Lakshmi,' (clinical categories never get 'Hi')."""
    who = merchant_name_for_greeting(ci)
    if not who:
        return "Hi there,"
    if style == "hi" and not who.startswith(("Dr.", "Doctor")):
        return f"Hi {who},"
    return f"{who},"


def owner_signature(ci: ComposeInput) -> str:
    """How the merchant introduces themself to a customer: 'Karthik from PowerHouse Fitness'."""
    m = ci.merchant
    if m.owner_first and ci.category.slug == "dentists":
        return f"Dr. {m.owner_first}'s team at {m.name}" if not m.name.lower().startswith("dr") else m.name
    if m.owner_first:
        return f"{m.owner_first} from {m.name}"
    return m.name


# ---------------------------------------------------------------- CTAs

_HINGLISH_YES = [
    "Bas YES reply kar dijiye.",
    "Haan ho toh YES bhej dijiye.",
    "Reply YES, baaki main sambhal loongi.",
]


def yes_cta(ci: ComposeInput, question: str, word: str = "YES") -> str:
    q = ensure_period(question) if not question.rstrip().endswith("?") else clean(question)
    if ci.lang == "hinglish_light" and word == "YES":
        return f"{q} {ci.pick(_HINGLISH_YES, 'cta')}"
    return f"{q} Reply {word}."


def open_cta(ci: ComposeInput, question: str, hinglish_tail: str = "") -> str:
    q = clean(question)
    if ci.lang == "hinglish_light" and hinglish_tail:
        return f"{q} {hinglish_tail}"
    return q


def rationale(kind: str, why: str, levers: list[str], extra: str = "") -> str:
    lev = ", ".join(levers)
    out = f"{kind}: {clean(why)}"
    if lev:
        out = ensure_period(out) + f" Levers: {lev}."
    if extra:
        out = ensure_period(out) + " " + clean(extra)
    return ensure_period(out)
