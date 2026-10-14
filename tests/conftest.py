from __future__ import annotations

import os
from pathlib import Path

import pytest

from horingsradar.config import load_settings

FIXTURES = Path(__file__).parent / "fixtures"
REGJERINGEN = FIXTURES / "regjeringen"
BASE = "https://www.regjeringen.no"
LIST_URL = f"{BASE}/no/dokument/hoyringar/id1763/"

# Never touch the network or a real API key in tests.
os.environ.pop("ANTHROPIC_API_KEY", None)


def fixture_text(name: str) -> str:
    return (REGJERINGEN / name).read_text(encoding="utf-8")


def fixture_bytes(name: str) -> bytes:
    return (REGJERINGEN / name).read_bytes()


def hearing_url(hearing_id: int) -> str:
    return f"{BASE}/no/dokumenter/horing/id{hearing_id}/"


@pytest.fixture
def settings(tmp_path: Path):
    s = load_settings(
        env_file=tmp_path / "missing.env",
        data_dir=tmp_path / "data",
        anthropic_api_key="test-key",
        web_password_hash="",
    )
    s.ensure_dirs()
    return s
