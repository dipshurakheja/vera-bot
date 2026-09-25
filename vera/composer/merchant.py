"""Merchant-facing strategies (send_as = "vera"), one per trigger kind.

Each strategy answers, in order:
  1. why now           -> the trigger fact (quoted from the payload / digest)
  2. why this merchant -> one or two of *their* numbers, offers, cohorts or reviews
  3. what to do        -> a single judgement call (sometimes contrarian)
  4. the easiest CTA   -> one low-friction ask, always the last sentence
Payloads can be rich (seed triggers) or placeholders (generated triggers); every
strategy degrades to merchant/category facts instead of inventing trigger data.
"""

from __future__ import annotations

import re
from typing import Optional

from ..context.views import num
from ..decision.signals import metric_label
from ..utils.text import (a_an, clean, ensure_period, fmt_int, humanize, inr, join_list,
                          lower_first, pct, pct_whole, possessive, sentences, strip_terminal, upper_first)
from ..utils.timeutil import days_until, fmt_date, fmt_time, parse_iso, weekday_name
from .base import ComposeInput, Draft, merchant_opener, rationale, yes_cta
from .knowledge import (beat_for_month, beat_sentence, best_active_offer, catalog_pick, current_beat, digest_text, offer_price,
                        pick_digest, pick_trend, premium_active_offer, related_offer, upcoming_beat)

_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_QUOTED = re.compile(r"['\"‘“]([^'\"’”]{3,60})['\"’”]")
_WEEKDAY_RANGE = re.compile(r"\b(mon|tue|wed|thu|fri)\w*\s*-\s*(mon|tue|wed|thu|fri)", re.I)

THEME_LABELS = {
    "wait_time": "wait times",
    "delivery_late": "late deliveries",
    "saturday_wait": "Saturday waiting times",
    "morning_crowd": "morning crowding",
    "weekend_busy": "the weekend rush",
    "doctor_manner": "how the doctor explains things",
    "stylist_skill": "stylist skill",
    "pizza_quality": "pizza quality",
    "thali_quality": "the thali",
    "equipment_quality": "the equipment",
    "instructor_quality": "instructor quality",
    "small_classes": "small class sizes",
    "delivery_speed": "delivery speed",
    "medicine_availability": "medicine availability",
}
_LAPSED_SHORT = {
    "haven't visited in 6+ months": "no visit in 6+ months",
    "haven't been back in 90+ days": "no visit in 90+ days",
    "haven't been back in 60+ days": "no visit in 60+ days",
}


# ------------------------------------------------------------------ helpers

def plural(noun: str) -> str:
    if noun.endswith("y") and not noun.endswith(("ay", "ey", "oy")):
        return noun[:-1] + "ies"
    return noun if noun.endswith("s") else noun + "s"


def peers(ci: ComposeInput) -> str:
    return f"peer {plural(ci.category.venue)} on magicpin"


def human_dates(text: str) -> str:
    """Rewrite ISO dates inside quoted source text: '2026-12-15' -> '15 Dec 2026'."""
    return _ISO_DATE.sub(lambda m: fmt_date(m.group(0), year=True), text or "")


def theme_label(theme: str) -> str:
    return THEME_LABELS.get(theme, humanize(theme))


def window_phrase(window: str) -> str:
    w = str(window or "").lower()
    if w in {"", "7d", "7_day", "week", "wow", "w/w"}:
        return "this week"
    if w in {"30d", "month", "mom"}:
        return "over the last 30 days"
    if w in {"1d", "day", "yesterday"}:
        return "vs your daily average"
    return f"over {humanize(w)}"


def metric_change(metric: str, delta: float, window: str = "7d") -> str:
    label = metric_label(metric)
    verb = "is" if label.endswith("rate") else "are"
    direction = "down" if delta < 0 else "up"
    return f"{upper_first(label)} {verb} {direction} {pct_whole(delta)} {window_phrase(window)}"


def avg_phrase(value: float) -> str:
    """'an 18 average' / 'a 12 average'."""
    n = fmt_int(value)
    return f"{a_an(n)} {n} average"


def quoted_text(*texts: str) -> Optional[str]:
    for t in texts:
        m = _QUOTED.search(t or "")
        if m:
            return clean(m.group(1))
    return None


def quoted_offer(*texts: str) -> Optional[str]:
    for t in texts:
        for m in _QUOTED.finditer(t or ""):
            if "₹" in m.group(1) or "@" in m.group(1):
                return clean(m.group(1))
    return None


def cta_verb(ci: ComposeInput, salt: str = "") -> str:
    return ci.pick(["Want me to", "Shall I", "Want me to"], "verb" + salt)


def offer_for_action(ci: ComposeInput, prefer_text: str = "") -> tuple[Optional[str], bool]:
    """(offer title, is_live). Live merchant offer first, else a catalog suggestion."""
    live = best_active_offer(ci.merchant, prefer_text)
    if live:
        return live, True
    cat = catalog_pick(ci.category, ci.merchant, prefer_text=prefer_text)
    return (str(cat["title"]), False) if cat else (None, False)


def offer_ref(offer: Optional[str], live: bool) -> str:
    if not offer:
        return "one clear offer"
    return f"your {offer}" if live else f"a '{offer}' offer"


def strength_line(ci: ComposeInput) -> Optional[str]:
    s, m = ci.signals, ci.merchant
    if s.pos_theme and s.pos_theme.get("occurrences_30d"):
        quote = clean(s.pos_theme.get("common_quote"))
        base = f"{s.pos_theme['occurrences_30d']} of your Google reviews this month praise {theme_label(str(s.pos_theme['theme']))}"
        return f"{base} (\"{quote}\")" if quote else base
    if s.ctr_above_peer:
        return f"your click-through rate is {pct(s.ctr)} vs {pct(s.peer_ctr)} for {peers(ci)}"
    if m.established_year:
        where = f" in {m.locality}" if m.locality else ""
        return f"you've been{where} since {m.established_year}"
    return None


def digest_parts(item: dict) -> tuple[str, str, str, str]:
    return (human_dates(clean(item.get("source"))), human_dates(clean(item.get("title"))),
            human_dates(clean(item.get("summary"))), human_dates(clean(item.get("actionable"))))


def action_from_digest(ci: ComposeInput, item: dict) -> tuple[str, dict]:
    """Turn a digest item's own 'actionable' line into one concrete, low-effort ask."""
    m, cat = ci.merchant, ci.category
    kind = str(item.get("kind", "")).lower()
    source, title, summary, actionable = digest_parts(item)
    act = actionable.lower()
    offer = quoted_offer(actionable, summary)
    tag = quoted_text(title, summary)
    if kind == "research":
        return (f"pull the abstract and draft a {cat.person}-friendly WhatsApp explainer you can forward",
                {"type": "patient_content", "digest_id": item.get("id"), "label": f"the abstract + {cat.person} WhatsApp draft"})
    if kind in {"compliance", "alert", "supply"}:
        return (f"draft a one-page action checklist for {m.name}",
                {"type": "checklist", "digest_id": item.get("id"), "label": "the checklist"})
    if kind == "cde":
        return ("save you a seat", {"type": "registration", "digest_id": item.get("id"), "label": "your registration"})
    if offer and any(w in act for w in ("package", "offer", "run", "add")):
        return (f"set up '{offer}' as an offer on your profile",
                {"type": "offer_setup", "offer": offer, "label": f"the '{offer}' listing"})
    if "description" in act and tag and "tag" in act:
        return (f"add '{tag}' to your Google profile description today",
                {"type": "profile_update", "text": tag, "label": "the description update"})
    if "description" in act:
        return ("draft the new line for your Google profile description",
                {"type": "profile_update", "topic": title, "label": "the description line"})
    if any(w in act for w in ("shelf", "counter", "restock")):
        return (f"draft a WhatsApp to your regular {cat.people} about the seasonal essentials",
                {"type": "customer_broadcast", "topic": title, "label": "the WhatsApp draft"})
    if any(w in act for w in ("class", "schedule", "slot")):
        return (f"draft the announcement for the new slot to send your {cat.people}",
                {"type": "customer_broadcast", "topic": title, "label": "the announcement"})
    if any(w in act for w in ("apply", "enable", "verify", "register")):
        step = strip_terminal(actionable.split(";")[0])
        return (f"walk you through it step by step ({lower_first(step)})",
                {"type": "walkthrough", "topic": title, "label": "the step-by-step"})
    if any(w in act for w in ("menu", "dessert", "dish")):
        return ("draft the menu line and a Google post for it",
                {"type": "gbp_post", "topic": title, "label": "the menu line + post"})
    rel = related_offer(m, title + " " + summary)
    hook = f"your {rel}" if rel else m.name
    return (f"draft a Google post that puts {hook} in front of that demand",
            {"type": "gbp_post", "topic": title, "offer": rel, "label": "the Google post draft"})


