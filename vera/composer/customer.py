"""Customer-facing strategies (send_as = "merchant_on_behalf").

Voice: the merchant speaking to their own customer. Rules enforced here:
  * language preference honoured (English / Hinglish / Hindi-leaning Hinglish,
    regional greeting for ta/te/kn/mr mixes)
  * only the merchant's *live* offers are mentioned — never catalog suggestions
  * real slots only (from the trigger payload); otherwise we offer to share slots
  * health specifics (molecules) only when consent covers refill reminders
"""

from __future__ import annotations

import re
from typing import Optional

from ..context.views import CustomerView
from ..utils.text import clean, humanize, join_list
from ..utils.timeutil import fmt_date, parse_iso
from .base import CATEGORY_EMOJI, ComposeInput, Draft, owner_signature, rationale
from .knowledge import beat_for_month, best_active_offer, related_offer, upcoming_beat

ROUTINE_SERVICE = {
    "dentists": ("routine check-up and cleaning", "check-up"),
    "salons": ("regular trim and touch-up", "appointment"),
    "gyms": ("next session", "session"),
    "restaurants": ("next meal with us", "visit"),
    "pharmacies": ("routine health check", "visit"),
}


# ------------------------------------------------------------------ helpers

def _mode(c: CustomerView) -> str:
    return c.lang_mode  # en | hinglish | hindi


def _hi(c: CustomerView) -> bool:
    return _mode(c) in {"hinglish", "hindi"}


def addressee(c: CustomerView) -> str:
    """Who actually reads the message (parent for kids)."""
    if c.parent_name:
        return c.parent_name.split(" ")[0]
    if _hi(c) and c.honorific != c.first_name:
        return c.honorific
    return c.first_name


def opener(ci: ComposeInput) -> str:
    c = ci.customer
    name = addressee(c)
    if c.via and not c.parent_name:  # message goes to an unnamed family member's phone
        return "Namaste!" if _hi(c) else "Hello!"
    if _mode(c) == "hindi":
        return f"Namaste {name}!" if name else "Namaste!"
    if c.regional_greeting:
        return f"{c.regional_greeting} {name}!" if name else f"{c.regional_greeting}!"
    return f"Hi {name}," if name else "Hello,"


def intro(ci: ComposeInput) -> str:
    m, c = ci.merchant, ci.customer
    emoji = "🧘" if "yoga" in m.name.lower() else CATEGORY_EMOJI.get(ci.category.slug, "")
    if _hi(c):
        where = f", {m.locality} se" if m.locality else ""
        return f"{m.name}{where} {emoji}".strip()
    return f"{owner_signature(ci)} here {emoji}".strip()


def slot_matches(pref: str, iso: str) -> bool:
    dt = parse_iso(iso)
    if not pref or dt is None:
        return False
    local_dt = dt
    wd, hour = local_dt.weekday(), local_dt.hour
    p = pref.lower()
    day_ok = True
    if "weekday" in p:
        day_ok = wd < 5
    elif "weekend" in p:
        day_ok = wd >= 5
    for i, day in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]):
        if day in p:
            day_ok = wd == i
    time_ok = True
    if "morning" in p:
        time_ok = hour < 12
    elif "afternoon" in p:
        time_ok = 12 <= hour < 17
    elif "evening" in p:
        time_ok = 16 <= hour < 21
    elif "night" in p:
        time_ok = hour >= 19
    return day_ok and time_ok


def pref_phrase(pref: str) -> str:
    return humanize(pref).replace("weekday evening", "weekday-evening").replace("saturday morning", "Saturday-morning") \
        .replace("saturday", "Saturday").replace("sunday", "Sunday")


def _fix_service(text: str) -> str:
    t = humanize(text)
    t = re.sub(r"\b(\d+) month\b", r"\1-month", t)
    m = re.match(r"(.+?) (\d+)day$", t)
    if m:
        t = f"{m.group(2)}-day {m.group(1)}"
    return t.replace("skin prep", "skin-prep")


def _yes(c: CustomerView, en: str, hi: str) -> str:
    return hi if _hi(c) else en


def _stop_line(c: CustomerView) -> str:
    return " Messages band karne ke liye STOP bhejein." if _hi(c) else " Reply STOP to opt out."


