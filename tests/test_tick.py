"""/v1/tick — decisions, gates, suppression, caps, specificity and category handling."""

from __future__ import annotations

import re

from conftest import NOW, clone, push

REQUIRED = {"conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
            "template_params", "body", "cta", "suppression_key", "rationale"}


def tick(client, triggers, now=NOW):
    r = client.post("/v1/tick", json={"now": now, "available_triggers": triggers})
    assert r.status_code == 200
    return r.json()["actions"]


def test_valid_trigger_produces_complete_action(loaded, seed):
    actions = tick(loaded, ["trg_001_research_digest_dentists"])
    assert len(actions) == 1
    a = actions[0]
    assert REQUIRED <= set(a)
    assert a["send_as"] == "vera" and a["customer_id"] is None
    assert a["suppression_key"] == "research:dentists:2026-W17"
    assert a["conversation_id"].startswith("conv_m_001_drmeera_research_digest")
    assert isinstance(a["template_params"], list) and all(isinstance(p, str) and p for p in a["template_params"])
    assert a["body"].strip() and a["rationale"].strip()


def test_unknown_trigger_and_empty_list(loaded):
    assert tick(loaded, ["trg_does_not_exist"]) == []
    assert tick(loaded, []) == []


def test_missing_merchant_context_means_no_send(client, seed):
    push(client, "category", "dentists", seed["categories"]["dentists"])
    push(client, "trigger", "trg_001_research_digest_dentists", seed["triggers"]["trg_001_research_digest_dentists"])
    assert tick(client, ["trg_001_research_digest_dentists"]) == []


def test_missing_customer_context_means_no_send(client, seed):
    for slug, c in seed["categories"].items():
        push(client, "category", slug, c)
    push(client, "merchant", "m_001_drmeera_dentist_delhi", seed["merchants"]["m_001_drmeera_dentist_delhi"])
    push(client, "trigger", "trg_003_recall_due_priya", seed["triggers"]["trg_003_recall_due_priya"])
    assert tick(client, ["trg_003_recall_due_priya"]) == []


def test_suppression_prevents_resend(loaded):
    assert len(tick(loaded, ["trg_018_supply_atorvastatin_recall"])) == 1
    assert tick(loaded, ["trg_018_supply_atorvastatin_recall"], now="2026-04-26T12:00:00Z") == []


def test_materially_changed_trigger_can_resend(loaded, seed):
    tid = "trg_018_supply_atorvastatin_recall"
    assert len(tick(loaded, [tid])) == 1
    same_again = push(loaded, "trigger", tid, seed["triggers"][tid], version=2)  # new version, same payload
    assert same_again.status_code == 200
    assert tick(loaded, [tid], now="2026-04-26T12:00:00Z") == []
    changed = clone(seed["triggers"][tid])
    changed["payload"]["affected_batches"].append("AT2024-1115")
    push(loaded, "trigger", tid, changed, version=3)
    again = tick(loaded, [tid], now="2026-04-26T13:00:00Z")
    assert len(again) == 1 and "AT2024-1115" in again[0]["body"]


def test_unrelated_future_trigger_not_suppressed(loaded):
    assert len(tick(loaded, ["trg_024_perf_spike_zen"])) == 1
    assert len(tick(loaded, ["trg_017_kids_yoga_trial_followup_karthik"], now="2026-04-26T10:35:00Z")) == 1


def test_twenty_action_cap(client, seed):
    for slug, c in seed["categories"].items():
        push(client, "category", slug, c)
    base_m = seed["merchants"]["m_003_studio11_salon_hyderabad"]
    base_t = seed["triggers"]["trg_008_curious_ask_studio11"]
    ids = []
    for i in range(30):
        m = clone(base_m)
        m["merchant_id"] = f"m_cap_{i:02d}"
        push(client, "merchant", m["merchant_id"], m)
        t = clone(base_t)
        t.update(id=f"trg_cap_{i:02d}", merchant_id=m["merchant_id"], suppression_key=f"cap:{i}")
        push(client, "trigger", t["id"], t)
        ids.append(t["id"])
    actions = tick(client, ids)
    assert len(actions) == 20
    assert len({a["conversation_id"] for a in actions}) == 20


def test_one_merchant_message_per_tick_picks_highest_priority(loaded):
    actions = tick(loaded, ["trg_001_research_digest_dentists", "trg_002_compliance_dci_radiograph",
                            "trg_022_cde_webinar_dentists"])
    assert [a["trigger_id"] for a in actions] == ["trg_002_compliance_dci_radiograph"]  # urgency 4 wins