# ------------------------------------------------------------------ research / knowledge

def research_digest(ci: ComposeInput) -> Draft:
    t, cat, m, s = ci.trigger, ci.category, ci.merchant, ci.signals
    item = cat.digest_item(t.p("top_item_id") or t.p("digest_item_id") or t.p("item_id"))
    if item is None and isinstance(t.p("top_item"), dict):
        item = t.p("top_item")
    if item is None:
        item = pick_digest(cat, m, s, prefer_kinds=("research", "trend"), exclude_ids=ci.used_digest_ids)
    if item is None:
        return trend_movement(ci)

    kind = str(item.get("kind", "research")).lower()
    source, title, summary, actionable = digest_parts(item)
    d = Draft(merchant_opener(ci, "name" if cat.slug in {"dentists", "pharmacies"} else "hi"), [], "",
              "binary_yes_no", f"vera_{kind}_digest_v1", "")
    d.meta["digest_id"] = item.get("id")

    anchor, cohort_hit = None, False
    seg = str(item.get("patient_segment", "")).lower().replace("_", " ")
    if s.cohort:
        _key, count, label = s.cohort
        first_word = label.lower().split()[0]
        if (seg and seg.split(" ")[0] in label.lower()) or first_word in digest_text(item):
            anchor, cohort_hit = f"your {fmt_int(count)} {label}", True

    body_sents = sentences(summary)
    if kind == "research":
        lead = f"{source} has one relevant to {anchor or m.name}:" if source else \
            f"This week's research digest has one relevant to {anchor or m.name}:"
        first = body_sents[0] if body_sents else title
        trial_n = item.get("trial_n")
        if trial_n and fmt_int(trial_n) not in first and str(trial_n) not in first:
            if re.search(r"\btrial\b", first, flags=re.I):
                first = re.sub(r"\btrial\b", f"trial ({fmt_int(trial_n)} patients)", first, count=1, flags=re.I)
            else:
                first = f"{strip_terminal(first)} ({fmt_int(trial_n)}-patient study)."
        if first and first[0].isupper() and not first.split(" ")[0].isupper():
            first = "a " + lower_first(first) if re.match(r"(multi|single|randomi[sz]ed|large|new)\b", first, re.I) else first
        d.lines.append(f"{lead} {ensure_period(first)}")
        if len(body_sents) > 1 and len(body_sents[1]) <= 90:
            d.lines.append(ensure_period(body_sents[1]))
        if actionable:
            d.lines.append(f"Practical takeaway: {lower_first(ensure_period(actionable))}")
    else:
        where = f"relevant to {anchor}" if anchor else f"worth a look for {m.name}"
        d.lines.append(f"one from this week's digest, {where}: \"{strip_terminal(title)}\" ({source}).")
        extra = next((x for x in body_sents if len(x) <= 110 and not x.lower().startswith(title.lower()[:20])), "")
        if extra:
            d.lines.append(ensure_period(extra))
        if actionable:
            d.lines.append(f"Practical takeaway: {lower_first(ensure_period(actionable))}")
    q, action = action_from_digest(ci, item)
    d.cta = yes_cta(ci, f"{cta_verb(ci)} {q}?")
    d.next_action = action
    d.facts = [f"digest:{item.get('id')}", f"source:{source}", f"anchor:{anchor or m.name}"]
    d.levers = ["source citation", "specific numbers", "reciprocity", "single YES CTA"]
    d.rationale = rationale(
        t.kind, f"surfaced '{title}' ({source}) — {'matches ' + anchor if cohort_hit else 'best-scoring digest item for this merchant'}; "
                f"the ask turns the item's own takeaway into one step", d.levers)
    return d


def trend_movement(ci: ComposeInput) -> Draft:
    t, cat, m = ci.trigger, ci.category, ci.merchant
    query = t.ptext("query")
    trend = {"query": query, "delta_yoy": t.pnum("delta_yoy"), "segment_age": t.ptext("segment_age")} if query \
        else pick_trend(cat, m)
    if not trend or num(trend.get("delta_yoy")) is None:
        return generic(ci)
    d = Draft(merchant_opener(ci, "hi"), [], "", "binary_yes_no", "vera_trend_v1", "")
    q_text, dlt = clean(trend.get("query")), float(trend.get("delta_yoy"))
    d.lines.append(f"searches for \"{q_text}\" are {'up' if dlt >= 0 else 'down'} {pct_whole(dlt)} year-on-year.")
    if trend.get("segment_age"):
        d.lines.append(f"Most of that demand is in the {humanize(trend['segment_age'])} age group.")
    offer, live = offer_for_action(ci, q_text)
    if live:
        q = f"{cta_verb(ci)} draft a Google post that puts your {offer} in front of those searches?"
    elif offer:
        q = f"{cta_verb(ci)} set up '{offer}' on your profile to catch that demand?"
    else:
        q = f"{cta_verb(ci)} draft a Google post for {m.name} around it?"
    d.cta = yes_cta(ci, q)
    d.next_action = {"type": "gbp_post", "topic": q_text, "offer": offer, "label": "the Google post draft"}
    d.levers = ["specific trend number", "curiosity", "effort externalization"]
    d.rationale = rationale(t.kind, f"'{q_text}' demand moved {pct_whole(dlt)} YoY; tied it to {offer or m.name}", d.levers)
    return d


def regulation_change(ci: ComposeInput) -> Draft:
    t, cat, m = ci.trigger, ci.category, ci.merchant
    item = cat.digest_item(t.p("top_item_id") or t.p("digest_item_id"))
    if item is None:
        item = pick_digest(cat, m, ci.signals, prefer_kinds=("compliance",), exclude_ids=ci.used_digest_ids)
    d = Draft(merchant_opener(ci), [], "", "binary_yes_no", "vera_compliance_alert_v1", "")
    deadline = t.ptext("deadline_iso") or clean((item or {}).get("date"))
    if not deadline and item:
        mdate = _ISO_DATE.search(str(item.get("title", "")))
        deadline = mdate.group(0) if mdate else None
    if item is None:
        d.lines.append(f"compliance heads-up: a {humanize(t.kind)} update for {plural(cat.singular)} just landed"
                       + (f", effective {fmt_date(deadline, year=True)}." if deadline else "."))
        d.cta = yes_cta(ci, f"{cta_verb(ci)} send you the 3-line summary and what it means for {m.name}?")
        d.next_action = {"type": "summary", "label": "the summary"}
        d.rationale = rationale(t.kind, "regulatory trigger without digest detail; offered a summary, no claims made",
                                ["urgency", "reciprocity"])
        return d
    source, title, summary, actionable = digest_parts(item)
    d.meta["digest_id"] = item.get("id")
    d.lines.append(f"compliance heads-up from {source}: {strip_terminal(title)}." if source else
                   f"compliance heads-up: {strip_terminal(title)}.")
    for sent in sentences(summary)[:2]:
        d.lines.append(ensure_period(sent))
    n = days_until(deadline, ci.now) if deadline else None
    if n is not None and 0 < n <= 400:
        d.lines.append(f"That's {n} days from today.")
        d.allow(n)
    if actionable:
        d.lines.append(f"To do: {lower_first(ensure_period(actionable))}")
    doc = "a one-page SOP update for your records" if "sop" in actionable.lower() else "a one-page compliance checklist"
    by = f" before {fmt_date(deadline)}" if deadline else ""
    d.cta = yes_cta(ci, f"{cta_verb(ci)} draft {doc} so {m.name} is covered{by}?")
    d.next_action = {"type": "checklist", "digest_id": item.get("id"), "label": doc.replace("a one-page", "the one-page")}
    d.levers = ["urgency with deadline", "source citation", "effort externalization"]
    d.rationale = rationale(t.kind, f"{source}: {strip_terminal(title)} — hard deadline, so led with the rule and "
                                    f"offered the paperwork", d.levers)
    return d


