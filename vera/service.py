"""VeraEngine — the stateful service behind the HTTP API.

Owns the versioned context store, the engine state (suppression, conversations,
recipient engagement), the optional LLM polisher, and the three operations the
judge drives: context pushes, ticks and replies.
"""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from typing import Any, Optional

from .composer.engine import build_input, compose, public_view
from .config import Settings
from .context.views import CategoryView, MerchantView
from .conversation import manager
from .decision.planner import Skip, evaluate, select
from .llm.polish import Polisher
from .llm.provider import build_provider
from .store.context_store import SCOPES, ContextStore
from .store.state_store import Conversation, EngineState, Turn
from .utils.logging import get_logger, log_event
from .utils.timeutil import iso_now, parse_iso, utcnow
from .validation.output import allowed_numbers, validate_message, validate_reply

log = get_logger("engine")

_ID_FIELD = {"merchant": "merchant_id", "customer": "customer_id", "trigger": "id", "category": "slug"}


class ContextError(ValueError):
    def __init__(self, reason: str, details: str) -> None:
        super().__init__(details)
        self.reason, self.details = reason, details


def _short(identifier: str, parts: int = 3) -> str:
    return "_".join(str(identifier or "x").split("_")[:parts])


class VeraEngine:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or Settings.from_env()
        self.store = ContextStore()
        self.state = EngineState()
        self.polisher = Polisher(build_provider(self.settings))
        self.started = time.time()
        self._tick_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="vera-llm") if self.polisher.enabled else None

    # ------------------------------------------------------------------ context
    def push_context(self, body: Any) -> tuple[int, dict]:
        try:
            scope, cid, version, payload, delivered_at = self._validate_context(body)
        except ContextError as exc:
            log_event(log, "context_rejected", reason=exc.reason)
            return 400, {"accepted": False, "reason": exc.reason, "details": exc.details}
        id_field = _ID_FIELD[scope]
        if not payload.get(id_field):
            payload = {**payload, id_field: cid}  # normalise without mutating the caller's object
        result = self.store.upsert(scope, cid, version, payload, delivered_at)
        if not result.accepted:
            log_event(log, "context_stale", scope=scope, context_id=cid, version=version,
                      current_version=result.current_version)
            return 409, {"accepted": False, "reason": "stale_version", "current_version": result.current_version}
        log_event(log, "context_stored", scope=scope, context_id=cid, version=version)
        return 200, {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": result.record.stored_at}

    @staticmethod
    def _validate_context(body: Any) -> tuple[str, str, int, dict, str]:
        if not isinstance(body, dict):
            raise ContextError("invalid_body", "request body must be a JSON object")
        missing = [k for k in ("scope", "context_id", "version", "payload") if k not in body]
        if missing:
            raise ContextError("missing_fields", f"missing required field(s): {', '.join(missing)}")
        scope = body.get("scope")
        if not isinstance(scope, str) or scope not in SCOPES:
            raise ContextError("invalid_scope", f"scope must be one of {list(SCOPES)}")
        cid = body.get("context_id")
        if not isinstance(cid, str) or not cid.strip() or len(cid) > 256:
            raise ContextError("invalid_context_id", "context_id must be a non-empty string (max 256 chars)")
        version = body.get("version")
        if isinstance(version, str) and version.strip().isdigit():
            version = int(version.strip())
        if isinstance(version, float) and version.is_integer():
            version = int(version)
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise ContextError("invalid_version", "version must be a non-negative integer")
        payload = body.get("payload")
        if not isinstance(payload, dict):
            raise ContextError("invalid_payload", "payload must be a JSON object")
        delivered_at = body.get("delivered_at") or ""
        if not isinstance(delivered_at, str):
            raise ContextError("invalid_delivered_at", "delivered_at must be an ISO-8601 string")
        return scope, cid.strip(), version, payload, delivered_at

    # ------------------------------------------------------------------ tick
    def tick(self, now_raw: Any, available: list[str]) -> dict:
        now = parse_iso(now_raw) or utcnow()
        started = time.perf_counter()
        with self._tick_lock:
            candidates, skips = evaluate(self.store, self.state, available, now, self.settings.enforce_trigger_expiry,
                                         self.settings.merchant_cooldown_minutes)
            chosen, deferred = select(candidates, self.settings.max_actions_per_tick)
            drafts = []
            for c in chosen:
                try:
                    used = frozenset(self.state.recipient(c.merchant_id).digest_items_sent)
                    msg = compose(c.category, c.merchant, c.trigger, c.customer, now=now, used_digest_ids=used)
                except Exception as exc:  # never let one trigger break the tick
                    log_event(log, "compose_failed", trigger_id=c.trigger_id, error_type=type(exc).__name__)
                    continue
                route = msg["_internal"]["route"]
                if route == "safe_minimal" or route.startswith("consent_blocked"):
                    skips.append(Skip(c.trigger_id, f"low_value_route:{route}"))
                    continue
                drafts.append((c, msg))
            if self.polisher.enabled and drafts:
                self._polish(drafts)
            actions = [self._commit(c, msg, now) for c, msg in drafts]
        for s in skips + deferred:
            log_event(log, "tick_skip", trigger_id=s.trigger_id, reason=s.reason)
        log_event(log, "tick_done", available=len(available), candidates=len(candidates), actions=len(actions),
                  latency_ms=int((time.perf_counter() - started) * 1000))
        return {"actions": actions}

    def _polish(self, drafts: list) -> None:
        budget = self.settings.tick_llm_budget_s
        futures = {}
        for idx, (c, msg) in enumerate(drafts):
            cat = CategoryView(c.category)
            facts = {"merchant": MerchantView(c.merchant).name, "kind": c.trigger.get("kind"),
                     "send_as": msg["send_as"]}
            futures[self._pool.submit(self.polisher.polish, msg["body"], facts, cat.taboos)] = idx
        done, _ = wait(futures, timeout=budget)
        for fut in done:
            idx = futures[fut]
            c, msg = drafts[idx]
            try:
                body, source = fut.result()
            except Exception:
                continue
            if source.startswith("llm"):
                candidate = dict(msg, body=body)
                allowed = allowed_numbers(c.category, c.merchant, c.trigger, c.customer)
                if not validate_message(candidate, CategoryView(c.category).taboos, allowed):
                    msg["body"] = body
                    msg["rationale"] += " (Polished by LLM; facts validated.)"
            msg["_internal"]["polish"] = source

    def _commit(self, c, msg: dict, now: datetime) -> dict:
        internal = msg["_internal"]
        kind = str(c.trigger.get("kind") or "trigger")
        who = _short(c.customer_id, 3) if c.customer_id else _short(c.merchant_id, 3)
        digest = hashlib.sha256(f"{c.suppression_key}|{c.trigger_id}".encode("utf-8")).hexdigest()[:6]
        conv_id = f"conv_{who}_{kind}_{digest}"
        n = 2
        while self.state.has_conversation(conv_id):
            conv_id = f"conv_{who}_{kind}_{digest}_{n}"
            n += 1
        pub = public_view(msg)
        action = {
            "conversation_id": conv_id,
            "merchant_id": c.merchant_id,
            "customer_id": c.customer_id,
            "send_as": pub["send_as"],
            "trigger_id": c.trigger_id,
            "template_name": pub["template_name"],
            "template_params": pub["template_params"],
            "body": pub["body"],
            "cta": pub["cta"],
            "suppression_key": pub["suppression_key"],
            "rationale": pub["rationale"],
        }
        with self.state.lock:
            conv = Conversation(conversation_id=conv_id, merchant_id=c.merchant_id, customer_id=c.customer_id,
                                trigger_id=c.trigger_id, kind=kind, send_as=pub["send_as"], created_at=iso_now(),
                                next_action=dict(internal.get("next_action") or {}),
                                meta=dict(internal.get("meta") or {}, trigger_version=c.record.version))
            conv.turns.append(Turn("bot", pub["body"], now.isoformat(), action="send"))
            conv.sent_bodies.add(pub["body"])
            conv.bot_sends = 1
            self.state.put_conversation(conv)
            self.state.suppress(c.suppression_key, c.trigger_id, c.record.version, c.record.digest, conv_id,
                                c.trigger.get("expires_at") if not c.expired else None)
            rs = self.state.recipient(c.merchant_id)
            rs.last_proactive_at = now
            if conv.meta.get("digest_id"):
                rs.digest_items_sent.add(str(conv.meta["digest_id"]))
        log_event(log, "tick_action", trigger_id=c.trigger_id, merchant_id=c.merchant_id, kind=kind,
                  route=internal.get("route"), score=c.score, send_as=pub["send_as"], conversation_id=conv_id,
                  suppression_key=c.suppression_key, expired=c.expired, polish=internal.get("polish", "off"))
        return action

    # ------------------------------------------------------------------ reply
    def reply(self, req: dict) -> dict:
        received = parse_iso(req.get("received_at")) or utcnow()
        conv_id = str(req["conversation_id"])
        from_role = str(req.get("from_role") or "merchant").lower()
        message = str(req.get("message") or "")
        with self.state.lock:
            conv = self.state.conversation(conv_id)
            if conv is None:
                is_customer = from_role == "customer"
                conv = Conversation(conversation_id=conv_id, merchant_id=req.get("merchant_id"),
                                    customer_id=req.get("customer_id"), trigger_id=None, kind="inbound",
                                    send_as="merchant_on_behalf" if is_customer else "vera", created_at=iso_now(),
                                    origin="inbound")
                self.state.put_conversation(conv)
            if not conv.merchant_id and req.get("merchant_id"):
                conv.merchant_id = req.get("merchant_id")
            if not conv.customer_id and req.get("customer_id"):
                conv.customer_id = req.get("customer_id")
            merchant = self.store.get("merchant", conv.merchant_id) or {}
            slug = MerchantView(merchant).category_slug
            ci = build_input(self.store.get("category", slug), merchant, self.store.get("trigger", conv.trigger_id),
                             self.store.get("customer", conv.customer_id), now=received)
            try:
                result = manager.handle(self.state, conv, ci, from_role, message, received, self.settings.opt_out_days)
            except Exception as exc:
                log_event(log, "reply_failed", conversation_id=conv_id, error_type=type(exc).__name__)
                result = {"action": "wait", "wait_seconds": 1800,
                          "rationale": "Internal fallback: could not process this turn safely; backing off 30 min."}
        errors = validate_reply(result)
        if errors:
            log_event(log, "reply_invalid", conversation_id=conv_id, errors=errors)
            result = {"action": "wait", "wait_seconds": 1800, "rationale": "Fallback after invalid reply composition."}
        last_intent = conv.turns[-2].intent if len(conv.turns) >= 2 else ""
        log_event(log, "reply_done", conversation_id=conv_id, from_role=from_role, intent=last_intent,
                  action=result["action"], stage=conv.stage, turn_number=req.get("turn_number"))
        return result

    # ------------------------------------------------------------------ ops
    def healthz(self) -> dict:
        return {"status": "ok", "uptime_seconds": int(time.time() - self.started),
                "contexts_loaded": self.store.counts()}

    def metadata(self) -> dict:
        s = self.settings
        return {
            "team_name": s.team_name,
            "team_members": list(s.team_members),
            "model": s.model_label,
            "approach": s.approach,
            "contact_email": s.contact_email,
            "version": s.version,
            "submitted_at": s.submitted_at,
        }

    def teardown(self) -> dict:
        self.store.clear()
        self.state.clear()
        log_event(log, "teardown")
        return {"wiped": True, "at": iso_now()}


def validate_tick_request(body: Any) -> tuple[Optional[dict], Optional[str]]:
    if not isinstance(body, dict):
        return None, "request body must be a JSON object"
    triggers = body.get("available_triggers", [])
    if triggers is None:
        triggers = []
    if not isinstance(triggers, list) or not all(isinstance(t, str) for t in triggers):
        return None, "available_triggers must be a list of strings"
    now = body.get("now")
    if now is not None and (not isinstance(now, str) or parse_iso(now) is None):
        return None, "now must be an ISO-8601 timestamp"
    return {"now": now, "available_triggers": triggers}, None


def validate_reply_request(body: Any) -> tuple[Optional[dict], Optional[str]]:
    if not isinstance(body, dict):
        return None, "request body must be a JSON object"
    cid = body.get("conversation_id")
    if not isinstance(cid, str) or not cid.strip():
        return None, "conversation_id is required (non-empty string)"
    if "message" not in body or not isinstance(body.get("message"), str):
        return None, "message is required (string)"
    role = body.get("from_role", "merchant")
    if role is not None and (not isinstance(role, str) or role.lower() not in {"merchant", "customer"}):
        return None, "from_role must be 'merchant' or 'customer'"
    for key in ("merchant_id", "customer_id"):
        if body.get(key) is not None and not isinstance(body.get(key), str):
            return None, f"{key} must be a string or null"
    turn = body.get("turn_number")
    if turn is not None and (isinstance(turn, bool) or not isinstance(turn, int)):
        return None, "turn_number must be an integer"
    if len(body.get("message", "")) > 8000:
        return None, "message too long (max 8000 chars)"
    return body, None
