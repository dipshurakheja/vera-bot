"""Read-only, defensive views over raw context payloads.

The judge can push partial or unfamiliar shapes, so every accessor tolerates
missing keys and wrong types. Views never mutate the underlying dicts.
"""

from __future__ import annotations

import math
import re
from functools import cached_property
from typing import Any, Optional

from ..utils.text import clean, humanize, strip_dr, tokens


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _l(value: Any) -> list:
    return value if isinstance(value, list) else []


def num(value: Any) -> Optional[float]:
    """Finite float or None (bools, NaN, inf, junk strings are all rejected)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value) if isinstance(value, (int, float)) else float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _text(value: Any) -> str:
    """String field or '' — never renders dicts/lists/None/NaN into copy."""
    if isinstance(value, str):
        v = value.strip()
        return "" if v.lower() in {"nan", "none", "null", "undefined"} else v
    if isinstance(value, (int, float)) and not isinstance(value, bool) and num(value) is not None:
        return str(value)
    return ""


_CATEGORY_NOUNS = {
    "dentists": ("dentist", "clinic", "patients", "patient"),
    "salons": ("salon", "salon", "clients", "client"),
    "restaurants": ("restaurant", "restaurant", "customers", "customer"),
    "gyms": ("gym", "gym", "members", "member"),
    "pharmacies": ("pharmacy", "pharmacy", "customers", "customer"),
}


class CategoryView:
    def __init__(self, raw: Optional[dict], slug_hint: str = "") -> None:
        self.raw = _d(raw)
        self._slug_hint = slug_hint

    @property
    def slug(self) -> str:
        return clean(_text(self.raw.get("slug")) or self._slug_hint).lower()

    @property
    def display_name(self) -> str:
        return clean(self.raw.get("display_name")) or self.slug.title()

    @property
    def voice(self) -> dict:
        return _d(self.raw.get("voice"))

    @property
    def tone(self) -> str:
        return clean(self.voice.get("tone"))

    @property
    def code_mix(self) -> str:
        return clean(self.voice.get("code_mix")).lower()

    @cached_property
    def taboos(self) -> list[str]:
        out = []
        for t in _l(self.voice.get("vocab_taboo")) + _l(self.voice.get("taboos")):
            base = re.sub(r"\(.*?\)", "", str(t)).strip().lower() if isinstance(t, str) else ""
            if len(base) >= 3 and re.search(r"[a-z]", base):
                out.append(base)
        return out

    @property
    def vocab(self) -> list[str]:
        return [v for v in _l(self.voice.get("vocab_allowed")) if isinstance(v, str) and len(v.strip()) > 2]

    @property
    def offer_catalog(self) -> list[dict]:
        return [o for o in _l(self.raw.get("offer_catalog")) if isinstance(o, dict) and _text(o.get("title"))]

    @property
    def peer_stats(self) -> dict:
        return _d(self.raw.get("peer_stats"))

    def peer(self, *keys: str) -> Optional[float]:
        for k in keys:
            v = num(self.peer_stats.get(k))
            if v is not None:
                return v
        return None

    @property
    def digest(self) -> list[dict]:
        return [d for d in _l(self.raw.get("digest")) if isinstance(d, dict) and _text(d.get("title"))]

    def digest_item(self, item_id: Any) -> Optional[dict]:
        if not item_id:
            return None
        for d in self.digest:
            if str(d.get("id")) == str(item_id):
                return d
        return None

    @property
    def content_library(self) -> list[dict]:
        return [c for c in _l(self.raw.get("patient_content_library")) if isinstance(c, dict)]

    @property
    def seasonal_beats(self) -> list[dict]:
        return [b for b in _l(self.raw.get("seasonal_beats"))
                if isinstance(b, dict) and _text(b.get("month_range")) and _text(b.get("note"))]

    @property
    def trend_signals(self) -> list[dict]:
        return [t for t in _l(self.raw.get("trend_signals")) if isinstance(t, dict) and _text(t.get("query"))]

    def _nouns(self) -> tuple[str, str, str, str]:
        if self.slug in _CATEGORY_NOUNS:
            return _CATEGORY_NOUNS[self.slug]
        base = self.slug.rstrip("s") or "business"
        return (base, "business", "customers", "customer")

    @property
    def singular(self) -> str:
        return self._nouns()[0]

    @property
    def venue(self) -> str:
        return self._nouns()[1]

    @property
    def people(self) -> str:
        return self._nouns()[2]

    @property
    def person(self) -> str:
        return self._nouns()[3]


class MerchantView:
    def __init__(self, raw: Optional[dict], merchant_id: str = "") -> None:
        self.raw = _d(raw)
        self._id_hint = merchant_id

    @property
    def merchant_id(self) -> str:
        return clean(self.raw.get("merchant_id") or self._id_hint)

    @property
    def category_slug(self) -> str:
        return clean(_text(self.raw.get("category_slug")) or _text(self.raw.get("category"))).lower()

    @property
    def identity(self) -> dict:
        return _d(self.raw.get("identity"))

    @property
    def name(self) -> str:
        return clean(_text(self.identity.get("name")) or _text(self.raw.get("name"))) or "your business"

    @property
    def has_name(self) -> bool:
        return bool(clean(_text(self.identity.get("name")) or _text(self.raw.get("name"))))

    @property
    def owner_first(self) -> str:
        """Owner first name without any 'Dr.' prefix."""
        raw = clean(_text(self.identity.get("owner_first_name")))
        first = strip_dr(raw).split(" ")[0] if raw else ""
        return first if re.search(r"[A-Za-z\u0900-\u097F]", first) else ""

    @property
    def city(self) -> str:
        return clean(_text(self.identity.get("city")))

    @property
    def locality(self) -> str:
        return clean(_text(self.identity.get("locality")))

    @property
    def verified(self) -> Optional[bool]:
        v = self.identity.get("verified")
        return v if isinstance(v, bool) else None

    @property
    def languages(self) -> list[str]:
        return [str(x).lower() for x in _l(self.identity.get("languages"))]

    @property
    def established_year(self) -> Optional[int]:
        v = num(self.identity.get("established_year"))
        return int(v) if v else None

    @property
    def subscription(self) -> dict:
        return _d(self.raw.get("subscription"))

    @property
    def performance(self) -> dict:
        return _d(self.raw.get("performance"))

    def perf(self, key: str) -> Optional[float]:
        return num(self.performance.get(key))

    @property
    def delta_7d(self) -> dict[str, float]:
        out = {}
        for k, v in _d(self.performance.get("delta_7d")).items():
            n = num(v)
            if n is not None:
                out[str(k)] = n
        return out

    @property
    def offers(self) -> list[dict]:
        return [o for o in _l(self.raw.get("offers")) if isinstance(o, dict) and _text(o.get("title"))]

    @property
    def active_offers(self) -> list[str]:
        return [clean(_text(o["title"])) for o in self.offers if str(o.get("status", "active")).lower() == "active"]

    @property
    def inactive_offers(self) -> list[str]:
        return [clean(_text(o["title"])) for o in self.offers
                if str(o.get("status", "")).lower() in {"expired", "paused", "inactive"}]

    @property
    def history(self) -> list[dict]:
        return [h for h in _l(self.raw.get("conversation_history")) if isinstance(h, dict)]

    @property
    def aggregate(self) -> dict:
        return _d(self.raw.get("customer_aggregate"))

    def agg(self, *keys: str) -> Optional[float]:
        for k in keys:
            v = num(self.aggregate.get(k))
            if v is not None:
                return v
        return None

    @property
    def signals(self) -> list[str]:
        return [s for s in _l(self.raw.get("signals")) if isinstance(s, str)]

    def signal_value(self, prefix: str) -> Optional[float]:
        """'stale_posts:22d' -> 22 ; 'dormant_with_vera_14d' -> 14 ; bare flag -> 0."""
        for s in self.signals:
            if s.startswith(prefix):
                m = re.search(r"(\d+)\s*d?\s*$", s)
                return float(m.group(1)) if m else 0.0
        return None

    def has_signal(self, prefix: str) -> bool:
        return any(s.startswith(prefix) for s in self.signals)

    @property
    def review_themes(self) -> list[dict]:
        return [r for r in _l(self.raw.get("review_themes")) if isinstance(r, dict) and _text(r.get("theme"))]

    @cached_property
    def keywords(self) -> set[str]:
        # content words only: signals are internal flags (e.g. 'delivery_not_set_up') and would mislead matching
        text = " ".join([
            self.name,
            " ".join(self.active_offers),
            " ".join(f"{r.get('theme', '')} {r.get('common_quote', '')}" for r in self.review_themes),
        ])
        return tokens(text)


_REGIONAL = {
    "ta": "Vanakkam",
    "te": "Namaskaram",
    "kn": "Namaskara",
    "mr": "Namaskar",
    "bn": "Nomoshkar",
}


class CustomerView:
    def __init__(self, raw: Optional[dict], customer_id: str = "") -> None:
        self.raw = _d(raw)
        self._id_hint = customer_id

    @property
    def customer_id(self) -> str:
        return clean(self.raw.get("customer_id") or self._id_hint)

    @property
    def merchant_id(self) -> str:
        return clean(self.raw.get("merchant_id"))

    @property
    def identity(self) -> dict:
        return _d(self.raw.get("identity"))

    @property
    def raw_name(self) -> str:
        return clean(_text(self.identity.get("name")))

    @property
    def has_name(self) -> bool:
        n = self.raw_name
        return bool(n) and not n.startswith("(")

    @property
    def first_name(self) -> str:
        """'Aanya (parent: Sneha)' -> 'Aanya'; 'Mr. Sharma' stays as-is."""
        n = re.sub(r"\(.*?\)", "", self.raw_name).strip()
        if not n:
            return ""
        if re.match(r"^(mr|mrs|ms|dr)\.?\s", n, flags=re.I):
            return n
        return n.split(" ")[0]

    @property
    def parent_name(self) -> str:
        m = re.search(r"parent:\s*([^)]+)\)", self.raw_name, flags=re.I)
        return clean(m.group(1)) if m else ""

    @property
    def is_child(self) -> bool:
        return bool(self.parent_name) or "child" in str(self.identity.get("age_band", "")).lower()

    @property
    def honorific(self) -> str:
        """'Mr. Sharma' -> 'Sharma ji' (respectful Hindi register)."""
        m = re.match(r"^(mr|mrs|ms)\.?\s+(\w+)", self.first_name, flags=re.I)
        return f"{m.group(2)} ji" if m else self.first_name

    @property
    def language_pref(self) -> str:
        return clean(_text(self.identity.get("language_pref"))).lower()

    @property
    def lang_mode(self) -> str:
        p = self.language_pref
        if p in {"hi", "hindi"}:
            return "hindi"
        if p.startswith("hi") and "mix" in p:
            return "hinglish"
        return "en"

    @property
    def regional_greeting(self) -> str:
        code = self.language_pref.split("-")[0].strip()
        return _REGIONAL.get(code, "")

    @property
    def senior(self) -> bool:
        if self.identity.get("senior_citizen") is True:
            return True
        m = re.match(r"(\d+)", str(self.identity.get("age_band", "")))
        return bool(m and int(m.group(1)) >= 60)

    @property
    def relationship(self) -> dict:
        return _d(self.raw.get("relationship"))

    @property
    def last_visit(self) -> str:
        return clean(_text(self.relationship.get("last_visit")))

    @property
    def visits_total(self) -> Optional[int]:
        v = num(self.relationship.get("visits_total"))
        return int(v) if v is not None else None

    @property
    def services(self) -> list[str]:
        return [humanize(s) for s in _l(self.relationship.get("services_received")) if s and s != "..."]

    @property
    def favourite(self) -> str:
        return clean(self.relationship.get("favourite_dish"))

    @property
    def state(self) -> str:
        return clean(_text(self.raw.get("state"))).lower()

    @property
    def preferences(self) -> dict:
        return _d(self.raw.get("preferences"))

    @property
    def preferred_slots(self) -> str:
        return clean(_text(self.preferences.get("preferred_slots"))).lower()

    @property
    def channel(self) -> str:
        return clean(_text(self.preferences.get("channel"))).lower()

    @property
    def via(self) -> str:
        """'whatsapp_via_son' -> 'son'."""
        m = re.search(r"via_(\w+)", self.channel)
        return m.group(1) if m else ""

    @property
    def reminder_opt_in(self) -> Optional[bool]:
        v = self.preferences.get("reminder_opt_in")
        return v if isinstance(v, bool) else None

    @property
    def consent(self) -> dict:
        return _d(self.raw.get("consent"))

    @property
    def consent_scope(self) -> set[str]:
        return {s.lower() for s in _l(self.consent.get("scope")) if isinstance(s, str)}

    @property
    def opted_in(self) -> bool:
        return bool(_text(self.consent.get("opted_in_at"))) or bool(self.consent_scope)

    @property
    def contactable(self) -> bool:
        if self.channel in {"none", "none_recorded", "no_channel"}:
            return False
        if "phone_redacted" in self.identity and self.identity.get("phone_redacted") in (None, ""):
            return False
        return True


class TriggerView:
    def __init__(self, raw: Optional[dict], trigger_id: str = "") -> None:
        self.raw = _d(raw)
        self._id_hint = trigger_id

    @property
    def trigger_id(self) -> str:
        return clean(self.raw.get("id") or self._id_hint)

    @property
    def kind(self) -> str:
        return clean(_text(self.raw.get("kind"))).lower() or "unknown"

    @property
    def scope(self) -> str:
        s = clean(self.raw.get("scope")).lower()
        if s in {"merchant", "customer"}:
            return s
        return "customer" if self.customer_id else "merchant"

    @property
    def source(self) -> str:
        return clean(self.raw.get("source")).lower()

    @property
    def payload(self) -> dict:
        return _d(self.raw.get("payload"))

    def p(self, key: str, default: Any = None) -> Any:
        v = self.payload.get(key, default)
        return default if v is None else v

    def pnum(self, key: str) -> Optional[float]:
        """Finite number from the payload, or None (junk types never reach the copy)."""
        return num(self.payload.get(key))

    def pint(self, key: str) -> Optional[int]:
        v = self.pnum(key)
        return int(round(v)) if v is not None else None

    def ptext(self, key: str, default: str = "") -> str:
        return _text(self.payload.get(key)) or default

    @property
    def is_placeholder(self) -> bool:
        meaningful = {k for k in self.payload if k not in {"placeholder", "metric_or_topic", "category"}}
        return bool(self.payload.get("placeholder")) or not meaningful

    @property
    def merchant_id(self) -> str:
        return clean(_text(self.raw.get("merchant_id")) or _text(self.payload.get("merchant_id")))

    @property
    def customer_id(self) -> str:
        return clean(_text(self.raw.get("customer_id")) or _text(self.payload.get("customer_id")))

    @property
    def urgency(self) -> int:
        v = num(self.raw.get("urgency"))
        return max(1, min(5, int(v))) if v is not None else 2

    @property
    def suppression_key(self) -> str:
        return clean(_text(self.raw.get("suppression_key")))

    @property
    def expires_at(self) -> str:
        return clean(_text(self.raw.get("expires_at")))
