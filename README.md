# Vera message engine — magicpin AI Challenge

A stateful HTTP bot that decides **what Vera should say next, to whom, and why**, for merchants and (on their behalf) their customers. It implements the judge contract in `challenge-testing-brief.md` (`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`, plus optional `/v1/teardown`). It also exposes the brief's pure `compose(category, merchant, trigger, customer?)` function.

The decision path is **fully deterministic and needs no LLM**. An LLM can optionally polish wording, but every polished message is validated and falls back to the deterministic draft on any problem.

## 1. Architecture

```
POST /v1/context ─► ContextStore (versioned, thread-safe, atomic replace)
POST /v1/tick ────► planner: gates ─► rank ─► 1 per recipient ─► cap 20
                        │
                        ▼
                    compose(): views ─► signals ─► consent/scope routing ─► kind strategy
                        │                                                      │
                        ▼                                                      ▼
                    validation (numbers grounded, taboo, 1 CTA, no URL) ◄── Draft
                        │  (optional LLM polish ─► strict re-validation ─► else keep draft)
                        ▼
                    EngineState: suppression ledger, conversation, next_action
POST /v1/reply ───► intent classifier ─► state machine (pitched ─► delivered ─► executed) ─► send/wait/end
```

| Package | Responsibility |
|---|---|
| `vera/api/` | FastAPI app, raw-body JSON parsing, size limits, graceful degradation |
| `vera/store/` | `ContextStore` (version semantics) and `EngineState` (suppression, conversations, opt-outs, quiet windows) |
| `vera/context/` | Defensive read-only views over judge payloads |
| `vera/decision/` | `signals.py` (typed facts), `policy.py` (consent, category fit, priority), `planner.py` (tick gates and ranking) |
| `vera/composer/` | `merchant.py` / `customer.py` strategies per trigger kind, `knowledge.py` (offer, digest and season relevance), `engine.py` (`compose()` and fallbacks) |
| `vera/conversation/` | `intents.py` (EN, Hinglish, Devanagari), `manager.py` (reply state machine), `fulfillment.py` (artifacts delivered on "yes") |
| `vera/llm/` | `LLMProvider` abstraction (Anthropic SDK, OpenAI-compatible) and the validated `Polisher` |
| `vera/validation/` | Output validation, including the number-grounding anti-hallucination check |
| `bot.py` | `compose()` and the ASGI `app` · `conversation_handlers.py`: `respond()` (brief §7.4) |

## 2. Setup and running locally

```bash
python -m pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080        # or: python bot.py
```

Python 3.11+. On Windows, set `PYTHONUTF8=1` when running the dataset generator or judge simulator, because they open files without an explicit encoding.

## 3. Environment variables

All are optional; see `.env.example`.

| Variable | Default | Purpose |
|---|---|---|
| `TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL`, `BOT_VERSION`, `SUBMITTED_AT` | placeholders | `/v1/metadata` |
| `LLM_PROVIDER` | empty (off) | `anthropic`, `openai`, `groq`, `deepseek`, `openrouter`, `ollama` or `openai_compatible` |
| `LLM_API_KEY`, `LLM_MODEL` (alias `MODEL`), `LLM_BASE_URL` | – | Provider credentials and model. Anthropic defaults to `claude-opus-5` |
| `VERA_LLM_MODE` | `polish` if a provider and key are set, else `off` | `off` or `polish` |
| `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`, `VERA_TICK_LLM_BUDGET_SECONDS` | 12 / 1 / 18 | Latency guards |
| `VERA_MAX_ACTIONS_PER_TICK` | 20 | Hard cap |
| `VERA_MAX_CONTEXT_BYTES` | 524288 | `/v1/context` body limit; larger bodies get 413 |
| `VERA_ENFORCE_TRIGGER_EXPIRY` | false | See the decision strategy section |
| `VERA_MERCHANT_COOLDOWN_MINUTES` | 30 | Defers non-urgent merchant triggers while another merchant conversation is active |
| `VERA_OPT_OUT_DAYS` | 30 | How long a merchant "stop" silences proactive sends |

## 4. API

