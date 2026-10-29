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

-- What changed after a case was first seen (procedure status, candidate, new variant, EEA status).
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

-- The English PDF of a case, and where its explanatory memorandum is.
CREATE TABLE IF NOT EXISTS eu_document (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES eu_case(id),
    url TEXT NOT NULL,
    sha256 TEXT,
    page_count INTEGER,
    text_path TEXT,
    memo_first_page INTEGER,
    memo_last_page INTEGER,
    memo_found INTEGER,
    memo_truncated INTEGER,
    memo_chars INTEGER,
    fetch_status TEXT NOT NULL,
    note TEXT,
    first_seen TEXT NOT NULL,
    last_changed TEXT,
    UNIQUE (case_id)
);

-- LLM assessments of a case. relevance is the final level; llm_relevance what the model said
-- (they differ only when a rule such as the EDPS rule raised the level, see `rule`).
CREATE TABLE IF NOT EXISTS eu_assessment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES eu_case(id),
    created_at TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL,
    llm_relevance TEXT,
    relevance TEXT,
    rule TEXT,
    relevance_reason TEXT,
    sectors TEXT,
    summary TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    input_chars INTEGER,
    cost_estimate REAL,
    raw_response_path TEXT,
    input_hash TEXT,
    notes TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS eu_finding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id INTEGER NOT NULL REFERENCES eu_assessment(id),
    category TEXT,
    description TEXT,
    chapter TEXT,
    page INTEGER,
    page_stated INTEGER,
    quote TEXT,
    quote_verified INTEGER NOT NULL DEFAULT 0,
    match_score REAL
);

CREATE TABLE IF NOT EXISTS eu_report (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    period_label TEXT NOT NULL,
    path TEXT NOT NULL,
    case_count INTEGER NOT NULL
);

-- What each report showed for a case; the baseline for "changed since the last report".
CREATE TABLE IF NOT EXISTS eu_report_item (
    report_id INTEGER NOT NULL REFERENCES eu_report(id),
    case_id INTEGER NOT NULL REFERENCES eu_case(id),
    assessment_id INTEGER REFERENCES eu_assessment(id),
    procedure_status TEXT,
    eea_status TEXT,
    PRIMARY KEY (report_id, case_id)
);

-- Have Your Say initiatives found by a candidate's COM number (eu_case.hys_initiative_id).
-- Only these fields are kept; the portal's JSON also names the officials responsible.
CREATE TABLE IF NOT EXISTS eu_hys_initiative (
    id INTEGER PRIMARY KEY,  -- the portal's initiative ID
    path TEXT NOT NULL,  -- 14628-Cloud-and-AI-Development-Act, for the page URL
    reference TEXT,
    short_title TEXT,
    first_seen TEXT NOT NULL,
    checked_at TEXT NOT NULL
);

-- One per stage of an initiative, each with its own feedback period.
CREATE TABLE IF NOT EXISTS eu_hys_publication (
    id INTEGER PRIMARY KEY,  -- the portal's publication ID
    initiative_id INTEGER NOT NULL REFERENCES eu_hys_initiative(id),
    type TEXT,
    stage TEXT,
    reference TEXT,
    published TEXT,
    deadline TEXT,
    status TEXT,
    planned_period TEXT,
    is_current INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_eu_case_date ON eu_case (date_document);
CREATE INDEX IF NOT EXISTS idx_eu_assessment_case ON eu_assessment (case_id, created_at);
CREATE INDEX IF NOT EXISTS idx_eu_finding_assessment ON eu_finding (assessment_id);
CREATE INDEX IF NOT EXISTS idx_eu_case_work ON eu_case (work_uri);
CREATE INDEX IF NOT EXISTS idx_eu_case_source_case ON eu_case_source (case_id);
CREATE INDEX IF NOT EXISTS idx_eu_case_change_case ON eu_case_change (case_id, detected_at);
CREATE INDEX IF NOT EXISTS idx_eu_hys_publication_initiative ON eu_hys_publication (initiative_id);
"""


# Columns added after milestone 1. Added with ALTER TABLE, so existing databases get them too.
EU_CASE_COLUMNS = {
    "eea_lex_id": "TEXT",  # numeric key for proposals, CELEX for adopted acts
    "eea_status": "TEXT",  # eu/sources/eealex.py STATUSES, or NOT_REGISTERED
    "eea_status_derived": "INTEGER",  # 1 when inferred from the list, not read from a fact sheet
    "eea_areas": "TEXT",  # JSON list, e.g. ["XI.III Data Protection"]
    "eea_jcd": "TEXT",
    "eea_checked_at": "TEXT",
    "reported_in": "INTEGER",  # the last EU report that showed the case
    "hys_initiative_id": "INTEGER",  # eu_hys_initiative, found by the COM number
    "hys_status": "TEXT",  # found | not_found | mismatch
    "hys_checked_at": "TEXT",
}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = db.connect(db_path)
    conn.executescript(EU_SCHEMA)
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(eu_case)")}
    for name, decl in EU_CASE_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE eu_case ADD COLUMN {name} {decl}")
    return conn


def record_change(
    conn: sqlite3.Connection, case_id: int, kind: str, old: str | None, new: str | None
) -> None:
    """kind: procedure_status | candidate | variant (a corrigendum or extra part) | eea_status |
    hys_feedback (a feedback period opened, or its deadline moved; old and new deadline)."""
    conn.execute(
        "INSERT INTO eu_case_change (case_id, detected_at, kind, old_value, new_value)"
        " VALUES (?, ?, ?, ?, ?)",
        (case_id, db.now_iso(), kind, old, new),
    )
