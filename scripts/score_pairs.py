#!/usr/bin/env python3
"""Score compositions with the official simulator's LLM judge prompt (no HTTP, no gating).

Scores the 30 canonical test pairs by default (or --all for every expanded trigger),
using judge_simulator.LLMScorer so the rubric and prompt are exactly the simulator's.
Reads the judge config from .env like scripts/run_judge.py (paced + retried).

    python scripts/score_pairs.py [--data expanded] [--all] [--kinds perf_dip,renewal_due] [--out reports/pairs.json]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from urllib import error as urlerror

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if not sys.flags.utf8_mode:
        return subprocess.call([sys.executable, "-X", "utf8", *sys.argv])
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "scripts"))
    from run_judge import install_user_agent, load_dotenv  # noqa: E402

    load_dotenv(ROOT / ".env")
    install_user_agent()
    import judge_simulator as js  # noqa: E402
    from bot import compose  # noqa: E402

    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "expanded"))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--kinds", default="")
    ap.add_argument("--now", default="2026-04-26T10:30:00Z")
    ap.add_argument("--out", default=str(ROOT / "reports" / "pairs_report.json"))
    args = ap.parse_args()

    js.LLM_PROVIDER = os.environ.get("JUDGE_LLM_PROVIDER", "")
    js.LLM_API_KEY = os.environ.get("JUDGE_LLM_API_KEY", "")
    js.LLM_MODEL = os.environ.get("JUDGE_LLM_MODEL", "")
    if not js.LLM_API_KEY:
        print("JUDGE_LLM_API_KEY is not set (.env)")
        return 1
    inner = js.create_provider()
    interval = float(os.environ.get("JUDGE_MIN_INTERVAL", "6") or 6)
    retries = int(os.environ.get("JUDGE_RETRIES", "4") or 4)
    state = {"last": 0.0, "retries": 0, "fallbacks": 0}

    class Paced(js.LLMProvider):
        def name(self):
            return inner.name()

        def complete(self, prompt, system=None):
            for attempt in range(retries + 1):
                wait = state["last"] + interval - time.time()
                if wait > 0:
                    time.sleep(wait)
                state["last"] = time.time()
                try:
                    return inner.complete(prompt, system)
                except urlerror.HTTPError as exc:
                    if exc.code not in (429, 500, 502, 503, 504) or attempt == retries:
                        raise
                except (urlerror.URLError, TimeoutError, ConnectionError):
                    if attempt == retries:
                        raise
                state["retries"] += 1
                time.sleep(min(60, 5 * 2 ** attempt))

    data = Path(args.data)
    load = lambda p: json.loads(p.read_text(encoding="utf-8"))  # noqa: E731
    if args.all:
        tids = sorted(p.stem for p in (data / "triggers").glob("*.json"))
        pairs = [{"test_id": t, "trigger_id": t} for t in tids]
    else:
        pairs = load(data / "test_pairs.json")["pairs"]
    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}

    ds = js.DatasetLoader(js.DATASET_DIR)
    scorer = js.LLMScorer(Paced(), ds)
    orig_fb = scorer._fallback_score

    def fb(action):
        state["fallbacks"] += 1
        return orig_fb(action)

    scorer._fallback_score = fb
    rows = []
    print(f"judge: {inner.name()} | pacing {interval:g}s")
    for p in pairs:
        t = load(data / "triggers" / f"{p['trigger_id']}.json")
        if kinds and t["kind"] not in kinds:
            continue
        m = load(data / "merchants" / f"{t['merchant_id']}.json")
        cat = load(data / "categories" / f"{m['category_slug']}.json")
        c = load(data / "customers" / f"{t['customer_id']}.json") if t.get("customer_id") else None
        msg = compose(cat, m, t, c, now=args.now)
        action = dict(msg, trigger_id=t["id"], merchant_id=m["merchant_id"], customer_id=t.get("customer_id"))
        s = scorer.score(action, cat, m, t, c)
        rows.append({"test_id": p.get("test_id"), "trigger_id": t["id"], "kind": t["kind"], "send_as": msg["send_as"],
                     "total": s.total, "spec": s.specificity, "cat": s.category_fit, "merch": s.merchant_fit,
                     "dq": s.decision_quality, "eng": s.engagement_compulsion, "body": msg["body"],
                     "reasons": {"spec": s.specificity_reason, "cat": s.category_fit_reason, "merch": s.merchant_fit_reason,
                                 "dq": s.decision_quality_reason, "eng": s.engagement_reason, "hint": s.hint}})
        print(f"{p.get('test_id', ''):>4} {t['kind']:<26} total={s.total:>2}  spec={s.specificity} cat={s.category_fit} "
              f"merch={s.merchant_fit} dq={s.decision_quality} eng={s.engagement_compulsion}", flush=True)
    if rows:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"judge": inner.name(), "rows": rows, "stats": state}, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        dims = ["spec", "cat", "merch", "dq", "eng", "total"]
        print("averages:", {d: round(statistics.mean(r[d] for r in rows), 2) for d in dims})
        print(f"fallback_scores={state['fallbacks']} retries={state['retries']} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
