"""HTTP client for the Urja Meter Ops portal.

Responsibilities (and nothing else):

* log in and keep a valid session (re-login on expiry, proactively and on 401)
* sign requests to the bulk export endpoint (HMAC-SHA256)
* be polite: client-side throttle, retries with backoff on 429/5xx/network errors
* translate portal error shapes into a small set of Python exceptions

Everything about *interpreting* payloads lives in ``parsing.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx

from ..config import Settings
from . import parsing

log = logging.getLogger("urja.portal")

# The portal's session lasts one hour and is *not* extended by activity, so we
# re-login a little before it runs out instead of waiting to be rejected.
SESSION_REFRESH_MARGIN_SECONDS = 120
DEFAULT_SESSION_SECONDS = 3600


class PortalError(Exception):
    """The portal answered, but not with what we asked for."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class PortalAuthError(PortalError):
    """Credentials rejected, or we could not establish a session."""


class PortalUnavailable(PortalError):
    """Portal unreachable, timing out, or still rate-limiting after retries."""


class MeterNotFound(PortalError):
    pass


class _Throttle:
    """Spaces request *starts* at least ``1/rps`` seconds apart."""

    def __init__(self, rps: float, clock: Callable[[], float] = time.monotonic):
        self._interval = 1.0 / rps if rps > 0 else 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()
        self._clock = clock

    async def wait(self) -> None:
        if not self._interval:
            return
        async with self._lock:
            now = self._clock()
            delay = self._next - now
            if delay > 0:
                await asyncio.sleep(delay)
                now = self._clock()
            self._next = max(now, self._next) + self._interval


def sign_request(method: str, path: str, query: str, secret: str, timestamp: str) -> str:
    """Reproduces the portal front-end's signer (found in the Transformers page bundle).

    ``signature = hex(HMAC_SHA256(secret, f"{METHOD}\\n{path}\\n{query}\\n{unix_ts}"))``
    """
    message = "\n".join([method.upper(), path, query, timestamp])
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


class PortalClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.settings = settings
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=settings.portal_base_url,
            timeout=settings.portal_timeout_seconds,
            transport=transport,
            headers={"User-Agent": "urja-api/1.0 (+read-only integration)", "Accept": "application/json"},
            follow_redirects=False,
        )
        self._throttle = _Throttle(settings.portal_max_rps)
        self._login_lock = asyncio.Lock()
        self._session_expires_at: float | None = None  # epoch seconds
        self._signing_secret: str | None = None
        self.stats = {"requests": 0, "retries": 0, "logins": 0, "rate_limited": 0}

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ auth

    @property
    def session_expires_at(self) -> float | None:
        return self._session_expires_at

    def _session_valid(self) -> bool:
        return (
            self._session_expires_at is not None
            and time.time() < self._session_expires_at - SESSION_REFRESH_MARGIN_SECONDS
        )

    async def ensure_session(self, *, force: bool = False) -> None:
        if not force and self._session_valid():
            return
        async with self._login_lock:
            if not force and self._session_valid():
                return
            await self._login()

    async def _login(self) -> None:
        if not self.settings.portal_email or not self.settings.portal_password:
            raise PortalAuthError("URJA_EMAIL / URJA_PASSWORD are not configured")
        self._http.cookies.clear()
        origin = {"Origin": self.settings.portal_base_url}
        creds = {"email": self.settings.portal_email, "password": self.settings.portal_password}

        # Primary path: the better-auth JSON endpoint the portal is built on.
        resp = await self._send("POST", "/api/auth/sign-in/email", json=creds, headers=origin)
        if resp.status_code in (404, 405):
            # Fallback: the SvelteKit form action the login page itself posts to.
            resp = await self._send(
                "POST",
                "/login",
                data=creds,
                headers={**origin, "x-sveltekit-action": "true"},
            )
            if resp.status_code == 200 and resp.json().get("type") == "failure":
                raise PortalAuthError("portal rejected the credentials", status=401)
        if resp.status_code in (400, 401, 403):
            raise PortalAuthError(f"portal rejected the login ({resp.status_code})", status=resp.status_code)
        if resp.status_code >= 400:
            raise PortalUnavailable(f"login failed with HTTP {resp.status_code}", status=resp.status_code)

        # Ask the portal when this session ends rather than hard-coding it.
        expires_at = time.time() + DEFAULT_SESSION_SECONDS
        try:
            sess = await self._send("GET", "/api/auth/get-session")
            body = sess.json() if sess.status_code == 200 else None
            if not body:
                raise PortalAuthError("login appeared to succeed but no session was created")
            raw = (body.get("session") or {}).get("expiresAt")
            if raw:
                expires_at = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except (ValueError, httpx.HTTPError):
            pass
        self._session_expires_at = expires_at
        self.stats["logins"] += 1
        log.info(
            "logged in to portal; session valid until %s",
            datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
        )

    # ------------------------------------------------------------ transport

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One HTTP request with throttling + retries for transient failures."""
        attempts = self.settings.portal_max_retries + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            await self._throttle.wait()
            self.stats["requests"] += 1
            try:
                resp = await self._http.request(method, path, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                log.warning("portal %s %s failed (%s), attempt %d/%d", method, path, exc, attempt + 1, attempts)
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    if resp.status_code == 429:
                        self.stats["rate_limited"] += 1
                    last_exc = PortalUnavailable(
                        f"portal returned {resp.status_code}", status=resp.status_code
                    )
                    retry_after = resp.headers.get("retry-after")
                    if attempt + 1 < attempts:
                        delay = float(retry_after) if retry_after and retry_after.isdigit() else None
                        await self._backoff(attempt, delay)
                        self.stats["retries"] += 1
                        continue
                    break
                return resp
            if attempt + 1 < attempts:
                await self._backoff(attempt)
                self.stats["retries"] += 1
        if isinstance(last_exc, PortalError):
            raise last_exc
        raise PortalUnavailable(f"portal unreachable: {last_exc}") from last_exc

    async def _backoff(self, attempt: int, explicit: float | None = None) -> None:
        # The portal's 429 carries no Retry-After header, so exponential + jitter.
        delay = explicit if explicit is not None else min(16.0, 1.0 * 2**attempt) + random.uniform(0, 0.5)
        await self._sleep(delay)

    async def _get_json(self, path: str, *, params: dict[str, Any] | None = None, headers: dict | None = None) -> Any:
        """Authenticated GET returning JSON; re-logs in once if the session was rejected."""
        await self.ensure_session()
        for attempt in range(2):
            resp = await self._send("GET", path, params=params, headers=headers)
            if resp.status_code == 401:
                body = _safe_json(resp)
                code = (body or {}).get("error")
                if code == "unauthorized" and attempt == 0:
                    log.info("portal session rejected; logging in again")
                    await self.ensure_session(force=True)
                    continue
                raise PortalError((body or {}).get("message", "unauthorized"), status=401, code=code)
            if resp.status_code == 404:
                body = _safe_json(resp) or {}
                raise MeterNotFound(body.get("message", "not found"), status=404, code=body.get("error"))
            if resp.status_code >= 400:
                raise PortalError(f"portal returned HTTP {resp.status_code}", status=resp.status_code)
            payload = _safe_json(resp)
            if payload is None:
                raise PortalError("portal returned a non-JSON response", status=resp.status_code)
            # SvelteKit data endpoints signal an expired session with a 200 redirect.
            if isinstance(payload, dict) and payload.get("type") == "redirect" and payload.get("location") == "/login":
                if attempt == 0:
                    await self.ensure_session(force=True)
                    continue
                raise PortalAuthError("portal keeps redirecting to /login")
            return payload
        raise PortalAuthError("could not obtain an authenticated response")

    # ------------------------------------------------------------- endpoints

    async def search_meters(self, q: str = "", page: int = 1) -> dict[str, Any]:
        """`/portal/meters/search` — 20 rows/page; `q` matches meter id or serial (substring)."""
        return await self._get_json("/portal/meters/search", params={"q": q, "page": page})

    async def list_transformers(self, page: int = 1) -> dict[str, Any]:
        return await self._get_json("/portal/dts", params={"page": page})

    async def all_transformers(self) -> list[dict[str, Any]]:
        first = await self.list_transformers(1)
        rows = list(first.get("data") or [])
        total, size = first.get("total", len(rows)), first.get("pageSize", 20) or 20
        pages = -(-total // size)
        for page in range(2, pages + 1):
            rows.extend((await self.list_transformers(page)).get("data") or [])
        return rows

    async def meter_page(self, meter_id: str) -> parsing.SvelteKitPage:
        payload = await self._get_json(f"/meters/{meter_id}/__data.json")
        page = parsing.parse_sveltekit_data(payload)
        if page.kind == "error" and page.error_status == 404:
            raise MeterNotFound(page.error_message or "Meter not found", status=404)
        if page.kind != "data":
            raise PortalError(f"unexpected meter page result: {page.kind}")
        return page

    async def meter_geo(self, meter_id: str) -> dict[str, Any]:
        return await self._get_json(f"/portal/meters/{meter_id}/geo")

    async def meter_energy(self, meter_id: str) -> list[dict[str, Any]]:
        return (await self._get_json(f"/portal/meters/{meter_id}/energy")).get("data") or []

    async def signing_secret(self, *, refresh: bool = False) -> str:
        if refresh or not self._signing_secret:
            body = await self._get_json("/portal/keys")
            self._signing_secret = body["data"]["signingSecret"]
        return self._signing_secret

    async def export_all(self) -> dict[str, Any]:
        """The bulk path: one signed request returns every meter with hierarchy and geo.

        The front-end always sends ``page=1``; the server ignores it and returns the
        full dataset, but we keep the query identical so the signature matches.
        """
        path, query = "/portal/export", "page=1"
        for attempt in range(2):
            secret = await self.signing_secret(refresh=attempt > 0)
            ts = str(int(time.time()))
            headers = {"x-timestamp": ts, "x-signature": sign_request("GET", path, query, secret, ts)}
            try:
                return await self._get_json(path, params={"page": 1}, headers=headers)
            except PortalError as exc:
                if exc.code == "signature_invalid" and attempt == 0:
                    log.info("export signature rejected; refreshing signing secret")
                    continue
                raise
        raise PortalError("export signature rejected twice")


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None
