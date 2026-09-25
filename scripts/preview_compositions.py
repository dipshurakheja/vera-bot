#!/usr/bin/env python3
"""Compose a message for every trigger in a dataset directory and print them for review.

Usage:
    python scripts/preview_compositions.py [--data expanded] [--kind perf_dip] [--now 2026-04-26T10:30:00Z] [--quiet]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.composer.engine import compose  # noqa: E402


def load_dir(path: Path, key: str) -> dict:
    out = {}
    for f in sorted(path.glob("*.json")):
        with open(f, encoding="utf-8") as fp:
            data = json.load(fp)
        out[data.get(key) or f.stem] = data
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "expanded"))
    ap.add_argument("--kind", default="")
    ap.add_argument("--trigger", default="")
    ap.add_argument("--now", default="2026-04-26T10:30:00Z")
    ap.add_argument("--quiet", action="store_true", help="only print failures + summary")
    args = ap.parse_args()
    base = Path(args.data)
    cats, mers = load_dir(base / "categories", "slug"), load_dir(base / "merchants", "merchant_id")
    custs, trgs = load_dir(base / "customers", "customer_id"), load_dir(base / "triggers", "id")
    failures, routes = 0, {}
    for tid, t in trgs.items():
        if args.kind and t.get("kind") != args.kind:
            continue
        if args.trigger and args.trigger not in tid:
            continue
        m = mers.get(t.get("merchant_id"))
        c = custs.get(t.get("customer_id")) if t.get("customer_id") else None
        cat = cats.get((m or {}).get("category_slug"))
        msg = compose(cat, m, t, c, now=args.now)
        internal = msg.pop("_internal")
        routes[internal["route"]] = routes.get(internal["route"], 0) + 1
        bad = internal["validation_errors"] or internal["route"] == "safe_minimal"
        failures += bool(bad)
        if args.quiet and not bad:
            continue
        print("=" * 100)
        print(f"{tid}  [{t.get('kind')}]  -> {msg['send_as']} / {msg['cta']} / route={internal['route']}")
        if bad:
            print(f"!! validation: {internal['validation_errors']}")
        print(msg["body"])
        print(f"-- rationale: {msg['rationale']}")
    print("=" * 100)
    print(f"routes: {json.dumps(routes, indent=0)}")
    print(f"failures: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
