"""SQLite storage. Schema follows docs/SPEC.md with the additions listed in docs/PLAN.md."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS hearing (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    department TEXT,
    original_department TEXT,
    doc_type TEXT,
    ingress TEXT,
    published_date TEXT,
    deadline TEXT,
    status_raw TEXT,
    status TEXT,
    address_list TEXT,
    dt_on_address_list INTEGER,
    dt_responded INTEGER,
    dt_response_no_comments INTEGER,
    tracked INTEGER NOT NULL DEFAULT 0,
    needs_assessment INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_checked TEXT,
    content_changed_at TEXT,
    reported_in INTEGER REFERENCES report(id)
);

CREATE TABLE IF NOT EXISTS document (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hearing_id INTEGER NOT NULL REFERENCES hearing(id),
    url TEXT NOT NULL,
    title TEXT,
    kind TEXT NOT NULL,
    section TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT,
    page_count INTEGER,
    text_path TEXT,
    fetch_status TEXT,
    note TEXT,
    first_seen TEXT NOT NULL,
    last_changed TEXT,
    removed INTEGER NOT NULL DEFAULT 0,
    UNIQUE (hearing_id, url)
);

CREATE TABLE IF NOT EXISTS assessment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hearing_id INTEGER NOT NULL REFERENCES hearing(id),
    created_at TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL,
    context TEXT NOT NULL,
    backtest_run_id INTEGER REFERENCES backtest_run(id),
    relevance TEXT,
    relevance_reason TEXT,
    sectors TEXT,
    summary TEXT,
    privacy_section TEXT,
    reading_guide TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    input_chars INTEGER,
    chunk_count INTEGER,
    batch INTEGER NOT NULL DEFAULT 0,
    cost_estimate REAL,
    raw_response_path TEXT,
    input_hash TEXT,
    notes TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS finding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id INTEGER NOT NULL REFERENCES assessment(id),
    document_id INTEGER REFERENCES document(id),
    document_ref TEXT,
    category TEXT,
    description TEXT,
    chapter TEXT,
    page INTEGER,
    page_stated INTEGER,
    quote TEXT,
    quote_verified INTEGER NOT NULL DEFAULT 0,
    match_score REAL
);

CREATE TABLE IF NOT EXISTS report (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    period_label TEXT NOT NULL,
    path TEXT NOT NULL,
    hearing_count INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'drift'
);

CREATE TABLE IF NOT EXISTS backtest_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    split TEXT,
    period_from TEXT,
    period_to TEXT,
    model TEXT,
    prompt_version TEXT,
    hearing_count INTEGER,
    estimated_cost REAL,
    actual_input_tokens INTEGER NOT NULL DEFAULT 0,
    actual_output_tokens INTEGER NOT NULL DEFAULT 0,
    results_path TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    metrics TEXT
);

CREATE TABLE IF NOT EXISTS job (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    triggered_by TEXT NOT NULL,
    args TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    pid INTEGER,
    log_path TEXT,
    summary TEXT,
    progress TEXT
);

CREATE TABLE IF NOT EXISTS setting (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_document_hearing ON document (hearing_id);
CREATE INDEX IF NOT EXISTS idx_assessment_hearing ON assessment (hearing_id, context, created_at);
CREATE INDEX IF NOT EXISTS idx_finding_assessment ON finding (assessment_id);
CREATE INDEX IF NOT EXISTS idx_hearing_published ON hearing (published_date);
CREATE INDEX IF NOT EXISTS idx_job_started ON job (started_at);

-- What changed in a hearing after it was first seen (deadline and documents; not status).
CREATE TABLE IF NOT EXISTS hearing_change (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hearing_id INTEGER NOT NULL REFERENCES hearing(id),
    detected_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    document_id INTEGER REFERENCES document(id),
    document_title TEXT
);
CREATE INDEX IF NOT EXISTS idx_change_hearing ON hearing_change (hearing_id, detected_at);

-- Which assessment and deadline each operational report showed; the baseline for changes.
CREATE TABLE IF NOT EXISTS report_item (
    report_id INTEGER NOT NULL REFERENCES report(id),
    hearing_id INTEGER NOT NULL REFERENCES hearing(id),
    assessment_id INTEGER REFERENCES assessment(id),
    deadline TEXT,
    PRIMARY KEY (report_id, hearing_id)
);
"""


def now_iso() -> str:
    """UTC timestamp with millisecond precision; sorts correctly as a string."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def parse_ts(value: str | None) -> datetime | None:
    """Parse a stored timestamp and convert it to local time for display."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone()


def format_ts(value: str | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    parsed = parse_ts(value)
    return parsed.strftime(fmt) if parsed else "–"


class DatabaseUnavailable(RuntimeError):
    pass


def connect(db_path: Path) -> sqlite3.Connection:
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.executescript(SCHEMA)
    except (OSError, sqlite3.OperationalError) as exc:
        raise DatabaseUnavailable(
            f"Kan ikke åpne databasen {db_path} for skriving ({exc}). Sjekk at DATA_DIR i .env"
            " er riktig (på lab: /var/lib/horingsradar) og skrivbar for tjenestebrukeren."
        ) from exc
    return conn


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def record_change(
    conn: sqlite3.Connection,
    hearing_id: int,
    kind: str,
    old: str | None = None,
    new: str | None = None,
    document_id: int | None = None,
    document_title: str | None = None,
) -> None:
    """kind: deadline | document_added | document_changed | document_removed."""
    conn.execute(
        "INSERT INTO hearing_change (hearing_id, detected_at, kind, old_value, new_value,"
        " document_id, document_title) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (hearing_id, now_iso(), kind, old, new, document_id, document_title),
    )


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO setting (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def runs_paused(conn: sqlite3.Connection) -> bool:
    return get_setting(conn, "scheduled_runs_paused", "0") == "1"


def latest_assessment(
    conn: sqlite3.Connection, hearing_id: int, context: str = "drift"
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM assessment WHERE hearing_id = ? AND context = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (hearing_id, context),
    ).fetchone()


def findings_for(conn: sqlite3.Connection, assessment_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT f.*, d.title AS document_title, d.url AS document_url, d.page_count "
        "FROM finding f LEFT JOIN document d ON d.id = f.document_id "
        "WHERE f.assessment_id = ? ORDER BY f.id",
        (assessment_id,),
    ).fetchall()


def documents_for(conn: sqlite3.Connection, hearing_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM document WHERE hearing_id = ? AND removed = 0 ORDER BY position, id",
        (hearing_id,),
    ).fetchall()
