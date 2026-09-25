"""Same inputs + same conversation state -> same outputs."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from conftest import NOW, push_dataset
from vera.api.app import create_app
from vera.composer.engine import compose
from vera.config import Settings


def test_compose_is_stable_across_calls(expanded):
    for tid, t in list(expanded["triggers"].items())[::7]:
        m = expanded["merchants"][t["merchant_id"]]
        c = expanded["customers"].get(t.get("customer_id")) if t.get("customer_id") else None
        cat = expanded["categories"][m["category_slug"]]
        outs = {json.dumps(compose(cat, m, t, c, now=NOW), sort_keys=True, ensure_ascii=False) for _ in range(5)}
        assert len(outs) == 1, tid


def _fresh_client(seed):
    c = TestClient(create_app(Settings(log_level="WARNING")))
    push_dataset(c, seed)
    return c


def test_tick_is_deterministic_across_fresh_engines(seed):
    a, b = _fresh_client(seed), _fresh_client(seed)
    ids = list(seed["triggers"])
    ra = a.post("/v1/tick", json={"now": NOW, "available_triggers": ids}).json()
    rb = b.post("/v1/tick", json={"now": NOW, "available_triggers": ids}).json()
    assert ra == rb and ra["actions"]


def test_trigger_order_does_not_change_decisions(seed):
    a, b = _fresh_client(seed), _fresh_client(seed)
    ids = list(seed["triggers"])
    ra = a.post("/v1/tick", json={"now": NOW, "available_triggers": ids}).json()["actions"]
    rb = b.post("/v1/tick", json={"now": NOW, "available_triggers": list(reversed(ids))}).json()["actions"]
    assert sorted(x["trigger_id"] for x in ra) == sorted(x["trigger_id"] for x in rb)


def test_reply_sequence_is_deterministic(seed):
    transcripts = []
    for _ in range(2):
        c = _fresh_client(seed)
        act = c.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_001_research_digest_dentists"]}).json()["actions"][0]
        out = []
        for i, msg in enumerate(["what is the source?", "yes", "confirm", "thanks"], start=2):
            out.append(c.post("/v1/reply", json={"conversation_id": act["conversation_id"], "merchant_id": act["merchant_id"],
                                                 "from_role": "merchant", "message": msg,
                                                 "received_at": "2026-04-26T10:45:00Z", "turn_number": i}).json())
        transcripts.append(out)
    assert transcripts[0] == transcripts[1]
