"""SQLite schema for the EU/EEA radar. Lives only in the EU instance's own database.

`db.connect` creates the shared tables (job, setting, ...); the EU tables are added on top.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from horingsradar import db

EU_SCHEMA = """
-- One case (an EU proposal, later also a consultation), whatever source it was found in.
CREATE TABLE IF NOT EXISTS eu_case (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    celex TEXT UNIQUE,
    com_ref TEXT,
    work_uri TEXT,
    title TEXT NOT NULL,
    doc_type TEXT,
    date_document TEXT,
    eea_flag INTEGER,
    procedure_ref TEXT,
    procedure_status TEXT,
    eurovoc TEXT,
    indexed INTEGER NOT NULL DEFAULT 0,
    candidate INTEGER NOT NULL DEFAULT 0,
    prefilter TEXT,
    needs_assessment INTEGER NOT NULL DEFAULT 0,
    source_modified TEXT,
    first_seen TEXT NOT NULL,
    last_checked TEXT
);

-- Where a case was found. Several sources can point to the same case.
CREATE TABLE IF NOT EXISTS eu_case_source (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES eu_case(id),
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT,
    first_seen TEXT NOT NULL,
    UNIQUE (source, source_id)
);

-- What changed after a case was first seen (procedure status, candidate, new variant).
CREATE TABLE IF NOT EXISTS eu_case_change (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES eu_case(id),
    detected_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT
);

-- English labels of the EuroVoc concepts seen so far.
CREATE TABLE IF NOT EXISTS eu_eurovoc (
    id TEXT PRIMARY KEY,
    label TEXT
);

CREATE INDEX IF NOT EXISTS idx_eu_case_date ON eu_case (date_document);
CREATE INDEX IF NOT EXISTS idx_eu_case_work ON eu_case (work_uri);
CREATE INDEX IF NOT EXISTS idx_eu_case_source_case ON eu_case_source (case_id);
CREATE INDEX IF NOT EXISTS idx_eu_case_change_case ON eu_case_change (case_id, detected_at);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = db.connect(db_path)
    conn.executescript(EU_SCHEMA)
    return conn


def record_change(
    conn: sqlite3.Connection, case_id: int, kind: str, old: str | None, new: str | None
) -> None:
    """kind: procedure_status | candidate | variant (a corrigendum or extra part)."""
    conn.execute(
        "INSERT INTO eu_case_change (case_id, detected_at, kind, old_value, new_value)"
        " VALUES (?, ?, ?, ?, ?)",
        (case_id, db.now_iso(), kind, old, new),
    )