def _winback_offer(ci: ComposeInput, focus: str = "") -> Optional[str]:
    m = ci.merchant
    if focus:
        rel = related_offer(m, focus)
        if rel:
            return rel
    frees = [o for o in m.active_offers if "free" in o.lower()]
    return frees[0] if frees else best_active_offer(m)


def _draft(ci: ComposeInput, template: str, cta_type: str = "binary_yes_no") -> Draft:
    return Draft(opener(ci), [], "", cta_type, template, "", send_as="merchant_on_behalf")


# ------------------------------------------------------------------ strategies

def recall_due(ci: ComposeInput, reinterpreted_from: str = "") -> Draft:
    t, m, c, cat = ci.trigger, ci.merchant, ci.customer, ci.category
    routine, short = ROUTINE_SERVICE.get(cat.slug, ("next visit", "visit"))
    svc = _fix_service(t.ptext("service_due")) or routine
    svc_short = svc.split(" ")[-1] if t.ptext("service_due") else short
    last = t.ptext("last_service_date") or c.last_visit
    due = t.ptext("due_date")
    slots = [s for s in (t.p("available_slots") or []) if isinstance(s, dict) and s.get("label")][:3]
    d = _draft(ci, "merchant_recall_reminder_v1", "multi_choice_slot" if slots else "binary_yes_no")
    d.lines.append(f"{intro(ci)}.")
    if _hi(c):
        if t.p("service_due") and last:
            d.lines.append(f"Aapki last {svc_short} {fmt_date(last)} ko hui thi, toh aapka {svc} recall"
                           + (f" {fmt_date(due)} tak due hai." if due else " ab due hai."))
        elif last:
            d.lines.append(f"Aapki last visit {fmt_date(last)} ko thi — ab {svc} ka sahi time hai.")
        else:
            d.lines.append(f"Aapka {svc} due hai.")
    else:
        if t.p("service_due") and last:
            d.lines.append(f"Your last {svc_short} was on {fmt_date(last)}, so your {svc} recall is due"
                           + (f" by {fmt_date(due)}." if due else " now."))
        elif last:
            d.lines.append(f"Our records show your last visit was on {fmt_date(last)}.")
            if c.visits_total and c.visits_total > 1:
                d.allow(c.visits_total)
                d.lines.append(f"After {c.visits_total} visits with us, we'd love to host you again." if cat.slug == "restaurants"
                               else f"After {c.visits_total} visits with us, it's a good time to book your {svc}.")
            elif cat.slug != "restaurants":
                d.lines.append(f"It's a good time to book your {svc}.")
        else:
            d.lines.append(f"Your {svc} is due.")
    if slots:
        labels = [clean(s["label"]) for s in slots]
        pref = c.preferred_slots
        match_all = pref and all(slot_matches(pref, s.get("iso", "")) for s in slots)
        if _hi(c):
            line = f"Aapke liye {len(slots)} slots ready hain: {' ya '.join(labels)}"
            line += f" — dono {pref_phrase(pref)} slots, jaise aap prefer karte hain." if match_all and len(slots) == 2 else "."
        else:
            line = f"We've kept {len(slots)} slots for you: {join_list(labels, 'or')}"
            line += f" — both {pref_phrase(pref)} slots, as you prefer." if match_all and len(slots) == 2 else "."
        d.lines.append(line)
        d.allow(len(slots))
        d.meta["slots"] = slots
    offer = related_offer(m, svc) or (best_active_offer(m) if cat.slug == "dentists" else None)
    free_hook = None if offer else next((o for o in m.active_offers if "free" in o.lower()), None)
    if offer:
        d.lines.append(f"{offer} applicable hai." if _hi(c) else f"{offer} applies.")
    elif free_hook:
        d.lines.append(f"Wapas aane par {free_hook} hamari taraf se." if _hi(c) else f"{free_hook} is on us when you're back.")
    if slots:
        days = [clean(s["label"]).split(" ")[0] for s in slots]
        if _hi(c):
            opts = ", ".join(f"{day} ke liye {i + 1}" for i, day in enumerate(days))
            d.cta = f"{opts} reply karein — ya apna convenient time bata dijiye."
        else:
            opts = ", ".join(f"{i + 1} for {day}" for i, day in enumerate(days))
            d.cta = f"Reply {opts}, or tell us a time that suits you."
        d.allow(*range(1, len(slots) + 1))
    elif cat.slug == "restaurants":
        d.cta = _yes(c, "Reply YES and we'll keep a table for you this week.",
                     "Is hafte aapke liye table reserve kar dein? YES reply karein.")
    else:
        d.cta = _yes(c, "Reply YES and we'll send you this week's open slots.",
                     "YES reply karein, hum is hafte ke open slots bhej denge.")
    d.next_action = {"type": "slot_booking", "slots": slots, "service": svc, "offer": offer or free_hook,
                     "label": "your booking"}
    d.levers = ["name + language match", "real dates/slots", "real price", "low-friction reply"]
    note = f"Trigger kind '{reinterpreted_from}' read as a routine repeat-care reminder for a {cat.singular}." if reinterpreted_from else ""
    d.rationale = rationale(t.kind if not reinterpreted_from else reinterpreted_from,
                            f"{svc} due for {c.customer_id}; honoured {c.language_pref or 'default'} language"
                            + (f" and {c.preferred_slots} preference" if slots and c.preferred_slots else ""),
                            d.levers, note)
    return d


