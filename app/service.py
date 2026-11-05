"""Glue between the portal client, the local index and the API layer.

Freshness policy (README "Caching & freshness"):

* The meter/transformer index is rebuilt from the bulk export when it's older
  than ``index_ttl_seconds`` (default 15 min) — lazily, on the first request
  that needs it, with a lock so concurrent requests trigger one sync.
* Consumption is fetched per meter on demand and cached for
  ``energy_ttl_seconds`` (default 10 min).
* If the portal is down when a refresh is due, we serve the last good copy and
  say so (``stale: true`` + ``X-Data-Stale`` header) rather than failing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from . import hierarchy as hier
from .config import Settings
from .portal import parsing
from .portal.client import MeterNotFound, PortalClient, PortalError
from .store import Store

log = logging.getLogger("urja.service")


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


class DataUnavailable(Exception):
    """Portal failed and we have nothing cached to fall back on."""


class UrjaService:
    def __init__(self, settings: Settings, client: PortalClient, store: Store):
        self.settings = settings
        self.client = client
        self.store = store
        self._sync_lock = asyncio.Lock()
        self._energy_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.last_sync_error: str | None = None

    # ------------------------------------------------------------- index

    def index_meta(self) -> dict[str, Any]:
        synced_at = self.store.get_state("synced_at")
        age = time.time() - synced_at if synced_at else None
        return {
            "synced_at": _iso(synced_at),
            "age_seconds": round(age, 1) if age is not None else None,
            "stale": age is None or age > self.settings.index_ttl_seconds,
            "source": self.store.get_state("source"),
            "meter_count": self.store.meter_count(),
            "last_error": self.last_sync_error,
        }

    async def ensure_index(self, *, force: bool = False) -> dict[str, Any]:
        """Make sure the index is fresh enough; returns index metadata."""
        meta = self.index_meta()
        if not force and not meta["stale"]:
            return meta
        async with self._sync_lock:
            meta = self.index_meta()
            if not force and not meta["stale"]:
                return meta
            try:
                await self._sync()
                self.last_sync_error = None
            except PortalError as exc:
                self.last_sync_error = f"{type(exc).__name__}: {exc}"
                log.warning("index sync failed: %s", self.last_sync_error)
                if self.store.meter_count() == 0:
                    raise DataUnavailable(f"portal unavailable and no cached data: {exc}") from exc
            return self.index_meta()

    async def _sync(self) -> None:
        started = time.time()
        transformers = await self.client.all_transformers()
        try:
            export = await self.client.export_all()
            meters = [parsing.normalise_export_row(r) for r in export.get("data") or []]
            source = "export"
        except PortalError as exc:
            # Fallback: page through search (no geo/hierarchy/install type).
            log.warning("bulk export failed (%s); falling back to paging through search", exc)
            meters = await self._crawl_search()
            source = "search_crawl"

        resolved = hier.resolve(meters, transformers)
        flags = defaultdict(list)
        for issue in resolved.issues:
            for mid in issue.meter_ids:
                flags[mid].append({"kind": issue.kind, "level": issue.level, "detail": issue.detail})
        for m in meters:
            m["resolved_path"] = resolved.path_for_dt(m["dt_code"]) or {
                lvl: {"code": "", "name": ""} for lvl in parsing.HIERARCHY_LEVELS
            }
            m["quality_flags"] = flags.get(m["meter_id"], [])
        for t in transformers:
            t["aliases"] = resolved.dt_aliases.get(t["code"], [])

        self.store.replace_index(meters, transformers)
        self.store.set_state(
            synced_at=time.time(),
            source=source,
            sync_duration_seconds=round(time.time() - started, 2),
            hierarchy_issues=[i.as_dict() for i in resolved.issues],
        )
        log.info("index synced from %s: %d meters, %d transformers", source, len(meters), len(transformers))

    async def _crawl_search(self) -> list[dict[str, Any]]:
        first = await self.client.search_meters("", 1)
        rows = list(first.get("data") or [])
        pages = -(-first.get("total", 0) // (first.get("pageSize") or 20))
        for page in range(2, pages + 1):
            rows.extend((await self.client.search_meters("", page)).get("data") or [])
        return [parsing.normalise_search_row(r) for r in rows]

    def hierarchy_issues(self) -> list[dict[str, Any]]:
        return self.store.get_state("hierarchy_issues", [])

    def resolved_hierarchy(self) -> tuple[hier.ResolvedHierarchy, dict[str, list[dict[str, Any]]]]:
        meters = self.store.all_meters()
        by_dt: dict[str, list[dict[str, Any]]] = defaultdict(list)
        dt_paths: dict[str, dict[str, dict[str, str]]] = {}
        for m in meters:
            by_dt[m["dt_code"]].append(m)
            dt_paths.setdefault(m["dt_code"], m["hierarchy"])
        aliases = {t["code"]: t["aliases"] for t in self.store.transformers()}
        return hier.ResolvedHierarchy(dt_paths=dt_paths, dt_aliases=aliases, issues=[]), by_dt

    # --------------------------------------------------------- live detail

    async def live_meter(self, meter_id: str) -> dict[str, Any]:
        """Fetch a meter straight from its portal detail page + geo endpoint."""
        page = await self.client.meter_page(meter_id)
        detail = parsing.normalise_meter_detail(page.data or {})
        lat, lng = parsing.parse_geo(await self.client.meter_geo(meter_id))
        detail["latitude"], detail["longitude"] = lat, lng
        return detail

    # --------------------------------------------------------- consumption

    async def consumption(self, meter_id: str, *, refresh: bool = False) -> dict[str, Any]:
        """Normalised series for a meter, served from cache when fresh."""
        async with self._energy_locks[meter_id]:
            cached = self.store.get_energy(meter_id)
            fresh = cached and (time.time() - cached[0]) < self.settings.energy_ttl_seconds
            stale = False
            if refresh or not fresh:
                try:
                    raw = await self.client.meter_energy(meter_id)
                    self.store.put_energy(meter_id, raw)
                    cached = (time.time(), raw)
                except MeterNotFound:
                    raise
                except PortalError as exc:
                    if not cached:
                        raise DataUnavailable(f"portal unavailable and no cached consumption: {exc}") from exc
                    log.warning("energy refresh for %s failed (%s); serving cached copy", meter_id, exc)
                    stale = True
        fetched_at, raw = cached
        series = parsing.normalise_energy(raw)
        return {"fetched_at": _iso(fetched_at), "stale": stale, "series": series}
