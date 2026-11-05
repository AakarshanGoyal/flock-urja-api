"""FastAPI application: the clean API in front of the Urja Meter Ops portal."""

from __future__ import annotations

import asyncio
import logging
import math
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Path as PathParam, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from . import models
from .config import Settings
from .hierarchy import build_tree
from .portal.client import MeterNotFound, PortalAuthError, PortalClient, PortalError
from .portal.parsing import HIERARCHY_LEVELS
from .service import DataUnavailable, UrjaService
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("urja.api")

STATIC_DIR = Path(__file__).parent / "static"

DESCRIPTION = """
A clean, read-only REST API over the **Urja Meter Ops** portal (a legacy
SvelteKit app with no public API).

* Meter, transformer and hierarchy data come from a **local index** rebuilt
  from the portal's bulk export (one signed request) every 15 minutes.
* Consumption is fetched **live per meter** and cached for 10 minutes.
* All timestamps are ISO-8601 in Asia/Kolkata (+05:30). Energy values are numbers, not strings.
* Responses served from a stale cache (portal down) carry `X-Data-Stale: true`.

See `PROTOCOL.md` in the repository for how the portal works underneath.
"""


def create_app(settings: Settings | None = None, client: PortalClient | None = None, warm_index: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        portal = client or PortalClient(settings)
        store = Store(settings.db_path)
        app.state.service = UrjaService(settings, portal, store)
        warm = None
        if warm_index:
            # Warm the index in the background so the first request is fast,
            # without blocking startup if the portal is slow or down.
            warm = asyncio.create_task(_warm(app.state.service))
        yield
        if warm:
            warm.cancel()
        await portal.aclose()
        store.close()

    app = FastAPI(
        title="Urja Meter API",
        version="1.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        responses={503: {"model": models.ErrorBody, "description": "Portal unavailable and no cached data"}},
    )
    _register_errors(app)
    _register_routes(app)
    return app


async def _warm(service: UrjaService) -> None:
    try:
        await service.ensure_index()
    except Exception as exc:  # noqa: BLE001 - startup warm-up must never crash the app
        log.warning("initial index sync failed: %s", exc)


def _svc(request: Request) -> UrjaService:
    return request.app.state.service


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message})


def _register_errors(app: FastAPI) -> None:
    @app.exception_handler(MeterNotFound)
    async def _not_found(_: Request, exc: MeterNotFound):
        return _err(404, "not_found", str(exc))

    @app.exception_handler(DataUnavailable)
    async def _unavailable(_: Request, exc: DataUnavailable):
        return _err(503, "portal_unavailable", str(exc))

    @app.exception_handler(PortalAuthError)
    async def _auth(_: Request, exc: PortalAuthError):
        return _err(502, "portal_auth_failed", str(exc))

    @app.exception_handler(PortalError)
    async def _portal(_: Request, exc: PortalError):
        return _err(502, "portal_error", str(exc))


def _location(m: dict[str, Any]) -> dict[str, float] | None:
    if m.get("latitude") is None or m.get("longitude") is None:
        return None
    return {"latitude": m["latitude"], "longitude": m["longitude"]}


def _summary(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "meter_id": m["meter_id"],
        "serial_no": m["serial_no"],
        "make": m["make"],
        "phase_type": m["phase_type"],
        "install_status": m["install_status"],
        "install_type": m["install_type"],
        "dt_code": m["dt_code"],
        "feeder_code": m["hierarchy"]["feeder"]["code"],
        "location": _location(m),
        "distance_km": m.get("distance_km"),
    }


def _full(m: dict[str, Any]) -> dict[str, Any]:
    out = _summary(m)
    out.update(
        build=m["build"],
        hierarchy=m["hierarchy"],
        portal_hierarchy=m["portal_hierarchy"],
        quality_flags=m["quality_flags"],
        links={
            "self": f"/meters/{m['meter_id']}",
            "consumption": f"/meters/{m['meter_id']}/consumption",
            "live": f"/meters/{m['meter_id']}/live",
            "transformer": f"/transformers/{m['dt_code']}",
        },
    )
    return out


def _mark_stale(response: Response, stale: bool) -> None:
    if stale:
        response.headers["X-Data-Stale"] = "true"


def _normalise_id(meter_id: str) -> str:
    # The portal's ids are case-sensitive (j100000 -> 404); accept either.
    return meter_id.strip().upper()


