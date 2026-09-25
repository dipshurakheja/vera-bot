"""Action mode: turn an accepted proposal into the actual artifact, then confirm execution.

Everything here is built from the stored contexts and the proposal the bot made
(`next_action`). Drafts are clearly drafts ("edit freely"); no new facts about the
merchant, their customers or the market are introduced.
"""

from __future__ import annotations

import re
from typing import Optional

from ..composer.base import ComposeInput
from ..composer.knowledge import best_active_offer, content_for, digest_text, related_offer
from ..composer.merchant import digest_parts, strength_line, theme_label
from ..utils.text import clean, fmt_int, humanize, join_list, strip_terminal, upper_first

DONE_TEXT = {
    "gbp_post": "the post is live on your Google profile",
    "offer_setup": "the offer is live on your profile",
    "review_replies": "the replies are queued under those reviews",
    "winback_message": "the message is queued to go out",
    "customer_broadcast": "the WhatsApp is queued to go out",
    "publish_plan": "the WhatsApp is queued to go out",
    "recall_outreach": "the note is queued for the matched customers",
    "checklist": "it's saved to your records, and I'll remind you a week before the deadline",
    "verification": "the verification request has gone to Google; share the code here when it arrives",
    "renewal": "the payment details are on their way here",
    "campaign_plan": "the plan is locked in, and I'll ping you when it's time to launch",
    "review_request": "it's queued for today's customers",
    "profile_update": "your description is updated",
    "patient_content": "it's queued for your patient list",
    "retention_program": "the challenge is set up and the announcement is queued",
    "curious_answer": "the post is live and the price reply is saved",
    "audit": "I've started on the first fix",
    "walkthrough": "we're on step 1",
    "summary": "noted",
}

NEXT_STEP = {
    "offer_setup": ("gbp_post", "draft a Google post announcing it"),
    "profile_update": ("gbp_post", "draft a Google post to go with it"),
    "review_replies": ("review_request", "draft a review request for your happy regulars too"),
    "patient_content": ("gbp_post", "draft a Google post around your {offer}"),
    "gbp_post": ("review_request", "draft a review request for this week's customers"),
}


def _quote(text: str) -> str:
    return f"\"{clean(text)}\""


def _offer(ci: ComposeInput, action: dict) -> Optional[str]:
    return action.get("offer") or best_active_offer(ci.merchant, str(action.get("topic") or ""))


def _post(ci: ComposeInput, topic: str, offer: Optional[str], with_review: bool = True,
          headline: str = "") -> str:
    """Customer-facing Google post draft. Single quotes inside so it nests cleanly in a quoted draft."""
    m = ci.merchant
    where = f"{m.name} in {m.locality}" if m.locality else m.name
    topic = strip_terminal(humanize(topic)) if topic else ""
    if topic and (re.search(r"\d|%", topic) or len(topic.split()) > 5):
        topic = ""  # data headlines / internal labels are not customer copy
    if topic and topic[:1].isupper() and not topic.split(" ")[0].isupper():
        topic = topic[0].lower() + topic[1:]
    if headline:
        text = f"{headline}" + (f" {offer} at {where}." if offer else "") + " Call or tap for directions."
    elif topic and offer:
        text = f"Thinking about {topic}? {where} has {offer} — call or tap for directions to book."
    elif offer:
        text = f"{offer} at {where}. Book today — call or tap for directions."
    elif topic:
        text = f"Thinking about {topic}? {where} can help — call or tap for directions to book a visit."
    else:
        text = f"Visit {where} this week — call or tap for directions."
    s = ci.signals
    if with_review and s.pos_theme and s.pos_theme.get("common_quote"):
        text += f" '{clean(s.pos_theme['common_quote'])}' — a recent review."
    return text


