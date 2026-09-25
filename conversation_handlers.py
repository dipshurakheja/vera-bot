"""Optional multi-turn handler (brief §7.4): respond(state, merchant_message) -> next move.

`state` is a plain dict so it can be used outside the HTTP server:
    {"conversation_id": "...", "category": {...}, "merchant": {...}, "trigger": {...} | None,
     "customer": {...} | None, "from_role": "merchant" | "customer",
     "_engine": <internal, created on first call and reused across turns>}
"""

from __future__ import annotations

from datetime import datetime, timezone

from vera.config import Settings
from vera.service import VeraEngine


def respond(state: dict, merchant_message: str) -> dict:
    engine: VeraEngine = state.get("_engine") or VeraEngine(Settings())
    if "_engine" not in state:
        state["_engine"] = engine
        for scope, key, id_field in (("category", "category", "slug"), ("merchant", "merchant", "merchant_id"),
                                     ("trigger", "trigger", "id"), ("customer", "customer", "customer_id")):
            payload = state.get(key)
            if isinstance(payload, dict) and payload.get(id_field):
                engine.push_context({"scope": scope, "context_id": payload[id_field], "version": 1, "payload": payload})
        trigger = state.get("trigger")
        if isinstance(trigger, dict) and trigger.get("id"):
            tick = engine.tick(datetime.now(timezone.utc).isoformat(), [trigger["id"]])
            if tick["actions"]:
                state["conversation_id"] = tick["actions"][0]["conversation_id"]
                state["opening_message"] = tick["actions"][0]["body"]
    merchant = state.get("merchant") or {}
    customer = state.get("customer") or {}
    return engine.reply({
        "conversation_id": state.get("conversation_id") or "conv_local",
        "merchant_id": merchant.get("merchant_id"),
        "customer_id": customer.get("customer_id"),
        "from_role": state.get("from_role", "merchant"),
        "message": merchant_message,
        "received_at": datetime.now(timezone.utc).isoformat(),
    })
