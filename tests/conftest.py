"""Shared fixtures: settings, app client, and the seed + expanded datasets (generated into tmp, never hard-coded)."""

from __future__ import annotations

import copy
import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.api.app import create_app  # noqa: E402
from vera.config import Settings  # noqa: E402
from vera.service import VeraEngine  # noqa: E402

DATASET = ROOT / "dataset"


def _load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as fp:
        return json.load(fp)


@pytest.fixture(scope="session")
def seed() -> dict:
    cats = {}
    for f in sorted((DATASET / "categories").glob("*.json")):
        d = _load_json(f)
        cats[d["slug"]] = d
    return {
        "categories": cats,
        "merchants": {m["merchant_id"]: m for m in _load_json(DATASET / "merchants_seed.json")["merchants"]},
        "customers": {c["customer_id"]: c for c in _load_json(DATASET / "customers_seed.json")["customers"]},
        "triggers": {t["id"]: t for t in _load_json(DATASET / "triggers_seed.json")["triggers"]},
    }


@pytest.fixture(scope="session")
def expanded(seed) -> dict:
    """Run the official generator in-process (same fixed seed) to get the 50/200/100 dataset + test pairs."""
    spec = importlib.util.spec_from_file_location("generate_dataset", DATASET / "generate_dataset.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    rnd = random.Random(gen.SEED)
    cats, m_seeds, c_seeds, t_seeds = gen.load_seeds(DATASET)
    merchants = gen.expand_merchants(m_seeds, rnd)
    customers = gen.expand_customers(c_seeds, merchants, rnd)
    triggers = gen.expand_triggers(t_seeds, merchants, customers, rnd)
    by_kind: dict = {}
    for t in triggers:
        by_kind.setdefault(t["kind"], []).append(t)
    pairs = []
    for kind, ts in sorted(by_kind.items()):
        for t in ts[:2]:
            pairs.append({"trigger_id": t["id"], "merchant_id": t["merchant_id"], "customer_id": t.get("customer_id")})
    return {
        "categories": cats,
        "merchants": {m["merchant_id"]: m for m in merchants},
        "customers": {c["customer_id"]: c for c in customers},
        "triggers": {t["id"]: t for t in triggers},
        "pairs": pairs[:30],
    }


@pytest.fixture()
def settings() -> Settings:
    return Settings(log_level="WARNING")


@pytest.fixture()
def engine(settings) -> VeraEngine:
    return VeraEngine(settings)


@pytest.fixture()
def client(settings, engine) -> TestClient:
    return TestClient(create_app(settings, engine))


def push(client_or_engine, scope: str, cid: str, payload: dict, version: int = 1):
    body = {"scope": scope, "context_id": cid, "version": version, "payload": payload,
            "delivered_at": "2026-04-26T10:00:00Z"}
    if isinstance(client_or_engine, VeraEngine):
        return client_or_engine.push_context(body)
    return client_or_engine.post("/v1/context", json=body)


def push_dataset(target, data: dict, customers: bool = True, triggers: bool = True) -> None:
    for slug, c in data["categories"].items():
        push(target, "category", slug, c)
    for mid, m in data["merchants"].items():
        push(target, "merchant", mid, m)
    if customers:
        for cid, c in data["customers"].items():
            push(target, "customer", cid, c)
    if triggers:
        for tid, t in data["triggers"].items():
            push(target, "trigger", tid, t)


@pytest.fixture()
def loaded(client, seed):
    push_dataset(client, seed)
    return client


def clone(d: dict) -> dict:
    return copy.deepcopy(d)


NOW = "2026-04-26T10:30:00Z"
QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about"]
ACTION_WORDS = ["done", "sending", "draft", "here", "confirm", "proceed", "next", "on it"]
