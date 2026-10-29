"""Read-only queries for the EU panel's status and statistics pages: figures per source
(docs/EU-SPEC.md, Panel og statistikk) and LLM usage."""

from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Any

from horingsradar import db
from horingsradar.eu.pipeline import counts
from horingsradar.eu.sources import oeil


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, args)]


def prefilter_reasons(conn: sqlite3.Connection) -> dict[str, int]:
    """Why the candidates are candidates: EuroVoc concepts, title keywords (a case can have
    both), or only an EDPS opinion (the prefilter let it through on neither)."""
    reasons: Counter[str] = Counter()
    for (raw,) in conn.execute("SELECT prefilter FROM eu_case WHERE candidate = 1"):
        data = db.loads(raw, {}) or {}
        for key in ("eurovoc", "keywords"):
            if data.get(key):
                reasons[key] += 1
        if data.get("edps") and not data.get("eurovoc") and not data.get("keywords"):
            reasons["edps_only"] += 1
    return {k: reasons.get(k, 0) for k in ("eurovoc", "keywords", "edps_only")}


def statistics(conn: sqlite3.Connection) -> dict[str, Any]:
    c = counts(conn)
    eea = _rows(
        conn,
        "SELECT COALESCE(eea_status, '') AS status, COUNT(*) AS n FROM eu_case"
        " WHERE candidate = 1 GROUP BY eea_status ORDER BY n DESC",
    )
    edps_links = dict(
        conn.execute("SELECT method, COUNT(*) FROM eu_edps_link GROUP BY method").fetchall()
    )
    raised = conn.execute(
        "SELECT COUNT(DISTINCT case_id) FROM eu_assessment WHERE status = 'ok' AND rule = 'edps'"
    ).fetchone()[0]
    oeil_status = [
        {**r, "label": oeil.status_label(r["status"])}
        for r in _rows(
            conn,
            "SELECT oeil_status AS status, COUNT(*) AS n FROM eu_case WHERE candidate = 1"
            " AND oeil_ref IS NOT NULL GROUP BY oeil_status ORDER BY n DESC",
        )
    ]
    oeil_missing = conn.execute(
        "SELECT COUNT(*) FROM eu_case WHERE candidate = 1 AND oeil_ref IS NULL"
        " AND oeil_checked_at IS NOT NULL"
    ).fetchone()[0]
    assessments = dict(
        conn.execute("SELECT status, COUNT(*) FROM eu_assessment GROUP BY status").fetchall()
    )
    usage = _rows(
        conn,
        "SELECT substr(created_at, 1, 7) AS month, COUNT(*) AS assessments,"
        " SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,"
        " COALESCE(SUM(cost_estimate), 0) AS cost FROM eu_assessment"
        " GROUP BY month ORDER BY month DESC LIMIT 24",
    )
    models = _rows(
        conn,
        "SELECT model, prompt_version, COUNT(*) AS n FROM eu_assessment"
        " GROUP BY model, prompt_version ORDER BY MAX(id) DESC",
    )
    return {
        "counts": c,
        "reasons": prefilter_reasons(conn),
        "eea": eea,
        "edps_notes": conn.execute("SELECT COUNT(*) FROM eu_edps").fetchone()[0],
        "edps_links": {
            "procedure": edps_links.get("procedure", 0),
            "title": edps_links.get("title", 0),
        },
        "edps_raised": raised,
        "oeil_status": oeil_status,
        "oeil_missing": oeil_missing,
        "assessments": {"ok": assessments.get("ok", 0), "failed": assessments.get("failed", 0)},
        "usage": usage,
        "total_cost": sum(u["cost"] for u in usage),
        "models": models,
        "runs": recent_runs(conn),
    }


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    runs = []
    for r in conn.execute(
        "SELECT * FROM job WHERE kind = 'run' ORDER BY id DESC LIMIT ?", (limit,)
    ):
        item = dict(r)
        item["summary_data"] = db.loads(r["summary"], {}) or {}
        start, end = db.parse_ts(r["started_at"]), db.parse_ts(r["finished_at"])
        item["minutes"] = round((end - start).total_seconds() / 60, 1) if start and end else None
        runs.append(item)
    return runs
