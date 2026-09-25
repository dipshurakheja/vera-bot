"""Reply handling: intent -> next move (send / wait / end) using conversation state + live contexts.

State machine per conversation:
    pitched --accept--> delivered --accept/confirm--> executed --(optional next step)--> pitched
Exits: opt-out/hostile (end), reject (end), repeated auto-replies (send -> wait -> end),
postpone (wait), turn cap (end). The bot never re-qualifies after an explicit yes.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta


from ..composer.base import ComposeInput
from ..composer.knowledge import best_active_offer, catalog_pick, offer_price
from ..store.state_store import Conversation, EngineState, Turn
from ..utils.text import clean, fmt_int, join_list, normalize_message, strip_terminal
from ..utils.timeutil import fmt_date, fmt_time, iso_now
from . import fulfillment as F
from .intents import Intent, classify

MAX_BOT_SENDS = 8
OFF_TOPIC_EXPERT = {"ca": "your CA", "bank": "your bank or financial advisor", "lawyer": "a lawyer",
                    "other": "someone who specialises in that"}


def _send(body: str, cta: str, rationale: str) -> dict:
    return {"action": "send", "body": body.strip(), "cta": cta, "rationale": rationale}


def _wait(seconds: int, rationale: str) -> dict:
    return {"action": "wait", "wait_seconds": int(max(60, seconds)), "rationale": rationale}


def _end(rationale: str) -> dict:
    return {"action": "end", "rationale": rationale}


def _join(lines: list[str], cta: str = "") -> str:
    out = ""
    for part in [clean(x) for x in lines if clean(x)] + ([clean(cta)] if clean(cta) else []):
        sep = "\n" if (part.startswith("•") or out.split("\n")[-1].startswith("•")) else " "
        out = f"{out}{sep}{part}" if out else part
    return out


def _hinglish(intent: Intent, ci: ComposeInput) -> bool:
    return intent.lang in {"hinglish", "hindi"}


def best_action(ci: ComposeInput) -> dict:
    """Most useful concrete action for a merchant when no proposal is pending."""
    s, m, cat = ci.signals, ci.merchant, ci.category
    pend = s.pending_intent
    if pend and pend.get("engagement") == "intent_action":
        what_m = re.search(r"want me to ([^?]+)\?", pend.get("vera_text", ""), flags=re.I)
        what = clean(what_m.group(1)) if what_m else "take care of what you asked for"
        topics = _topics_in(ci, pend.get("merchant_text", ""))
        if "post" in what.lower():
            n = re.search(r"(\d+) posts?", what)
            return {"type": "gbp_post", "topics": topics, "count": int(n.group(1)) if n else max(1, len(topics)),
                    "label": f"the {n.group(1) + ' ' if n else ''}posts you asked for", "from_history": True}
        if "list" in what.lower():
            return {"type": "recall_outreach", "molecule": "", "label": "the list you asked for", "from_history": True}
        return {"type": "summary", "label": what, "from_history": True}
    if s.no_active_offers:
        offer = catalog_pick(cat, m)
        if offer:
            return {"type": "offer_setup", "offer": offer["title"], "label": f"the '{offer['title']}' listing"}
    if s.stale_posts_days:
        return {"type": "gbp_post", "offer": best_active_offer(m), "label": "a fresh Google post"}
    if s.unverified:
        return {"type": "verification", "label": "the verification steps"}
    if s.lapsed_count:
        return {"type": "winback_message", "count": s.lapsed_count, "label": "the win-back message"}
    if s.neg_theme:
        return {"type": "review_replies", "theme": s.neg_theme.get("theme"), "label": "the review replies"}
    return {"type": "audit", "label": "a quick listing audit"}


def _topics_in(ci: ComposeInput, text: str) -> list[str]:
    """Services the merchant named, in their own words ('aligners'), matched against category vocabulary."""
    low = (text or "").lower()
    found: list[str] = []
    stems = []
    for o in ci.category.offer_catalog:
        head = re.split(r"\s*@|\(|:", str(o.get("title", "")))[0].strip()
        if head:
            stems.append((head.split(" ")[-1].lower().rstrip("s"), head.lower()))
    stems += [(v.lower().rstrip("s"), v.lower()) for v in ci.category.vocab]
    for stem, full in stems:
        if len(stem) <= 3:
            continue
        m = re.search(rf"\b({re.escape(stem)}\w*)", low)
        if not m:
            continue
        word = m.group(1)
        phrase = full if full.endswith(word) or word in full else word
        phrase = re.sub(rf"\b{re.escape(stem)}\w*$", word, phrase)
        if not any(word in f for f in found):
            found.append(phrase)
    return found[:3]


def _label(conv: Conversation, ci: ComposeInput) -> str:
    return str((conv.next_action or best_action(ci)).get("label") or "the next step")


# ------------------------------------------------------------------ answers

def answer_question(ci: ComposeInput, conv: Conversation, message: str) -> str:
    low = (message or "").lower()
    action = conv.next_action or {}
    m, t, cat = ci.merchant, ci.trigger, ci.category
    digest_id = action.get("digest_id") or conv.meta.get("digest_id")
    item = cat.digest_item(digest_id) if digest_id else None
    if re.search(r"\b(price|cost|charges?|fees?|kitna|kitne|how much|rate|paisa|paise)\b", low):
        if action.get("type") == "renewal" and action.get("amount"):
            return f"The {action.get('plan') or ''} plan renewal is ₹{fmt_int(action['amount'])}.".replace("  ", " ")
        if item and "₹" in str(item.get("actionable", "")) + str(item.get("summary", "")):
            src = str(item.get("actionable") or item.get("summary"))
            return f"From the source: {strip_terminal(src)}."
        offer = action.get("offer") or best_active_offer(m)
        if offer and offer_price(offer):
            return f"The offer on the table is {offer}."
        return "I don't have a price for that in front of me, and I'd rather not guess."
    if re.search(r"\b(source|study|research|proof|data|where (is|did|does)|link|kahan se|evidence)\b", low):
        if item:
            return f"It's from {clean(item.get('source'))}: \"{strip_terminal(clean(item.get('title')))}\"."
        return f"It's from {m.name}'s own Google listing numbers (last 30 days), compared with peer {cat.people} benchmarks."
    if re.search(r"\b(who are you|are you (a )?(bot|human|robot|ai|real)|kaun|what is vera)\b", low):
        return f"I'm Vera, magicpin's AI assistant — I help {m.name} with its Google listing, offers and customer messages."
    if re.search(r"\b(how|kaise|steps?|process)\b", low):
        return "It's simple: I draft it, you check it, and nothing goes live until you reply CONFIRM."
    if re.search(r"\b(when|kab|deadline|date|what time|kitne baje)\b", low):
        for key in ("deadline_iso", "date", "match_time_iso", "due_date", "stock_runs_out_iso"):
            if t.p(key):
                v = t.p(key)
                when = fmt_date(v, weekday=True) + (f", {fmt_time(v)}" if len(str(v)) > 10 and "T" in str(v) else "")
                return f"The date to plan around is {when}."
        return "Whenever suits you — I can have it ready today."
    if re.search(r"\b(what will|what do (i|you) get|include|kya milega|what exactly|details)\b", low):
        return f"You'll get {_label(conv, ci)}, ready to use — nothing goes out without your OK."
    return f"Short version: {_label(conv, ci)} is ready to go, and nothing goes out without your OK."


# ------------------------------------------------------------------ merchant replies

def merchant_turn(state: EngineState, conv: Conversation, ci: ComposeInput, intent: Intent, message: str,
                  now: datetime, opt_out_days: int) -> dict:
    rs = state.recipient(conv.merchant_id or "unknown")
    hi = _hinglish(intent, ci)
    m = ci.merchant

    if intent.name == "auto_reply":
        conv.auto_reply_streak += 1
        rs.auto_reply_count += 1
        n = max(conv.auto_reply_streak, rs.auto_reply_count)
        if n == 1:
            label = _label(conv, ci)
            body = f"Looks like an auto-reply 🙂 When the owner sees this, just reply YES and I'll share {label}."
            if hi:
                body = f"Lagta hai yeh auto-reply hai 🙂 Owner dekhein toh bas YES reply kar dein — main {label} bhej dungi."
            return _send(body, "binary_yes_no", "Detected a WhatsApp Business auto-reply (canned phrasing); one short "
                                                "note flagged for the owner, no new pitch.")
        if n == 2:
            state.set_quiet(conv.merchant_id or "unknown", now + timedelta(hours=24))
            return _wait(86400, "Second auto-reply in a row — owner isn't at the phone. Backing off 24h instead of "
                                "burning turns.")
        state.set_quiet(conv.merchant_id or "unknown", now + timedelta(days=3))
        conv.end_reason = "auto_reply"
        return _end(f"Auto-reply {n}x with no human response — zero engagement signal, closing the conversation.")

    # a real human message resets auto-reply tracking
    conv.auto_reply_streak = 0
    rs.auto_reply_count = 0

    if intent.name == "opt_out":
        state.opt_out(conv.merchant_id or "unknown", now, opt_out_days)
        conv.end_reason = "opt_out"
        return _end("Merchant asked us to stop" + (" (with frustration)" if "hostile" in intent.flags else "") +
                    f"; ending and suppressing proactive messages to this merchant for {opt_out_days} days.")

    if intent.name == "hostile":
        conv.hostile_count += 1
        if conv.hostile_count >= 2:
            state.set_quiet(conv.merchant_id or "unknown", now + timedelta(days=7))
            conv.end_reason = "hostile"
            return _end("Second frustrated message — exiting politely and pausing outreach for 7 days.")
        body = "Sorry for the bother — that's not the experience I want to give you."
        if intent.off_topic_kind:
            body += f" On your question: that's one for {OFF_TOPIC_EXPERT[intent.off_topic_kind]}, outside what I can do."
        body += f" I'll only message when there's something specific for {m.name}; reply STOP anytime and I won't message again."
        return _send(body, "none", "Merchant frustrated: apologised, no pitch, clear opt-out path.")

    if intent.name == "reject":
        state.set_quiet(conv.merchant_id or "unknown", now + timedelta(hours=24))
        conv.end_reason = "reject"
        return _end("Merchant declined this proposal; closing gracefully without pushing (other topics remain open).")

    if intent.name == "postpone":
        secs = intent.wait_seconds or 1800
        conv.wait_until = (now + timedelta(seconds=secs)).isoformat()
        state.set_quiet(conv.merchant_id or "unknown", now + timedelta(seconds=secs))
        return _wait(secs, f"Merchant asked for time; backing off {secs // 60} min before following up.")

    if intent.name == "off_topic":
        label = _label(conv, ci)
        who = OFF_TOPIC_EXPERT.get(intent.off_topic_kind, OFF_TOPIC_EXPERT["other"])
        body = f"That one's best handled by {who} — it's outside what I can help with here. Coming back to {label}: " \
               f"shall I go ahead? Reply YES."
        if hi:
            body = f"Yeh {who} behtar sambhal payenge — main isme help nahi kar paungi. Wapas {label} par: " \
                   f"shuru karun? YES bhej dijiye."
        return _send(body, "binary_yes_no", "Out-of-scope request declined politely; redirected to the original thread.")

    if intent.name == "accept":
        return _advance(state, conv, ci, intent, message, now)

    if intent.name == "statement":
        action = conv.next_action or {}
        if action.get("type") == "curious_answer" and conv.stage == "pitched":
            return _advance(state, conv, ci, intent, message, now)
        if conv.stage == "delivered" and re.search(r"\b(change|shorter|longer|add|remove|instead|edit|hindi|english)\b",
                                                   message.lower()):
            return _send("Noted — I'll make that change and keep everything else as drafted. Reply CONFIRM when "
                         "you're happy and I'll take it live.", "binary_confirm_cancel",
                         "Merchant requested an edit to the delivered draft; acknowledged, kept action mode.")
        return _clarify(conv, ci, hi, now)

    if intent.name == "question":
        ans = answer_question(ci, conv, message)
        if conv.stage == "delivered":
            cta = "Reply CONFIRM when you're happy with it." if not hi else "Theek lage toh CONFIRM reply kar dijiye."
            return _send(f"{ans} {cta}", "binary_confirm_cancel", "Answered the merchant's question from context; "
                                                                  "kept the pending confirmation.")
        cta = "Shall I go ahead? Reply YES." if not hi else "Shuru karun? Haan ho toh YES bhej dijiye."
        return _send(f"{ans} {cta}", "binary_yes_no", "Answered from supplied context (no guessing), then one CTA.")

    if intent.name == "thanks":
        if conv.stage in {"executed", "delivered"} or conv.meta.get("executed_count"):
            conv.end_reason = "complete"
            return _end("Merchant acknowledged; nothing pending — closing the loop without extra messages.")
        return _clarify(conv, ci, hi, now)

    return _clarify(conv, ci, hi, now)


def _clarify(conv: Conversation, ci: ComposeInput, hi: bool, now: datetime) -> dict:
    if conv.clarify_count >= 1:
        return _wait(3600, "Still ambiguous after one clarification — waiting instead of pestering.")
    conv.clarify_count += 1
    label = _label(conv, ci)
    body = f"Just to be sure — shall I go ahead with {label}? Reply YES, or NO if it's not for you."
    if hi:
        body = f"Bas confirm kar dun — {label} ke saath aage badhun? YES ya NO reply kar dijiye."
    return _send(body, "binary_yes_no", "Ambiguous reply; one binary clarification.")


def _advance(state: EngineState, conv: Conversation, ci: ComposeInput, intent: Intent, message: str,
             now: datetime) -> dict:
    """Intent transition: move the pending proposal forward. Never asks a qualifying question."""
    if conv.stage in {"pitched", ""} or not conv.next_action:
        action = conv.next_action or best_action(ci)
        conv.next_action = action
        if action.get("type") == "gbp_post" and action.get("topics"):
            lines = [f"On it — here are {action.get('label', 'the posts')}, focused on {join_list(action['topics'])}:"]
            offer = best_active_offer(ci.merchant)
            topics = list(action["topics"])
            for i, topic in enumerate(topics[: max(1, action.get("count", len(topics)))]):
                lines.append(f"• Post {i + 1}: \"{F._post(ci, topic, None, with_review=(i == 0))}\"")
            if offer and len(lines) - 1 < action.get("count", 1):
                lines.append(f"• Post {len(lines)}: \"{F._post(ci, '', offer, with_review=False)}\"")
            cta, cta_type = "Reply CONFIRM and I'll publish them, or tell me what to change.", "binary_confirm_cancel"
        else:
            lines, cta, cta_type = F.deliver(ci, action, message)
            if not lines[0].lower().startswith(("done", "on it", "here", "love it", "pulling", "publishing")):
                lines.insert(0, "On it.")
        if "modify" in intent.flags:
            lines.insert(1, "Noted on the changes — built in below.")
        conv.stage = "executed" if cta_type == "none" else "delivered"
        return _send(_join(lines, cta), cta_type,
                     "Merchant committed — switched straight to action mode and delivered the artifact "
                     f"({action.get('type')}); single confirm CTA, no re-qualification.")
    if conv.stage == "delivered":
        action = conv.next_action
        body = F.executed(ci, action)
        conv.stage = "executed"
        conv.meta["executed_count"] = int(conv.meta.get("executed_count", 0)) + 1
        step = F.next_step(action, ci)
        if step and conv.bot_sends < 5:
            ntype, text = step
            offer = action.get("offer") or (best_active_offer(ci.merchant) if "around your" in text else None)
            conv.next_action = {"type": ntype, "label": text.replace("draft ", "the ", 1), "offer": offer}
            conv.stage = "pitched"
            body += f" Want me to also {text}? Reply YES."
            return _send(body, "binary_yes_no", "Executed the confirmed action and offered one natural next step.")
        return _send(body, "none", "Executed the confirmed action; nothing else pending.")
    conv.end_reason = "complete"
    return _end("Work already delivered and executed; closing instead of inventing new asks.")


# ------------------------------------------------------------------ customer replies

def customer_turn(state: EngineState, conv: Conversation, ci: ComposeInput, intent: Intent, message: str,
                  now: datetime, opt_out_days: int) -> dict:
    c = ci.customer
    hi = (c.lang_mode in {"hinglish", "hindi"}) if c else intent.lang != "en"
    m = ci.merchant
    action = conv.next_action or {}
    slots = conv.meta.get("slots") or action.get("slots") or []
    key = conv.customer_id or "unknown_customer"

    if intent.name in {"opt_out", "hostile"}:
        state.opt_out(key, now, 365)
        conv.end_reason = "opt_out"
        return _end("Customer opted out — no further messages from this merchant's number via Vera.")
    if intent.name == "reject":
        conv.end_reason = "reject"
        return _end("Customer declined; closing politely without follow-up.")
    if intent.name == "postpone":
        return _wait(intent.wait_seconds or 3600, "Customer asked for time; backing off.")
    if intent.name == "auto_reply":
        return _wait(86400, "Auto-reply from the customer's phone; retry tomorrow.")

    if intent.name == "slot_choice" and slots:
        s = slots[min(intent.slot_index or 0, len(slots) - 1)]
        conv.stage = "executed"
        where = f", {m.locality}" if m.locality else ""
        offer = action.get("offer")
        if hi:
            body = f"Ho gaya ✅ Aapka slot {s.get('label')} ke liye book hai — {m.name}{where}."
            body += f" {offer} apply hoga." if offer else ""
            body += " Badalna ho toh yahin reply kar dijiye."
        else:
            body = f"Done ✅ You're booked for {s.get('label')} at {m.name}{where}."
            body += f" {offer} applies." if offer else ""
            body += " Reply here if you need to change it."
        return _send(body, "none", "Customer picked a slot — confirmed the booking with the real slot label.")

    if intent.name == "accept":
        conv.stage = "executed"
        t = action.get("type")
        if t == "refill_dispatch":
            body = "Confirmed ✅ Order dispatch ho raha hai saved address par; delivery update yahin milega." if hi else \
                "Confirmed ✅ We're dispatching to your saved address; you'll get a delivery update here."
        elif t == "appointment_confirm":
            body = f"Confirmed ✅ Kal milte hain — {m.name}." if hi else f"Confirmed ✅ See you tomorrow at {m.name}."
        elif slots and len(slots) == 1:
            s = slots[0]
            body = f"Done ✅ {s.get('label')} aapke liye save hai." if hi else f"Done ✅ {s.get('label')} is saved for you."
        else:
            body = (f"Great! {m.name} ki team aapko is hafte ke available time yahin bhej degi." if hi else
                    f"Great! The {m.name} team will send you this week's available times right here.")
        return _send(body, "none", "Customer said yes — confirmed next step without adding new asks.")

    if intent.name == "question":
        low = message.lower()
        if re.search(r"\b(price|cost|charges?|fees?|kitna|how much)\b", low):
            offer = action.get("offer") or best_active_offer(m)
            ans = f"{offer} applicable hai." if (offer and hi) else (f"It's {offer}." if offer else
                                                                   "The team will confirm the exact price when you book.")
        elif re.search(r"\b(where|address|location|kahan)\b", low):
            ans = f"Hum {m.locality} mein hain." if hi and m.locality else (
                f"We're in {m.locality}, {m.city}." if m.locality else "The team will share the address.")
        else:
            ans = "Team aapko jaldi confirm karegi." if hi else "The team will confirm that for you shortly."
        cta = "Book karne ke liye YES reply karein." if hi else "Reply YES to book."
        return _send(f"{ans} {cta}", "binary_yes_no", "Answered the customer's question from merchant context.")

    if intent.name == "thanks":
        conv.end_reason = "complete"
        return _end("Customer acknowledged; nothing pending.")

    if re.search(r"\b(morning|evening|afternoon|night|saturday|sunday|weekend|weekday|after|before|subah|shaam)\b",
                 message.lower()):
        pref = next(w for w in ["morning", "evening", "afternoon", "night", "saturday", "sunday", "weekend", "weekday",
                                "subah", "shaam", "after", "before"] if w in message.lower())
        body = f"Noted — hum aapke liye {pref} ka slot dekh kar confirm karte hain." if hi else \
            f"Noted — we'll find you a {pref} slot and confirm shortly."
        return _send(body, "none", "Customer stated a time preference; acknowledged without inventing a slot.")
    if conv.clarify_count >= 1:
        return _wait(3600, "Ambiguous customer reply after one clarification; waiting.")
    conv.clarify_count += 1
    body = "Kya hum aapke liye slot book karein? YES reply karein." if hi else "Shall we book it for you? Reply YES."
    return _send(body, "binary_yes_no", "Ambiguous customer reply; one binary clarification.")


# ------------------------------------------------------------------ entry

def handle(state: EngineState, conv: Conversation, ci: ComposeInput, from_role: str, message: str,
           received_at: datetime, opt_out_days: int) -> dict:
    """Classify + respond. Caller holds state.lock and persists the conversation."""
    rkey = conv.customer_id if from_role == "customer" and conv.customer_id else (conv.merchant_id or "unknown")
    rs = state.recipient(rkey)
    norm = normalize_message(message)
    previous = rs.message_counts.get(norm, 0) if norm else 0
    if norm:
        rs.message_counts[norm] = previous + 1
    intent = classify(message, slots=conv.meta.get("slots") or (conv.next_action or {}).get("slots"),
                      repeat_count=previous, received_at=received_at)
    conv.turns.append(Turn(from_role, message, received_at.isoformat(), intent=intent.name))

    if conv.status == "ended":
        restart = intent.name in {"accept", "question", "statement"} and conv.end_reason not in {"opt_out"}
        if conv.end_reason == "opt_out" and re.search(r"\b(hi vera|start|resume|restart)\b", norm):
            state.recipient(rkey).opted_out_until = None
            restart = True
        if not restart:
            return _end("Conversation already closed; not re-engaging.")
        conv.status, conv.end_reason, conv.stage = "open", "", conv.stage or "pitched"

    if conv.bot_sends >= MAX_BOT_SENDS:
        conv.end_reason = "turn_cap"
        result = _end("Conversation reached the turn cap; closing to avoid over-messaging.")
    elif from_role == "customer" or conv.send_as == "merchant_on_behalf":
        result = customer_turn(state, conv, ci, intent, message, received_at, opt_out_days)
    else:
        result = merchant_turn(state, conv, ci, intent, message, received_at, opt_out_days)

    if result["action"] == "send":
        if result["body"] in conv.sent_bodies:
            result = _wait(3600, "Would have repeated an earlier message verbatim; waiting instead (anti-repetition).")
        else:
            conv.sent_bodies.add(result["body"])
            conv.bot_sends += 1
            conv.turns.append(Turn("bot", result["body"], iso_now(), action="send"))
            conv.status = "open"
    if result["action"] == "wait":
        conv.status = "waiting"
        conv.turns.append(Turn("bot", "", iso_now(), action="wait"))
    elif result["action"] == "end":
        conv.status = "ended"
        conv.turns.append(Turn("bot", "", iso_now(), action="end"))
    return result
