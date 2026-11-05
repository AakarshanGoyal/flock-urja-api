"""Pure functions that turn raw portal payloads into clean Python structures.

Nothing in here does I/O, so all of the portal's data quirks are handled (and
unit-tested) in one place. See PROTOCOL.md for where each payload comes from.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

# The portal shows naive local timestamps. The utility is in Jaipur, so we
# assume Indian Standard Time and emit timezone-aware ISO-8601 strings.
IST = timezone(timedelta(hours=5, minutes=30), name="IST")

HIERARCHY_LEVELS = ["zone", "circle", "division", "subdivision", "substation", "feeder", "dt"]

# Labels used by the meter detail page (SvelteKit load data) for each level.
DETAIL_HIERARCHY_LABELS = {
    "Zone": "zone",
    "Circle": "circle",
    "Division": "division",
    "Subdivision": "subdivision",
    "Sub Station": "substation",
    "Feeder": "feeder",
    "DT": "dt",
}


class PortalFormatError(ValueError):
    """Raised when a payload doesn't look like anything we've seen before."""


# --------------------------------------------------------------------------- #
# SvelteKit __data.json ("devalue" flattened encoding)
# --------------------------------------------------------------------------- #

_DEVALUE_SPECIAL = {-1: None, -2: None, -3: float("nan"), -4: float("inf"), -5: float("-inf"), -6: -0.0}


def devalue_unflatten(values: list[Any]) -> Any:
    """Decode SvelteKit's `devalue` flattened format.

    The payload is a flat list where index 0 is the root and every object/array
    member is an *index* into that list rather than a literal value, e.g.
    ``[{"meterId": 1}, "J100000"]`` decodes to ``{"meterId": "J100000"}``.
    Shared references are reused, which is why values like "Decommissioned"
    appear only once even when referenced twice.
    """
    if not isinstance(values, list) or not values:
        raise PortalFormatError("devalue payload must be a non-empty list")

    cache: dict[int, Any] = {}

    def hydrate(index: int) -> Any:
        if index in _DEVALUE_SPECIAL:
            return _DEVALUE_SPECIAL[index]
        if index in cache:
            return cache[index]
        raw = values[index]
        if isinstance(raw, list):
            if raw and isinstance(raw[0], str):
                tag = raw[0]
                if tag == "Date":
                    out = raw[1]
                elif tag in ("Set",):
                    out = [hydrate(i) for i in raw[1:]]
                elif tag == "Map":
                    out = {hydrate(raw[i]): hydrate(raw[i + 1]) for i in range(1, len(raw), 2)}
                else:  # BigInt, RegExp, null-prototype objects... not used by this portal
                    out = raw[1] if len(raw) > 1 else None
                cache[index] = out
                return out
            out_list: list[Any] = []
            cache[index] = out_list
            out_list.extend(hydrate(i) for i in raw)
            return out_list
        if isinstance(raw, dict):
            out_dict: dict[str, Any] = {}
            cache[index] = out_dict
            for key, ref in raw.items():
                out_dict[key] = hydrate(ref)
            return out_dict
        cache[index] = raw
        return raw

    return hydrate(0)


@dataclass
class SvelteKitPage:
    """Result of decoding a `/<route>/__data.json` response."""

    kind: str  # "data", "redirect" or "error"
    data: dict[str, Any] | None = None
    redirect_to: str | None = None
    error_status: int | None = None
    error_message: str | None = None


def parse_sveltekit_data(payload: dict[str, Any]) -> SvelteKitPage:
    """Decode the *page* node of a SvelteKit `__data.json` response.

    Quirk: a missing meter comes back as HTTP 200 with an ``error`` node whose
    ``status`` is 404, and an expired session comes back as HTTP 200 with
    ``{"type": "redirect", "location": "/login"}``. The HTTP status alone is
    therefore useless for this endpoint.
    """
    top = payload.get("type")
    if top == "redirect":
        return SvelteKitPage(kind="redirect", redirect_to=payload.get("location"))
    if top != "data":
        raise PortalFormatError(f"unexpected __data.json type: {top!r}")

    nodes = payload.get("nodes") or []
    # Node 0 = root layout, node 1 = (app) layout with the user, last = the page.
    page = nodes[-1] if nodes else None
    if page is None:
        raise PortalFormatError("__data.json has no page node")
    if page.get("type") == "error":
        err = page.get("error") or {}
        return SvelteKitPage(kind="error", error_status=page.get("status"), error_message=err.get("message"))
    if page.get("type") == "redirect":
        return SvelteKitPage(kind="redirect", redirect_to=page.get("location"))
    if page.get("type") != "data":
        raise PortalFormatError(f"unexpected page node type: {page.get('type')!r}")
    return SvelteKitPage(kind="data", data=devalue_unflatten(page["data"]))


# --------------------------------------------------------------------------- #
# Meter detail (two different shapes depending on the meter's "build")
# --------------------------------------------------------------------------- #

# Legacy meters: [{"parameterName": "Serial No", "parameterValue": "..."}]
_LEGACY_KEYS = {
    "Meter ID": "meter_id",
    "Serial No": "serial_no",
    "Make": "make",
    "Phase Type": "phase_type",
    "Installation Status": "install_status",
    "Installation Type": "install_type",
}
# v2 meters: {"classData": "{\"installed_meter\": {\"SerialNo\": ...}}"}  (a JSON *string*)
_V2_KEYS = {
    "MeterId": "meter_id",
    "SerialNo": "serial_no",
    "Make": "make",
    "PhaseType": "phase_type",
    "InstallationStatus": "install_status",
    "InstallationType": "install_type",
}

_LABEL_RE = re.compile(r"^\s*(?P<name>.*?)\s*\((?P<code>[^()]*)\)\s*$")


def parse_hierarchy_label(label: str | None) -> dict[str, str]:
    """Split a detail-page label like ``"Jaipur Zone 1 (Z-01)"`` into name/code."""
    if not label:
        return {"name": "", "code": ""}
    m = _LABEL_RE.match(label)
    if not m:
        return {"name": label.strip(), "code": ""}
    return {"name": m.group("name"), "code": m.group("code").strip()}


def normalise_meter_detail(page_data: dict[str, Any]) -> dict[str, Any]:
    """Normalise the meter detail page's load data into one canonical dict.

    Returns ``{"meter_id", "serial_no", "make", "phase_type", "install_status",
    "install_type", "build", "hierarchy": {level: {name, code}}}``.
    """
    detail = page_data.get("detail") or {}
    fields: dict[str, Any] = {}

    if "classData" in detail:
        build = "v2"
        try:
            inner = json.loads(detail["classData"]).get("installed_meter") or {}
        except (TypeError, json.JSONDecodeError) as exc:
            raise PortalFormatError("v2 meter has unparseable classData") from exc
        for src, dst in _V2_KEYS.items():
            if src in inner:
                fields[dst] = inner[src]
    elif "data" in detail:
        build = "legacy"
        for row in detail["data"]:
            dst = _LEGACY_KEYS.get(row.get("parameterName"))
            if dst:
                fields[dst] = row.get("parameterValue")
    else:
        raise PortalFormatError("meter detail is neither legacy nor v2 shaped")

    hierarchy_raw = page_data.get("hierarchy") or {}
    hierarchy = {
        level: parse_hierarchy_label(hierarchy_raw.get(label))
        for label, level in DETAIL_HIERARCHY_LABELS.items()
    }
    fields.setdefault("meter_id", page_data.get("meterId"))
    fields["build"] = build
    fields["hierarchy"] = hierarchy
    return fields


# --------------------------------------------------------------------------- #
# Export rows (the bulk path) and search rows
# --------------------------------------------------------------------------- #


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def normalise_export_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert one `/portal/export` row into our snake_case meter record."""
    geo = row.get("geo") or {}
    hierarchy_in = row.get("hierarchy") or {}
    hierarchy = {}
    for level in HIERARCHY_LEVELS:
        node = hierarchy_in.get(level) or {}
        hierarchy[level] = {"name": (node.get("name") or "").strip(), "code": (node.get("code") or "").strip()}
    lat, lng = geo.get("lat"), geo.get("lng")
    return {
        "meter_id": row["meterId"],
        "serial_no": _blank_to_none(row.get("serialNo")),
        "make": _blank_to_none(row.get("make")),
        "phase_type": _blank_to_none(row.get("phaseType")),
        "install_status": _blank_to_none(row.get("installStatus")),
        "install_type": _blank_to_none(row.get("installType")),
        "build": _blank_to_none(row.get("build")),
        "dt_code": _blank_to_none(row.get("dtCode")),
        "latitude": float(lat) if lat not in (None, "") else None,
        "longitude": float(lng) if lng not in (None, "") else None,
        "hierarchy": hierarchy,
    }


