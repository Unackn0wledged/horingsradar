"""Configuration loaded from environment variables and an optional `.env` file.

Precedence: real environment variables > `.env` file > defaults. The `.env` file is looked up
via `ENV_FILE`, then the current directory, then the repository root.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
MIN_REQUEST_INTERVAL = 2.0


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal KEY=VALUE file. Supports comments, `export` and quoted values."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def find_env_file() -> Path | None:
    explicit = os.environ.get("ENV_FILE")
    if explicit:
        return Path(explicit)
    for candidate in (Path.cwd() / ".env", REPO_ROOT / ".env"):
        if candidate.is_file():
            return candidate
    return None


class Settings(BaseModel):
    data_dir: Path = Path("./data")
    user_agent: str = "Horingsradar/0.1 (pilot for Datatilsynet)"
    request_interval_seconds: float = MIN_REQUEST_INTERVAL
    http_timeout_seconds: float = 60.0
    html_cache_max_age_seconds: int = 3600
    regjeringen_base_url: str = "https://www.regjeringen.no"
    initial_lookback_days: int = 90
    discovery_overlap_days: int = 14
    max_list_pages: int = 60

    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-5-5"
    llm_effort: str = "medium"
    llm_max_output_tokens: int = 16000
    llm_timeout_seconds: float = 600.0
    llm_refusal_fallback: bool = True
    anthropic_api_key: str = Field(default="", repr=False)
    anthropic_base_url: str = "https://api.anthropic.com"
    prompt_version: str = "v1"
    prompts_dir: Path = REPO_ROOT / "prompts"
    max_input_tokens: int = 150_000
    chars_per_token: float = 3.0

    price_input_per_mtok: float = 2.0
    price_output_per_mtok: float = 10.0
    batch_price_factor: float = 0.5
    backtest_poll_seconds: int = 60

    web_bind: str = "127.0.0.1:8080"
    web_username: str = "operator"
    web_password_hash: str = Field(default="", repr=False)
    run_timer_unit: str = "horingsradar-run.timer"

    log_level: str = "INFO"

    @field_validator("request_interval_seconds")
    @classmethod
    def _min_interval(cls, v: float) -> float:
        if v < MIN_REQUEST_INTERVAL:
            raise ValueError(f"REQUEST_INTERVAL_SECONDS must be at least {MIN_REQUEST_INTERVAL}")
        return v

    @field_validator("llm_provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        if v != "anthropic":
            raise ValueError("Only LLM_PROVIDER=anthropic is implemented in the pilot")
        return v

    @property
    def web_binds(self) -> list[tuple[str, int]]:
        binds = []
        for part in self.web_bind.split(","):
            part = part.strip()
            if not part:
                continue
            host, _, port = part.rpartition(":")
            binds.append((host.strip("[]"), int(port)))
        return binds

    # Paths inside DATA_DIR
    @property
    def db_path(self) -> Path:
        return self.data_dir / "horingsradar.sqlite3"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "horingsradar.lock"

    @property
    def http_cache_dir(self) -> Path:
        return self.data_dir / "http_cache"

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def text_dir(self) -> Path:
        return self.data_dir / "text"

    @property
    def llm_raw_dir(self) -> Path:
        return self.data_dir / "llm_raw"

    @property
    def llm_cache_dir(self) -> Path:
        return self.data_dir / "llm_cache"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def backtests_dir(self) -> Path:
        return self.data_dir / "backtests"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.http_cache_dir,
            self.documents_dir,
            self.text_dir,
            self.llm_raw_dir,
            self.llm_cache_dir,
            self.reports_dir,
            self.backtests_dir,
            self.logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def load_settings(env_file: Path | None = None, **overrides: object) -> Settings:
    values: dict[str, object] = {}
    path = env_file or find_env_file()
    if path and path.is_file():
        values.update({k.lower(): v for k, v in parse_env_file(path).items()})
    for name in Settings.model_fields:
        env_value = os.environ.get(name.upper())
        if env_value is not None:
            values[name] = env_value
    values.update(overrides)
    known = {k: v for k, v in values.items() if k in Settings.model_fields}
    return Settings(**known)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