def appointment_tomorrow(ci: ComposeInput) -> Draft:
    t, m, c = ci.trigger, ci.merchant, ci.customer
    when = t.ptext("slot_label") or t.ptext("time_label") or t.ptext("appointment_time")
    svc = _fix_service(t.p("service", ""))
    d = _draft(ci, "merchant_appointment_reminder_v1", "binary_confirm_cancel")
    d.lines.append(f"{intro(ci)}.")
    where = f" {m.locality}" if m.locality else ""
    if _hi(c):
        line = "Ek chhota sa reminder: kal aapka appointment hai"
        line += f" ({when})" if when else ""
        line += f" — {svc}" if svc else ""
        d.lines.append(line + ".")
        if m.locality:
            d.lines.append(f"Hum{where} mein hain.")
        d.cta = "Confirm karne ke liye YES reply karein, ya time badalna ho toh bata dijiye."
    else:
        line = "A quick reminder: you're booked with us tomorrow"
        line += f", {when}" if when else ""
        line += f" for {svc}" if svc else ""
        d.lines.append(line + ".")
        if m.locality:
            d.lines.append(f"We're in{where}.")
        d.cta = "Reply YES to confirm, or tell us if you'd like to reschedule."
    d.next_action = {"type": "appointment_confirm", "label": "your appointment"}
    d.levers = ["utility reminder", "single confirm CTA", "reschedule escape hatch"]
    d.rationale = rationale(t.kind, f"transactional reminder for {c.customer_id}; no upsell to keep trust", d.levers)
    return d