def normalise_search_row(row: dict[str, Any]) -> dict[str, Any]:
    """Search rows are a subset of export rows (no geo, hierarchy, build, install type)."""
    return {
        "meter_id": row["meterId"],
        "serial_no": _blank_to_none(row.get("serialNo")),
        "make": _blank_to_none(row.get("make")),
        "phase_type": _blank_to_none(row.get("phaseType")),
        "install_status": _blank_to_none(row.get("installStatus")),
        "install_type": None,
        "build": None,
        "dt_code": _blank_to_none(row.get("dtCode")),
        "latitude": None,
        "longitude": None,
        "hierarchy": {level: {"name": "", "code": ""} for level in HIERARCHY_LEVELS},
    }


def parse_geo(payload: dict[str, Any]) -> tuple[float | None, float | None]:
    """`/portal/meters/{id}/geo` returns lat/lng as *strings*."""
    data = payload.get("data") or {}
    lat, lng = data.get("latitude"), data.get("longitude")
    to_f = lambda v: float(v) if v not in (None, "") else None  # noqa: E731
    return to_f(lat), to_f(lng)


# --------------------------------------------------------------------------- #
# Energy / consumption
# --------------------------------------------------------------------------- #


def parse_portal_timestamp(raw: str) -> datetime:
    """Parse ``"24/06/2026 00:30"`` (DD/MM/YYYY HH:MM, local time) into an aware datetime."""
    return datetime.strptime(raw.strip(), "%d/%m/%Y %H:%M").replace(tzinfo=IST)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


