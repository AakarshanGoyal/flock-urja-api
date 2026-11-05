"""SQLite-backed local index of meters and transformers, plus a consumption cache.

Why SQLite: the whole dataset is ~400 meters / 40 transformers, it comes from
one bulk export call, and the questions we want to answer (filter on any
attribute, "what's near here", group by hierarchy level) are trivial SQL. It
is a single file with zero setup. See README "Scaling" for where it stops
being the right tool.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from typing import Any, Iterable

from .portal.parsing import HIERARCHY_LEVELS

SCHEMA = """
CREATE TABLE IF NOT EXISTS meters (
    meter_id        TEXT PRIMARY KEY,
    serial_no       TEXT,
    make            TEXT,
    phase_type      TEXT,
    install_status  TEXT,
    install_type    TEXT,
    build           TEXT,
    dt_code         TEXT,
    latitude        REAL,
    longitude       REAL,
    zone_code TEXT, zone_name TEXT,
    circle_code TEXT, circle_name TEXT,
    division_code TEXT, division_name TEXT,
    subdivision_code TEXT, subdivision_name TEXT,
    substation_code TEXT, substation_name TEXT,
    feeder_code TEXT, feeder_name TEXT,
    dt_name TEXT,
    portal_hierarchy TEXT,      -- the path exactly as the portal reported it (JSON)
    quality_flags TEXT          -- JSON list of repairs/observations for this meter
);
CREATE INDEX IF NOT EXISTS idx_meters_dt ON meters(dt_code);
CREATE INDEX IF NOT EXISTS idx_meters_status ON meters(install_status);
CREATE INDEX IF NOT EXISTS idx_meters_latlng ON meters(latitude, longitude);

CREATE TABLE IF NOT EXISTS transformers (
    code          TEXT PRIMARY KEY,
    name          TEXT,
    feeder_code   TEXT,
    capacity_kva  REAL,
    aliases       TEXT
);

CREATE TABLE IF NOT EXISTS energy_cache (
    meter_id    TEXT PRIMARY KEY,
    fetched_at  REAL NOT NULL,
    raw         TEXT NOT NULL      -- raw portal rows (JSON); normalised on read
);

