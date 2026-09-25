"""FastAPI surface: the 5 judge endpoints (+ optional /v1/teardown).

Robustness rules:
  * bodies are read raw and parsed here, so malformed JSON gets a clean 400 (never a stack trace)
  * size limits enforced before parsing (500 KB-class context payloads, smaller for tick/reply)
  * engine work runs in a thread pool so a slow optional LLM call never blocks /v1/healthz
  * an internal failure in /v1/tick degrades to {"actions": []} and in /v1/reply to a wait action
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..config import Settings
from ..service import VeraEngine, validate_reply_request, validate_tick_request
from ..utils.logging import configure, get_logger, log_event, request_id_var

log = get_logger("api")


def create_app(settings: Optional[Settings] = None, engine: Optional[VeraEngine] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure(settings.log_level)
    engine = engine or VeraEngine(settings)
    app = FastAPI(title="Vera message engine", version=settings.version, docs_url="/docs", redoc_url=None)
    app.state.engine = engine

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:  # last line of defence — never leak internals
            log_event(log, "unhandled_error", path=request.url.path, error_type=type(exc).__name__)
            response = JSONResponse({"error": "internal_error", "request_id": rid}, status_code=500)
        response.headers["x-request-id"] = rid
        if request.url.path != "/v1/healthz":
            log_event(log, "http_request", method=request.method, path=request.url.path, status=response.status_code,
                      latency_ms=int((time.perf_counter() - started) * 1000))
        request_id_var.reset(token)
        return response

    async def read_json(request: Request, limit: int) -> tuple[Any, Optional[JSONResponse]]:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            return None, JSONResponse({"accepted": False, "reason": "payload_too_large",
                                       "details": f"body exceeds {limit} bytes"}, status_code=413)
        raw = await request.body()
        if len(raw) > limit:
            return None, JSONResponse({"accepted": False, "reason": "payload_too_large",
                                       "details": f"body exceeds {limit} bytes"}, status_code=413)
        if not raw.strip():
            return None, JSONResponse({"accepted": False, "reason": "invalid_json", "details": "empty body"},
                                      status_code=400)
        try:
            return json.loads(raw.decode("utf-8")), None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, JSONResponse({"accepted": False, "reason": "invalid_json",
                                       "details": f"malformed JSON: {exc.__class__.__name__}"}, status_code=400)

    @app.get("/v1/healthz")
    async def healthz():
        return engine.healthz()

    @app.get("/v1/metadata")
    async def metadata():
        return engine.metadata()

    @app.post("/v1/context")
    async def push_context(request: Request):
        body, err = await read_json(request, settings.max_context_bytes)
        if err:
            return err
        try:
            status, payload = await run_in_threadpool(engine.push_context, body)
        except Exception as exc:
            log_event(log, "context_error", error_type=type(exc).__name__)
            return JSONResponse({"accepted": False, "reason": "internal_error"}, status_code=500)
        return JSONResponse(payload, status_code=status)

    @app.post("/v1/tick")
    async def tick(request: Request):
        body, err = await read_json(request, settings.max_request_bytes)
        if err:
            return JSONResponse({"error": err_reason(err), "actions": []}, status_code=err.status_code)
        data, problem = validate_tick_request(body)
        if problem:
            return JSONResponse({"error": "invalid_request", "details": problem, "actions": []}, status_code=400)
        try:
            return await run_in_threadpool(engine.tick, data["now"], data["available_triggers"])
        except Exception as exc:
            log_event(log, "tick_error", error_type=type(exc).__name__)
            return {"actions": []}

    @app.post("/v1/reply")
    async def reply(request: Request):
        body, err = await read_json(request, settings.max_request_bytes)
        if err:
            return JSONResponse({"error": err_reason(err)}, status_code=err.status_code)
        data, problem = validate_reply_request(body)
        if problem:
            return JSONResponse({"error": "invalid_request", "details": problem}, status_code=400)
        try:
            return await run_in_threadpool(engine.reply, data)
        except Exception as exc:
            log_event(log, "reply_error", error_type=type(exc).__name__)
            return {"action": "wait", "wait_seconds": 1800,
                    "rationale": "Internal fallback: could not process this turn safely; backing off 30 min."}

    @app.post("/v1/teardown")
    async def teardown():
        return await run_in_threadpool(engine.teardown)

    return app


def err_reason(resp: JSONResponse) -> str:
    try:
        return json.loads(resp.body).get("reason", "invalid_request")
    except Exception:
        return "invalid_request"
