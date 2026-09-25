"""Rule-based, deterministic intent classification for merchant/customer replies.

Handles English, Hinglish (romanised Hindi) and Devanagari. Order of checks
matters: safety exits (opt-out, hostility) and auto-replies are evaluated before
anything that could be mistaken for acceptance ("not interested" vs "interested").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from ..utils.text import normalize_message
from ..utils.timeutil import IST

AUTO_REPLY = [
    r"thank(s| you) for (contacting|reaching out|your (message|query|enquiry|inquiry)|messaging|writing to)",
    r"(our|the) (team|executive|representative)s? will (get back|respond|reply|contact|revert|call)",
    r"will (get back|respond|revert|reply) to you (shortly|soon|as soon|at the earliest)",
    r"(we|i) (will|'ll|shall) (respond|reply|get back) (shortly|soon)",
    r"(currently|presently) (unavailable|closed|away|not available|out of (the )?office)",
    r"(outside|beyond) (our )?(business|working|office) hours",
    r"this is an? (automated|auto[- ]?generated|automatic) (message|reply|response)",
    r"\bauto[- ]?reply\b", r"automated assistant", r"do not reply to this",
    r"we have received your (message|query|request)",
    r"(for|in case of) (urgent|immediate) (queries|assistance|help|requirements)",
    r"aapki jaankari ke liye", r"hamari team", r"team tak (pahuncha|pahunch)", r"jald hi (sampark|jawab|aapse)",
    r"(business|store|office|clinic) (hours|timings) (are|is)",
]
OPT_OUT = [
    r"\b(stop|unsubscribe|opt[- ]?out)\b",
    r"\b(don'?t|do not|dont|never) (message|text|contact|call|disturb|send|ping|bother|spam)",
    r"\b(stop|quit|cease) (messaging|texting|sending|contacting|bothering|spamming|these)",
    r"\bremove (me|my number|us)\b", r"\bno more (messages|texts|whatsapps?)\b", r"\bleave (me|us) alone\b",
    r"\b(band|bandh) (karo|kar do|kijiye)\b", r"\bmat (bhejo|bhejna|bhejiye)\b", r"\bmessage mat\b", r"\bblock (kar|you|this)",
    r"बंद करो", r"मत भेजो",
]
HOSTILE = [
    r"\b(useless|spam+y?|scam|fraud|bakwas|bekaar|bekar|idiot|stupid|nonsense|rubbish|pathetic|irritating|annoying|"
    r"harass\w*|shut up|waste of (my )?time|chutiya|bewakoof|pagal|get lost|go to hell|bloody|damn)\b",
    r"\bwhy (are|r) (you|u) (bothering|disturbing|spamming|messaging)", r"\b(bothering|disturbing|pestering) me\b",
]
REJECT = [
    r"\bnot interested\b", r"\bno,? thanks?\b", r"\bno thank you\b", r"\b(don'?t|do not|dont) (need|want|require)\b",
    r"\b(nahi|nahin|nai) chahiye\b", r"\bzaroorat nahi\b", r"\bnot for (me|us)\b", r"\bnot needed\b",
    r"\bno need\b", r"\bnot required\b", r"^(no|nope|nah|nahi|nahin|na)\b", r"\bnot (really )?keen\b", r"\bpass on (this|it)\b",
    r"\bi'?ll pass\b", r"नहीं चाहिए",
]
POSTPONE = [
    r"\b(later|baad me(in)?|baad mein|thodi der)\b", r"\bnot (now|today|right now|this week)\b", r"\babhi nahi\b",
    r"\b(busy|in a meeting|driving|travell?ing|out of (town|station)|on leave|with (a )?patients?)\b",
    r"\b(tomorrow|kal)\b", r"\bnext (week|month)\b", r"\b(call|message|ping|text|remind) me\b",
    r"\bin (\d+|an|a few|ek|do) ?(min|mins|minutes|hour|hours|hrs|hr|days?|ghante|ghanta)\b",
    r"\bafter (\d+|lunch|dinner|some time)\b", r"\b(let me think|will think|will see|dekhte hain|sochta|sochti|soch ke)\b",
    r"\b(get back to you|revert later)\b", r"\bweekend\b", r"\b(evening|shaam|night|raat) (me|mein|ko)?\b",
]
OFF_TOPIC = {
    "ca": r"\b(gst|income tax|itr|tax (filing|return)s?|file (my )?(taxes|returns|gst)|tds|audit my books|accounting|balance sheet)\b",
    "bank": r"\b(loan|emi|credit card|insurance|mutual funds?|stock market|shares|crypto|bitcoin|fd rates?)\b",
    "lawyer": r"\b(legal notice|lawyer|court case|rent agreement|trademark|fir)\b",
    "other": r"\b(cricket score|recipe|movie|election|politics|lottery|job opening|visa|passport|aadhaar|pan card|electricity bill|horoscope)\b",
}
ACCEPT = [
    r"^(yes|yeah|yep|yup|ya|yea|y|ok|okay|okk+|k|sure|done|haan|han|haa+|ji|hanji|haanji|bilkul|zaroor|chalo|chalega|"
    r"go ahead|proceed|confirm|confirmed|please do|do it|send|sounds good|great|perfect|agreed)\b",
    r"\b(go ahead|let'?s do (it|this)|lets do (it|this)|do it|sounds good|sounds great|please (send|do|share|go ahead|proceed|draft|set)|"
    r"send (it|me|the|over|now)|share (it|the)|yes please|ok(ay)? (send|do|go|let)|kar do|kardo|kar dijiye|bhej do|bhejo|bhej dijiye|"
    r"shuru karo|chalo karte|(i )?want to join|sign me up|count me in|(?<!not )interested|i'?m in|book it|lock it|set it up|"
    r"publish it|make it live|go live|theek hai|thik hai|what'?s next|whats next|next step)\b",
    r"\bconfirm(ed)?\b", r"हाँ|हां|ठीक है|भेज दो",
]
THANKS = [r"^(thanks|thank you|thx|ty|dhanyavad|dhanyawad|shukriya|great thanks|ok thanks|okay thanks|thanks a lot|noted)\b",
          r"^(👍|🙏|👌)+$"]
QUESTION_START = r"^(what|how|why|when|where|which|who|can you|could you|will you|is it|is there|are there|does|do you|" \
                 r"kya|kaise|kitna|kitne|kab|kahan|kaun|kyun|kyon|price|cost|charges?|fees?|rate)\b"
MODIFY = r"\b(change|instead|shorter|longer|in hindi|in english|modify|edit|tweak|but make|except|remove)\b"
HINDI_TOKENS = {"hai", "haan", "nahi", "nahin", "kya", "kaise", "karo", "kar", "mujhe", "aap", "hum", "bhej", "chahiye",
                "abhi", "theek", "accha", "acha", "ji", "bhai", "yaar", "matlab", "karna", "kitna", "kab", "mein", "hoon",
                "raha", "rahi", "kijiye", "dijiye", "bata", "batao", "samajh", "thoda", "baad"}
SLOT_WORDS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass
class Intent:
    name: str
    wait_seconds: Optional[int] = None
    slot_index: Optional[int] = None
    off_topic_kind: str = ""
    lang: str = "en"
    flags: set = field(default_factory=set)


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def detect_language(message: str) -> str:
    if re.search(r"[ऀ-ॿ]", message or ""):
        return "hindi"
    toks = set(normalize_message(message).split())
    return "hinglish" if len(toks & HINDI_TOKENS) >= 1 else "en"


def parse_wait(text: str, received_at: Optional[datetime]) -> int:
    t = text.lower()
    m = re.search(r"(\d+)\s*(min|mins|minutes|minute)\b", t)
    if m:
        return max(300, int(m.group(1)) * 60)
    m = re.search(r"(\d+)\s*(hr|hrs|hour|hours|ghante|ghanta)\b", t)
    if m:
        return int(m.group(1)) * 3600
    m = re.search(r"(\d+)\s*(day|days|din)\b", t)
    if m:
        return int(m.group(1)) * 86400
    if re.search(r"\b(an|1|ek) (hour|hr|ghanta)\b", t):
        return 3600
    if re.search(r"\bnext (week|month)\b", t):
        return 7 * 86400 if "week" in t else 30 * 86400
    if re.search(r"\b(tomorrow|kal)\b", t):
        if received_at:
            local = received_at.astimezone(IST)
            target = (local + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
            return max(3600, int((target - local).total_seconds()))
        return 86400
    if re.search(r"\b(evening|shaam)\b", t):
        if received_at:
            local = received_at.astimezone(IST)
            target = local.replace(hour=18, minute=0, second=0, microsecond=0)
            if target > local + timedelta(minutes=30):
                return int((target - local).total_seconds())
        return 4 * 3600
    if re.search(r"\b(not today|weekend)\b", t):
        return 86400
    if re.search(r"\b(not now|abhi nahi|not right now|let me think|will think|will see|dekhte hain)\b", t):
        return 4 * 3600
    return 1800  # busy / later / in a meeting: short back-off


def slot_choice(text: str, slots: list[dict]) -> Optional[int]:
    t = normalize_message(text)
    m = re.fullmatch(r"(option |slot )?([1-9])( please| pls| plz)?", t)
    if m:
        idx = int(m.group(2)) - 1
        return idx if idx < max(1, len(slots)) else None
    if re.search(r"\b(first|pehla|pehli)\b", t):
        return 0
    if re.search(r"\b(second|doosra|dusra|doosri)\b", t) and len(slots) > 1:
        return 1
    for i, s in enumerate(slots):
        label = str(s.get("label", "")).lower()
        day = label[:3]
        if day in SLOT_WORDS and re.search(rf"\b{day}\w*\b", t):
            return i
    return None


def classify(message: str, *, slots: Optional[list[dict]] = None, repeat_count: int = 0,
             received_at: Optional[datetime] = None) -> Intent:
    raw = (message or "").strip()
    low = raw.lower()
    norm = normalize_message(raw)
    lang = detect_language(raw)
    if not norm and not re.search(r"[ऀ-ॿ]|👍|🙏|👌", raw):
        return Intent("ambiguous", lang=lang)

    words = len(norm.split())
    if _any(AUTO_REPLY, low) or (repeat_count >= 2 and words >= 4):
        return Intent("auto_reply", lang=lang)

    hostile = _any(HOSTILE, low)
    if _any(OPT_OUT, low):
        return Intent("opt_out", lang=lang, flags={"hostile"} if hostile else set())

    off_kind = next((k for k, p in OFF_TOPIC.items() if re.search(p, low)), "")
    if hostile:
        return Intent("hostile", lang=lang, off_topic_kind=off_kind)

    if slots:
        idx = slot_choice(raw, slots)
        if idx is not None:
            return Intent("slot_choice", slot_index=idx, lang=lang)

    is_question = "?" in raw or bool(re.search(QUESTION_START, norm))
    accepted = _any(ACCEPT, low) or _any(ACCEPT, norm)
    postponed = _any(POSTPONE, norm)
    rejected = _any(REJECT, norm) and not re.search(r"\bno problem\b|\bno worries\b", norm)

    if off_kind and not accepted:
        return Intent("off_topic", off_topic_kind=off_kind, lang=lang)
    if postponed and not re.search(r"\bnot now\b.*\b(but|lekin)\b.*\b(yes|send|do)\b", norm):
        if rejected and not re.search(r"\bnot (now|today|right now|this week)\b|\babhi nahi\b", norm):
            return Intent("reject", lang=lang)
        return Intent("postpone", wait_seconds=parse_wait(raw, received_at), lang=lang)
    if rejected and not (accepted and re.search(r"\b(just|only|instead|but)\b", norm)):
        return Intent("reject", lang=lang)
    if accepted:
        flags = set()
        if re.search(MODIFY, norm):
            flags.add("modify")
        if is_question:
            flags.add("question")
        return Intent("accept", lang=lang, flags=flags)
    if _any(THANKS, norm) or _any(THANKS, raw):
        return Intent("thanks", lang=lang)
    if off_kind:
        return Intent("off_topic", off_topic_kind=off_kind, lang=lang)
    if is_question:
        return Intent("question", lang=lang)
    if words >= 2 and not re.fullmatch(r"(hmm+|hm+|ok\??|maybe|not sure|dunno|idk|pata nahi|\?+)", norm):
        return Intent("statement", lang=lang)
    return Intent("ambiguous", lang=lang)
