"""In-process smoke test: load the seed dataset, tick every trigger, print actions."""
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LOG_LEVEL", "WARNING")
from fastapi.testclient import TestClient  # noqa: E402

from bot import app  # noqa: E402

c = TestClient(app)
for f in glob.glob("dataset/categories/*.json"):
    p = json.load(open(f, encoding="utf-8"))
    c.post("/v1/context", json={"scope": "category", "context_id": p["slug"], "version": 1, "payload": p})
for name, key, scope in (("merchants", "merchant_id", "merchant"), ("customers", "customer_id", "customer"),
                         ("triggers", "id", "trigger")):
    for x in json.load(open(f"dataset/{name}_seed.json", encoding="utf-8"))[name]:
        c.post("/v1/context", json={"scope": scope, "context_id": x[key], "version": 1, "payload": x})
print(c.get("/v1/healthz").json())
tids = [x["id"] for x in json.load(open("dataset/triggers_seed.json", encoding="utf-8"))["triggers"]]
now = sys.argv[1] if len(sys.argv) > 1 else "2026-04-26T10:30:00Z"
r = c.post("/v1/tick", json={"now": now, "available_triggers": tids}).json()
print(len(r["actions"]), "actions")
for a in r["actions"]:
    print(f"- {a['trigger_id']} -> {a['conversation_id']} [{a['send_as']}/{a['cta']}]")
r2 = c.post("/v1/tick", json={"now": now, "available_triggers": tids}).json()
print("second tick:", len(r2["actions"]), [a["trigger_id"] for a in r2["actions"]])
