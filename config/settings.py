"""Loads and validates configuration from environment variables / .env."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator

BASE_URLS = {
    "demo": "https://demo-api.ramp.com",
    "production": "https://api.ramp.com",
}

# Every endpoint this tool calls, and the scope Ramp requires for it:
#   GET  /transactions            transactions:read
#   GET  /users/{id}              users:read
#   GET  /funds/{id}              funds:read        (card entity fallback)
#   GET  /departments             departments:read
#   GET  /entities                entities:read     (entity names in reports only)
#   GET  /accounting/fields(-options)  accounting:read
#   POST /accounting/codings      accounting:write
DEFAULT_SCOPES = (
    "transactions:read users:read funds:read departments:read entities:read "
    "accounting:read accounting:write"
)


class Settings(BaseModel):
    client_id: str = Field(min_length=1)
    client_secret: str = Field(min_length=1)
    environment: Literal["demo", "production"] = "demo"
    scopes: str = DEFAULT_SCOPES

    # The Ramp accounting field that holds the NetSuite Department. Set either
    # the field's ramp_id (preferred; see scripts/discover_fields.py) or its name.
    department_field_id: str = ""
    department_field_name: str = "Department"

    # Optional JSON file overriding Ramp department -> NetSuite department option.
    department_map_file: str = ""

    poll_interval_minutes: int = Field(default=15, ge=1)
    lookback_days: int = Field(default=30, ge=1)
    dry_run: bool = False
    log_level: str = "INFO"
    state_db_path: str = "state.db"
    cache_ttl_seconds: int = Field(default=900, ge=0)
    max_retries: int = Field(default=5, ge=0)

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @property
    def base_url(self) -> str:
        return BASE_URLS[self.environment]


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    if env_file:
        load_dotenv(env_file)
    env = os.environ
    raw = {
        "client_id": env.get("RAMP_CLIENT_ID", ""),
        "client_secret": env.get("RAMP_CLIENT_SECRET", ""),
        "environment": env.get("RAMP_ENVIRONMENT", "demo").strip().lower(),
        "scopes": env.get("RAMP_SCOPES") or DEFAULT_SCOPES,
        "department_field_id": env.get("NETSUITE_DEPARTMENT_TRACKING_CATEGORY_ID", ""),
        "department_field_name": env.get("NETSUITE_DEPARTMENT_FIELD_NAME", "Department"),
        "department_map_file": env.get("DEPARTMENT_MAP_FILE", ""),
        "poll_interval_minutes": env.get("POLL_INTERVAL_MINUTES", "15"),
        "lookback_days": env.get("LOOKBACK_DAYS", "30"),
        "dry_run": _bool(env.get("DRY_RUN")),
        "log_level": env.get("LOG_LEVEL", "INFO"),
        "state_db_path": env.get("STATE_DB_PATH", "state.db"),
        "cache_ttl_seconds": env.get("CACHE_TTL_SECONDS", "900"),
        "max_retries": env.get("MAX_RETRIES", "5"),
    }
    try:
        return Settings(**raw)
    except ValidationError as exc:
        raise SystemExit(f"Invalid configuration:\n{exc}") from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
