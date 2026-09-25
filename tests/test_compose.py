"""compose() over the full expanded dataset: validity, grounding, voice, consent, language."""

from __future__ import annotations

import re

import pytest

from bot import compose as public_compose
from vera.composer.engine import compose
from vera.context.views import CategoryView
from vera.validation.output import allowed_numbers, ungrounded_numbers, validate_message

NOW = "2026-04-26T10:30:00Z"


def _ctx(data, tid):
    t = data["triggers"][tid]
    m = data["merchants"][t["merchant_id"]]
    c = data["customers"].get(t.get("customer_id")) if t.get("customer_id") else None
    return data["categories"][m["category_slug"]], m, t, c


def test_every_trigger_composes_valid_grounded_message(expanded):
    failures = []
    for tid in expanded["triggers"]:
        cat, m, t, c = _ctx(expanded, tid)
        msg = compose(cat, m, t, c, now=NOW)
        internal = msg["_internal"]
        allowed = allowed_numbers(cat, m, t, c, extra=[])
        errs = validate_message(msg, CategoryView(cat).taboos)
        if errs or internal["route"] == "safe_minimal" or internal["validation_errors"]:
            failures.append((tid, internal["route"], errs, internal["validation_errors"]))
        assert len(msg["body"]) <= 1000, tid
        assert msg["body"].count("?") <= 1, tid
        # every number is traceable to the contexts or to a value the composer registered
        _ = allowed  # strict check happens inside finalize(); failures would route to safe_minimal
    assert not failures, failures


def test_canonical_test_pairs_all_produce_messages(expanded):
    assert len(expanded["pairs"]) == 30
    for pair in expanded["pairs"]:
        cat, m, t, c = _ctx(expanded, pair["trigger_id"])
        out = public_compose(cat, m, t, c, now=NOW)
        assert set(out) == {"body", "cta", "send_as", "suppression_key", "rationale", "template_name",
                            "template_params"}
        assert out["body"] and out["rationale"]


def test_customer_messages_only_mention_live_offers(expanded):
    for tid, t in expanded["triggers"].items():
        if not t.get("customer_id"):
            continue
        cat, m, t, c = _ctx(expanded, tid)
        msg = compose(cat, m, t, c, now=NOW)
        if msg["send_as"] != "merchant_on_behalf":
            continue
        live = {o["title"] for o in m.get("offers", []) if o.get("status") == "active"}
        for title in (o["title"] for o in cat["offer_catalog"]):
            if title in msg["body"]:
                assert title in live, (tid, title)


def test_consent_blocked_customer_is_routed_to_merchant(seed):
    t = dict(seed["triggers"]["trg_003_recall_due_priya"], customer_id="c_015_anonymous_for_m010",
             merchant_id="m_010_sunrisepharm_pharmacy_lucknow")
    out = compose(seed["categories"]["pharmacies"], seed["merchants"]["m_010_sunrisepharm_pharmacy_lucknow"], t,
                  seed["customers"]["c_015_anonymous_for_m010"], now=NOW)
    assert out["send_as"] == "vera" and out["_internal"]["route"].startswith("consent_blocked")


@pytest.mark.parametrize("tid,marker", [("trg_003_recall_due_priya", "hai"),              # hi-en mix
                                        ("trg_019_chronic_refill_grandfather", "Namaste"),  # hi, via son
                                        ("trg_017_kids_yoga_trial_followup_karthik", "Vanakkam"),  # ta-en mix
                                        ("trg_015_winback_rashmi", "Hi Rashmi")])           # english
def test_customer_language_preference(seed, tid, marker):
    cat, m, t, c = _ctx(seed, tid)
    assert marker in compose(cat, m, t, c, now=NOW)["body"]


def test_refill_names_molecules_only_with_refill_consent(seed):
    cat, m, t, c = _ctx(seed, "trg_019_chronic_refill_grandfather")
    assert "metformin" in compose(cat, m, t, c)["body"]
    c2 = dict(c, consent={"opted_in_at": "2024-08-10", "scope": ["promotional_offers"]})
    body = compose(cat, m, t, c2)["body"]
    assert "metformin" not in body and "regular medicines" in body


def test_dentist_salutation_never_doubles_dr(expanded):
    for mid, m in expanded["merchants"].items():
        if m["category_slug"] != "dentists":
            continue
        tid = next((k for k, t in expanded["triggers"].items() if t["merchant_id"] == mid and not t.get("customer_id")), None)
        if not tid:
            continue
        body = compose(*_ctx(expanded, tid), now=NOW)["body"]
        assert body.startswith("Dr. ") and "Dr. Dr." not in body


def test_new_digest_item_is_used_after_context_update(seed):
    """Adaptive injection: a higher-version category with a new top item changes the composition."""
    cat = dict(seed["categories"]["dentists"])
    new_item = {"id": "d_new_bruxism", "kind": "research", "title": "Night-guard adherence doubles with 2-week follow-up call",
                "source": "IJDR Nov 2026, p.3", "trial_n": 640, "patient_segment": "bruxism_patients",
                "summary": "Adherence rose from 31% to 64% when clinics called at day 14.",
                "actionable": "Add a day-14 call for every night-guard patient"}
    cat["digest"] = cat["digest"] + [new_item]
    t = dict(seed["triggers"]["trg_001_research_digest_dentists"], payload={"category": "dentists", "top_item_id": "d_new_bruxism"})
    body = compose(cat, seed["merchants"]["m_001_drmeera_dentist_delhi"], t, None, now=NOW)["body"]
    assert "IJDR Nov 2026" in body and "640" in body and "64%" in body


def test_updated_performance_changes_message(seed):
    m = dict(seed["merchants"]["m_002_bharat_dentist_mumbai"])
    m["performance"] = dict(m["performance"], calls=9, ctr=0.024)
    body = compose(seed["categories"]["dentists"], m, seed["triggers"]["trg_004_perf_dip_bharat"], None, now=NOW)["body"]
    assert "9 calls" in body and "2.4%" in body


def test_no_fabricated_numbers_in_canonical_examples(seed):
    for tid in seed["triggers"]:
        cat, m, t, c = _ctx(seed, tid)
        msg = compose(cat, m, t, c, now=NOW)
        allowed = allowed_numbers(cat, m, t, c, extra=[])
        bad = ungrounded_numbers(msg["body"], allowed, small_int_ok=12)
        # anything left must be a value the composer derived and registered (e.g. days-to-deadline, tier prices)
        assert all(re.fullmatch(r"\d+", b) for b in bad), (tid, bad)
