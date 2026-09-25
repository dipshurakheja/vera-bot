#!/usr/bin/env python3
"""Write submission.jsonl (brief §7.2): one composed message per canonical test pair.

    python dataset/generate_dataset.py --seed-dir dataset --out expanded
    python scripts/generate_submission.py [--data expanded] [--out submission.jsonl] [--now 2026-04-26T10:30:00Z]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import compose  # noqa: E402


def load(path: Path) -> dict:
    with open(path, encoding="utf-8") as fp:
        return json.load(fp)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "expanded"))
    ap.add_argument("--out", default=str(ROOT / "submission.jsonl"))
    ap.add_argument("--now", default="2026-04-26T10:30:00Z")
    args = ap.parse_args()
    data = Path(args.data)
    pairs = load(data / "test_pairs.json")["pairs"]
    with open(args.out, "w", encoding="utf-8") as out:
        for p in pairs:
            trigger = load(data / "triggers" / f"{p['trigger_id']}.json")
            merchant = load(data / "merchants" / f"{p['merchant_id']}.json")
            category = load(data / "categories" / f"{merchant['category_slug']}.json")
            customer = load(data / "customers" / f"{p['customer_id']}.json") if p.get("customer_id") else None
            msg = compose(category, merchant, trigger, customer, now=args.now)
            row = {"test_id": p["test_id"], "body": msg["body"], "cta": msg["cta"], "send_as": msg["send_as"],
                   "suppression_key": msg["suppression_key"], "rationale": msg["rationale"]}
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {len(pairs)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
