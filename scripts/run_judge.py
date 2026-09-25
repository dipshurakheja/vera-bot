#!/usr/bin/env python3
"""Run the official judge_simulator.py with configuration from environment variables / .env.

The simulator hard-codes its config at the top of the file; this wrapper sets those
module globals so the official file stays untouched:

    BOT_URL=http://localhost:8080
    JUDGE_LLM_PROVIDER=openai|anthropic|gemini|deepseek|groq|ollama|openrouter   (falls back to LLM_PROVIDER)
    JUDGE_LLM_API_KEY=...                                                          (falls back to LLM_API_KEY)
    JUDGE_LLM_MODEL=...                                                            (optional)
    JUDGE_MIN_INTERVAL=6        seconds between judge LLM calls (free-tier rate limits)
    JUDGE_RETRIES=4             retries on HTTP 429 / 5xx with exponential backoff
    TEST_SCENARIO=all|warmup|phase2_short|auto_reply_hell|intent_transition|hostile|full_evaluation
    JUDGE_REPORT=reports/judge_report.json

Values already set in the environment win over .env. The API key is never printed.

Additions over the plain simulator (none change how it scores):
  * pacing + retries so rate limits don't silently turn into placeholder "5" scores
  * a count of fallback scores at the end (should be 0 for trustworthy results)
  * a JSON report of every scored message with the judge's reasons

With no API key it runs OFFLINE: flow checks are real, dimension scores are heuristic only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib import error as urlerror

ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.strip() and key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip()


def install_user_agent() -> None:
    """Some providers (Groq via Cloudflare) return 403 for urllib's default 'Python-urllib/x.y' agent.
    The simulator doesn't set one, so install a global opener that adds a normal User-Agent."""
    from urllib import request as urlrequest

    opener = urlrequest.build_opener()
    opener.addheaders = [("User-Agent", "vera-judge-runner/1.0 (+https://magicpin.com)")]
    urlrequest.install_opener(opener)


def main() -> int:
    if not sys.flags.utf8_mode:  # the simulator opens dataset files without an encoding (breaks ₹ on Windows)
        return subprocess.call([sys.executable, "-X", "utf8", *sys.argv])
    load_dotenv(ROOT / ".env")
    install_user_agent()
    sys.path.insert(0, str(ROOT))
    import judge_simulator as js  # noqa: WPS433

    js.BOT_URL = os.environ.get("BOT_URL", js.BOT_URL)
    js.LLM_PROVIDER = os.environ.get("JUDGE_LLM_PROVIDER") or os.environ.get("LLM_PROVIDER") or js.LLM_PROVIDER
    js.LLM_API_KEY = os.environ.get("JUDGE_LLM_API_KEY") or os.environ.get("LLM_API_KEY") or js.LLM_API_KEY
    js.LLM_MODEL = os.environ.get("JUDGE_LLM_MODEL", js.LLM_MODEL)
    js.TEST_SCENARIO = os.environ.get("TEST_SCENARIO", js.TEST_SCENARIO)
    min_interval = float(os.environ.get("JUDGE_MIN_INTERVAL", "0") or 0)
    retries = int(os.environ.get("JUDGE_RETRIES", "3") or 3)
    report_path = ROOT / os.environ.get("JUDGE_REPORT", "reports/judge_report.json")

    stats = {"calls": 0, "retries": 0, "fallbacks": 0}
    records: list[dict] = []

    class Paced(js.LLMProvider):
        """Wraps a simulator provider: spacing between calls + retry on 429/5xx."""

        def __init__(self, inner):
            self.inner = inner
            self.last = 0.0

        def name(self) -> str:
            return self.inner.name() + (f" [paced {min_interval:g}s, {retries} retries]" if min_interval else "")

        def complete(self, prompt: str, system: str = None) -> str:
            for attempt in range(retries + 1):
                wait = self.last + min_interval - time.time()
                if wait > 0:
                    time.sleep(wait)
                self.last = time.time()
                stats["calls"] += 1
                try:
                    return self.inner.complete(prompt, system)
                except urlerror.HTTPError as exc:
                    if exc.code not in (429, 500, 502, 503, 504) or attempt == retries:
                        raise
                except (urlerror.URLError, TimeoutError, ConnectionError):
                    if attempt == retries:
                        raise
                stats["retries"] += 1
                time.sleep(min(60, 5 * 2 ** attempt))
            raise RuntimeError("unreachable")

    original_fallback = js.LLMScorer._fallback_score
    original_score = js.LLMScorer.score

    def counting_fallback(self, action):
        stats["fallbacks"] += 1
        return original_fallback(self, action)

    def recording_score(self, action, category, merchant, trigger, customer=None):
        result = original_score(self, action, category, merchant, trigger, customer)
        records.append({
            "trigger_id": action.get("trigger_id"), "kind": trigger.get("kind"),
            "merchant_id": action.get("merchant_id"), "send_as": action.get("send_as"), "body": action.get("body"),
            "total": result.total, "specificity": result.specificity, "category_fit": result.category_fit,
            "merchant_fit": result.merchant_fit, "decision_quality": result.decision_quality,
            "engagement": result.engagement_compulsion,
            "reasons": {"specificity": result.specificity_reason, "category_fit": result.category_fit_reason,
                        "merchant_fit": result.merchant_fit_reason, "decision_quality": result.decision_quality_reason,
                        "engagement": result.engagement_reason, "hint": result.hint},
        })
        return result

    js.LLMScorer._fallback_score = counting_fallback
    js.LLMScorer.score = recording_score

    def finish() -> None:
        if records:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps({"model": js.LLM_MODEL, "provider": js.LLM_PROVIDER, "stats": stats,
                                               "messages": records}, indent=2, ensure_ascii=False), encoding="utf-8")
        js.print_section("RUNNER STATS")
        js.print_info(f"judge LLM calls={stats['calls']} retries={stats['retries']} fallback_scores={stats['fallbacks']}")
        if stats["fallbacks"]:
            js.print_warn("Some scores are placeholders (LLM call failed) — rerun before trusting the averages.")
        if records:
            js.print_info(f"per-message report: {report_path}")

    if js.LLM_API_KEY or js.LLM_PROVIDER == "ollama":
        original_create = js.create_provider
        js.create_provider = lambda: Paced(original_create())
        try:
            js.main()
        except SystemExit as exc:
            finish()
            return int(exc.code or 0)
        finish()
        return 0

    class OfflineJudge(js.LLMProvider):
        """No LLM available: answers the connectivity probe, then forces the simulator's heuristic scoring."""

        def name(self) -> str:
            return "OFFLINE (no judge LLM key — heuristic fallback scores only)"

        def complete(self, prompt: str, system: str = None) -> str:
            if "Say 'ready'" in prompt:
                return "ready"
            raise RuntimeError("offline judge: no LLM configured")

    js.print_header("magicpin AI Challenge — LLM Judge (offline wrapper)")
    js.print_warn("No judge LLM key set: flow checks are real, dimension scores are heuristic only.")
    judge = js.JudgeSimulator(OfflineJudge())
    ok = judge.run(js.TEST_SCENARIO)
    finish()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
