"""The EDPS rule (CLAUDE.md, docs/EU-SPEC.md): a case with an EDPS opinion on the proposal is at
least `middels`. It is deterministic, can only raise the level, and is the only code that changes
the level the model set. Both the model's level (llm_relevance) and the final one are stored.
"""

from __future__ import annotations

import logging
import sqlite3

from horingsradar import db
from horingsradar.llm.schema import RELEVANCE_ORDER

log = logging.getLogger(__name__)

EDPS = "edps"
EDPS_FLOOR = "middels"
EDPS_NOTE = "EDPS-regelen anvendt uten nytt LLM-kall: en EDPS-uttalelse gir minst middels."


def has_edps(conn: sqlite3.Connection, case_id: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM eu_edps_link WHERE case_id = ? LIMIT 1", (case_id,)).fetchone()
        is not None
    )


def final_relevance(llm_relevance: str, edps: bool) -> tuple[str, str | None]:
    """(final level, rule that set it or None)."""
    if edps and RELEVANCE_ORDER[llm_relevance] < RELEVANCE_ORDER[EDPS_FLOOR]:
        return EDPS_FLOOR, EDPS
    return llm_relevance, None


def apply_to_assessed(conn: sqlite3.Connection, case_id: int) -> int | None:
    """An opinion found after the case was assessed: if the latest assessment is below the floor,
    store a copy of it (same text and findings, no LLM call) with the raised level. Returns the
    new assessment's ID, or None when nothing changes."""
    latest = conn.execute(
        "SELECT * FROM eu_assessment WHERE case_id = ? ORDER BY id DESC LIMIT 1", (case_id,)
    ).fetchone()
    if latest is None or latest["status"] != "ok":
        return None
    relevance, rule = final_relevance(latest["llm_relevance"], True)
    if rule is None or latest["relevance"] == relevance:
        return None
    row = dict(latest)
    old_id = row.pop("id")
    row.update(
        created_at=db.now_iso(),
        relevance=relevance,
        rule=rule,
        input_tokens=0,
        output_tokens=0,
        cost_estimate=0.0,
        notes=db.dumps([*db.loads(latest["notes"], []), EDPS_NOTE]),
    )
    columns = ", ".join(row)
    cur = conn.execute(
        f"INSERT INTO eu_assessment ({columns}) VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )
    new_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO eu_finding (assessment_id, category, description, chapter, page,"
        " page_stated, quote, quote_verified, match_score)"
        " SELECT ?, category, description, chapter, page, page_stated, quote, quote_verified,"
        " match_score FROM eu_finding WHERE assessment_id = ? ORDER BY id",
        (new_id, old_id),
    )
    log.info("Case %s: EDPS rule raised %s to %s", case_id, latest["llm_relevance"], relevance)
    return new_id