| Endpoint | Behaviour |
|---|---|
| `POST /v1/context` | First or higher version: `200 {accepted, ack_id, stored_at}`. Same or lower version: `409 {accepted:false, reason:"stale_version", current_version}` with no change (as in api-call-examples 1.5). Bad scope, version or payload: `400 {accepted:false, reason, details}`. Malformed JSON: `400 invalid_json`. Oversize: `413`. |
| `POST /v1/tick` | `{"actions":[...]}` with `conversation_id, merchant_id, customer_id, send_as, trigger_id, template_name, template_params, body, cta, suppression_key, rationale`. Never more than 20. An internal error degrades to `{"actions":[]}`. |
| `POST /v1/reply` | `{"action":"send","body","cta","rationale"}`, `{"action":"wait","wait_seconds","rationale"}` or `{"action":"end","rationale"}`. Unknown conversation IDs are handled. An internal error degrades to a wait. |
| `GET /v1/healthz` | `{status, uptime_seconds, contexts_loaded}`. No I/O and no LLM. |
| `GET /v1/metadata` | Team, model, approach and version, from the environment |
| `POST /v1/teardown` | Wipes all state (testing brief §11) |

Example requests:

```bash
curl -X POST localhost:8080/v1/context -H 'Content-Type: application/json' \
  -d '{"scope":"trigger","context_id":"trg_x","version":1,"payload":{"kind":"perf_dip","merchant_id":"m_001_drmeera_dentist_delhi","payload":{"metric":"calls","delta_pct":-0.4}}}'
curl -X POST localhost:8080/v1/tick  -H 'Content-Type: application/json' -d '{"now":"2026-04-26T10:30:00Z","available_triggers":["trg_x"]}'
curl -X POST localhost:8080/v1/reply -H 'Content-Type: application/json' \
  -d '{"conversation_id":"conv_...","merchant_id":"m_001_drmeera_dentist_delhi","from_role":"merchant","message":"Yes, go ahead","received_at":"2026-04-26T10:45:00Z","turn_number":2}'
```

## 5. Decision-engine strategy

1. **Gates, per listed trigger.** A trigger is skipped, with a logged reason, when any of these holds:
   - The trigger, merchant, category or (for customer scope) customer context is missing.
   - Its suppression key was already sent. A new version with a changed payload is allowed once more.
   - The merchant or customer opted out.
   - The merchant is in a quiet window after asking to wait, rejecting, or auto-replying. Urgency 5 overrides this.
   - A conversation for this trigger is already in flight.
   - Another merchant conversation is still active (cooldown). Urgency 5 overrides this.
   - The trigger isn't relevant to the category, for example a `category_relevance` list that excludes it.
   - Customer consent doesn't cover the purpose. Examples: `reminder_opt_in=false` blocks reminders, and a customer with no opt-in or channel is never contacted.
2. **Rank** by urgency, kind weight and signal magnitude. Placeholder payloads and past-expiry triggers get small penalties. Then keep **one message per recipient per tick** and cap the total at 20. Deferred triggers aren't suppressed, so they go out on a later tick.
3. **Compose with a kind strategy.** There are 20+ merchant kinds and 8 customer kinds, with keyword routing for unseen kinds. Each strategy answers four questions:
   - *Why now:* the trigger fact, quoted from the payload or digest item.
   - *Why this merchant:* their numbers against peer benchmarks, their live offers, cohorts, review themes, or conversation history.
   - *What to do:* one judgement call. Examples: "don't price-match the competitor", "a Sunday IPL match is a delivery night", "the April dip is seasonal, so fix retention".
   - *One low-friction CTA*, always the last sentence.
4. **Placeholder triggers** (generated ones with no payload) never get invented facts. The strategy falls back to merchant and category data: its 7-day deltas, customers served, seasonal beats, and best-matching digest item.
5. **Customer-facing messages** only cite the merchant's *live* offers and real slots, and honour language preference (Hinglish, Hindi, or a regional greeting). Molecule names are included only when consent covers refill reminders.
6. **Expiry.** The judge says `available_triggers` are "active right now", and the local simulator sends a wall-clock `now` that is months after the dataset's expiry dates. So a listed-but-expired trigger is only de-prioritised, not dropped. Set `VERA_ENFORCE_TRIGGER_EXPIRY=true` for strict behaviour.

## 6. Replies and conversation state

The intent classifier covers auto-reply, opt-out, hostile, reject, postpone (with parsed durations such as "in 2 hours" or "kal"), off-topic, accept, question, slot choice, thanks, statement and ambiguous. It works in English, Hinglish and Devanagari.

