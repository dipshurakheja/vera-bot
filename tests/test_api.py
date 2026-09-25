"""Operational endpoints and failure behaviour."""

from __future__ import annotations

from conftest import NOW


def test_healthz_shape(client):
    r = client.get("/v1/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert isinstance(body["uptime_seconds"], int)
    assert body["contexts_loaded"] == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    assert r.headers.get("x-request-id")


def test_healthz_after_full_warmup(client, expanded):
    from conftest import push_dataset

    push_dataset(client, expanded, triggers=False)
    assert client.get("/v1/healthz").json()["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200,
                                                                    "trigger": 0}


def test_metadata_shape(client):
    body = client.get("/v1/metadata").json()
    for key in ("team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"):
        assert key in body
    assert isinstance(body["team_members"], list)


def test_metadata_from_env(monkeypatch):
    from vera.config import Settings

    monkeypatch.setenv("TEAM_NAME", "Team X")
    monkeypatch.setenv("TEAM_MEMBERS", "A, B")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    s = Settings.from_env()
    assert s.team_name == "Team X" and s.team_members == ("A", "B")
    assert s.llm_enabled and "sk-test" not in repr(s)


def test_unknown_route_and_method(client):
    assert client.get("/v1/nope").status_code == 404
    assert client.get("/v1/tick").status_code == 405


def test_teardown_wipes_state(loaded):
    loaded.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_024_perf_spike_zen"]})
    assert loaded.post("/v1/teardown").json()["wiped"] is True
    assert loaded.get("/v1/healthz").json()["contexts_loaded"]["merchant"] == 0


def test_tick_internal_error_degrades_to_empty(client, engine, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(engine, "tick", boom)
    r = client.post("/v1/tick", json={"now": NOW, "available_triggers": ["x"]})
    assert r.status_code == 200 and r.json() == {"actions": []}
    assert "secret" not in r.text


def test_reply_internal_error_degrades_to_wait(client, engine, monkeypatch):
    monkeypatch.setattr(engine, "reply", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("secret")))
    r = client.post("/v1/reply", json={"conversation_id": "c", "message": "hi"})
    assert r.status_code == 200 and r.json()["action"] == "wait" and "secret" not in r.text


def test_strategy_crash_falls_back_to_safe_message(seed, monkeypatch):
    from vera.composer import engine as composer_engine

    def broken(_ci):
        raise ZeroDivisionError

    monkeypatch.setitem(composer_engine.MERCHANT_STRATEGIES, "perf_spike", broken)
    msg = composer_engine.compose(seed["categories"]["gyms"], seed["merchants"]["m_008_zenyoga_gym_chennai"],
                                  seed["triggers"]["trg_024_perf_spike_zen"])
    assert msg["_internal"]["route"] == "safe_minimal" and msg["body"] and msg["cta"] == "binary_yes_no"


def test_latency_budget(loaded, seed):
    import time

    started = time.perf_counter()
    r = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": list(seed["triggers"])})
    assert r.status_code == 200
    assert time.perf_counter() - started < 5.0  # far inside the 30 s budget