def chronic_refill_due(ci: ComposeInput) -> Draft:
    t, m, c, cat = ci.trigger, ci.merchant, ci.customer, ci.category
    if cat.slug != "pharmacies":
        return recall_due(ci, reinterpreted_from=t.kind)
    raw_mols = t.p("molecule_list")
    mols = [x.strip() for x in raw_mols if isinstance(x, str) and x.strip()] if isinstance(raw_mols, list) else []
    health_ok = bool(c.consent_scope & {"refill_reminders", "chronic_refill", "prescription_reminders"})
    runs_out = t.ptext("stock_runs_out_iso")
    last = t.ptext("last_refill")
    d = _draft(ci, "merchant_refill_reminder_v1", "binary_confirm_cancel")
    who = c.honorific if _hi(c) else (c.first_name or "your")
    d.lines.append(f"{intro(ci)}.")
    named = mols and health_ok
    meds_en = f"{len(mols)} regular medicines ({join_list(mols)})" if named else "regular medicines"
    meds_hi = f"{len(mols)} regular medicines — {join_list(mols, 'aur')} —" if named else "regular medicines"
    if named:
        d.allow(len(mols))
    if _hi(c):
        subj = f"{who} ki" if who else "Aapki"
        line = f"{subj} {meds_hi} {fmt_date(runs_out)} tak khatam ho jayengi" if runs_out else f"{subj} {meds_hi} refill ke liye due hain"
        line += f" (pichhla refill {fmt_date(last)} ko hua tha)." if last else "."
        d.lines.append(line.replace("  ", " "))
        d.lines.append("Same dose, same pack ready rakh rahe hain.")
    else:
        subj = f"{who}'s" if who and who != "your" else "Your"
        line = f"{subj} {meds_en} run out on {fmt_date(runs_out)}" if runs_out else f"{subj} {meds_en} are due for a refill"
        line += f" (last refill {fmt_date(last)})." if last else "."
        d.lines.append(line)
        d.lines.append("We're keeping the same dose and pack ready.")
    perks = []
    for offer in m.active_offers:
        ol = offer.lower()
        if "senior" in ol and not c.senior:
            continue
        if "delivery" in ol and t.p("delivery_address_saved") is False:
            continue
        if any(w in ol for w in ("senior", "delivery", "refill")):
            perks.append(offer)
    if perks:
        if _hi(c):
            d.lines.append(f"{join_list(perks, 'aur')} — dono apply honge." if len(perks) == 2 else f"{perks[0]} apply hoga.")
        else:
            d.lines.append(f"{join_list(perks)} apply.")
    recall = next((x for x in ci.category.digest if str(x.get("kind")) == "alert"
                   and any(mo.lower() in str(x.get("title", "")).lower() for mo in mols)), None) if named else None
    if recall:
        mol = next(mo for mo in mols if mo.lower() in str(recall.get("title", "")).lower())
        d.lines.append(f"{mol.capitalize()} ka batch current recall list se check karke hi bhejenge." if _hi(c) else
                       f"We'll check the {mol} batch against the current recall list before dispatch.")
    if t.p("delivery_address_saved"):
        d.cta = ("Saved address par dispatch ke liye CONFIRM reply karein, ya dosage mein koi badlaav ho toh bata dijiye."
                 if _hi(c) else "Reply CONFIRM to dispatch to your saved address, or tell us if the dosage has changed.")
    else:
        d.cta = ("Pack ready rakhne ke liye CONFIRM reply karein." if _hi(c)
                 else "Reply CONFIRM and we'll keep the pack ready for pickup.")
    d.next_action = {"type": "refill_dispatch", "molecules": mols if named else [], "label": "the refill"}
    d.levers = ["precise date", "same-pack continuity", "real perks from live offers", "single CONFIRM CTA"]
    d.rationale = rationale(t.kind, f"refill runs out {fmt_date(runs_out) if runs_out else 'soon'}; "
                                    f"{'named molecules (refill consent present)' if named else 'kept medicines generic (no refill-specific consent)'}"
                                    + (f"; addressed via {c.via}" if c.via else ""), d.levers)
    return d


def customer_lapsed(ci: ComposeInput) -> Draft:
    t, m, c, cat = ci.trigger, ci.merchant, ci.customer, ci.category
    days = t.pnum("days_since_last_visit")
    focus = humanize(t.ptext("previous_focus")) or humanize(c.preferences.get("training_focus", ""))
    months = t.pint("previous_membership_months")
    d = _draft(ci, "merchant_winback_v1")
    d.lines.append(f"{intro(ci)}.")
    unit = {"gyms": "session", "salons": "visit", "dentists": "visit", "restaurants": "visit", "pharmacies": "visit"}.get(cat.slug, "visit")
    no_judgement = {"gyms": "happens to everyone, no judgment", "dentists": "just a friendly check-in",
                    "salons": "we've missed you", "restaurants": "we've missed you", "pharmacies": "just checking in"}
    nj = no_judgement.get(cat.slug, "we've missed you")
    if isinstance(days, (int, float)) and days > 0:
        weeks = int(round(days / 7.0))
        d.allow(weeks)
        gap_en = f"It's been about {weeks} weeks since your last {unit}" if weeks >= 2 else f"It's been {int(days)} days since your last {unit}"
        gap_hi = f"Aapki last {unit} ko lagbhag {weeks} hafte ho gaye" if weeks >= 2 else f"Aapki last {unit} ko {int(days)} din ho gaye"
    elif c.last_visit:
        gap_en = f"Our records show your last visit was on {fmt_date(c.last_visit)}"
        gap_hi = f"{fmt_date(c.last_visit)} ke baad aap nahi aaye"
    else:
        gap_en, gap_hi = "It's been a while since your last visit", "Kaafi time ho gaya"
    if _hi(c):
        d.lines.append(f"{gap_hi} — koi baat nahi, aisa sabke saath hota hai.")
    else:
        d.lines.append(f"{gap_en} — {nj}.")
    if months:
        d.allow(months)
    offer = _winback_offer(ci, focus)
    if focus and offer:
        d.lines.append(f"Agar {focus} abhi bhi goal hai, toh {offer} se dobara shuru kar sakte hain." if _hi(c) else
                       f"If {focus} is still the goal, {offer} is an easy way to restart.")
    elif focus:
        d.lines.append(f"Agar {focus} abhi bhi goal hai, hum plan phir se set kar denge." if _hi(c) else
                       f"If {focus} is still the goal, we'll set your plan up again.")
    elif offer:
        d.lines.append(f"{offer} abhi available hai." if _hi(c) else f"{offer} is on right now if you'd like to come back.")
    if c.visits_total and c.visits_total > 2 and not months:
        d.allow(c.visits_total)
        d.lines.append(f"Aap {c.visits_total} baar aa chuke hain — aapki jagah yahin hai." if _hi(c) else
                       f"You've been in {c.visits_total} times — your spot's still here.")
    pref = c.preferred_slots
    spot_en = f"a {pref_phrase(pref)} spot" if pref else "a spot this week"
    spot_hi = f"{pref_phrase(pref)} slot" if pref else "is hafte ek slot"
    if cat.slug == "restaurants":
        spot_en, spot_hi = "a table this week", "is hafte ek table"
    if cat.slug == "pharmacies":
        d.cta = _yes(c, "Need anything restocked? Reply YES and we'll keep it ready.",
                     "Kuch restock karna ho? YES reply karein, hum ready rakhenge.")
    else:
        d.cta = _yes(c, f"Want us to hold {spot_en} for you? Reply YES, no commitment.",
                     f"Aapke liye {spot_hi} hold kar dein? YES reply karein, koi commitment nahi.")
    d.cta += _stop_line(c)
    d.next_action = {"type": "slot_booking", "slots": [], "service": focus or unit, "offer": offer, "label": "your spot"}
    d.levers = ["no-shame framing", "goal continuity", "no-commitment ask", "opt-out respected"]
    d.rationale = rationale(t.kind, f"{c.state or 'lapsed'} customer; warm win-back tied to "
                                    f"{focus or 'their history'} with the merchant's live offer", d.levers)
    return d