| Situation | Behaviour |
|---|---|
| Auto-replies, per conversation **and per merchant** | 1st: a short note for the owner. 2nd: wait 24 h. 3rd: end. The same message repeated three times also counts as an auto-reply. |
| "Yes", "let's do it" | Switches straight to action mode and delivers the artifact: post draft, offer listing, checklist, patient or customer message, and so on. It never asks another qualifying question. |
| "Confirm" | Executes the action and offers at most one natural next step. |
| Stop | Ends and opts the merchant out for 30 days. |
| Hostile | First time: an apology with an opt-out path. Second time: ends. |
| Off-topic, e.g. GST | Politely redirected to the right expert, then back to the thread. |
| Questions | Answered only from context (source, price, date), never guessed. |
| Repeats and length | Bodies are never repeated verbatim; the conversation ends after 8 bot turns. |

## 7. LLM strategy and deterministic fallback

The LLM is used **only to polish wording** of an already-decided, already-validated draft. It never decides what to say.

- The `Polisher` sends the draft plus a fact sheet and expects `{"body": ...}` JSON back.
- The rewrite is rejected if it adds or drops any number, loses the CTA keyword (YES, CONFIRM, STOP), adds questions or URLs, introduces new proper nouns, uses taboo words, or grows longer than the draft. On rejection the deterministic draft is used.
- Results are cached by prompt hash, so identical inputs give identical output within a process.
- OpenAI-compatible providers run at `temperature=0` with a fixed seed. Current Claude models reject sampling parameters, so the Anthropic provider sends `effort: "low"` with server-side refusal fallbacks, and relies on the cache plus validation for stability.
- Tick polishing runs in parallel under an 18 s budget. Anything not finished in time uses the draft.
- Replies are never polished, which keeps multi-turn behaviour deterministic.
- The default is **off**. The shipped behaviour is fully deterministic.

## 8. Testing

```bash
python -m pytest -q          # 146 tests (incl. fuzz regression)
```

The suite covers:

- **Context:** versioning (first, same, lower, higher), validation, the 500 KB boundary, and a concurrent version race.
- **Tick:** gates, suppression and material-change resend, the 20-action cap, one message per merchant, cooldown, consent, category relevance, expiry policy, specificity, and category voice.
- **Reply:** every intent, the three-strike auto-reply rule within and across conversations, the full multi-turn flow with no repetition, customer slot booking, and request validation.
- **Compose:** every one of the 100 expanded triggers composes a valid, number-grounded message, including all 30 canonical pairs. Customer messages cite only live offers. Adaptive context injection is covered for both new digest items and updated performance numbers.
- **Determinism:** repeated compose calls, fresh engines, trigger order, and reply transcripts all give identical results.
- **LLM layer:** retries, accepted rewrites, hallucinated or malformed or failing output falling back, and engine integration.

Dev helpers:

```bash
python scripts/preview_compositions.py [--kind perf_dip] [--quiet]   # review every composed message
python scripts/converse.py trg_001_research_digest_dentists "yes" "confirm" "thanks"
python scripts/generate_submission.py   # writes submission.jsonl for the 30 test pairs
```

The first and third helpers need the expanded dataset: `python dataset/generate_dataset.py --seed-dir dataset --out expanded`.

## 9. Judge simulator

`judge_simulator.py` hard-codes its config, so `scripts/run_judge.py` sets it from the environment without editing the official file:

```bash
BOT_URL=http://localhost:8080 JUDGE_LLM_PROVIDER=openai JUDGE_LLM_API_KEY=sk-... TEST_SCENARIO=all python scripts/run_judge.py
```

With no judge key, it runs **offline**. All flow checks run for real, but dimension scores are the simulator's heuristic placeholders. The local results with no key are below.

| Scenario | Result |
|---|---|
| warmup | PASS |
| auto_reply_hell | PASS (send, wait, end) |
| intent_transition | PASS |
| hostile | PASS |
| full_evaluation | 10 actions; heuristic specificity 10/10 |

## 10. Deployment

Any host that runs a Python ASGI app works (Render, Fly, Railway, a VM, or an ngrok tunnel):

```bash
docker build -t vera . && docker run -p 8080:8080 --env-file .env vera
```

Run a **single worker**: state is in memory by design, so don't restart during the test window. `/v1/teardown` wipes state at the end.

## 11. Trade-offs and what would help

- Deterministic templates trade some fluency for zero hallucination, a latency of about 30 ms per tick, and perfect reproducibility. The optional polish layer recovers fluency safely.
- Several generated triggers carry placeholder payloads. The engine degrades to merchant and category facts rather than inventing trigger details, and skips low-value fallbacks in `/v1/tick`.
- Most useful additional context: real appointment slots and service history for customers, the merchant's rating and review count, and structured digest summaries written at patient reading level.