def cde_opportunity(ci: ComposeInput) -> Draft:
    t, cat, m = ci.trigger, ci.category, ci.merchant
    item = cat.digest_item(t.p("digest_item_id") or t.p("top_item_id"))
    if item is None:
        item = pick_digest(cat, m, ci.signals, prefer_kinds=("cde",), exclude_ids=ci.used_digest_ids)
    if item is None or str(item.get("kind", "")).lower() not in {"cde", "event", "webinar"}:
        return research_digest(ci)
    source, title, summary, actionable = digest_parts(item)
    d = Draft(merchant_opener(ci), [], "", "binary_yes_no", "vera_cde_invite_v1", "")
    when = item.get("date")
    when_txt = ""
    if when:
        when_txt = fmt_date(when, weekday=True)
        if parse_iso(when) and len(str(when)) > 10:
            when_txt += f", {fmt_time(when)}"
    credits = t.pint("credits") or (int(num(item.get("credits"))) if num(item.get("credits")) else None)
    fee_txt = strip_terminal(actionable) if ("₹" in actionable or "free" in actionable.lower()) else humanize(t.ptext("fee"))
    bits = [b for b in (when_txt, f"{credits} CDE credits" if credits else "", fee_txt) if b]
    d.lines.append(f"{source} has a session worth blocking: \"{strip_terminal(title)}\"" + (f" — {'; '.join(bits)}." if bits else "."))
    if summary:
        d.lines.append(ensure_period(summary))
    d.cta = yes_cta(ci, f"{cta_verb(ci)} save you a seat?")
    d.next_action = {"type": "registration", "digest_id": item.get("id"), "label": "your registration", "when": when_txt}
    d.levers = ["specific date/credits", "professional growth", "single YES CTA"]
    d.rationale = rationale(t.kind, f"CDE event '{strip_terminal(title)}' on {when_txt or 'upcoming date'}; one-tap registration",
                            d.levers)
    return d


# ------------------------------------------------------------------ performance

def _dip_context(ci: ComposeInput, metric: str) -> Optional[str]:
    """30-day comparison for a dip: only below-peer numbers count as weakness; above-peer is framed as a cushion."""
    s = ci.signals
    focus = "calls" if metric.startswith("call") else "views" if metric.startswith("view") else ""
    below, above = [], []
    if focus in {"calls", ""} and s.calls is not None and s.peer_calls:
        (below if s.calls_below_peer else above).append(
            (f"{fmt_int(s.calls)} calls", f"{avg_phrase(s.peer_calls)} for {peers(ci)}"))
    if focus == "views" and s.views is not None and s.peer_views:
        (below if s.views_below_peer else above).append(
            (f"{fmt_int(s.views)} profile views", f"{avg_phrase(s.peer_views)} for {peers(ci)}"))
    if s.ctr_below_peer:
        below.append((f"{a_an(pct(s.ctr))} {pct(s.ctr)} click-through rate", f"{pct(s.peer_ctr)}"))
    if below:
        return "Over the last 30 days that's " + join_list(f"{a} vs {b}" for a, b in below) + "."
    if above and focus:
        a, b = above[0]
        return f"You're still ahead over 30 days ({a} vs {b}), so this is a recent slide worth catching early."
    return None


def perf_dip(ci: ComposeInput) -> Draft:
    t, m, s = ci.trigger, ci.merchant, ci.signals
    metric = (t.ptext("metric") or (s.drop[0] if s.drop else "")).replace("_pct", "")
    delta = t.pnum("delta_pct")
    if delta is None and s.drop and s.drop[0].replace("_pct", "") == metric:
        delta = s.drop[1]
    d = Draft(merchant_opener(ci), [], "", "binary_yes_no", "vera_perf_dip_v1", "")
    if metric and isinstance(delta, (int, float)) and delta < 0:
        base = t.pnum("vs_baseline")
        line = metric_change(metric, float(delta), t.ptext("window", "7d"))
        line += f" (your usual baseline is {fmt_int(base)})." if base else "."
        d.lines.append(line)
        ctx = _dip_context(ci, metric)
    else:
        ctx = _dip_context(ci, "")
        d.lines.append("your listing numbers have dipped this week.")
    if ctx:
        d.lines.append(ctx)

    offer, live = offer_for_action(ci)
    if s.no_active_offers and offer:
        d.lines.append(f"The clearest gap: {m.name} has no live offer, so searchers see nothing concrete to act on.")
        q = f"{cta_verb(ci)} put '{offer}' live on your profile today?"
        d.next_action = {"type": "offer_setup", "offer": offer, "label": f"the '{offer}' listing"}
        why = "no active offer while demand falls"
    elif s.lapsed_count:
        d.lines.append(f"Fastest recovery lever: {fmt_int(s.lapsed_count)} {ci.category.people} {s.lapsed_label}.")
        around = f" around your {offer}" if live else ""
        q = f"{cta_verb(ci)} draft a win-back WhatsApp to those {fmt_int(s.lapsed_count)}{around}?"
        d.next_action = {"type": "winback_message", "count": s.lapsed_count, "offer": offer if live else None,
                         "label": "the win-back message draft"}
        why = f"{s.lapsed_count} lapsed {ci.category.people} are the fastest recoverable demand"
    elif s.stale_posts_days:
        d.lines.append(f"Your last Google post went up {s.stale_posts_days} days ago.")
        q = f"{cta_verb(ci)} draft a fresh post" + (f" around your {offer}?" if live else "?")
        d.next_action = {"type": "gbp_post", "offer": offer if live else None, "label": "the post draft"}
        why = "stale posts while discovery drops"
    elif s.neg_theme:
        th = s.neg_theme
        d.lines.append(f"Reviews may be part of it: {th.get('occurrences_30d', 'several')} this month mention "
                       f"{theme_label(str(th['theme']))}.")
        q = f"{cta_verb(ci)} draft calm public replies to those reviews?"
        d.next_action = {"type": "review_replies", "theme": th.get("theme"), "label": "the review replies"}
        why = "negative review theme likely depressing conversion"
    elif live:
        q = f"{cta_verb(ci)} push your {offer} in a Google post this week?"
        d.next_action = {"type": "gbp_post", "offer": offer, "label": "the post draft"}
        why = "re-surface the live offer to recover discovery"
    else:
        q = f"{cta_verb(ci)} run a quick listing audit and send you the single biggest fix?"
        d.next_action = {"type": "audit", "label": "the audit"}
        why = "no stronger lever in the data"
    d.cta = yes_cta(ci, q)
    d.levers = ["loss aversion", "peer benchmark", "one concrete fix", "single YES CTA"]
    d.rationale = rationale(t.kind, f"{metric or 'performance'} dip; chose one fix — {why}", d.levers)
    return d