def trial_followup(ci: ComposeInput) -> Draft:
    t, m, c, cat = ci.trigger, ci.merchant, ci.customer, ci.category
    trial_date = t.ptext("trial_date")
    options = [o for o in (t.p("next_session_options") or []) if isinstance(o, dict) and o.get("label")]
    trial_kind = next((s for s in c.services if "trial" in s), "")
    child = c.first_name if c.parent_name else ""
    d = _draft(ci, "merchant_trial_followup_v1")
    d.lines.append(f"{intro(ci)}.")
    what = trial_kind or "trial"
    if child:
        d.lines.append(f"Thank you for bringing {child} to the {what} on {fmt_date(trial_date)}." if trial_date else
                       f"Thank you for bringing {child} for the {what}.")
    else:
        first = {"gyms": "first session", "salons": "first visit", "dentists": "first visit",
                 "restaurants": "first meal with us", "pharmacies": "first order"}.get(cat.slug, "first visit")
        d.lines.append(f"Thanks for coming in for your {what} on {fmt_date(trial_date)}." if trial_date else
                       f"Thanks for trying {m.name} — hope your {first} went well.")
    if options:
        lab = clean(options[0]["label"])
        pref = c.preferred_slots
        fits = f" — right in the {pref_phrase(pref)} slot you prefer" if pref and slot_matches(pref, options[0].get("iso", "")) else ""
        d.lines.append(f"The next session is {lab}{fits}.")
        d.meta["slots"] = options
    else:
        offer = best_active_offer(m)
        if offer:
            d.lines.append(f"If you'd like to continue, {offer} is on right now.")
    ask_for = f"{child} a spot" if child else "your spot"
    nxt = {"gyms": "your next session", "salons": "your next appointment", "dentists": "your next appointment",
           "restaurants": "a table for your next visit"}.get(cat.slug, "your next visit")
    if options:
        d.cta = f"Shall we save {ask_for}? Reply YES."
    elif cat.slug == "pharmacies":
        d.cta = "Want us to keep your next order ready? Reply YES."
    else:
        d.cta = f"Want us to book {child + ' in' if child else nxt}? Reply YES."
    if _hi(c):
        d.cta = ("Agla order ready rakh dein? YES reply karein." if cat.slug == "pharmacies"
                 else "Agla slot save kar dein? YES reply karein.")
    d.next_action = {"type": "slot_booking", "slots": options, "service": what, "label": "the next session"}
    d.levers = ["reciprocity (thanks)", "real next slot", "preference match", "single YES CTA"]
    d.rationale = rationale(t.kind, f"post-trial conversion for {c.customer_id}"
                                    + (" addressed to the parent" if child else ""), d.levers)
    return d


