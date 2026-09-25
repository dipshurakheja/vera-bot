"""Fuzz regression: corrupted contexts must never crash compose/tick/reply or leak junk into copy."""

from __future__ import annotations

import copy
import random

from conftest import push
from vera.composer.engine import compose
from vera.validation.output import ARTIFACT_RE, validate_reply

JUNK = [None, "", "abc", 0, -5, 3.7, True, [], {}, [1, "x"], {"a": 1}, "2026-13-45", "₹", 10**12, "NaN"]


def _mutate(obj, rnd, depth=0, p=0.25):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if rnd.random() < p / (depth + 1):
                if rnd.random() < 0.2:
                    continue
                out[k] = rnd.choice(JUNK)
            else:
                out[k] = _mutate(v, rnd, depth + 1, p)
        return out
    if isinstance(obj, list):
        return [_mutate(x, rnd, depth + 1, p) for x in obj if rnd.random() > 0.1]
    return obj


def test_compose_survives_corrupted_contexts(expanded):
    rnd = random.Random(42)
    tids = list(expanded["triggers"])
    for _ in range(400):
        t = expanded["triggers"][rnd.choice(tids)]
        m = expanded["merchants"][t["merchant_id"]]
        c = expanded["customers"].get(t.get("customer_id")) if t.get("customer_id") else None
        cat = expanded["categories"][m["category_slug"]]
        args = [_mutate(copy.deepcopy(x), rnd) if x is not None else None for x in (cat, m, t, c)]
        msg = compose(*args, now=rnd.choice(["2026-04-26T10:30:00Z", None, "garbage"]))
        assert msg["body"] and msg["send_as"] in {"vera", "merchant_on_behalf"}
        assert not ARTIFACT_RE.search(msg["body"]), msg["body"]
        assert "[1," not in msg["body"] and "{'a'" not in msg["body"]


def test_engine_survives_corrupted_pushes_and_replies(client, expanded):
    rnd = random.Random(7)
    for scope, data, key in (("category", expanded["categories"], "slug"), ("merchant", expanded["merchants"], "merchant_id"),
                             ("customer", expanded["customers"], "customer_id"), ("trigger", expanded["triggers"], "id")):
        for k, v in list(data.items())[:60]:
            assert push(client, scope, k, _mutate(copy.deepcopy(v), rnd, p=0.15)).status_code == 200
    actions = client.post("/v1/tick", json={"now": "2026-04-26T10:30:00Z",
                                            "available_triggers": list(expanded["triggers"])}).json()["actions"]
    msgs = ["yes", "no", "stop", "?", "", "1", "haan", "in 5 mins", "gst?", "CONFIRM", "नमस्ते", "x" * 3000]
    for a in actions:
        for i in range(3):
            r = client.post("/v1/reply", json={"conversation_id": a["conversation_id"], "merchant_id": a["merchant_id"],
                                               "customer_id": a["customer_id"], "message": rnd.choice(msgs),
                                               "from_role": "customer" if a["customer_id"] else "merchant",
                                               "received_at": rnd.choice(["2026-04-26T11:00:00Z", None]), "turn_number": i + 2})
            assert r.status_code == 200 and not validate_reply(r.json())