def test_cooldown_then_release_after_conversation_ends(loaded):
    first = tick(loaded, ["trg_002_compliance_dci_radiograph"])
    assert len(first) == 1
    assert tick(loaded, ["trg_001_research_digest_dentists"], now="2026-04-26T10:35:00Z") == []  # still active
    r = loaded.post("/v1/reply", json={"conversation_id": first[0]["conversation_id"], "merchant_id": first[0]["merchant_id"],
                                       "from_role": "merchant", "message": "No thanks, not interested",
                                       "received_at": "2026-04-26T10:36:00Z", "turn_number": 2})
    assert r.json()["action"] == "end"
    # reject sets a 24h quiet window for low-urgency pitches
    assert tick(loaded, ["trg_001_research_digest_dentists"], now="2026-04-26T11:00:00Z") == []
    assert len(tick(loaded, ["trg_001_research_digest_dentists"], now="2026-04-27T12:00:00Z")) == 1


def test_customer_facing_send_as_and_consent(loaded):
    actions = tick(loaded, ["trg_003_recall_due_priya"])
    assert len(actions) == 1
    a = actions[0]
    assert a["send_as"] == "merchant_on_behalf" and a["customer_id"] == "c_001_priya_for_m001"
    assert a["cta"] == "multi_choice_slot"
    assert "Wed 5 Nov, 6pm" in a["body"] and "₹299" in a["body"]


def test_customer_without_consent_is_never_contacted(loaded, seed):
    t = clone(seed["triggers"]["trg_003_recall_due_priya"])
    t.update(id="trg_recall_anon", merchant_id="m_010_sunrisepharm_pharmacy_lucknow",
             customer_id="c_015_anonymous_for_m010", suppression_key="recall:anon")
    push(loaded, "trigger", t["id"], t)
    assert tick(loaded, ["trg_recall_anon"]) == []


def test_reminder_opt_out_blocks_reminders(loaded, seed):
    c = clone(seed["customers"]["c_001_priya_for_m001"])
    c["preferences"]["reminder_opt_in"] = False
    push(loaded, "customer", c["customer_id"], c, version=2)
    assert tick(loaded, ["trg_003_recall_due_priya"]) == []


def test_trigger_not_relevant_to_category_is_skipped(loaded, seed):
    t = clone(seed["triggers"]["trg_006_festival_diwali"])
    t.update(id="trg_diwali_dentist", merchant_id="m_001_drmeera_dentist_delhi", suppression_key="festival:dentist")
    push(loaded, "trigger", t["id"], t)  # payload.category_relevance excludes dentists
    assert tick(loaded, ["trg_diwali_dentist"]) == []


def test_expired_trigger_policy(client, seed, settings):
    from fastapi.testclient import TestClient

    from conftest import push_dataset
    from vera.api.app import create_app
    from vera.config import Settings

    push_dataset(client, seed)
    late = "2026-06-01T10:00:00Z"  # after trg_001 expiry (2026-05-03)
    assert len(tick(client, ["trg_001_research_digest_dentists"], now=late)) == 1  # listed as active -> trusted
    strict = TestClient(create_app(Settings(log_level="WARNING", enforce_trigger_expiry=True)))
    push_dataset(strict, seed)
    assert tick(strict, ["trg_001_research_digest_dentists"], now=late) == []


def test_specificity_grounded_numbers(loaded):
    body = tick(loaded, ["trg_001_research_digest_dentists"])[0]["body"]
    assert "2,100" in body and "38%" in body and "JIDA" in body and "124" in body
    body = tick(loaded, ["trg_004_perf_dip_bharat"])[0]["body"]
    assert "50%" in body and "12" in body and "1.8%" in body


def test_category_voice(loaded):
    actions = tick(loaded, ["trg_001_research_digest_dentists", "trg_024_perf_spike_zen", "trg_018_supply_atorvastatin_recall",
                            "trg_010_ipl_match_delhi", "trg_008_curious_ask_studio11"])
    by_merchant = {a["merchant_id"]: a["body"] for a in actions}
    assert by_merchant["m_001_drmeera_dentist_delhi"].startswith("Dr. Meera,")
    assert "Hi Padma" in by_merchant["m_008_zenyoga_gym_chennai"]
    assert "AT2024-1102" in by_merchant["m_009_apollo_pharmacy_jaipur"]
    for body in by_merchant.values():
        low = body.lower()
        for taboo in ("guaranteed", "miracle", "100% safe", "best in city"):
            assert taboo not in low
        assert body.count("?") <= 1
        assert not re.search(r"https?://|www\.", body)


def test_invalid_tick_requests(client):
    r = client.post("/v1/tick", json={"now": NOW, "available_triggers": "trg_1"})
    assert r.status_code == 400 and r.json()["actions"] == []
    r = client.post("/v1/tick", content=b"{bad", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["actions"] == []
    r = client.post("/v1/tick", json={"now": "yesterday", "available_triggers": []})
    assert r.status_code == 400


def test_tick_without_now_uses_server_clock(loaded):
    r = loaded.post("/v1/tick", json={"available_triggers": ["trg_024_perf_spike_zen"]})
    assert r.status_code == 200 and len(r.json()["actions"]) == 1