def perf_spike(ci: ComposeInput) -> Draft:
    t, m, s = ci.trigger, ci.merchant, ci.signals
    metric = (t.ptext("metric") or (s.rise[0] if s.rise else "")).replace("_pct", "")
    delta = t.pnum("delta_pct")
    if delta is None and s.rise and s.rise[0].replace("_pct", "") == metric:
        delta = s.rise[1]
    driver = humanize(t.ptext("likely_driver"))
    d = Draft(merchant_opener(ci, "hi"), [], "", "binary_yes_no", "vera_perf_spike_v1", "")
    if metric and isinstance(delta, (int, float)) and delta > 0:
        base = t.pnum("vs_baseline")
        line = metric_change(metric, float(delta), t.ptext("window", "7d"))
        if base:
            line += f" (baseline {fmt_int(base)})"
        line += f", and the likely driver is your {driver}." if driver else "."
        d.lines.append(line)
    else:
        d.lines.append(f"good week for {m.name}: your listing is trending up.")
    if s.ctr_above_peer:
        d.lines.append(f"Your click-through rate is already {pct(s.ctr)} vs {pct(s.peer_ctr)} for {peers(ci)}, "
                       f"so people who find you act — the job now is more reach.")
    elif s.calls_above_peer:
        d.lines.append(f"You're at {fmt_int(s.calls)} calls in 30 days vs {avg_phrase(s.peer_calls)} for {peers(ci)}.")
    elif s.views is not None and s.peer_views:
        d.lines.append(f"At {fmt_int(s.views)} profile views over 30 days (peer average {fmt_int(s.peer_views)}), "
                       f"this is the moment to give visitors a reason to call.")
    offer, live = offer_for_action(ci, driver)
    if driver:
        topic = re.sub(r"\s*(post|campaign|ad)$", "", driver)
        q = f"{cta_verb(ci)} draft 2 follow-up posts on {topic} to keep the momentum going?"
        d.next_action = {"type": "gbp_post", "topic": topic, "offer": offer if live else None, "count": 2,
                         "label": "the 2 follow-up posts"}
        why = f"capitalise on what's working ({driver})"
    elif live:
        q = f"{cta_verb(ci)} pin your {offer} to the top of your profile while traffic is up?"
        d.next_action = {"type": "gbp_post", "offer": offer, "label": "the pinned post"}
        why = "convert extra traffic with the live offer"
    elif offer:
        q = f"{cta_verb(ci)} put '{offer}' live so the extra traffic has something to act on?"
        d.next_action = {"type": "offer_setup", "offer": offer, "label": f"the '{offer}' listing"}
        why = "traffic up but no offer to convert it"
    else:
        q = f"{cta_verb(ci)} draft a post to ride the momentum?"
        d.next_action = {"type": "gbp_post", "label": "the post draft"}
        why = "ride the momentum"
    d.cta = yes_cta(ci, q)
    d.levers = ["positive reinforcement", "specific delta", "momentum", "single YES CTA"]
    d.rationale = rationale(t.kind, f"{metric or 'traffic'} spike; {why}", d.levers)
    return d


def seasonal_perf_dip(ci: ComposeInput) -> Draft:
    t, cat, m, s = ci.trigger, ci.category, ci.merchant, ci.signals
    metric = (t.ptext("metric") or (s.drop[0] if s.drop else "views")).replace("_pct", "")
    delta = t.pnum("delta_pct")
    if delta is None and s.drop:
        delta = s.drop[1]
    d = Draft(merchant_opener(ci), [], "", "binary_yes_no", "vera_seasonal_dip_v1", "")
    beat = current_beat(cat, ci.now, ("lowest", "lull", "slowdown", "retention")) or \
        upcoming_beat(cat, ci.now, ("lowest", "lull", "slowdown", "retention"))
    item = pick_digest(cat, m, s, prefer_kinds=("seasonal",), exclude_ids=ci.used_digest_ids)
    if isinstance(delta, (int, float)) and delta < 0:
        d.lines.append(f"{metric_change(metric, float(delta), t.ptext('window', '7d'))} — but this is the expected "
                       f"seasonal lull, not a {m.name} problem.")
    else:
        d.lines.append(f"numbers are softer this week, and that's seasonal, not a {m.name} problem.")
    if beat:
        d.lines.append(beat_sentence(beat, cat))
    if item and str(item.get("kind")) == "seasonal" and item.get("actionable"):
        d.lines.append(f"The smart money move ({clean(item.get('source'))}): {lower_first(ensure_period(human_dates(item['actionable'])))}")
    people = cat.people
    if s.churn and s.churn[1] is not None:
        members = f" across {fmt_int(s.total_people)} {people}" if s.total_people else ""
        d.lines.append(f"Where to focus instead: your monthly churn is {pct(s.churn[0])} vs {pct(s.churn[1])} "
                       f"for {peers(ci)}{members}.")
    elif s.total_people:
        d.lines.append(f"Where to focus instead: keeping your {fmt_int(s.total_people)} {people} engaged.")
    program = {"gyms": "a 4-week summer attendance challenge", "salons": "a rebooking reminder",
               "restaurants": "a regulars-only weekday offer", "dentists": "a recall reminder",
               "pharmacies": "a refill-reminder push"}.get(cat.slug, "a retention message")
    d.cta = yes_cta(ci, f"{cta_verb(ci)} draft {program} for your {people}?")
    d.next_action = {"type": "retention_program", "program": program, "label": program.replace("a ", "the ", 1)}
    d.allow(4)
    d.levers = ["anxiety pre-emption", "contrarian reframe", "peer benchmark", "single YES CTA"]
    d.rationale = rationale(t.kind, "expected seasonal dip — reassured, moved effort from acquisition to retention", d.levers)
    return d


_THRESHOLDS = [100, 250, 500, 1000, 1500, 2000, 2500, 5000, 10000, 25000, 50000]


def milestone_reached(ci: ComposeInput) -> Draft:
    t, cat, m, s = ci.trigger, ci.category, ci.merchant, ci.signals
    metric = t.ptext("metric")
    now_v, target = t.pnum("value_now"), t.pnum("milestone_value")
    imminent = bool(t.p("is_imminent"))
    d = Draft(merchant_opener(ci, "hi"), [], "", "binary_yes_no", "vera_milestone_v1", "")
    label = metric_label(metric) if metric else ""
    review_ask = False
    if isinstance(now_v, (int, float)) and isinstance(target, (int, float)):
        if imminent and target > now_v:
            gap = int(target - now_v)
            d.allow(gap)
            d.lines.append(f"{m.name} is {gap} {label} away from {fmt_int(target)} (at {fmt_int(now_v)} today).")
        else:
            d.lines.append(f"{m.name} just crossed {fmt_int(target)} {label} — nice.")
        if "review" in metric and s.peer_reviews and now_v >= s.peer_reviews:
            d.lines.append(f"That's already above the {fmt_int(s.peer_reviews)} average for {peers(ci)}.")
        review_ask = "review" in metric
    else:
        total = m.agg("total_unique_ytd")
        crossed = max([x for x in _THRESHOLDS if total and total >= x], default=None)
        if crossed:
            d.lines.append(f"milestone: your customer records show {fmt_int(total)} {cat.people} served at {m.name} "
                           f"so far this year — past the {fmt_int(crossed)} mark.")
            d.allow(crossed)
        elif s.views_above_peer:
            d.lines.append(f"milestone month: {fmt_int(s.views)} profile views in 30 days, well above the "
                           f"{fmt_int(s.peer_views)} average for {peers(ci)}.")
        else:
            d.lines.append(f"{m.name} hit a new listing milestone this month.")
        if s.ctr_above_peer:
            d.lines.append(f"And people who find you act on it: {pct(s.ctr)} click-through vs {pct(s.peer_ctr)} for {peers(ci)}.")
        review_ask = True
    if s.pos_theme:
        d.lines.append(f"Your regulars are vocal: {s.pos_theme.get('occurrences_30d')} of your Google reviews this month praise "
                       f"{theme_label(str(s.pos_theme['theme']))}.")
    if review_ask:
        q = f"{cta_verb(ci)} draft a one-line thank-you + review request you can send to today's regulars?"
        d.next_action = {"type": "review_request", "label": "the review request"}
    else:
        q = f"{cta_verb(ci)} turn it into a thank-you post for your {cat.people}?"
        d.next_action = {"type": "gbp_post", "headline": f"Thank you for making {m.name} a local favourite!",
                         "label": "the thank-you post"}
    d.cta = yes_cta(ci, q)
    d.levers = ["goal gradient", "social proof", "effort externalization"]
    d.rationale = rationale(t.kind, "milestone moment — converted pride into a review/visibility action", d.levers)
    return d


# ------------------------------------------------------------------ subscription lifecycle

