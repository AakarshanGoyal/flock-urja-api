"""Response models. These drive both validation and the generated openapi.json."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

InstallStatus = Literal["Installed", "Faulty", "Decommissioned"]


class HierarchyNodeRef(BaseModel):
    code: Optional[str] = Field(None, examples=["F-007"])
    name: Optional[str] = Field(None, examples=["Feeder 7"])


class MeterHierarchy(BaseModel):
    zone: HierarchyNodeRef
    circle: HierarchyNodeRef
    division: HierarchyNodeRef
    subdivision: HierarchyNodeRef
    substation: HierarchyNodeRef
    feeder: HierarchyNodeRef
    dt: HierarchyNodeRef


class QualityFlag(BaseModel):
    kind: str = Field(..., examples=["blank_code_filled"])
    level: str = Field(..., examples=["feeder"])
    detail: str


class Location(BaseModel):
    latitude: float = Field(..., examples=[26.938961])
    longitude: float = Field(..., examples=[75.830957])


class MeterSummary(BaseModel):
    meter_id: str = Field(..., examples=["J100000"])
    serial_no: Optional[str] = Field(None, examples=["SE33962"])
    make: Optional[str] = Field(None, examples=["HPL"])
    phase_type: Optional[str] = Field(None, examples=["single"])
    install_status: Optional[str] = Field(None, examples=["Installed"])
    install_type: Optional[str] = Field(None, examples=["Whole Current"])
    dt_code: Optional[str] = Field(None, examples=["DT-001"])
    feeder_code: Optional[str] = Field(None, examples=["F-001"])
    location: Optional[Location] = None
    distance_km: Optional[float] = Field(None, description="Only present for `near` queries.")


class Meter(MeterSummary):
    build: Optional[str] = Field(None, description="Portal record format: `legacy` or `v2`.", examples=["legacy"])
    hierarchy: MeterHierarchy = Field(..., description="Repaired network path (see PROTOCOL.md).")
    portal_hierarchy: Optional[MeterHierarchy] = Field(
        None, description="The path exactly as the portal reported it, before repairs."
    )
    quality_flags: list[QualityFlag] = Field(default_factory=list)
    links: dict[str, str] = Field(default_factory=dict)


class LiveMeter(BaseModel):
    meter_id: str
    serial_no: Optional[str] = None
    make: Optional[str] = None
    phase_type: Optional[str] = None
    install_status: Optional[str] = None
    install_type: Optional[str] = None
    build: str
    hierarchy: MeterHierarchy
    location: Optional[Location] = None
    differences_from_index: dict[str, dict[str, Optional[str]]] = Field(
        default_factory=dict, description="Fields where the live portal disagrees with the cached index."
    )


class Page(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int


class IndexMeta(BaseModel):
    synced_at: Optional[str] = Field(None, description="When the local index was last rebuilt (UTC).")
    age_seconds: Optional[float] = None
    stale: bool
    source: Optional[str] = Field(None, description="`export` (bulk path) or `search_crawl` (fallback).")
    meter_count: int
    last_error: Optional[str] = None


class MeterList(BaseModel):
    data: list[MeterSummary]
    pagination: Page
    index: IndexMeta


class Reading(BaseModel):
    timestamp: str = Field(..., description="ISO-8601, Asia/Kolkata (+05:30).", examples=["2026-06-24T00:30:00+05:30"])
    kwh_register: Optional[float] = Field(None, description="Cumulative active-energy register (kWh).")
    kvah_register: Optional[float] = Field(None, description="Cumulative apparent-energy register (kVAh).")
    voltage_r: Optional[float] = Field(None, description="R-phase voltage (V).")
    kwh_consumed: Optional[float] = Field(None, description="kWh used since the previous reading.")
    kvah_consumed: Optional[float] = None


class DailyConsumption(BaseModel):
    date: str = Field(..., examples=["2026-06-24"])
    kwh: Optional[float] = None
    kvah: Optional[float] = None
    power_factor: Optional[float] = Field(None, description="kWh / kVAh for the day.")
    intervals: int = Field(..., description="Number of intervals that contributed.")


class ConsumptionSummary(BaseModel):
    first_reading: Optional[str] = None
    last_reading: Optional[str] = None
    total_kwh: Optional[float] = None
    total_kvah: Optional[float] = None
    average_daily_kwh: Optional[float] = None
    power_factor: Optional[float] = None
    voltage_min: Optional[float] = None
    voltage_max: Optional[float] = None
    voltage_avg: Optional[float] = None


class Consumption(BaseModel):
    meter_id: str
    install_status: Optional[str] = None
    resolution: str = Field(..., description="Native resolution of the portal series: `30min` or `daily`.")
    interval_minutes: Optional[int] = None
    granularity: Literal["raw", "daily"]
    fetched_at: Optional[str] = None
    stale: bool = False
    summary: ConsumptionSummary
    issues: list[str] = Field(default_factory=list, description="Data problems found and how they were handled.")
    readings: Optional[list[Reading]] = None
    daily: Optional[list[DailyConsumption]] = None


class Transformer(BaseModel):
    code: str = Field(..., examples=["DT-007"])
    name: Optional[str] = Field(None, examples=["Sanganer DT 7"])
    feeder_code: Optional[str] = Field(None, examples=["F-007"])
    capacity_kva: Optional[float] = Field(None, examples=[63])
    aliases: list[str] = Field(default_factory=list, description="Other names meters use for this DT.")
    meter_count: int
    status_counts: dict[str, int]


class TransformerDetail(Transformer):
    hierarchy: Optional[MeterHierarchy] = None
    meters: list[MeterSummary]


class HierarchyNode(BaseModel):
    id: str = Field(..., description="Path-qualified id; codes are only unique within their parent.", examples=["Z-01/C-01/D-07"])
    level: str
    code: str
    name: str
    meter_count: int
    status_counts: dict[str, int]
    aliases: Optional[list[str]] = None
    meters: Optional[list[str]] = None
    children: Optional[list["HierarchyNode"]] = None


class HierarchyIssue(BaseModel):
    kind: str
    level: str
    dt_code: Optional[str] = None
    detail: str
    meter_ids: list[str]


class Stats(BaseModel):
    meters: int
    transformers: int
    by_status: dict[str, int]
    by_make: dict[str, int]
    by_phase: dict[str, int]
    by_install_type: dict[str, int]
    by_zone: dict[str, int]
    meters_with_quality_flags: int
    index: IndexMeta


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    portal_session_expires_at: Optional[str] = None
    index: IndexMeta
    portal_stats: dict[str, int]


class ErrorBody(BaseModel):
    error: str = Field(..., examples=["meter_not_found"])
    message: str
