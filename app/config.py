"""Runtime configuration, read from environment variables (or a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    portal_base_url: str
    portal_email: str
    portal_password: str
    db_path: str
    # How long the bulk meter/transformer index is considered fresh.
    index_ttl_seconds: float
    # How long a meter's consumption series is cached before we re-fetch it.
    energy_ttl_seconds: float
    # Client-side throttle towards the portal (requests per second).
    portal_max_rps: float
    portal_timeout_seconds: float
    portal_max_retries: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            portal_base_url=os.getenv("URJA_BASE_URL", "https://urja-ops.flockenergy.tech").rstrip("/"),
            portal_email=os.getenv("URJA_EMAIL", ""),
            portal_password=os.getenv("URJA_PASSWORD", ""),
            db_path=os.getenv("URJA_DB_PATH", "urja_index.sqlite3"),
            index_ttl_seconds=_float("URJA_INDEX_TTL_SECONDS", 15 * 60),
            energy_ttl_seconds=_float("URJA_ENERGY_TTL_SECONDS", 10 * 60),
            portal_max_rps=_float("URJA_PORTAL_MAX_RPS", 2.0),
            portal_timeout_seconds=_float("URJA_PORTAL_TIMEOUT_SECONDS", 20.0),
            portal_max_retries=int(_float("URJA_PORTAL_MAX_RETRIES", 4)),
        )
