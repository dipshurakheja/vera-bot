"""/v1/reply — intents, state machine, auto-reply handling, exits, customer flows."""

from __future__ import annotations

import pytest

from conftest import ACTION_WORDS, NOW, QUALIFYING

M1 = "m_001_drmeera_dentist_delhi"


def reply(client, conv, msg, turn=2, merchant=M1, role="merchant", customer=None, at="2026-04-26T10:45:00Z"):
    r = client.post("/v1/reply", json={"conversation_id": conv, "merchant_id": merchant, "customer_id": customer,
                                       "from_role": role, "message": msg, "received_at": at, "turn_number": turn})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["action"] in {"send", "wait", "end"} and out["rationale"]
    if out["action"] == "send":
        assert out["body"].strip() and out["cta"]
    if out["action"] == "wait":
        assert isinstance(out["wait_seconds"], int) and out["wait_seconds"] > 0
    return out


def start(client, trigger_id):
    r = client.post("/v1/tick", json={"now": NOW, "available_triggers": [trigger_id]}).json()["actions"]
    assert r, f"no action for {trigger_id}"
    return r[0]


def test_accept_switches_to_action_mode(loaded):
    a = start(loaded, "trg_001_research_digest_dentists")
    out = reply(loaded, a["conversation_id"], "Yes please send the abstract. Also draft the patient WhatsApp.")
    assert out["action"] == "send"
    low = out["body"].lower()
    assert any(w in low for w in ACTION_WORDS)
    assert not any(q in low for q in QUALIFYING)
    assert "JIDA" in out["body"] and out["cta"] == "binary_confirm_cancel"


def test_intent_transition_on_unknown_conversation_uses_merchant_history(loaded):
    out = reply(loaded, "conv_intent_1", "Ok lets do it. Whats next?")
    low = out["body"].lower()
    assert out["action"] == "send"
    assert any(w in low for w in ACTION_WORDS) and not any(q in low for q in QUALIFYING)
    assert "whitening" in low and "aligners" in low  # the merchant's own earlier request


@pytest.mark.parametrize("msg", ["Not interested.", "no thanks", "nahi chahiye"])
def test_reject_ends(loaded, msg):
    a = start(loaded, "trg_024_perf_spike_zen")
    assert reply(loaded, a["conversation_id"], msg, merchant=a["merchant_id"])["action"] == "end"


def test_stop_ends_and_suppresses_future_ticks(loaded):
    a = start(loaded, "trg_002_compliance_dci_radiograph")
    assert reply(loaded, a["conversation_id"], "Stop messaging me. This is useless spam.")["action"] == "end"
    later = loaded.post("/v1/tick", json={"now": "2026-04-28T10:00:00Z",
                                          "available_triggers": ["trg_001_research_digest_dentists"]}).json()
    assert later["actions"] == []


@pytest.mark.parametrize("msg,min_wait", [("call me tomorrow", 3600), ("busy right now, later", 60),
                                          ("in 2 hours please", 7200), ("not now", 3600)])
def test_postpone_waits(loaded, msg, min_wait):
    a = start(loaded, "trg_024_perf_spike_zen")
    out = reply(loaded, a["conversation_id"], msg, merchant=a["merchant_id"])
    assert out["action"] == "wait" and out["wait_seconds"] >= min_wait


def test_auto_reply_same_conversation_send_wait_end(loaded):
    a = start(loaded, "trg_004_perf_dip_bharat")
    canned = "Thank you for contacting Bharat Dental Care! Our team will respond shortly."
    actions = [reply(loaded, a["conversation_id"], canned, turn=i, merchant=a["merchant_id"])["action"]
               for i in range(2, 5)]
    assert actions == ["send", "wait", "end"]


def test_auto_reply_across_conversations_same_merchant(loaded):
    canned = "Thank you for contacting us! Our team will respond shortly."
    seen = [reply(loaded, f"conv_auto_{i}", canned, turn=i + 1)["action"] for i in range(1, 5)]
    assert seen[:3] == ["send", "wait", "end"]


def test_repeated_identical_message_is_treated_as_auto_reply(loaded):
    a = start(loaded, "trg_024_perf_spike_zen")
    msg = "We are open 9 to 9 all days, visit us anytime"
    results = [reply(loaded, a["conversation_id"], msg, turn=i, merchant=a["merchant_id"])["action"] for i in range(2, 7)]
    assert results[-1] == "end" and "send" in results


