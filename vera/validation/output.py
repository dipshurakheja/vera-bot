"""Output validation for composed messages and replies.

The grounding check is the anti-hallucination guard: every numeric literal in a
message body must trace back to a number present in the supplied contexts (or a
value the composer explicitly derived and registered). The LLM path runs it in
strict mode; failures there trigger the deterministic fallback.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from ..composer.base import CTA_TYPES
from ..utils.text import number_tokens

URL_RE = re.compile(r"(https?://|www\.)\S+", re.I)
ARTIFACT_RE = re.compile(r"\{\{?\s*\w*\s*\}?\}|\bNone\b|\bnull\b|\bnan\b|\bundefined\b|\[object")
CLOCK_RE = re.compile(r"T(\d{2}):(\d{2})")
SEND_AS = {"vera", "merchant_on_behalf"}
REPLY_ACTIONS = {"send", "wait", "end"}
MAX_BODY_CHARS = 1500


def _add_value(out: set, v: float) -> None:
    for x in (v, abs(v), v * 100, abs(v) * 100):
        out.add(round(x, 2))
        out.add(round(x, 1))
        out.add(float(round(x)))


def allowed_numbers(*sources: Any, extra: Iterable[Any] = ()) -> set[float]:
    """Every number reachable from the contexts, in the renderings composers use."""
    out: set[float] = set()

    def walk(node: Any) -> None:
        if node is None or isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            _add_value(out, float(node))
        elif isinstance(node, str):
            for tok in number_tokens(node) + re.findall(r"\d+(?:\.\d+)?", node):  # also 'dormant_38d'
                try:
                    _add_value(out, float(tok))
                except ValueError:
                    pass
            for hh, _mm in CLOCK_RE.findall(node):
                hour = int(hh)
                out.add(float(hour % 12 or 12))
        elif isinstance(node, dict):
            for k, v in node.items():
                for digits in re.findall(r"\d+", str(k)):   # e.g. lapsed_90d_plus -> 90
                    _add_value(out, float(digits))
                walk(v)
        elif isinstance(node, (list, tuple, set, frozenset)):
            for v in node:
                walk(v)

    for s in sources:
        walk(s)
    for v in extra:
        walk(v)
    return out


def ungrounded_numbers(body: str, allowed: set[float], small_int_ok: int = 12) -> list[str]:
    bad = []
    for tok in number_tokens(body):
        try:
            v = float(tok)
        except ValueError:
            continue
        if v.is_integer() and abs(v) <= small_int_ok:
            continue
        if round(v, 2) in allowed or round(v, 1) in allowed:
            continue
        if any(abs(v - a) < 0.051 for a in allowed):
            continue
        bad.append(tok)
    return bad


def taboo_hits(body: str, taboos: Iterable[str]) -> list[str]:
    low = body.lower()
    hits = []
    for t in taboos:
        t = t.strip().lower()
        if t and re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", low):
            hits.append(t)
    return hits


def validate_message(msg: dict, taboos: Iterable[str] = (), allowed: set[float] | None = None,
                     small_int_ok: int = 12) -> list[str]:
    errors: list[str] = []
    body = msg.get("body")
    if not isinstance(body, str) or not body.strip():
        return ["empty_body"]
    if len(body) > MAX_BODY_CHARS:
        errors.append("body_too_long")
    if msg.get("cta") not in CTA_TYPES:
        errors.append("invalid_cta")
    if msg.get("send_as") not in SEND_AS:
        errors.append("invalid_send_as")
    if not str(msg.get("suppression_key") or "").strip():
        errors.append("missing_suppression_key")
    if not str(msg.get("rationale") or "").strip():
        errors.append("missing_rationale")
    if URL_RE.search(body):
        errors.append("url_in_body")
    if ARTIFACT_RE.search(body):
        errors.append("template_artifact")
    if body.count("?") > 1:
        errors.append("multiple_questions")
    hits = taboo_hits(body, taboos)
    if hits:
        errors.append("taboo:" + ",".join(hits))
    if allowed is not None:
        bad = ungrounded_numbers(body, allowed, small_int_ok)
        if bad:
            errors.append("ungrounded_numbers:" + ",".join(bad[:5]))
    return errors


def validate_reply(resp: dict) -> list[str]:
    errors = []
    action = resp.get("action")
    if action not in REPLY_ACTIONS:
        return ["invalid_action"]
    if action == "send":
        if not isinstance(resp.get("body"), str) or not resp["body"].strip():
            errors.append("empty_body")
        elif URL_RE.search(resp["body"]):
            errors.append("url_in_body")
        if resp.get("cta") not in CTA_TYPES:
            errors.append("invalid_cta")
    if action == "wait":
        ws = resp.get("wait_seconds")
        if not isinstance(ws, int) or ws <= 0:
            errors.append("invalid_wait_seconds")
    if not str(resp.get("rationale") or "").strip():
        errors.append("missing_rationale")
    return errors
