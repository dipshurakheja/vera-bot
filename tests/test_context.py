"""/v1/context — versioning, validation, size limits, concurrency."""

from __future__ import annotations

import random
import threading

from conftest import clone, push


def test_first_version_accepted(client, seed):
    r = push(client, "category", "dentists", seed["categories"]["dentists"])
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["ack_id"] == "ack_dentists_v1"
    assert body["stored_at"].endswith("Z")


def test_same_version_is_noop_conflict(client, engine, seed):
    m = seed["merchants"]["m_001_drmeera_dentist_delhi"]
    assert push(client, "merchant", m["merchant_id"], m).status_code == 200
    changed = clone(m)
    changed["performance"]["views"] = 99999
    r = push(client, "merchant", m["merchant_id"], changed, version=1)
    assert r.status_code == 409
    assert r.json() == {"accepted": False, "reason": "stale_version", "current_version": 1}
    assert engine.store.get("merchant", m["merchant_id"])["performance"]["views"] == 2410  # untouched


def test_lower_version_does_not_overwrite(client, engine, seed):
    m = seed["merchants"]["m_002_bharat_dentist_mumbai"]
    newer = clone(m)
    newer["performance"]["views"] = 1234
    assert push(client, "merchant", m["merchant_id"], newer, version=5).status_code == 200
    r = push(client, "merchant", m["merchant_id"], m, version=3)
    assert r.status_code == 409 and r.json()["current_version"] == 5
    assert engine.store.get("merchant", m["merchant_id"])["performance"]["views"] == 1234


def test_higher_version_replaces_atomically(client, engine, seed):
    m = seed["merchants"]["m_001_drmeera_dentist_delhi"]
    push(client, "merchant", m["merchant_id"], m, version=1)
    v2 = clone(m)
    v2["performance"]["views"] = 2580
    r = push(client, "merchant", m["merchant_id"], v2, version=2)
    assert r.status_code == 200 and r.json()["ack_id"].endswith("_v2")
    stored = engine.store.get_record("merchant", m["merchant_id"])
    assert stored.version == 2 and stored.payload["performance"]["views"] == 2580


def test_multiple_scopes_counted_in_healthz(client, seed):
    push(client, "category", "salons", seed["categories"]["salons"])
    push(client, "merchant", "m_003_studio11_salon_hyderabad", seed["merchants"]["m_003_studio11_salon_hyderabad"])
    push(client, "customer", "c_005_kavya_for_m003", seed["customers"]["c_005_kavya_for_m003"])
    push(client, "trigger", "trg_008_curious_ask_studio11", seed["triggers"]["trg_008_curious_ask_studio11"])
    assert client.get("/v1/healthz").json()["contexts_loaded"] == {"category": 1, "merchant": 1, "customer": 1,
                                                                    "trigger": 1}


def test_same_id_different_scopes_do_not_collide(client, engine):
    push(client, "merchant", "x1", {"identity": {"name": "A"}})
    push(client, "trigger", "x1", {"kind": "perf_dip"})
    assert engine.store.get("merchant", "x1")["identity"]["name"] == "A"
    assert engine.store.get("trigger", "x1")["kind"] == "perf_dip"


def test_missing_id_field_is_normalised(client, engine):
    push(client, "merchant", "m_new", {"category_slug": "dentists", "identity": {"name": "New Clinic"}})
    assert engine.store.get("merchant", "m_new")["merchant_id"] == "m_new"


def test_invalid_scope(client):
    r = client.post("/v1/context", json={"scope": "shop", "context_id": "a", "version": 1, "payload": {}})
    assert r.status_code == 400 and r.json()["reason"] == "invalid_scope"


def test_invalid_versions(client):
    for bad in (-1, "abc", True, 1.5, None):
        r = client.post("/v1/context", json={"scope": "merchant", "context_id": "a", "version": bad, "payload": {}})
        assert r.status_code == 400, bad
        assert r.json()["reason"] == "invalid_version"


def test_numeric_string_version_is_accepted(client):
    r = client.post("/v1/context", json={"scope": "merchant", "context_id": "a", "version": "7", "payload": {}})
    assert r.status_code == 200 and r.json()["ack_id"] == "ack_a_v7"


def test_missing_fields(client):
    r = client.post("/v1/context", json={"scope": "merchant", "payload": {}})
    assert r.status_code == 400
    assert r.json()["reason"] == "missing_fields"
    assert "context_id" in r.json()["details"] and "version" in r.json()["details"]


def test_payload_must_be_object(client):
    r = client.post("/v1/context", json={"scope": "merchant", "context_id": "a", "version": 1, "payload": [1, 2]})
    assert r.status_code == 400 and r.json()["reason"] == "invalid_payload"


def test_malformed_json(client):
    r = client.post("/v1/context", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["reason"] == "invalid_json"
    r = client.post("/v1/context", content=b"", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_500kb_boundary(client):
    big = {"slug": "big", "blob": "x" * 490_000}
    r = push(client, "category", "big", big)
    assert r.status_code == 200
    too_big = {"slug": "huge", "blob": "x" * 560_000}
    r = push(client, "category", "huge", too_big)
    assert r.status_code == 413 and r.json()["reason"] == "payload_too_large"
    assert client.get("/v1/healthz").status_code == 200  # still alive


def test_concurrent_version_race_keeps_highest(client, engine):
    versions = list(range(1, 33))
    random.Random(7).shuffle(versions)
    errors = []

    def worker(v):
        try:
            r = push(client, "merchant", "m_race", {"identity": {"name": f"v{v}"}}, version=v)
            assert r.status_code in (200, 409)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(v,)) for v in versions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    rec = engine.store.get_record("merchant", "m_race")
    assert rec.version == 32 and rec.payload["identity"]["name"] == "v32"
