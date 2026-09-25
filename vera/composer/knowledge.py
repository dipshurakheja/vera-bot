"""Relevance ranking over category knowledge: offers, digest items, trends, seasonal beats.

All choices are deterministic (score, then stable index) and only ever return
items that exist in the supplied contexts.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Optional

from ..context.views import CategoryView, MerchantView
from ..decision.signals import Signals
from ..utils.text import tokens
from ..utils.timeutil import month_of, month_range_contains, month_range_start

_PRICE = re.compile(r"₹\s?([\d,]+)")
_STOP = {"the", "and", "for", "with", "your", "you", "per", "off", "free", "new", "all", "get", "one", "any",
         "care", "plus", "pack", "clinic", "studio", "centre", "center", "salon", "cafe", "house", "gym",
         "pharmacy", "medicos", "fitness", "health", "dental", "family", "express", "junction", "lounge"}

# catalog words that only make sense for some merchants in a vertical
_SPECIFIC = {"pizza", "thali", "brunch", "match", "cake", "bridal", "keratin", "aligner", "pediatric", "root",
             "canal", "whitening", "diabetic", "glucometer", "yoga", "couple", "annual", "membership",
             "biryani", "senior", "birthday", "combo"}

_TYPE_PREF = {
    "dentists": ["service_at_price", "free_service"],
    "salons": ["service_at_price", "free_addon"],
    "restaurants": ["free_addon", "service_at_price", "bogo"],
    "gyms": ["free_trial", "service_at_price", "free_service"],
    "pharmacies": ["free_service", "free_addon", "service_at_price"],
}


def offer_price(title: str) -> Optional[int]:
    m = _PRICE.search(title if isinstance(title, str) else "")
    return int(m.group(1).replace(",", "")) if m else None


def _content_tokens(text: str) -> set[str]:
    return {t for t in tokens(text) if t not in _STOP}


def related_offer(merchant: MerchantView, text: str) -> Optional[str]:
    """Active offer sharing the most content words with `text` (None if no overlap)."""
    want = _content_tokens(text)
    best, best_score = None, 0
    for title in merchant.active_offers:
        score = len(_content_tokens(title) & want)
        if score > best_score:
            best, best_score = title, score
    return best


def best_active_offer(merchant: MerchantView, prefer_text: str = "") -> Optional[str]:
    if not merchant.active_offers:
        return None
    if prefer_text:
        rel = related_offer(merchant, prefer_text)
        if rel:
            return rel
    # service+price beats free add-ons beats discounts (brief §3: service+price converts best)
    def score(title: str) -> tuple:
        t = title.lower()
        s = 0
        if offer_price(title) is not None:
            s += 3
        if "free" in t:
            s += 2
        if "%" in t:
            s -= 2
        return (-s, merchant.active_offers.index(title))
    return sorted(merchant.active_offers, key=score)[0]


def catalog_pick(category: CategoryView, merchant: MerchantView, audience: str = "new_user",
                 prefer_text: str = "") -> Optional[dict]:
    """Best canonical catalog offer to *suggest* (never presented as already live)."""
    prefs = _TYPE_PREF.get(category.slug, ["service_at_price", "free_service", "free_addon", "free_trial"])
    active = {a.lower() for a in merchant.active_offers}
    mkeys = _content_tokens(merchant.name) | merchant.keywords | _content_tokens(prefer_text)
    has_delivery = any("delivery" in k and (merchant.agg(k) or 0) > 0 for k in merchant.aggregate)
    priced = [offer_price(o["title"]) for o in category.offer_catalog if o.get("type") == "service_at_price"]
    cheapest = min([p for p in priced if p is not None], default=None)
    best, best_key = None, None
    for idx, offer in enumerate(category.offer_catalog):
        title = str(offer["title"])
        if title.lower() in active:
            continue
        otype = str(offer.get("type", ""))
        score = 10 - 2 * prefs.index(otype) if otype in prefs else 3
        ttoks = _content_tokens(title)
        if ttoks & mkeys:
            score += 4
        specific = {t for t in ttoks if t in _SPECIFIC}
        if specific and not (specific & mkeys):
            score -= 6
        if otype == "percentage_discount":
            score -= 3
        if offer.get("audience") == audience:
            score += 1
        if "delivery" in title.lower() and has_delivery:
            score += 2
        if cheapest is not None and offer_price(title) == cheapest:
            score += 2
        key = (score, -idx)
        if best_key is None or key > best_key:
            best, best_key = offer, key
    return best


_KIND_WEIGHT = {"alert": 4, "trend": 4, "research": 3, "compliance": 3, "supply": 3, "seasonal": 3,
                "tech": 2, "compete": 2, "cde": 1}

_COHORT_HINTS = {
    "high_risk_adult_count": ("high_risk", "high-risk"),
    "chronic_rx_count": ("chronic",),
    "total_active_members": ("member", "retention"),
}


def digest_text(item: dict) -> str:
    return " ".join(str(item.get(k, "")) for k in ("title", "summary", "actionable", "patient_segment")).lower()


def pick_digest(category: CategoryView, merchant: MerchantView, sig: Signals,
                prefer_kinds: Iterable[str] = (), exclude_ids: Iterable[str] = ()) -> Optional[dict]:
    prefer = set(prefer_kinds)
    exclude = set(exclude_ids)
    weak = sig.weak_metrics()
    best, best_key = None, None
    for idx, item in enumerate(category.digest):
        kind = str(item.get("kind", "")).lower()
        text = digest_text(item)
        score = _KIND_WEIGHT.get(kind, 1)
        if kind in prefer:
            score += 5
        if str(item.get("id")) in exclude:
            score -= 10
        if re.search(r"\d", str(item.get("title", "")) + str(item.get("summary", ""))):
            score += 1
        if sig.cohort and any(h in text for h in _COHORT_HINTS.get(sig.cohort[0], ())):
            score += 3
        if "calls" in weak and "call" in text:
            score += 2
        if "views" in weak and any(w in text for w in ("impression", "visibility", "views", "searches")):
            score += 2
        if "retention" in weak and "retention" in text:
            score += 2
        score += min(2, len(_content_tokens(text) & merchant.keywords))
        key = (score, -idx)
        if best_key is None or key > best_key:
            best, best_key = item, key
    return best


def pick_trend(category: CategoryView, merchant: MerchantView) -> Optional[dict]:
    best, best_key = None, None
    for idx, t in enumerate(category.trend_signals):
        try:
            delta = float(t.get("delta_yoy") or 0)
        except (TypeError, ValueError):
            delta = 0.0
        overlap = len(_content_tokens(str(t.get("query", ""))) & (merchant.keywords | _content_tokens(merchant.name)))
        key = (overlap, delta, -idx)
        if best_key is None or key > best_key:
            best, best_key = t, key
    return best


def current_beat(category: CategoryView, now: Optional[datetime], must_contain: Iterable[str] = ()) -> Optional[dict]:
    month = month_of(now)
    words = [w.lower() for w in must_contain]
    for beat in category.seasonal_beats:
        note = str(beat.get("note", "")).lower()
        if words and not _mentions(note, words):
            continue
        if month is not None and month_range_contains(str(beat.get("month_range", "")), month):
            return beat
    return None


def _mentions(note: str, words: list[str]) -> bool:
    return any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", note) for w in words if w)


def beat_for_month(category: CategoryView, month: Optional[int], must_contain: Iterable[str] = ()) -> Optional[dict]:
    words = [w.lower() for w in must_contain]
    if month is None:
        return None
    for beat in category.seasonal_beats:
        note = str(beat.get("note", "")).lower()
        if words and not _mentions(note, words):
            continue
        if month_range_contains(str(beat.get("month_range", "")), month):
            return beat
    return None


def upcoming_beat(category: CategoryView, now: Optional[datetime], must_contain: Iterable[str]) -> Optional[dict]:
    """Nearest seasonal beat (by start month) whose note mentions any of `must_contain` (whole words)."""
    words = [w.lower() for w in must_contain]
    month = month_of(now)
    candidates = []
    for idx, beat in enumerate(category.seasonal_beats):
        note = str(beat.get("note", "")).lower()
        if not _mentions(note, words):
            continue
        rng = str(beat.get("month_range", ""))
        if month is not None and month_range_contains(rng, month):
            distance = 0
        else:
            start = month_range_start(rng)
            distance = ((start - month) % 12) if (start and month) else idx + 1
        candidates.append((distance, idx, beat))
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2] if candidates else None


_GENERIC = {"patients", "patient", "customers", "adults", "adult", "high", "risk", "months", "month", "week",
            "weeks", "shows", "study", "trial", "research", "lower", "better", "higher", "people", "most", "new"}


def content_for(category: CategoryView, text: str, min_overlap: int = 3) -> Optional[dict]:
    """Patient/customer content-library item that is genuinely about the same topic (>= 3 distinctive words)."""
    want = _content_tokens(text) - _GENERIC
    best, best_score = None, 0
    for item in category.content_library:
        have = _content_tokens(str(item.get("title", "")) + " " + str(item.get("body", ""))) - _GENERIC
        score = len(have & want)
        if score > best_score:
            best, best_score = item, score
    return best if best_score >= min_overlap else None


def premium_active_offer(merchant: MerchantView) -> Optional[str]:
    """Highest-priced live offer (packages/festive bundles upsell from the top of the menu)."""
    priced = [(offer_price(o) or 0, -i, o) for i, o in enumerate(merchant.active_offers)]
    priced = [p for p in priced if p[0] > 0]
    return max(priced)[2] if priced else best_active_offer(merchant)


def beat_sentence(beat: dict, category: CategoryView) -> str:
    rng = str(beat.get("month_range", "")).strip()
    note = str(beat.get("note", "")).strip().rstrip(".")
    plural = {"pharmacy": "pharmacies"}.get(category.singular, category.singular + "s")
    return f"{rng} pattern for {plural}: {note}."