def deliver(ci: ComposeInput, action: dict, message: str = "") -> tuple[list[str], str, str]:
    """Artifact for an accepted proposal -> (lines, cta, cta_type)."""
    t = action.get("type", "summary")
    m, cat, s = ci.merchant, ci.category, ci.signals
    lines: list[str] = []
    confirm = "Reply CONFIRM and I'll {x}, or tell me what to change."

    if t == "patient_content":
        item = cat.digest_item(action.get("digest_id"))
        if item:
            source, title, summary, _ = digest_parts(item)
            lib = content_for(cat, digest_text(item))
            draft = lib["body"] if lib else (
                f"Quick update from {m.name}: new research ({source}) — {strip_terminal(title)}. "
                f"If you'd like to know whether this applies to you, reply here and we'll set up a quick check.")
            lines += [f"Here's the reference: {source} — \"{strip_terminal(title)}\".",
                      f"{upper_first(cat.person)} WhatsApp draft (edit freely): {_quote(draft)}"]
        else:
            lines.append(f"{upper_first(cat.person)} WhatsApp draft (edit freely): "
                         f"{_quote(f'Quick update from {m.name} — reply here and we will set up your next visit.')}")
        who = f"your {fmt_int(s.cohort[1])} {s.cohort[2]}" if s.cohort else f"your {cat.people}"
        return lines, confirm.format(x=f"queue it for {who}"), "binary_confirm_cancel"

    if t == "gbp_post":
        offer = _offer(ci, action)
        topic = str(action.get("topic") or "")
        if action.get("headline"):
            lines.append(f"Google post draft: {_quote(_post(ci, '', action.get('offer'), headline=action['headline']))}")
            return lines, confirm.format(x="publish it"), "binary_confirm_cancel"
        if action.get("count", 1) >= 2:
            lines += [f"• Post 1: {_quote(_post(ci, topic, None))}",
                      f"• Post 2: {_quote(_post(ci, '', offer, with_review=False) if offer else _post(ci, topic, None, with_review=False))}"]
        else:
            lines.append(f"Google post draft: {_quote(_post(ci, topic, offer))}")
        return lines, confirm.format(x="publish it"), "binary_confirm_cancel"

    if t == "offer_setup":
        offer = action.get("offer") or "your offer"
        where = f"{m.name}, {m.locality}" if m.locality else m.name
        lines += [f"Here's how '{offer}' will show on your Google profile:",
                  f"• Offer: {offer}",
                  "• Runs until you pause it (one message to me stops it)",
                  f"• Line: {_quote(f'{offer} at {where} — walk in or call to book.')}"]
        return lines, confirm.format(x="make it live"), "binary_confirm_cancel"

    if t == "review_replies":
        theme = theme_label(str(action.get("theme") or "")) if action.get("theme") else "this"
        sign = m.owner_first or m.name
        reply = (f"Thank you for telling us — {theme} isn't the experience we want for you. We're fixing it this week "
                 f"and would love the chance to make it right on your next visit. — {sign}")
        lines.append(f"Public reply draft: {_quote(reply)}")
        return lines, confirm.format(x="post it under those reviews"), "binary_confirm_cancel"

    if t in {"winback_message", "customer_broadcast", "publish_plan", "retention_program", "review_request"}:
        offer = _offer(ci, action)
        if t == "winback_message":
            body = f"Hi [Name], it's been a while since we saw you at {m.name}! " + \
                   (f"{offer} is on right now. " if offer else "") + "Reply YES and we'll keep a slot for you."
            count = action.get("count")
            target = f"the {fmt_int(count)} customers" if count else "your lapsed customers"
        elif t == "customer_broadcast":
            items = [re.sub(r"\s*[+-]\d+%", "", str(x)).strip() for x in action.get("items") or [] if "+" in str(x)]
            topic = action.get("topic") or "this season's essentials"
            body = f"Hi [Name], {m.name} here. For {topic}: " + \
                   (f"{join_list(items)} are stocked and ready. " if items else "we've restocked what you'll need. ") + \
                   "Reply to reserve, or order for delivery."
            target = f"your regular {cat.people}"
        elif t == "publish_plan":
            topic = action.get("topic") or "the new program"
            body = f"Hi [Name], {m.name} is launching {topic}! Reply YES and we'll share the details and hold a spot for you."
            target = action.get("audience") or f"your {cat.people}"
            lines.append(f"Publishing the {topic} as a Google post now.")
        elif t == "retention_program":
            program = str(action.get("program") or "a retention challenge").replace("a ", "", 1)
            free = next((o for o in m.active_offers if "free" in o.lower()), None)
            reward = free or "a shout-out on your page"
            body = f"Hi [Name], {m.name} is starting a {program}! Show up consistently for the next few weeks and " \
                   f"you'll get {reward}. Reply IN to join."
            target = f"your {cat.people}"
        else:
            praise = theme_label(str(s.pos_theme["theme"])) if s.pos_theme else "your visit"
            body = f"Thanks for choosing {m.name} today! If you enjoyed {praise}, a quick Google review helps us a lot 🙏"
            target = "today's customers"
        lines.append(f"Message draft (edit freely): {_quote(body)}")
        return lines, confirm.format(x=f"send it to {target}"), "binary_confirm_cancel"

    if t == "recall_outreach":
        mol = action.get("molecule") or "the affected medicine"
        batches = join_list(action.get("batches") or [])
        lines.append(f"Pulling the {mol} list from your repeat-prescription records now.")
        note = (f"Namaste! {m.name} here. A batch of {mol}" + (f" ({batches})" if batches else "") +
                " is under a voluntary recall for low strength — not a safety risk, but please bring your pack in "
                "and we'll replace it.")
        lines.append(f"Customer note draft: {_quote(note)}")
        return lines, confirm.format(x="send it to the matched customers"), "binary_confirm_cancel"

    if t == "checklist":
        item = cat.digest_item(action.get("digest_id"))
        bullets = []
        if item:
            source, title, summary, actionable = digest_parts(item)
            for part in re.split(r";\s*", actionable):
                if part.strip():
                    bullets.append(f"• {upper_first(strip_terminal(part))}")
            date = re.search(r"\b\d{1,2} \w{3} 20\d\d\b", title)
            if date:
                bullets.append(f"• Deadline: {date.group(0)}")
            bullets.append(f"• Source on file: {source}")
        lines.append(f"Here's {action.get('label', 'the checklist')}:")
        lines += bullets or ["• Review the update", "• Document what you changed", "• Keep a copy on file"]
        return lines, confirm.format(x="save it to your records"), "binary_confirm_cancel"

    if t == "registration":
        item = cat.digest_item(action.get("digest_id"))
        title = strip_terminal(clean(item.get("title"))) if item else "the session"
        when = action.get("when")
        lines.append(f"Done — registering you for \"{title}\"" + (f" ({when})." if when else ".") +
                     " You'll get the joining details here before it starts.")
        return lines, "", "none"

    if t == "verification":
        path = action.get("path") or "a postcard or phone call"
        lines += [f"Here's how we'll verify {m.name}:",
                  "• Step 1: I send the verification request to Google",
                  f"• Step 2: Google sends a code by {path}",
                  "• Step 3: you share the code here and I complete it"]
        return lines, confirm.format(x="start step 1 now"), "binary_confirm_cancel"

    if t == "renewal":
        plan, amount, fix = action.get("plan") or "your", action.get("amount"), action.get("fix")
        amt = f" ({'₹' + fmt_int(amount)})" if amount else ""
        lines.append(f"Summary: {plan} plan renewal{amt}" + (f", plus {fix} set up in the same week." if fix else "."))
        return lines, confirm.format(x="send the payment details here"), "binary_confirm_cancel"

    if t == "campaign_plan":
        fest, offer = action.get("festival") or "festive", _offer(ci, action)
        lines += [f"Here's the {fest} plan:",
                  f"• Hero offer: {offer}" if offer else "• Hero offer: your best-selling service",
                  "• Google post goes live first, WhatsApp to regulars a week before",
                  "• I'll remind you when it's time to launch"]
        return lines, confirm.format(x="lock it in"), "binary_confirm_cancel"

    if t == "profile_update":
        text = action.get("text") or action.get("topic") or m.name
        where = f", {m.locality}" if m.locality else ""
        lines.append(f"Updated description line: {_quote(f'{upper_first(text)} — {m.name}{where}.')}")
        return lines, confirm.format(x="publish it"), "binary_confirm_cancel"

    if t == "curious_answer":
        service = extract_service(ci, message) or "that service"
        offer = related_offer(m, service)
        where = f"{m.name}, {m.locality}" if m.locality else m.name
        price = f"is {offer.split('@')[-1].strip()}" if offer and "@" in offer else "is [your price]"
        lines += ["Love it — here's what I made from that:",
                  f"• Google post: {_quote(f'{upper_first(service)} at {where}' + (f' — {offer}' if offer else '') + '. Book today: call or tap for directions.')}",
                  f"• WhatsApp price reply: {_quote(f'Thanks for asking! {upper_first(service)} at {m.name} {price}. Shall we book you a slot?')}"]
        return lines, confirm.format(x="publish the post"), "binary_confirm_cancel"

    if t in {"audit", "walkthrough", "summary"}:
        gaps = audit_gaps(ci)
        lines.append(f"Quick read on {m.name}:")
        lines += [f"• {g}" for g in gaps[:3]] or ["• Nothing urgent — your listing basics are in place."]
        return lines, confirm.format(x="start on the first one"), "binary_confirm_cancel"

    lines.append(f"Here's {action.get('label', 'the draft')} — ready when you are.")
    return lines, confirm.format(x="go ahead"), "binary_confirm_cancel"