def wedding_followup(ci: ComposeInput) -> Draft:
    t, m, c, cat = ci.trigger, ci.merchant, ci.customer, ci.category
    wedding = t.ptext("wedding_date") or clean(c.preferences.get("wedding_date"))
    trial = t.ptext("trial_completed")
    days = t.pint("days_to_wedding")
    step = _fix_service(t.ptext("next_step_window_open")) or "pre-bridal prep"
    d = _draft(ci, "merchant_bridal_followup_v1")
    d.opener = d.opener.replace(",", " 💍") if d.opener.startswith("Hi ") else d.opener
    trial_txt = f" — hope you loved the bridal trial on {fmt_date(trial)}!" if trial else "!"
    d.lines.append(f"{owner_signature(ci)} here{trial_txt}")
    line = f"{days} days to your wedding on {fmt_date(wedding)}" if days and wedding else (
        f"Your wedding is on {fmt_date(wedding)}" if wedding else "Your big day is coming up")
    wd = parse_iso(wedding)
    beat = (beat_for_month(cat, wd.month, ("wedding", "bridal")) if wd else None) or upcoming_beat(
        cat, ci.now, ("wedding", "bridal"))
    rush = ""
    if beat:
        mult = re.search(r"(\d+x)", str(beat.get("note", "")))
        rush = f", before the {beat.get('month_range')} bridal rush" + (f" when bookings run {mult.group(1)} normal" if mult else "")
    d.lines.append(f"{line}, so this is the right window to start your {step}{rush}.")
    pref = c.preferred_slots
    slot = f"a {pref_phrase(pref)} slot" if pref else "a slot"
    d.cta = f"Want us to block {slot} for your first session next week? Reply YES and we'll share timings and the package details."
    d.next_action = {"type": "slot_booking", "slots": [], "service": step, "label": "your first session"}
    d.levers = ["date specificity", "window urgency", "preference honoured", "single YES CTA"]
    d.rationale = rationale(t.kind, f"bridal client in prep window ({days} days out); no price quoted because no matching live offer",
                            d.levers)
    return d


def slot_open(ci: ComposeInput) -> Draft:
    t, m, c = ci.trigger, ci.merchant, ci.customer
    slot = t.p("slot")
    label = t.ptext("slot_label") or (clean(slot.get("label")) if isinstance(slot, dict) else "")
    d = _draft(ci, "merchant_slot_open_v1")
    d.lines.append(f"{intro(ci)}.")
    d.lines.append(f"A {label} slot just opened up." if label else "A slot just opened up this week.")
    offer = best_active_offer(m)
    if offer:
        d.lines.append(f"{offer} applies.")
    d.cta = _yes(c, "Want it? Reply YES and it's yours.", "Chahiye? YES reply karein.")
    d.next_action = {"type": "slot_booking", "slots": [{"label": label}] if label else [], "label": "the slot"}
    d.levers = ["scarcity (real opening)", "single YES CTA"]
    d.rationale = rationale(t.kind, "real capacity opening offered to a relevant customer", d.levers)
    return d


def generic_customer(ci: ComposeInput) -> Draft:
    t, m, c = ci.trigger, ci.merchant, ci.customer
    d = _draft(ci, "merchant_generic_v1")
    d.lines.append(f"{intro(ci)}.")
    if c.last_visit:
        d.lines.append(f"Aapki last visit {fmt_date(c.last_visit)} ko thi." if _hi(c) else
                       f"Your last visit with us was on {fmt_date(c.last_visit)}.")
    offer = best_active_offer(m)
    if offer:
        d.lines.append(f"{offer} abhi available hai." if _hi(c) else f"{offer} is on right now.")
    d.cta = _yes(c, "Want us to set something up for you? Reply YES.", "Kuch set kar dein? YES reply karein.") + _stop_line(c)
    d.next_action = {"type": "slot_booking", "slots": [], "label": "your visit"}
    d.levers = ["relationship continuity", "single YES CTA"]
    d.rationale = rationale(t.kind, "unrecognised customer trigger; kept to relationship facts and live offers", d.levers)
    return d
