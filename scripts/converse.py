"""Drive a multi-turn conversation in-process: tick a seed trigger, then feed merchant replies.

    python scripts/converse.py trg_001_research_digest_dentists "Yes please send the abstract" "confirm" "thanks"
"""
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LOG_LEVEL", "WARNING")
from vera.config import Settings  # noqa: E402
from vera.service import VeraEngine  # noqa: E402

eng = VeraEngine(Settings(log_level="WARNING"))
data = Path(os.environ.get("VERA_DATA", "expanded"))
for scope, sub, key in (("category", "categories", "slug"), ("merchant", "merchants", "merchant_id"),
                        ("customer", "customers", "customer_id"), ("trigger", "triggers", "id")):
    for f in glob.glob(str(data / sub / "*.json")):
        p = json.load(open(f, encoding="utf-8"))
        eng.push_context({"scope": scope, "context_id": p[key], "version": 1, "payload": p})
tid, replies = sys.argv[1], sys.argv[2:]
tick = eng.tick("2026-04-26T10:30:00Z", [tid])
if not tick["actions"]:
    print("no action for", tid)
    raise SystemExit(1)
a = tick["actions"][0]
print(f"VERA  > {a['body']}\n")
role = "customer" if a["customer_id"] else "merchant"
for i, msg in enumerate(replies, start=2):
    r = eng.reply({"conversation_id": a["conversation_id"], "merchant_id": a["merchant_id"], "customer_id": a["customer_id"],
                   "from_role": role, "message": msg, "received_at": "2026-04-26T10:4%d:00Z" % i, "turn_number": i})
    print(f"{role.upper():8}> {msg}")
    print(f"VERA  [{r['action']}{' ' + str(r.get('wait_seconds')) if r['action'] == 'wait' else ''}] > "
          f"{r.get('body', '')}\n        ({r['rationale']})\n")