def extract_service(ci: ComposeInput, message: str) -> Optional[str]:
    low = (message or "").lower()
    for term in list(ci.category.vocab) + [re.split(r"\s*@|\(|:", o)[0] for o in ci.merchant.active_offers] + \
            [re.split(r"\s*@|\(|:", str(o.get("title", "")))[0] for o in ci.category.offer_catalog]:
        term = clean(term)
        if term and term.lower() in low:
            return term
    words = re.findall(r"[A-Za-z][A-Za-z-]+", message or "")
    words = [w for w in words if w.lower() not in {"the", "mostly", "and", "is", "it", "its", "our", "we", "this", "week",
                                                     "most", "people", "asking", "for", "about", "i", "think", "probably"}]
    return " ".join(words[:3]) if words else None


def audit_gaps(ci: ComposeInput) -> list[str]:
    s, m, cat = ci.signals, ci.merchant, ci.category
    gaps = []
    if s.no_active_offers:
        gaps.append("No live offer — add one service+price hook")
    if s.unverified:
        gaps.append("Google profile unverified — verification lifts visibility")
    if s.ctr_below_peer:
        gaps.append(f"Click-through rate {s.ctr * 100:.1f}% vs {s.peer_ctr * 100:.1f}% for peers — photos and offer need work")
    if s.stale_posts_days:
        gaps.append(f"Last Google post was {s.stale_posts_days} days ago — post weekly")
    if s.neg_theme:
        gaps.append(f"Reviews flag {theme_label(str(s.neg_theme['theme']))} — reply publicly")
    if s.lapsed_count:
        gaps.append(f"{fmt_int(s.lapsed_count)} lapsed {cat.people} — a win-back message is the cheapest demand")
    return gaps


def executed(ci: ComposeInput, action: dict) -> str:
    t = action.get("type", "summary")
    done = DONE_TEXT.get(t, "done")
    return f"Done ✅ {upper_first(done)}. I'll check how it performs and report back here next week."


def next_step(action: dict, ci: Optional[ComposeInput] = None) -> Optional[tuple[str, str]]:
    step = NEXT_STEP.get(action.get("type", ""))
    if not step:
        return None
    ntype, text = step
    if "{offer}" in text:
        offer = action.get("offer") or (best_active_offer(ci.merchant) if ci else None)
        if not offer:
            return None
        text = text.format(offer=offer)
    return ntype, text


def strength(ci: ComposeInput) -> Optional[str]:
    return strength_line(ci)