@dataclass
class EnergySeries:
    readings: list[dict[str, Any]]
    resolution: str  # "30min", "daily", "unknown"
    interval_minutes: int | None
    issues: list[str] = field(default_factory=list)


def normalise_energy(rows: list[dict[str, Any]]) -> EnergySeries:
    """Clean a raw `/portal/meters/{id}/energy` series.

    * kWh / kVAh are *cumulative register* readings sent as strings; we parse
      them to floats and derive per-interval consumption from the deltas.
    * Some series end with a duplicated timestamp whose kWh/voltage are blank;
      we keep one row per timestamp, preferring the most complete one.
    * Series come in two resolutions (half-hourly and daily); we detect which.
    """
    issues: list[str] = []
    by_ts: dict[datetime, dict[str, Any]] = {}
    bad_ts = 0
    for row in rows:
        try:
            ts = parse_portal_timestamp(row["timestamp"])
        except (KeyError, ValueError, AttributeError):
            bad_ts += 1
            continue
        clean = {
            "timestamp": ts,
            "kwh_register": _num(row.get("kwh")),
            "kvah_register": _num(row.get("kvah")),
            "voltage_r": _num(row.get("voltR")),
        }
        completeness = sum(v is not None for k, v in clean.items() if k != "timestamp")
        if ts in by_ts:
            issues.append(f"duplicate_timestamp:{ts.isoformat()}")
            prev = by_ts[ts]
            prev_completeness = sum(v is not None for k, v in prev.items() if k != "timestamp")
            if completeness <= prev_completeness:
                continue
        by_ts[ts] = clean
    if bad_ts:
        issues.append(f"unparseable_timestamps:{bad_ts}")

    ordered = [by_ts[t] for t in sorted(by_ts)]

    deltas = [
        (b["timestamp"] - a["timestamp"]).total_seconds() / 60 for a, b in zip(ordered, ordered[1:])
    ]
    interval = int(median(deltas)) if deltas else None
    resolution = {30: "30min", 1440: "daily"}.get(interval or -1, "unknown")
    if interval:
        gaps = sum(1 for d in deltas if d > interval * 1.5)
        if gaps:
            issues.append(f"gaps:{gaps}")

    prev_kwh = prev_kvah = None
    out: list[dict[str, Any]] = []
    for r in ordered:
        kwh, kvah = r["kwh_register"], r["kvah_register"]
        consumed = kwh - prev_kwh if (kwh is not None and prev_kwh is not None) else None
        apparent = kvah - prev_kvah if (kvah is not None and prev_kvah is not None) else None
        if consumed is not None and consumed < 0:
            issues.append(f"register_went_backwards:{r['timestamp'].isoformat()}")
            consumed = None
        if kwh is None:
            issues.append(f"missing_kwh:{r['timestamp'].isoformat()}")
        out.append(
            {
                "timestamp": r["timestamp"].isoformat(),
                "kwh_register": kwh,
                "kvah_register": kvah,
                "voltage_r": r["voltage_r"],
                "kwh_consumed": round(consumed, 4) if consumed is not None else None,
                "kvah_consumed": round(apparent, 4) if apparent is not None else None,
            }
        )
        if kwh is not None:
            prev_kwh = kwh
        if kvah is not None:
            prev_kvah = kvah

    return EnergySeries(readings=out, resolution=resolution, interval_minutes=interval, issues=issues)
