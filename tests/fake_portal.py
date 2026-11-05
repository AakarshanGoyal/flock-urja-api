"""An in-process imitation of the Urja portal, built from captured responses.

It reproduces the behaviours our client depends on: cookie sessions that
expire, 401 JSON on /portal/*, 200-redirect on __data.json, HMAC-signed export
with a timestamp window, a rate limiter without Retry-After, and the data
quirks (two detail formats, string numbers, duplicate energy rows).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx

FIXTURES = Path(__file__).parent / "fixtures"
EMAIL, PASSWORD = "operator@urja.local", "test-password"
SECRET = "test-signing-secret"
COOKIE = "__Secure-better-auth.session_token"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


def _daily_energy(seed: int) -> list[dict]:
    base, rate = 5000 + seed * 37.0, 5 + (seed % 7)
    rows = []
    for d in range(8):
        day = datetime(2026, 6, 23) + timedelta(days=d)
        rows.append(
            {
                "timestamp": day.strftime("%d/%m/%Y %H:%M"),
                "kwh": f"{base + rate * d:.2f}",
                "kvah": f"{(base + rate * d) * 1.08:.2f}",
                "voltR": str(220 + (seed + d) % 20),
            }
        )
    return rows


class FakePortal:
    def __init__(self):
        self.export = load("export_sample.json")["data"]
        self.dts = load("dts.json")
        self.sessions: dict[str, float] = {}  # token -> expiry epoch
        self.session_seconds = 3600
        self.rate_limit_next = 0  # respond 429 to the next N requests
        self.fail_next_5xx = 0
        self.export_broken = False
        self.down = False
        self.calls: list[str] = []
        self.logins = 0

    # -- helpers -----------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def expire_all_sessions(self):
        self.sessions.clear()

    def _authed(self, request: httpx.Request) -> bool:
        cookie = request.headers.get("cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and self.sessions.get(value, 0) > time.time():
                return True
        return False

    @staticmethod
    def _json(status: int, body, headers: dict | None = None) -> httpx.Response:
        return httpx.Response(status, json=body, headers=headers or {})

    # -- router ------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if self.down:
            raise httpx.ConnectError("portal down", request=request)
        if self.rate_limit_next > 0:
            self.rate_limit_next -= 1
            return self._json(429, {"error": "rate_limited", "message": "Rate limit exceeded; slow down."})
        if self.fail_next_5xx > 0:
            self.fail_next_5xx -= 1
            return httpx.Response(502, text="Bad Gateway")

        if path == "/api/auth/sign-in/email" and request.method == "POST":
            if request.headers.get("origin") is None:
                return self._json(403, {"code": "MISSING_OR_NULL_ORIGIN"})
            body = json.loads(request.content or b"{}")
            if body.get("email") != EMAIL or body.get("password") != PASSWORD:
                return self._json(401, {"code": "INVALID_EMAIL_OR_PASSWORD"})
            token = f"tok{len(self.sessions) + self.logins}"
            self.logins += 1
            self.sessions[token] = time.time() + self.session_seconds
            return self._json(200, {"redirect": False, "token": token}, {"set-cookie": f"{COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Lax"})

        if path == "/api/auth/get-session":
            if not self._authed(request):
                return self._json(200, None)
            exp = max(self.sessions.values())
            expires = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            return self._json(200, {"session": {"expiresAt": expires}, "user": {"email": EMAIL}})

        if path.endswith("/__data.json"):
            if not self._authed(request):
                return self._json(200, {"type": "redirect", "location": "/login"})
            meter_id = path.split("/")[2]
            fixture = {"J100000": "meter_page_legacy.json", "J100004": "meter_page_v2.json"}.get(meter_id, "meter_page_not_found.json")
            return self._json(200, load(fixture))

        if path.startswith("/portal/"):
            if not self._authed(request):
                return self._json(401, {"error": "unauthorized", "message": "A valid session is required."})
            return self._portal(request, path)

        return self._json(404, {"error": "not_found"})

    def _portal(self, request: httpx.Request, path: str) -> httpx.Response:
        qs = parse_qs(request.url.query.decode())
        page = max(1, int((qs.get("page") or ["1"])[0] or 1))
        if path == "/portal/meters/search":
            q = (qs.get("q") or [""])[0]
            rows = [m for m in self.export if q in m["meterId"] or q in m["serialNo"]]
            chunk = rows[(page - 1) * 20 : page * 20]
            keys = ["meterId", "serialNo", "make", "phaseType", "installStatus", "dtCode"]
            return self._json(200, {"data": [{k: m[k] for k in keys} for m in chunk], "total": len(rows), "page": page, "pageSize": 20})
        if path == "/portal/dts":
            return self._json(200, {"data": self.dts[(page - 1) * 20 : page * 20], "total": len(self.dts), "page": page, "pageSize": 20})
        if path == "/portal/keys":
            return self._json(200, {"data": {"signingSecret": SECRET}})
        if path == "/portal/export":
            if self.export_broken:
                return httpx.Response(500, text="boom")
            ts = request.headers.get("x-timestamp", "")
            sig = request.headers.get("x-signature", "")
            msg = "\n".join(["GET", path, request.url.query.decode(), ts])
            good = hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
            if not ts.isdigit() or abs(time.time() - int(ts)) > 300 or not hmac.compare_digest(sig, good):
                return self._json(401, {"error": "signature_invalid", "message": "Missing or invalid request signature."})
            return self._json(200, {"data": self.export, "total": len(self.export)})
        parts = path.split("/")  # ['', 'portal', 'meters', id, kind]
        if len(parts) == 5 and parts[2] == "meters":
            meter_id, kind = parts[3], parts[4]
            m = next((x for x in self.export if x["meterId"] == meter_id), None)
            if not m:
                return self._json(404, {"error": "not_found", "message": "Meter not found"})
            if kind == "geo":
                return self._json(200, {"data": {"latitude": str(m["geo"]["lat"]), "longitude": str(m["geo"]["lng"])}})
            if kind == "energy":
                if meter_id == "J100089":
                    return self._json(200, load("energy_daily_duplicate.json"))
                if meter_id == "J100000":
                    return self._json(200, load("energy_half_hourly_head.json"))
                return self._json(200, {"data": _daily_energy(int(meter_id[1:]) % 97)})
        return self._json(404, {"error": "not_found", "message": "Not found"})
