"""End-to-end check against the REAL portal, using credentials from .env.

    python scripts/smoke_test.py

Runs the whole stack in-process (no server needed): logs in, syncs the index
from the signed bulk export, then calls each endpoint once and prints a
one-line result. Makes roughly 10 requests to the portal.
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dataclasses import replace  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

settings = replace(Settings.from_env(), db_path=str(Path(tempfile.mkdtemp()) / "smoke.sqlite3"))
if not settings.portal_password:
    sys.exit("Set URJA_EMAIL / URJA_PASSWORD in .env first (see .env.example).")

ok = True


def check(name, response, predicate):
    global ok
    try:
        body = response.json()
        passed = bool(response.status_code == 200 and predicate(body))
    except Exception as exc:  # noqa: BLE001
        body, passed = str(exc), False
    ok &= passed
    print(f"{'PASS' if passed else 'FAIL'}  {name:<38} HTTP {response.status_code}")
    if not passed:
        print("      ", str(body)[:400])
    return body


with TestClient(create_app(settings, warm_index=False)) as c:
    meta = check("POST /sync (login + signed export)", c.post("/sync"), lambda b: b["meter_count"] > 0 and b["source"] == "export")
    print(f"       -> {meta.get('meter_count')} meters from {meta.get('source')}")
    check("GET /meters?status=Faulty", c.get("/meters", params={"status": "Faulty"}), lambda b: b["pagination"]["total"] > 0)
    check("GET /meters/J100044 (repaired path)", c.get("/meters/J100044"), lambda b: b["hierarchy"]["feeder"]["code"])
    check("GET /meters/J100000/live (legacy)", c.get("/meters/J100000/live"), lambda b: b["build"] == "legacy")
    check("GET /meters/J100004/live (v2)", c.get("/meters/J100004/live"), lambda b: b["build"] == "v2")
    cons = check("GET /meters/J100000/consumption", c.get("/meters/J100000/consumption", params={"granularity": "daily"}), lambda b: len(b["daily"]) > 0)
    print(f"       -> resolution {cons.get('resolution')}, {cons.get('summary', {}).get('total_kwh')} kWh")
    check("GET /meters/J100089/consumption (dup row)", c.get("/meters/J100089/consumption"), lambda b: any("duplicate" in i for i in b["issues"]))
    r = c.get("/meters/J999999")
    print(f"{'PASS' if r.status_code == 404 else 'FAIL'}  {'GET /meters/J999999 -> 404':<38} HTTP {r.status_code}")
    ok &= r.status_code == 404
    check("GET /transformers/DT-007", c.get("/transformers/DT-007"), lambda b: b["meter_count"] > 0)
    check("GET /hierarchy?depth=feeder", c.get("/hierarchy", params={"depth": "feeder"}), lambda b: len(b) == 3)
    check("GET /stats", c.get("/stats"), lambda b: b["meters"] > 0)
    health = check("GET /health", c.get("/health"), lambda b: b["status"] == "ok")
    print(f"       -> portal stats: {health.get('portal_stats')}")

print("\nALL GOOD" if ok else "\nSOME CHECKS FAILED - see above")
sys.exit(0 if ok else 1)
