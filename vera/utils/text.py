"""Formatting helpers: Indian number grouping, percentages, slugs, sentences."""

from __future__ import annotations

import math
import re
from typing import Any, Iterable

_WS = re.compile(r"\s+")


def indian_group(n: int) -> str:
    """1234567 -> '12,34,567' (Indian digit grouping)."""
    sign = "-" if n < 0 else ""
    s = str(abs(int(n)))
    if len(s) <= 3:
        return sign + s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return sign + ",".join(parts) + "," + tail


def fmt_int(value: Any) -> str:
    try:
        return indian_group(int(round(float(value))))
    except (TypeError, ValueError, OverflowError):
        return ""


def inr(value: Any) -> str:
    """₹ amount with Indian grouping; keeps paise only when non-zero."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(f):
        return ""
    if abs(f - round(f)) < 1e-9:
        return "₹" + indian_group(int(round(f)))
    return "₹" + f"{f:,.2f}"


def pct(ratio: Any, digits: int = 1) -> str:
    """0.021 -> '2.1%'; 0.03 -> '3.0%' (rates keep one decimal); 0.10 -> '10%'."""
    try:
        v = float(ratio) * 100.0
    except (TypeError, ValueError):
        return str(ratio)
    return f"{v:.{digits}f}%"


def pct_whole(ratio: Any) -> str:
    """-0.5 -> '50%', 0.18 -> '18%' (absolute, rounded)."""
    try:
        v = abs(float(ratio)) * 100.0
    except (TypeError, ValueError):
        return str(ratio)
    if abs(v - round(v)) < 0.05:
        return f"{int(round(v))}%"
    return f"{v:.1f}%"


def humanize(slug: Any) -> str:
    """'kids_yoga_summer_camp' -> 'kids yoga summer camp'."""
    text = _renderable(slug).replace("_", " ").replace("-", " ")
    return _WS.sub(" ", text).strip()


def _renderable(value: Any) -> str:
    """Text for copy; containers, None and NaN/inf never render as '{...}' / 'None' / 'nan'."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return str(value)


def clean(text: Any) -> str:
    return _WS.sub(" ", _renderable(text)).strip()


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9'\"‘“(])")


def sentences(text: Any) -> list[str]:
    """Split on sentence punctuation followed by whitespace + a capital/digit (keeps '1.5 mSv' intact)."""
    t = clean(text)
    if not t:
        return []
    return [p.strip() for p in _SENT_SPLIT.split(t) if p.strip()]


def first_sentence(text: Any) -> str:
    parts = sentences(text)
    return parts[0] if parts else ""


def strip_terminal(text: str) -> str:
    return clean(text).rstrip(" .;:,")


def ensure_period(text: str) -> str:
    t = clean(text)
    if not t:
        return t
    return t if t[-1] in ".!?" else t + "."


def lower_first(text: str) -> str:
    t = clean(text)
    if len(t) >= 2 and t[0].isupper() and not t[1].isupper():
        return t[0].lower() + t[1:]
    return t


def upper_first(text: str) -> str:
    t = clean(text)
    return t[0].upper() + t[1:] if t else t


def join_list(items: Iterable[str], conj: str = "and") -> str:
    items = [i for i in (clean(x) for x in items) if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" {conj} " + items[-1]


def strip_dr(name: str) -> str:
    return re.sub(r"^\s*(dr\.?|doctor)\s+", "", clean(name), flags=re.I)


def tokens(text: Any) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", str(text or "").lower()) if len(t) > 2}


def normalize_message(text: str) -> str:
    """Lowercase, strip punctuation/emoji, collapse whitespace — used for intent + repeat detection."""
    t = str(text or "").lower()
    t = re.sub(r"[^\w\s']", " ", t)
    return _WS.sub(" ", t).strip()


_NUM = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)")


def number_tokens(text: str) -> list[str]:
    """Numeric literals in a string, commas removed ('₹1,499' -> '1499')."""
    return [m.replace(",", "") for m in _NUM.findall(text or "")]


def a_an(next_word: str) -> str:
    """Article for the following token: 'an 18', 'an 8', 'an 11', 'a 12', 'an offer', 'a CTR'."""
    w = clean(next_word).lstrip("₹'\"")
    if not w:
        return "a"
    if w[0].isdigit():
        digits = re.match(r"[\d,]+", w).group(0).replace(",", "")
        if digits.startswith("8") or digits in {"11", "18"} or (len(digits) in (2, 5) and digits[:2] in {"11", "18"}):
            return "an"
        return "a"
    return "an" if w[0].lower() in "aeiou" else "a"


def possessive(name: str) -> str:
    n = clean(name)
    return n + "'" if n.endswith("s") else n + "'s"