def renewal_due(ci: ComposeInput) -> Draft:
    t, m, s, cat = ci.trigger, ci.merchant, ci.signals, ci.category
    days = t.pint("days_remaining") if t.pint("days_remaining") is not None else (s.days_remaining or s.renewal_days)
    plan = t.ptext("plan") or clean(m.subscription.get("plan")) or "current"
    amount = t.pnum("renewal_amount")
    is_trial = str(plan).lower() == "trial" or s.sub_status == "trial"
    if s.sub_status == "expired" and not t.pint("days_remaining"):
        return winback_eligible(ci)
    d = Draft(merchant_opener(ci), [], "", "binary_yes_no", "vera_renewal_v1", "")
    fix_offer, fix_live = offer_for_action(ci)
    fix = None
    if s.no_active_offers and fix_offer:
        fix = f"a live '{fix_offer}' offer"
    elif s.unverified:
        fix = "Google verification"
    elif s.stale_posts_days:
        fix = "fresh weekly posts"
    elif s.lapsed_count:
        fix = f"a win-back push to your {fmt_int(s.lapsed_count)} lapsed {cat.people}"
    stats = []
    if s.views is not None:
        stats = [f"{fmt_int(s.views)} profile views", f"{fmt_int(s.calls or 0)} calls"]
        if s.directions:
            stats.append(f"{fmt_int(s.directions)} direction requests")
    far_out = isinstance(days, (int, float)) and days > 45 and not is_trial
    if far_out:
        d.lines.append(f"no renewal action needed — your {plan} plan has {days} days left.")
        if stats:
            d.lines.append(f"But the last 30 days ({join_list(stats)}) say one fix would make them count more: {fix or 'a sharper offer'}.")
        q = f"{cta_verb(ci)} set that up this week?"
        d.next_action = {"type": "offer_setup" if fix_offer and s.no_active_offers else "audit",
                         "offer": fix_offer, "label": fix or "the fix"}
        why = f"renewal is {days} days away — not worth a reminder; used the moment to improve value instead"
    else:
        if is_trial:
            d.lines.append(f"your trial ends in {days} days." if days is not None else "your trial is ending soon.")
        else:
            head = f"your {plan} plan renews in {days} days" if days is not None else f"your {plan} plan is up for renewal"
            d.lines.append(head + (f" ({inr(amount)})." if amount else "."))
        if stats:
            d.lines.append(f"Last 30 days on your listing: {join_list(stats)}.")
        weak = s.drop is not None or s.calls_below_peer or s.ctr_below_peer
        noun = "upgrade" if is_trial else "renewal"
        if weak and fix:
            gap = f"{metric_label(s.drop[0])} down {pct_whole(s.drop[1])} this week" if s.drop else \
                f"calls below the {fmt_int(s.peer_calls)} peer average"
            d.lines.append(f"Honest read: the numbers are soft ({gap}), so I'd pair the {noun} with {fix}.")
            q = f"{cta_verb(ci)} send the {noun} details with that plan attached?"
        elif s.views_above_peer or s.calls_above_peer:
            d.lines.append(f"That's ahead of {peers(ci)} — worth keeping the momentum unbroken.")
            q = f"{cta_verb(ci)} send the {noun} details now?"
        else:
            q = f"{cta_verb(ci)} send the {noun} details so there's no gap in your listing management?"
        if is_trial:
            q = f"{cta_verb(ci)} share the plan options before the trial lapses?"
        d.next_action = {"type": "renewal", "amount": amount, "plan": plan, "fix": fix, "label": f"the {noun} details"}
        why = f"{days} days to {noun}; showed their own numbers and one fix instead of a bare reminder"
    d.cta = yes_cta(ci, q)
    d.levers = ["loss aversion (deadline)", "value proof from own numbers", "single YES CTA"]
    d.rationale = rationale(t.kind, why, d.levers)
    return d


def winback_eligible(ci: ComposeInput) -> Draft:
    t, m, s, cat = ci.trigger, ci.merchant, ci.signals, ci.category
    days = t.pint("days_since_expiry") or s.days_since_expiry
    dip = t.pnum("perf_dip_pct")
    added = t.pint("lapsed_customers_added_since_expiry")
    d = Draft(merchant_opener(ci, "hi"), [], "", "binary_yes_no", "vera_winback_merchant_v1", "")
    plan = clean(m.subscription.get("plan")) or "Vera"
    d.lines.append(f"it's been {days} days since {possessive(m.name)} {plan} plan lapsed." if days else
                   f"{possessive(m.name)} {plan} plan has lapsed.")
    bits = []
    if isinstance(dip, (int, float)) and dip < 0:
        bits.append(f"calls are down {pct_whole(dip)}")
    elif s.drop:
        bits.append(f"{metric_label(s.drop[0])} are down {pct_whole(s.drop[1])} this week")
    if added:
        bits.append(f"{fmt_int(added)} more {cat.people} have slipped into your lapsed list")
    if bits:
        tail = ""
        if s.lapsed_count and added:
            tail = f", which now stands at {fmt_int(s.lapsed_count)} ({_LAPSED_SHORT.get(s.lapsed_label, s.lapsed_label)})"
        d.lines.append(f"Since then, {join_list(bits)}{tail}.")
    offer, live = offer_for_action(ci)
    target = f"those {fmt_int(added)}" if added else (
        f"your {fmt_int(s.lapsed_count)} lapsed {cat.people}" if s.lapsed_count else "your lapsed regulars")
    around = f" around {offer_ref(offer, live)}" if offer else ""
    d.cta = yes_cta(ci, f"{cta_verb(ci)} draft a win-back message to {target}{around}? You approve before anything goes out.")
    d.next_action = {"type": "winback_message", "count": added or s.lapsed_count, "offer": offer,
                     "label": "the win-back message draft"}
    d.levers = ["loss aversion", "specific counts", "low-risk ask (approve first)"]
    d.rationale = rationale(t.kind, "lapsed subscriber losing customers — led with the cost of the gap, offered a no-risk restart",
                            d.levers)
    return d


def dormant_with_vera(ci: ComposeInput) -> Draft:
    t, m, s, cat = ci.trigger, ci.merchant, ci.signals, ci.category
    days = t.pint("days_since_last_merchant_message") or s.dormant_days
    last_topic = humanize(t.ptext("last_topic"))
    d = Draft(merchant_opener(ci, "hi"), [], "", "binary_yes_no", "vera_reengage_v1", "")
    opener = f"it's been {days} days since we last spoke" if days else "it's been a while since we last spoke"
    skip = f", so no {last_topic.replace('subscription expiry', 'subscription')} talk today" if last_topic else ""
    d.lines.append(f"{opener}{skip} — just one thing worth knowing.")
    item = pick_digest(cat, m, s, prefer_kinds=("seasonal", "trend"), exclude_ids=ci.used_digest_ids)
    if item:
        source, title, summary, actionable = digest_parts(item)
        d.meta["digest_id"] = item.get("id")
        d.lines.append(f"{upper_first(strip_terminal(title))}" + (f" ({source})." if source else "."))
        if s.drop:
            d.lines.append(f"Your dashboard shows {metric_label(s.drop[0])} down {pct_whole(s.drop[1])} this week, so it's a timely way back in.")
        q, action = action_from_digest(ci, item)
        d.cta = yes_cta(ci, f"{cta_verb(ci)} {q}?")
        d.next_action = action
    else:
        tr = pick_trend(cat, m)
        if tr:
            d.lines.append(f"Searches for \"{tr.get('query')}\" are up {pct_whole(tr.get('delta_yoy', 0))} year-on-year.")
        offer, live = offer_for_action(ci)
        d.cta = yes_cta(ci, f"{cta_verb(ci)} draft a post around {offer_ref(offer, live)} to catch it?")
        d.next_action = {"type": "gbp_post", "offer": offer, "label": "the post draft"}
    d.levers = ["reciprocity (value before ask)", "curiosity", "timeliness", "single YES CTA"]
    d.rationale = rationale(t.kind, f"merchant quiet for {days or 'many'} days — re-opened with category value, "
                                    f"not a repeat of the last topic ({last_topic or 'n/a'})", d.levers)
    return d


