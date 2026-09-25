"""Optional LLM polish: rewrite a deterministic draft for naturalness, never for content.

Pipeline: draft -> prompt (draft + fact sheet) -> LLM -> JSON parse -> strict
validation -> accept, else keep the deterministic draft. Results are cached by
prompt hash so identical inputs always yield identical output within a process.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from typing import Iterable, Optional

from ..utils.logging import get_logger, log_event
from ..utils.text import number_tokens
from ..validation.output import URL_RE, taboo_hits
from .provider import LLMError, LLMProvider

log = get_logger("polish")

SYSTEM = (
    "You are an editor for WhatsApp messages that an assistant called Vera sends to Indian small-business owners "
    "(or, on their behalf, to their customers). You improve flow and warmth only. Hard rules:\n"
    "1. Keep every fact, number, price, date, name, source citation and offer exactly as given. Never add a number, "
    "statistic, name, date, offer, competitor or claim that is not in the draft.\n"
    "2. Keep the same single call-to-action as the last sentence, including reply keywords such as YES, CONFIRM, "
    "STOP or numbered options.\n"
    "3. At most one question mark. No URLs. No hype words. Keep the same language mix (English/Hinglish).\n"
    "4. Do not make it longer than the draft.\n"
    'Return only JSON: {"body": "<rewritten message>"}'
)

_COMMON = {"Quick", "Also", "Just", "Here", "Hi", "Hello", "Namaste", "Want", "Shall", "Reply", "Your", "You", "The",
           "This", "That", "And", "But", "So", "If", "It", "We", "Our", "One", "Good", "Great", "Thanks", "Thank",
           "Heads", "Worth", "Happy", "Please", "Bas", "Haan", "Aapke", "Aapki", "Aap", "Hum", "Ek", "Done", "Sure",
           "Okay", "Let", "Let's", "Before", "Since", "With", "For", "Over", "Right", "Now", "Today", "Tonight"}
_KEYWORDS = ("YES", "CONFIRM", "STOP", "Reply 1", "Reply YES")


class Polisher:
    def __init__(self, provider: Optional[LLMProvider], cache_size: int = 2048) -> None:
        self.provider = provider
        self._cache: OrderedDict[str, Optional[str]] = OrderedDict()
        self._lock = threading.Lock()
        self._cache_size = cache_size

    @property
    def enabled(self) -> bool:
        return self.provider is not None

    def polish(self, draft: str, facts: dict, taboos: Iterable[str] = ()) -> tuple[str, str]:
        """Return (body, source) where source is 'llm', 'llm_cache', or 'deterministic[:reason]'."""
        if self.provider is None:
            return draft, "deterministic"
        prompt = ("Draft message:\n" + draft + "\n\nFact sheet (for reference only; do not add facts from it):\n"
                  + json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str)[:4000])
        key = hashlib.sha256((SYSTEM + prompt + self.provider.model).encode("utf-8")).hexdigest()
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                cached = self._cache[key]
                return (cached, "llm_cache") if cached else (draft, "deterministic:cached_rejection")
        try:
            raw = self.provider.generate(prompt, SYSTEM, max_tokens=800)
            candidate = parse_body(raw)
            problems = check_rewrite(draft, candidate, taboos)
        except LLMError as exc:
            log_event(log, "llm_failure_fallback", error=str(exc))
            return draft, "deterministic:llm_error"   # transient: not cached
        accepted = candidate if not problems else None
        with self._lock:
            self._cache[key] = accepted
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        if problems:
            log_event(log, "llm_output_rejected", problems=problems[:5])
            return draft, "deterministic:validation"
        return candidate, "llm"


def parse_body(raw: str) -> str:
    m = re.search(r"\{[\s\S]*\}", raw or "")
    if not m:
        raise LLMError("no JSON object in completion")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        raise LLMError("malformed JSON in completion") from None
    body = data.get("body") if isinstance(data, dict) else None
    if not isinstance(body, str) or not body.strip():
        raise LLMError("missing body")
    return body.strip()


def _proper_nouns(text: str) -> set[str]:
    return {w for w in re.findall(r"\b[A-Z][a-z]{2,}\b", text) if w not in _COMMON}


def check_rewrite(draft: str, candidate: str, taboos: Iterable[str] = ()) -> list[str]:
    problems = []
    if len(candidate) > len(draft) * 1.15 + 20:
        problems.append("longer_than_draft")
    if len(candidate) < len(draft) * 0.5:
        problems.append("dropped_content")
    draft_nums = sorted(number_tokens(draft))
    cand_nums = number_tokens(candidate)
    extra = [n for n in cand_nums if n not in draft_nums]
    if extra:
        problems.append("new_numbers:" + ",".join(extra[:5]))
    missing = [n for n in set(draft_nums) if n not in cand_nums]
    if missing:
        problems.append("dropped_numbers:" + ",".join(sorted(missing)[:5]))
    for kw in _KEYWORDS:
        if kw in draft and kw not in candidate:
            problems.append(f"lost_keyword:{kw}")
    if candidate.count("?") > max(1, draft.count("?")):
        problems.append("extra_questions")
    if URL_RE.search(candidate):
        problems.append("url")
    hits = taboo_hits(candidate, taboos)
    if hits:
        problems.append("taboo")
    new_names = _proper_nouns(candidate) - {w for w in _proper_nouns(draft)} - \
        {w.capitalize() for w in re.findall(r"[a-zA-Z]+", draft)}
    if len(new_names) > 1:
        problems.append("new_proper_nouns:" + ",".join(sorted(new_names)[:3]))
    return problems