def test_off_topic_is_declined_and_redirected(loaded):
    a = start(loaded, "trg_001_research_digest_dentists")
    out = reply(loaded, a["conversation_id"], "Btw can you also help me with my GST filing this month?")
    assert out["action"] == "send" and "CA" in out["body"] and "YES" in out["body"]


def test_hostile_then_off_topic_then_exit(loaded):
    a = start(loaded, "trg_023_competitor_opened_dentist")
    first = reply(loaded, a["conversation_id"], "Why are you bothering me. This is useless.")
    assert first["action"] == "send" and "sorry" in first["body"].lower() and first["cta"] == "none"
    second = reply(loaded, a["conversation_id"], "can you help me file my GST?", turn=3)
    assert second["action"] == "send" and "CA" in second["body"]
    third = reply(loaded, a["conversation_id"], "this is rubbish, idiot", turn=4)
    assert third["action"] == "end"


def test_ambiguous_clarifies_once_then_waits(loaded):
    a = start(loaded, "trg_024_perf_spike_zen")
    assert reply(loaded, a["conversation_id"], "hmm", merchant=a["merchant_id"])["action"] == "send"
    assert reply(loaded, a["conversation_id"], "hmm", turn=3, merchant=a["merchant_id"])["action"] == "wait"


def test_question_answered_from_context(loaded):
    a = start(loaded, "trg_001_research_digest_dentists")
    out = reply(loaded, a["conversation_id"], "What is the source of this?")
    assert out["action"] == "send" and "JIDA Oct 2026" in out["body"]


def test_full_multi_turn_flow_without_repetition(loaded):
    a = start(loaded, "trg_004_perf_dip_bharat")
    conv, mid = a["conversation_id"], a["merchant_id"]
    steps = [reply(loaded, conv, "yes go ahead", 2, mid), reply(loaded, conv, "confirm", 3, mid),
             reply(loaded, conv, "yes", 4, mid), reply(loaded, conv, "confirm", 5, mid), reply(loaded, conv, "thanks", 6, mid)]
    assert [s["action"] for s in steps] == ["send", "send", "send", "send", "end"]
    bodies = [a["body"]] + [s["body"] for s in steps if s["action"] == "send"]
    assert len(bodies) == len(set(bodies))
    assert "Dental Cleaning @ ₹299" in steps[0]["body"]  # the offer proposed in the opener
    assert steps[1]["body"].startswith("Done")


def test_customer_slot_booking_in_hinglish(loaded):
    a = start(loaded, "trg_003_recall_due_priya")
    out = reply(loaded, a["conversation_id"], "2", role="customer", customer=a["customer_id"])
    assert out["action"] == "send" and "Thu 6 Nov, 5pm" in out["body"] and "book" in out["body"]
    assert reply(loaded, a["conversation_id"], "thank you", 3, role="customer", customer=a["customer_id"])["action"] == "end"


def test_customer_stop(loaded):
    a = start(loaded, "trg_015_winback_rashmi")
    out = reply(loaded, a["conversation_id"], "STOP", role="customer", customer=a["customer_id"], merchant=a["merchant_id"])
    assert out["action"] == "end"


def test_refill_confirm(loaded):
    a = start(loaded, "trg_019_chronic_refill_grandfather")
    out = reply(loaded, a["conversation_id"], "CONFIRM", role="customer", customer=a["customer_id"],
                merchant=a["merchant_id"])
    assert out["action"] == "send" and "Confirmed" in out["body"]


def test_unknown_conversation_unknown_merchant_still_valid(client):
    out = reply(client, "conv_ghost", "hello?", merchant="m_unknown")
    assert out["action"] in {"send", "wait", "end"}


def test_ended_conversation_stays_closed(loaded):
    a = start(loaded, "trg_024_perf_spike_zen")
    reply(loaded, a["conversation_id"], "stop", merchant=a["merchant_id"])
    assert reply(loaded, a["conversation_id"], "ok", 3, merchant=a["merchant_id"])["action"] == "end"


@pytest.mark.parametrize("body,status", [
    ({"merchant_id": M1, "from_role": "merchant", "message": "hi"}, 400),
    ({"conversation_id": "c1", "from_role": "merchant"}, 400),
    ({"conversation_id": "c1", "from_role": "robot", "message": "hi"}, 400),
    ({"conversation_id": "c1", "message": "hi", "turn_number": "two"}, 400),
    ({"conversation_id": "c1", "message": "hi"}, 200),
])
def test_reply_request_validation(client, body, status):
    assert client.post("/v1/reply", json=body).status_code == status


def test_reply_malformed_json(client):
    r = client.post("/v1/reply", content=b"[oops", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