def gbp_unverified(ci: ComposeInput) -> Draft:
    t, m, s = ci.trigger, ci.merchant, ci.signals
    path = humanize(t.ptext("verification_path"))
    uplift = t.pnum("estimated_uplift_pct")
    d = Draft(merchant_opener(ci), [], "", "binary_yes_no", "vera_gbp_verify_v1", "")
    line = f"{possessive(m.name)} Google profile is still unverified"
    line += f" — verifying it is estimated to lift your visibility by about {pct_whole(uplift)}." if uplift else "."
    d.lines.append(line)
    if s.views is not None and s.peer_views and s.views_below_peer:
        d.lines.append(f"Right now you're at {fmt_int(s.views)} profile views a month vs {fmt_int(s.peer_views)} for {peers(ci)}.")
    if path:
        ways = path.replace(" or ", " or a ")
        d.lines.append(f"Google verifies by a {ways}; I can walk you through it.")
    d.cta = yes_cta(ci, f"{cta_verb(ci)} start the verification with you now, step by step?")
    d.next_action = {"type": "verification", "path": path, "label": "the verification steps"}
    d.levers = ["loss aversion (visibility gap)", "effort externalization", "single YES CTA"]
    d.rationale = rationale(t.kind, "unverified listing capping discovery; offered a guided fix", d.levers)
    return d


# ------------------------------------------------------------------ market / external

def competitor_opened(ci: ComposeInput) -> Draft:
    t, m, s, cat = ci.trigger, ci.merchant, ci.signals, ci.category
    name = t.ptext("competitor_name")
    dist = t.pnum("distance_km")
    their = t.ptext("their_offer")
    opened = t.ptext("opened_date")
    d = Draft(merchant_opener(ci), [], "", "binary_yes_no", "vera_competitor_watch_v1", "")
    mine = None
    if name:
        where = f"{dist} km from you" if dist else "near you"
        when = f" on {fmt_date(opened)}" if opened else ""
        line = f"heads-up: {name} opened {where}{when}"
        if their:
            mine = related_offer(m, their)
            line += f", leading with {their}"
            tp, mp = offer_price(their), offer_price(mine or "")
            if mine and tp is not None and mp is not None and mp > tp:
                d.allow(mp - tp)
                line += f" — {inr(mp - tp)} under your {mine}"
        d.lines.append(line + ".")
        d.lines.append("I wouldn't race them on price.")
    else:
        where = f" in {m.locality}" if m.locality else " near you"
        d.lines.append(f"heads-up: a new {cat.singular} has opened{where}.")
    strength = strength_line(ci)
    if strength:
        d.lines.append(f"Your edge is already visible: {strength}.")
    if name:
        offer = mine or best_active_offer(m)
        tail = f" plus your {offer}" if offer else ""
        q = f"{cta_verb(ci)} draft a Google post that leads with that{tail}?"
        d.next_action = {"type": "gbp_post", "offer": offer, "label": "the post draft"}
    else:
        q = f"{cta_verb(ci)} compare your listing with theirs (offer, photos, reviews) and send the one gap to fix first?"
        d.next_action = {"type": "audit", "label": "the comparison"}
    d.cta = yes_cta(ci, q)
    d.levers = ["loss aversion", "curiosity", "judgement (don't price-match)", "single YES CTA"]
    d.rationale = rationale(t.kind, f"new competitor {name or '(unnamed in payload)'} — advised differentiation over a price war",
                            d.levers)
    return d


_FESTIVAL_WORDS = ["festival", "diwali", "wedding", "christmas", "new year", "holi", "eid", "puja", "navratri"]


def festival_upcoming(ci: ComposeInput) -> Draft:
    t, m, cat = ci.trigger, ci.merchant, ci.category
    fest = t.ptext("festival")
    date = t.ptext("date")
    days = t.pint("days_until")
    d = Draft(merchant_opener(ci, "hi"), [], "", "binary_yes_no", "vera_festival_v1", "")
    if days is None and date:
        days = days_until(date, ci.now)
        if days is not None:
            d.allow(days)
    words = ([fest.lower()] if fest else []) + _FESTIVAL_WORDS
    beat = None
    if date and parse_iso(date):
        beat = beat_for_month(cat, parse_iso(date).month, words)
    beat = beat or upcoming_beat(cat, ci.now, words)
    live = premium_active_offer(m)
    offer, is_live = (live, True) if live else offer_for_action(ci, fest)
    ref = offer_ref(offer, is_live)
    if fest:
        head = f"{fest} is on {fmt_date(date)}" if date else f"{fest} is coming up"
        if isinstance(days, (int, float)) and days > 45:
            d.lines.append(f"{head} — {int(days)} days out. Too early to promote, but the right time to plan.")
            q = f"{cta_verb(ci)} pencil in a {fest} package around {ref} and remind you 6 weeks before to launch it?"
        elif isinstance(days, (int, float)) and days > 10:
            d.lines.append(f"{head} — {int(days)} days to go, which is launch-prep time.")
            q = f"{cta_verb(ci)} draft the {fest} post around {ref} now?"
        elif isinstance(days, (int, float)):
            d.lines.append(f"{head} — only {int(days)} days left.")
            q = f"{cta_verb(ci)} put a {fest} post live today with {ref}?"
        else:
            d.lines.append(f"{head}.")
            q = f"{cta_verb(ci)} draft the {fest} post around {ref}?"
    else:
        d.lines.append("festive season is on the radar.")
        q = f"{cta_verb(ci)} set up a festive post around {ref}?"
    if beat:
        d.lines.append(beat_sentence(beat, cat))
    d.cta = yes_cta(ci, q)
    d.next_action = {"type": "campaign_plan", "festival": fest or "festive season", "offer": offer,
                     "label": f"the {fest or 'festive'} plan"}
    d.levers = ["timeliness", "seasonal data", "effort externalization", "single YES CTA"]
    when = f"in {int(days)} days" if isinstance(days, (int, float)) else "upcoming"
    d.rationale = rationale(t.kind, f"{fest or 'festive season'} {when}; matched the ask to the lead time and the "
                                    f"category's seasonal pattern", d.levers)
    return d


def ipl_match_today(ci: ComposeInput) -> Draft:
    t, m, s, cat = ci.trigger, ci.merchant, ci.signals, ci.category
    match = t.ptext("match", "tonight's match")
    venue = t.ptext("venue")
    when = t.ptext("match_time_iso")
    weeknight = t.p("is_weeknight")
    d = Draft(merchant_opener(ci, "hi"), [], "", "binary_yes_no", "vera_match_day_v1", "")
    time_txt = f", {fmt_time(when)}" if when else ""
    day_txt = weekday_name(when, long=True) if when else ""
    d.lines.append(f"{match}{' at ' + venue if venue else ''} tonight{time_txt}.")
    item = next((x for x in cat.digest if "ipl" in digest_text(x)), None)
    delivery, dine = m.agg("delivery_orders_30d"), m.agg("dine_in_orders_30d")
    offer = best_active_offer(m)
    if weeknight is False:
        if item:
            sents = sentences(human_dates(str(item.get("summary", ""))))
            d.lines.append(f"Worth knowing ({clean(item.get('source'))}): {' '.join(ensure_period(x) for x in sents[:2])}")
        d.lines.append(f"Tonight is a {day_txt or 'weekend'} match, so treat it as a delivery night, not a dine-in promo night.")
        if delivery and dine and delivery > dine:
            d.lines.append(f"Delivery is already your bigger channel ({fmt_int(delivery)} orders vs {fmt_int(dine)} dine-in in 30 days).")
        if offer and _WEEKDAY_RANGE.search(offer):
            d.lines.append(f"Keep your {offer} for the weeknight matches.")
        q = f"{cta_verb(ci)} draft a delivery-only match-night post for tonight?"
        d.next_action = {"type": "gbp_post", "headline": f"{match} tonight? Watch at home — {m.name} delivers.",
                         "label": "the match-night post"}
        why = "weekend match historically cuts dine-in covers — steered to delivery (contrarian call)"
    else:
        if item:
            d.lines.append(f"Weeknight matches are your window ({clean(item.get('source'))}).")
        hook = f"your {offer}" if offer else "a match-night combo"
        q = f"{cta_verb(ci)} put up a match-night post with {hook} before the evening rush?"
        d.next_action = {"type": "gbp_post", "headline": f"{match} tonight — catch it with us at {m.name}!",
                         "offer": offer, "label": "the match-night post"}
        why = "weeknight match lifts covers — push the offer"
    d.cta = yes_cta(ci, q)
    d.levers = ["timeliness", "counter-intuitive data", "existing-offer leverage", "single YES CTA"]
    d.rationale = rationale(t.kind, why, d.levers)
    return d


