"""Settings for the EU/EEA radar instance: the shared settings plus the EU fields.

`RADAR=eu` marks the instance's `.env`. The EU commands refuse to start without it, so they
never write to another instance's database by mistake.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator

from horingsradar.config import Settings, find_env_file, parse_env_file

RADAR = "eu"


class EUSettings(Settings):
    radar: str = ""
    # EEA-Lex asks for 5 seconds between requests (robots.txt); the EU instance uses it for all.
    request_interval_seconds: float = 5.0
    # First run looks this far back for proposals; consultations that are open are always taken.
    eu_initial_lookback_days: int = 730
    # Later runs go back to the previous run minus this. EuroVoc indexing arrives days or weeks
    # after a proposal is published, so recent proposals are looked at again.
    eu_discovery_overlap_days: int = 60
    # One keyword per line; empty means the built-in list (eu/prefilter.py).
    eu_keywords_file: Path | None = None
    eu_prompt_version: str = "v1"

    @field_validator("request_interval_seconds")
    @classmethod
    def _eea_lex_crawl_delay(cls, v: float) -> float:
        if v < 5.0:
            raise ValueError(
                "REQUEST_INTERVAL_SECONDS must be at least 5 for the EU radar (EEA-Lex)"
            )
        return v

    @field_validator("eu_keywords_file", mode="before")
    @classmethod
    def _empty_path(cls, v: object) -> object:
        return None if v in ("", None) else v


def load_eu_settings(env_file: Path | None = None, **overrides: object) -> EUSettings:
    values: dict[str, object] = {}
    path = env_file or find_env_file()
    if path and path.is_file():
        values.update({k.lower(): v for k, v in parse_env_file(path).items()})
    for name in EUSettings.model_fields:
        env_value = os.environ.get(name.upper())
        if env_value is not None:
            values[name] = env_value
    values.update(overrides)
    known = {k: v for k, v in values.items() if k in EUSettings.model_fields}
    return EUSettings(**known)


@lru_cache(maxsize=1)
def get_eu_settings() -> EUSettings:
    return load_eu_settings()


def require_eu_instance(settings: EUSettings) -> None:
    if settings.radar != RADAR:
        raise SystemExit(
            "horingsradar-eu krever RADAR=eu i .env (se .env.eu.example). Stopper, så "
            f"databasen i {settings.data_dir} ikke brukes av feil radar."
        )