CREATE TABLE IF NOT EXISTS sync_state (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""

SORTABLE = {"meter_id", "serial_no", "make", "phase_type", "install_status", "install_type", "dt_code"}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _sql_haversine(lat1, lon1, lat2, lon2):  # registered as a SQLite function
    if None in (lat1, lon1, lat2, lon2):
        return None
    return haversine_km(lat1, lon1, lat2, lon2)


class Store:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.create_function("haversine_km", 4, _sql_haversine, deterministic=True)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------- sync state

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_state(self, **values: Any) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO sync_state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(k, json.dumps(v)) for k, v in values.items()],
            )

    # -------------------------------------------------------------- writes

    def replace_index(self, meters: Iterable[dict[str, Any]], transformers: Iterable[dict[str, Any]]) -> None:
        """Atomically swap in a fresh snapshot (readers never see a half-written index)."""
        cols = [
            "meter_id", "serial_no", "make", "phase_type", "install_status", "install_type", "build",
            "dt_code", "latitude", "longitude",
            *[f"{lvl}_{part}" for lvl in HIERARCHY_LEVELS[:-1] for part in ("code", "name")],
            "dt_name", "portal_hierarchy", "quality_flags",
        ]
        placeholders = ", ".join("?" for _ in cols)
        rows = []
        for m in meters:
            path = m["resolved_path"]
            values = [
                m["meter_id"], m["serial_no"], m["make"], m["phase_type"], m["install_status"],
                m["install_type"], m["build"], m["dt_code"], m["latitude"], m["longitude"],
            ]
            for lvl in HIERARCHY_LEVELS[:-1]:
                values += [path[lvl]["code"] or None, path[lvl]["name"] or None]
            values += [path["dt"]["name"] or None, json.dumps(m["hierarchy"]), json.dumps(m.get("quality_flags", []))]
            rows.append(values)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM meters")
            self._conn.execute("DELETE FROM transformers")
            self._conn.executemany(f"INSERT INTO meters({', '.join(cols)}) VALUES({placeholders})", rows)
            self._conn.executemany(
                "INSERT INTO transformers(code, name, feeder_code, capacity_kva, aliases) VALUES(?, ?, ?, ?, ?)",
                [
                    (t["code"], t.get("name"), t.get("feederCode"), t.get("capacityKva"), json.dumps(t.get("aliases", [])))
                    for t in transformers
                ],
            )

    def put_energy(self, meter_id: str, raw_rows: list[dict[str, Any]]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO energy_cache(meter_id, fetched_at, raw) VALUES(?, ?, ?) "
                "ON CONFLICT(meter_id) DO UPDATE SET fetched_at = excluded.fetched_at, raw = excluded.raw",
                (meter_id, time.time(), json.dumps(raw_rows)),
            )

    # --------------------------------------------------------------- reads

    def get_energy(self, meter_id: str) -> tuple[float, list[dict[str, Any]]] | None:
        row = self._conn.execute("SELECT fetched_at, raw FROM energy_cache WHERE meter_id = ?", (meter_id,)).fetchone()
        return (row["fetched_at"], json.loads(row["raw"])) if row else None

    def meter_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM meters").fetchone()[0]

    def get_meter(self, meter_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM meters WHERE meter_id = ?", (meter_id,)).fetchone()
        return self._meter_row(row) if row else None

    def query_meters(
        self,
        *,
        filters: dict[str, Any],
        q: str | None = None,
        near: tuple[float, float, float] | None = None,
        has_quality_flags: bool | None = None,
        sort: str = "meter_id",
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[int, list[dict[str, Any]]]:
        where, params = [], []
        for column, value in filters.items():
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                where.append(f"{column} IN ({', '.join('?' for _ in value)})")
                params.extend(value)
            else:
                where.append(f"{column} = ? COLLATE NOCASE")
                params.append(value)
        if q:
            where.append("(meter_id LIKE ? OR serial_no LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if has_quality_flags is not None:
            if has_quality_flags:
                where.append("(quality_flags != '[]' AND quality_flags IS NOT NULL)")
            else:
                where.append("(quality_flags = '[]' OR quality_flags IS NULL)")

        select = "SELECT *"
        order = None
        if near:
            lat, lng, radius = near
            # Cheap bounding-box prefilter on the index, exact distance after.
            dlat = radius / 111.0
            dlng = radius / (111.0 * max(math.cos(math.radians(lat)), 1e-6))
            where.append("latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?")
            params += [lat - dlat, lat + dlat, lng - dlng, lng + dlng]
            where.append("haversine_km(?, ?, latitude, longitude) <= ?")
            params += [lat, lng, radius]
            select += ", haversine_km(?, ?, latitude, longitude) AS distance_km"
            order = "distance_km ASC, meter_id ASC"

        if order is None:
            desc = sort.startswith("-")
            col = sort.lstrip("-")
            if col not in SORTABLE:
                col = "meter_id"
            order = f"{col} {'DESC' if desc else 'ASC'}, meter_id ASC"

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total = self._conn.execute(f"SELECT COUNT(*) FROM meters {where_sql}", params).fetchone()[0]
        select_params = ([near[0], near[1]] if near else []) + params
        rows = self._conn.execute(
            f"{select} FROM meters {where_sql} ORDER BY {order} LIMIT ? OFFSET ?",
            select_params + [page_size, (page - 1) * page_size],
        ).fetchall()
        return total, [self._meter_row(r) for r in rows]

    def all_meters(self) -> list[dict[str, Any]]:
        return [self._meter_row(r) for r in self._conn.execute("SELECT * FROM meters ORDER BY meter_id")]

    def facet_counts(self, column: str) -> dict[str, int]:
        rows = self._conn.execute(
            f"SELECT COALESCE({column}, 'Unknown') AS k, COUNT(*) AS n FROM meters GROUP BY k ORDER BY n DESC"
        ).fetchall()
        return {r["k"]: r["n"] for r in rows}

    def transformers(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT t.*, COUNT(m.meter_id) AS meter_count,
                      SUM(m.install_status = 'Installed') AS installed,
                      SUM(m.install_status = 'Faulty') AS faulty,
                      SUM(m.install_status = 'Decommissioned') AS decommissioned
               FROM transformers t LEFT JOIN meters m ON m.dt_code = t.code
               GROUP BY t.code ORDER BY t.code"""
        ).fetchall()
        return [self._transformer_row(r) for r in rows]

    def transformer(self, code: str) -> dict[str, Any] | None:
        return next((t for t in self.transformers() if t["code"] == code), None)

    # -------------------------------------------------------------- mapping

    @staticmethod
    def _meter_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        hierarchy = {lvl: {"code": d.pop(f"{lvl}_code"), "name": d.pop(f"{lvl}_name")} for lvl in HIERARCHY_LEVELS[:-1]}
        hierarchy["dt"] = {"code": d["dt_code"], "name": d.pop("dt_name")}
        d["hierarchy"] = hierarchy
        d["portal_hierarchy"] = json.loads(d["portal_hierarchy"]) if d.get("portal_hierarchy") else None
        d["quality_flags"] = json.loads(d["quality_flags"]) if d.get("quality_flags") else []
        if d.get("distance_km") is not None:
            d["distance_km"] = round(d["distance_km"], 3)
        return d

    @staticmethod
    def _transformer_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["aliases"] = json.loads(d["aliases"]) if d.get("aliases") else []
        d["status_counts"] = {
            "Installed": d.pop("installed") or 0,
            "Faulty": d.pop("faulty") or 0,
            "Decommissioned": d.pop("decommissioned") or 0,
        }
        return d