def category_seasonal(ci: ComposeInput) -> Draft:
    t, m, s, cat = ci.trigger, ci.merchant, ci.signals, ci.category
    season = re.sub(r"\s*20\d\d$", "", humanize(t.ptext("season", "this season")))
    trends = t.p("trends", []) if isinstance(t.p("trends", []), list) else []
    parsed = []
    for tr in trends:
        mt = re.match(r"(.+?)_demand_([+-]\d+)", str(tr))
        if mt:
            raw = mt.group(1)
            label = raw.replace("_", "/") if raw.count("_") == 1 and "cold" in raw else humanize(raw)
            parsed.append(f"{label} {mt.group(2)}%")
    d = Draft(merchant_opener(ci), [], "", "binary_yes_no", "vera_seasonal_shelf_v1", "")
    if parsed:
        d.lines.append(f"{season} demand shift is here: {join_list(parsed)}.")
    else:
        beat = current_beat(cat, ci.now)
        d.lines.append(f"seasonal shift: {clean(beat.get('note'))}." if beat else f"{season} is changing what {cat.people} ask for.")
    item = next((x for x in cat.digest if str(x.get("kind")) == "seasonal"), None)
    if item and item.get("actionable"):
        d.lines.append(f"Suggested shelf move: {lower_first(ensure_period(item['actionable']))}")
    if s.repeat_pct and s.total_people:
        d.lines.append(f"Your records show {fmt_int(s.total_people)} customers this year, {pct_whole(s.repeat_pct)} of them "
                       f"repeat buyers, so a heads-up to regulars moves stock fastest.")
    item_name = f"{season} essentials" if season and season != "this season" else "seasonal essentials"
    d.cta = yes_cta(ci, f"{cta_verb(ci)} draft a '{item_name}' WhatsApp for your repeat {cat.people}?")
    d.next_action = {"type": "customer_broadcast", "topic": item_name, "items": parsed, "label": f"the '{item_name}' WhatsApp"}
    d.levers = ["specific demand deltas", "timeliness", "effort externalization"]
    d.rationale = rationale(t.kind, "seasonal demand shift with concrete deltas — turned into a shelf move + customer broadcast",
                            d.levers)
    return d


def supply_alert(ci: ComposeInput) -> Draft:
    t, m, s, cat = ci.trigger, ci.merchant, ci.signals, ci.category
    molecule = t.ptext("molecule")
    batches = [b for b in t.p("affected_batches", []) if isinstance(b, str)] if isinstance(t.p("affected_batches", []), list) else []
    mfr = t.ptext("manufacturer")
    item = cat.digest_item(t.p("alert_id")) or next(
        (x for x in cat.digest if molecule and molecule.lower() in digest_text(x)), None)
    d = Draft(merchant_opener(ci), [], "", "binary_yes_no", "vera_supply_alert_v1", "")
    followup = any(molecule and molecule.lower() in str(h.get("body", "")).lower() for h in m.history)
    src = clean(item.get("source")) if item else ""
    batch_txt = join_list(batches)
    what = f"{molecule or 'product'} batch{'es' if len(batches) > 1 else ''} {batch_txt}".strip()
    if mfr:
        what += f" ({mfr})"
    if followup:
        d.lines.append(f"update on the {molecule} recall you asked about: the affected batches are {batch_txt}"
                       + (f" ({mfr})" if mfr else "") + (f", per {src}." if src else "."))
    else:
        d.lines.append(f"urgent: voluntary recall on {what}" + (f", per {src}." if src else "."))
    if item:
        summ = sentences(item.get("summary"))
        if summ and "sub-potency" in summ[0].lower():
            d.lines.append("Reason: sub-potency.")
        for sent in summ[1:3]:
            d.lines.append(ensure_period(sent))
    if s.cohort and s.cohort[0] == "chronic_rx_count":
        d.lines.append(f"You have {fmt_int(s.cohort[1])} chronic-Rx customers; I can filter the ones on {molecule or 'it'}.")
    q = f"{cta_verb(ci)} pull that list now and draft the WhatsApp note for them?" if molecule else \
        f"{cta_verb(ci)} draft the customer note?"
    d.cta = yes_cta(ci, q)
    d.next_action = {"type": "recall_outreach", "molecule": molecule, "batches": batches,
                     "label": "the affected-customer list + WhatsApp note"}
    d.levers = ["urgency", "specific batch numbers", "bounded risk framing", "end-to-end workflow offer"]
    d.rationale = rationale(t.kind, f"{molecule or 'product'} batch recall — urgent, precise, calm; offered list + patient note",
                            d.levers, "Continues the merchant's earlier request for the list." if followup else "")
    return d


def review_theme_emerged(ci: ComposeInput) -> Draft:
    t, m, s = ci.trigger, ci.merchant, ci.signals
    theme = t.ptext("theme") or clean((s.neg_theme or {}).get("theme"))
    occ = t.pint("occurrences_30d") or (s.neg_theme or {}).get("occurrences_30d")
    quote = t.ptext("common_quote") or clean((s.neg_theme or {}).get("common_quote"))
    rising = str(t.p("trend", "")).lower() == "rising"
    d = Draft(merchant_opener(ci), [], "", "binary_yes_no", "vera_review_theme_v1", "")
    if theme:
        line = f"{occ} reviews in the last 30 days mention {theme_label(str(theme))}" if occ else \
            f"recent reviews keep mentioning {theme_label(str(theme))}"
        if rising:
            line += ", and it's rising"
        line += f" — one says \"{quote}\"." if quote else "."
        d.lines.append(line)
        pos = s.pos_theme
        if pos and pos.get("theme") != theme:
            d.lines.append(f"The good news: {pos.get('occurrences_30d')} of your reviews praise {theme_label(str(pos['theme']))}, "
                           f"so this reads as fixable ops, not a reputation problem.")
        q = f"{cta_verb(ci)} draft calm public replies to those reviews, plus one line on what you're fixing?"
        d.next_action = {"type": "review_replies", "theme": theme, "quote": quote, "label": "the review replies"}
    else:
        d.lines.append("a recurring theme is showing up in your latest Google reviews.")
        if s.views is not None:
            d.lines.append(f"With {fmt_int(s.views)} people viewing your profile a month, replies are public proof you listen.")
        q = f"{cta_verb(ci)} pull those reviews and draft replies for you to approve?"
        d.next_action = {"type": "review_replies", "label": "the review replies"}
    d.cta = yes_cta(ci, q)
    d.levers = ["loss aversion (public reviews)", "specific quote", "effort externalization"]
    d.rationale = rationale(t.kind, f"review theme '{theme or 'unspecified in payload'}' — address publicly before it compounds",
                            d.levers)
    return d


_ASKS = {
    "restaurants": "which dish is moving fastest this week?",
    "gyms": "which class or time slot is filling fastest this week?",
    "pharmacies": "what are customers asking for most at the counter this week?",
    "dentists": "which treatment are patients asking about most this week?",
}