def _register_routes(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health", response_model=models.Health, tags=["meta"], summary="Service and portal health")
    async def health(request: Request):
        svc = _svc(request)
        meta = svc.index_meta()
        exp = svc.client.session_expires_at
        return {
            "status": "ok" if meta["meter_count"] and not svc.last_sync_error else "degraded",
            "portal_session_expires_at": datetime.fromtimestamp(exp, tz=timezone.utc).isoformat() if exp else None,
            "index": meta,
            "portal_stats": svc.client.stats,
        }

    @app.post("/sync", response_model=models.IndexMeta, tags=["meta"], summary="Force a rebuild of the local index")
    async def sync(request: Request):
        return await _svc(request).ensure_index(force=True)

    # ------------------------------------------------------------ meters

    @app.get("/meters", response_model=models.MeterList, tags=["meters"], summary="List and filter meters")
    async def list_meters(
        request: Request,
        response: Response,
        status: Optional[list[str]] = Query(None, description="Installed / Faulty / Decommissioned (repeatable)"),
        make: Optional[list[str]] = Query(None, description="e.g. HPL, Genus, L&T (repeatable)"),
        phase: Optional[Literal["single", "three"]] = None,
        install_type: Optional[str] = Query(None, description="`Whole Current` or `CT Operated`"),
        build: Optional[Literal["legacy", "v2"]] = None,
        dt: Optional[str] = Query(None, description="Transformer code, e.g. DT-007"),
        feeder: Optional[str] = Query(None, examples=["F-007"]),
        substation: Optional[str] = Query(None, examples=["SS-07"]),
        zone: Optional[str] = Query(None, examples=["Z-01"]),
        q: Optional[str] = Query(None, description="Substring match on meter id or serial number"),
        near_lat: Optional[float] = Query(None, ge=-90, le=90, description="With near_lng: sort by distance"),
        near_lng: Optional[float] = Query(None, ge=-180, le=180),
        radius_km: float = Query(2.0, gt=0, le=100, description="Radius for near queries"),
        has_quality_flags: Optional[bool] = Query(None, description="Only meters whose data needed repair"),
        sort: str = Query("meter_id", description="Field to sort by; prefix with `-` for descending"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=500),
    ):
        svc = _svc(request)
        meta = await svc.ensure_index()
        if (near_lat is None) != (near_lng is None):
            raise HTTPException(422, "near_lat and near_lng must be given together")
        filters: dict[str, Any] = {
            "install_status": status,
            "make": make,
            "phase_type": phase,
            "install_type": install_type,
            "build": build,
            "dt_code": dt.upper() if dt else None,
            "feeder_code": feeder.upper() if feeder else None,
            "substation_code": substation.upper() if substation else None,
            "zone_code": zone.upper() if zone else None,
        }
        near = (near_lat, near_lng, radius_km) if near_lat is not None else None
        total, rows = svc.store.query_meters(
            filters=filters,
            q=q,
            near=near,
            has_quality_flags=has_quality_flags,
            sort=sort,
            page=page,
            page_size=page_size,
        )
        _mark_stale(response, meta["stale"])
        return {
            "data": [_summary(r) for r in rows],
            "pagination": {"page": page, "page_size": page_size, "total": total, "total_pages": max(1, math.ceil(total / page_size))},
            "index": meta,
        }

    meter_id_param = PathParam(..., description="Meter id, e.g. J100000 (case-insensitive)", examples=["J100000"])

    @app.get(
        "/meters/{meter_id}",
        response_model=models.Meter,
        tags=["meters"],
        summary="One meter, with its repaired network path",
        responses={404: {"model": models.ErrorBody}},
    )
    async def get_meter(request: Request, response: Response, meter_id: str = meter_id_param):
        svc = _svc(request)
        meta = await svc.ensure_index()
        m = svc.store.get_meter(_normalise_id(meter_id))
        if not m:
            raise MeterNotFound(f"Meter {meter_id} not found")
        _mark_stale(response, meta["stale"])
        return _full(m)

    @app.get(
        "/meters/{meter_id}/live",
        response_model=models.LiveMeter,
        tags=["meters"],
        summary="Fetch a meter directly from the portal (bypasses the index)",
        description="Reads the portal's meter detail page and geo endpoint. Handles both the "
        "`legacy` and `v2` record formats, and reports any fields that differ from the cached index.",
        responses={404: {"model": models.ErrorBody}},
    )
    async def live_meter(request: Request, meter_id: str = meter_id_param):
        svc = _svc(request)
        mid = _normalise_id(meter_id)
        live = await svc.live_meter(mid)
        indexed = svc.store.get_meter(mid)
        diffs: dict[str, dict[str, Optional[str]]] = {}
        if indexed:
            for key in ("serial_no", "make", "phase_type", "install_status", "install_type"):
                if (live.get(key) or None) != (indexed.get(key) or None):
                    diffs[key] = {"live": live.get(key), "index": indexed.get(key)}
        return {
            "meter_id": live.get("meter_id") or mid,
            "serial_no": live.get("serial_no"),
            "make": live.get("make"),
            "phase_type": live.get("phase_type"),
            "install_status": live.get("install_status"),
            "install_type": live.get("install_type"),
            "build": live["build"],
            "hierarchy": live["hierarchy"],
            "location": _location(live),
            "differences_from_index": diffs,
        }

    @app.get(
        "/meters/{meter_id}/consumption",
        response_model=models.Consumption,
        response_model_exclude_none=True,
        tags=["meters"],
        summary="Consumption readings for a meter",
        description="The portal stores cumulative register readings (as strings, DD/MM/YYYY timestamps). "
        "This endpoint returns numbers, ISO timestamps, de-duplicated rows and per-interval consumption. "
        "`granularity=daily` aggregates to IST calendar days (an interval belongs to the day it starts in).",
        responses={404: {"model": models.ErrorBody}},
    )
    async def consumption(
        request: Request,
        response: Response,
        meter_id: str = meter_id_param,
        granularity: Literal["raw", "daily"] = "raw",
        start: Optional[date] = Query(None, alias="from", description="Inclusive start date (IST)"),
        end: Optional[date] = Query(None, alias="to", description="Inclusive end date (IST)"),
        refresh: bool = Query(False, description="Bypass the consumption cache"),
    ):
        svc = _svc(request)
        mid = _normalise_id(meter_id)
        result = await svc.consumption(mid, refresh=refresh)
        series = result["series"]
        readings = [
            r for r in series.readings
            if (start is None or r["timestamp"][:10] >= start.isoformat())
            and (end is None or r["timestamp"][:10] <= end.isoformat())
        ]
        try:  # status is a nice-to-have here; don't fail consumption if the index can't load
            await svc.ensure_index()
        except DataUnavailable:
            pass
        indexed = svc.store.get_meter(mid)
        issues = list(series.issues)
        summary = _consumption_summary(readings, series.interval_minutes)
        status = indexed["install_status"] if indexed else None
        if status == "Decommissioned" and (summary.get("total_kwh") or 0) > 0:
            issues.append("decommissioned_meter_still_reporting_consumption")
        _mark_stale(response, result["stale"])
        body: dict[str, Any] = {
            "meter_id": mid,
            "install_status": status,
            "resolution": series.resolution,
            "interval_minutes": series.interval_minutes,
            "granularity": granularity,
            "fetched_at": result["fetched_at"],
            "stale": result["stale"],
            "summary": summary,
            "issues": issues,
        }
        if granularity == "raw":
            body["readings"] = readings
        else:
            body["daily"] = _daily(readings, series.interval_minutes)
        return body

    # ------------------------------------------------------- transformers

    @app.get("/transformers", response_model=list[models.Transformer], tags=["network"], summary="All distribution transformers")
    async def transformers(request: Request, response: Response):
        svc = _svc(request)
        meta = await svc.ensure_index()
        _mark_stale(response, meta["stale"])
        return svc.store.transformers()

    @app.get(
        "/transformers/{code}",
        response_model=models.TransformerDetail,
        tags=["network"],
        summary="One transformer, its network path and its meters",
        responses={404: {"model": models.ErrorBody}},
    )
    async def transformer(request: Request, response: Response, code: str = PathParam(..., examples=["DT-007"])):
        svc = _svc(request)
        meta = await svc.ensure_index()
        t = svc.store.transformer(code.upper())
        if not t:
            raise MeterNotFound(f"Transformer {code} not found")
        _, meters = svc.store.query_meters(filters={"dt_code": t["code"]}, page=1, page_size=10_000)
        _mark_stale(response, meta["stale"])
        return {**t, "hierarchy": meters[0]["hierarchy"] if meters else None, "meters": [_summary(m) for m in meters]}

    # ---------------------------------------------------------- hierarchy

    @app.get(
        "/hierarchy",
        response_model=list[models.HierarchyNode],
        response_model_exclude_none=True,
        tags=["network"],
        summary="Reconstructed network tree with meter counts",
        description="Zone > Circle > Division > Subdivision > Substation > Feeder > DT. Node ids are "
        "path-qualified because the portal reuses codes under different parents.",
    )
    async def hierarchy(
        request: Request,
        depth: Literal["zone", "circle", "division", "subdivision", "substation", "feeder", "dt"] = "dt",
        include_meters: bool = Query(False, description="List meter ids under each DT"),
    ):
        svc = _svc(request)
        await svc.ensure_index()
        resolved, by_dt = svc.resolved_hierarchy()
        return build_tree(resolved, by_dt, max_depth=depth, include_meters=include_meters)

    @app.get(
        "/hierarchy/issues",
        response_model=list[models.HierarchyIssue],
        tags=["network"],
        summary="Data-quality problems found while reconstructing the hierarchy",
    )
    async def hierarchy_issues(
        request: Request,
        kind: Optional[str] = Query(None, description="e.g. blank_code_filled, dt_name_mismatch, code_reused_across_parents"),
        level: Optional[str] = Query(None, description=f"One of {', '.join(HIERARCHY_LEVELS)}"),
    ):
        svc = _svc(request)
        await svc.ensure_index()
        return [
            i for i in svc.hierarchy_issues()
            if (kind is None or i["kind"] == kind) and (level is None or i["level"] == level)
        ]

    @app.get("/stats", response_model=models.Stats, tags=["meta"], summary="Fleet summary counts")
    async def stats(request: Request):
        svc = _svc(request)
        meta = await svc.ensure_index()
        st = svc.store
        flagged = sum(1 for m in st.all_meters() if m["quality_flags"])
        return {
            "meters": st.meter_count(),
            "transformers": len(st.transformers()),
            "by_status": st.facet_counts("install_status"),
            "by_make": st.facet_counts("make"),
            "by_phase": st.facet_counts("phase_type"),
            "by_install_type": st.facet_counts("install_type"),
            "by_zone": st.facet_counts("zone_code"),
            "meters_with_quality_flags": flagged,
            "index": meta,
        }


def _consumption_summary(readings: list[dict[str, Any]], interval: Optional[int]) -> dict[str, Any]:
    if not readings:
        return {}
    kwh = [r["kwh_consumed"] for r in readings if r["kwh_consumed"] is not None]
    kvah = [r["kvah_consumed"] for r in readings if r["kvah_consumed"] is not None]
    volts = [r["voltage_r"] for r in readings if r["voltage_r"] is not None]
    total_kwh = round(sum(kwh), 3) if kwh else None
    total_kvah = round(sum(kvah), 3) if kvah else None
    days = (len(kwh) * interval / 1440) if (kwh and interval) else None
    return {
        "first_reading": readings[0]["timestamp"],
        "last_reading": readings[-1]["timestamp"],
        "total_kwh": total_kwh,
        "total_kvah": total_kvah,
        "average_daily_kwh": round(total_kwh / days, 3) if (total_kwh is not None and days) else None,
        "power_factor": round(total_kwh / total_kvah, 3) if (total_kwh and total_kvah) else None,
        "voltage_min": min(volts) if volts else None,
        "voltage_max": max(volts) if volts else None,
        "voltage_avg": round(sum(volts) / len(volts), 1) if volts else None,
    }


def _daily(readings: list[dict[str, Any]], interval: Optional[int]) -> list[dict[str, Any]]:
    """Bucket interval consumption by the IST day each interval *starts* in.

    A reading at time T closes the interval (T - interval, T]. That way the
    00:00 reading of a daily series counts towards the previous day, matching
    the half-hourly series.
    """
    buckets: dict[str, dict[str, Any]] = {}
    step = timedelta(minutes=interval or 0)
    for r in readings:
        if r["kwh_consumed"] is None:
            continue
        start = datetime.fromisoformat(r["timestamp"]) - step
        b = buckets.setdefault(start.date().isoformat(), {"kwh": 0.0, "kvah": 0.0, "intervals": 0, "has_kvah": False})
        b["kwh"] += r["kwh_consumed"]
        b["intervals"] += 1
        if r["kvah_consumed"] is not None:
            b["kvah"] += r["kvah_consumed"]
            b["has_kvah"] = True
    out = []
    for day in sorted(buckets):
        b = buckets[day]
        kvah = round(b["kvah"], 3) if b["has_kvah"] else None
        out.append(
            {
                "date": day,
                "kwh": round(b["kwh"], 3),
                "kvah": kvah,
                "power_factor": round(b["kwh"] / b["kvah"], 3) if kvah else None,
                "intervals": b["intervals"],
            }
        )
    return out


app = create_app()