def curious_ask(ci: ComposeInput) -> Draft:
    t, m, s, cat = ci.trigger, ci.merchant, ci.signals, ci.category
    d = Draft(merchant_opener(ci, "hi"), [], "", "open_ended", "vera_curious_ask_v1", "")
    ask = _ASKS.get(cat.slug, f"which service are {m.locality + ' ' if m.locality else ''}{cat.people} asking about most this week?")
    d.lines.append(f"quick one: {ask}")
    if s.rise:
        d.lines.append(f"Your dashboard shows {metric_label(s.rise[0])} up {pct_whole(s.rise[1])} this week, so something's working.")
    guess_line = None
    if s.pos_theme:
        quote = clean(s.pos_theme.get("common_quote"))
        text = f"{s.pos_theme.get('theme', '')} {quote}".lower()
        vocab = next((v for v in cat.vocab if v.lower() in text), None)
        if vocab and quote:
            guess_line = f"My guess is {vocab}, going by reviews like \"{quote}\"."
        elif str(s.pos_theme.get("theme", "")).endswith("_quality"):
            item = theme_label(str(s.pos_theme["theme"]))
            guess_line = f"My guess is {item} — {s.pos_theme.get('occurrences_30d')} of your Google reviews this month praise it."
    if not guess_line and m.active_offers:
        guess_line = f"My guess is your {m.active_offers[0]}."
    if guess_line:
        d.lines.append(guess_line)
    q = "Tell me in one line and I'll turn it into a Google post plus a ready WhatsApp reply for price questions."
    d.cta = f"{q} Bas ek line bhej dijiye." if ci.lang == "hinglish_light" else q
    d.next_action = {"type": "curious_answer", "label": "the post + WhatsApp reply"}
    d.levers = ["asking the merchant", "reciprocity", "low-effort reply"]
    d.rationale = rationale(t.kind, "weekly curiosity ask with a data-backed guess; the answer becomes ready-to-use content",
                            d.levers)
    return d


def active_planning_intent(ci: ComposeInput) -> Draft:
    t, m, s, cat = ci.trigger, ci.merchant, ci.signals, ci.category
    topic = humanize(t.ptext("intent_topic", "the plan"))
    topic_l = topic.lower()
    d = Draft(merchant_opener(ci), [], "", "binary_yes_no", "vera_plan_draft_v1", "")
    base_offer = best_active_offer(m, topic)
    base_price = offer_price(base_offer or "")
    prior = ""
    for h in reversed(m.history):
        body = str(h.get("body", ""))
        if str(h.get("from", "")).lower() == "vera" and re.search(r"\d", body) and \
                any(w in body.lower() for w in topic_l.split() if len(w) > 3):
            prior = clean(body)
            break
    d.lines.append(f"here's a first draft of the {topic} — edit anything:")
    if any(w in topic_l for w in ("bulk", "corporate", "catering", "office", "party")) and base_price:
        for qty, factor in ((10, 0.93), (25, 0.87), (50, 0.80)):
            price = int(round(base_price * factor / 5.0) * 5)
            d.allow(qty, price)
            d.lines.append(f"• {qty}+ orders/day: {inr(price)} each (vs {inr(base_price)} retail)")
        d.lines.append("• Order by 5pm the day before; delivered in a fixed lunch window")
        d.allow(5)
        d.lines.append(f"Built on your {base_offer}.")
        audience = f"office admins in {m.locality}" if m.locality else "nearby office admins"
    elif prior:
        bits = re.findall(r"(\d+-week[^,.]*|\d+ classes/week|age \d+-\d+|₹[\d,]+)", prior)
        if bits:
            d.lines.append("• Format: " + ", ".join(bits))
        kids = "kid" in topic_l
        d.lines.append("• Small batches so every child gets attention" if kids else "• Capped batch size for quality")
        if base_offer:
            d.lines.append(f"• Hook for new families: your {base_offer}" if kids else f"• Hook: your {base_offer}")
        d.lines.append(f"• Launch: Google post + Instagram carousel + WhatsApp to your existing {cat.people}")
        audience = f"parents among your {cat.people}" if kids else f"your {cat.people}"
    else:
        d.lines.append(f"• What: {topic}, run as a fixed-date batch")
        if base_offer:
            d.lines.append(f"• Price anchor: your {base_offer}")
        if s.total_people:
            d.lines.append(f"• First audience: your {fmt_int(s.total_people)} {cat.people}")
        d.lines.append("• Launch: Google post + WhatsApp broadcast")
        audience = f"your {cat.people}"
    d.cta = f"Reply YES and I'll publish it as a Google post and draft the WhatsApp for {audience}."
    if ci.lang == "hinglish_light":
        d.cta += " Kuch badalna ho toh bata dijiye."
    d.next_action = {"type": "publish_plan", "topic": topic, "audience": audience, "label": f"the {topic} launch"}
    d.levers = ["intent handoff (action mode)", "complete draft artifact", "effort externalization"]
    d.rationale = rationale(t.kind, f"merchant already asked for the {topic}; delivered the draft instead of re-qualifying",
                            d.levers, "Reused the earlier Vera proposal from conversation history." if prior else "")
    return d


def external_event(ci: ComposeInput) -> Draft:
    """weather_* / local_news_event / other external happenings with free-form payloads."""
    t, m, cat = ci.trigger, ci.merchant, ci.category
    p = t.payload
    headline = clean(p.get("headline") or p.get("title") or p.get("event") or p.get("description") or humanize(t.kind))
    bits = []
    for key in ("temperature_c", "temp_c", "city", "area", "date", "duration_hours"):
        v = p.get(key)
        if v in (None, ""):
            continue
        if key in {"temperature_c", "temp_c"}:
            bits.append(f"{v}°C")
        elif key == "date":
            bits.append(fmt_date(v))
        elif key == "duration_hours":
            bits.append(f"{v}h")
        else:
            bits.append(humanize(v))
    d = Draft(merchant_opener(ci, "hi"), [], "", "binary_yes_no", "vera_external_event_v1", "")
    d.lines.append(f"heads-up: {upper_first(headline)}" + (f" ({', '.join(bits)})." if bits else "."))
    beat = current_beat(cat, ci.now)
    if beat:
        d.lines.append(beat_sentence(beat, cat))
    offer, live = offer_for_action(ci, headline)
    d.cta = yes_cta(ci, f"{cta_verb(ci)} put up a quick post with {offer_ref(offer, live)} for today?")
    d.next_action = {"type": "gbp_post", "topic": headline, "offer": offer, "label": "the post"}
    d.levers = ["timeliness", "relevance", "single YES CTA"]
    d.rationale = rationale(t.kind, f"external event '{headline}' — tied to {offer or m.name}", d.levers)
    return d


def generic(ci: ComposeInput) -> Draft:
    """Unknown trigger kind: state only supplied facts, anchor on the merchant's strongest signal."""
    t, m, s = ci.trigger, ci.merchant, ci.signals
    d = Draft(merchant_opener(ci, "hi"), [], "", "binary_yes_no", "vera_generic_v1", "")
    facts = []
    for k, v in sorted(t.payload.items()):
        if k in {"placeholder", "metric_or_topic"} or isinstance(v, (dict, list)) or v in (None, ""):
            continue
        shown = fmt_date(v) if isinstance(v, str) and re.match(r"^20\d\d-\d\d-\d\d", v) else v
        facts.append(f"{humanize(k)} {str(shown).replace('?', '')}")
        if len(facts) == 3:
            break
    d.lines.append(f"flagging a {humanize(t.kind)} update for {m.name}" + (f": {'; '.join(facts)}." if facts else "."))
    if s.drop:
        d.lines.append(f"Context: {metric_label(s.drop[0])} are down {pct_whole(s.drop[1])} this week.")
    elif s.rise:
        d.lines.append(f"Context: {metric_label(s.rise[0])} are up {pct_whole(s.rise[1])} this week.")
    offer, live = offer_for_action(ci)
    if live:
        q = f"{cta_verb(ci)} draft a post around your {offer} to act on it?"
        d.next_action = {"type": "gbp_post", "offer": offer, "label": "the post draft"}
    elif offer:
        q = f"{cta_verb(ci)} set up '{offer}' on your profile to act on it?"
        d.next_action = {"type": "offer_setup", "offer": offer, "label": f"the '{offer}' listing"}
    else:
        q = f"{cta_verb(ci)} send you the one action I'd take on it?"
        d.next_action = {"type": "summary", "label": "the recommended action"}
    d.cta = yes_cta(ci, q)
    d.levers = ["relevance", "single YES CTA"]
    d.rationale = rationale(t.kind, "unrecognised trigger kind — stated only supplied facts, anchored on merchant data", d.levers)
    return d
