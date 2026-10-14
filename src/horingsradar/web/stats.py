"""Read-only queries for the panel's status and statistics pages."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

from horingsradar import db
from horingsradar.llm.schema import RELEVANCE_LABELS

_next_run_cache: dict[str, tuple[float, str]] = {}


def next_scheduled_run(timer_unit: str) -> str:
    """Read (never control) the systemd timer. Cached for a minute."""
    cached = _next_run_cache.get(timer_unit)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    value = "ukjent (systemd-timer ikke funnet)"
    if shutil.which("systemctl"):
        try:
            out = subprocess.run(
                [
                    "systemctl",
                    "show",
                    timer_unit,
                    "--property=NextElapseUSecRealtime",
                    "--value",
                ],
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
            if out and out != "n/a":
                value = out
        except (OSError, subprocess.SubprocessError):
            pass
    _next_run_cache[timer_unit] = (time.monotonic(), value)
    return value


def last_job(conn: sqlite3.Connection, kind: str | None = None) -> dict[str, Any] | None:
    sql = "SELECT * FROM job WHERE status != 'running'"
    args: tuple = ()
    if kind:
        sql += " AND kind = ?"
        args = (kind,)
    row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
    if not row:
        return None
    job = dict(row)
    job["summary_data"] = db.loads(job["summary"], {}) or {}
    return job


def latest_relevance_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT a.relevance, a.status FROM assessment a"
        " JOIN hearing h ON h.id = a.hearing_id"
        " WHERE h.tracked = 1 AND a.context = 'drift' AND a.id = ("
        "   SELECT a2.id FROM assessment a2 WHERE a2.hearing_id = a.hearing_id"
        "   AND a2.context = 'drift' ORDER BY a2.created_at DESC, a2.id DESC LIMIT 1)"
    ).fetchall()
    counts: Counter[str] = Counter()
    for r in rows:
        counts[r["relevance"] if r["status"] == "ok" else "feilet"] += 1
    return {key: counts.get(key, 0) for key in ("hoy", "middels", "lav", "ingen", "feilet")}


def statistics(conn: sqlite3.Connection, settings) -> dict[str, Any]:
    today = date.today()
    # Hearings processed per ISO week (distinct hearings with a drift assessment), last 12 weeks
    weeks: dict[str, set[int]] = defaultdict(set)
    since = (today - timedelta(weeks=12)).isoformat()
    for r in conn.execute(
        "SELECT hearing_id, created_at FROM assessment WHERE context = 'drift' AND created_at >= ?",
        (since,),
    ):
        local = db.parse_ts(r["created_at"])
        iso = local.isocalendar()
        weeks[f"{iso.year}-U{iso.week:02d}"].add(r["hearing_id"])
    per_week = [{"week": k, "count": len(v)} for k, v in sorted(weeks.items())]

    relevance = latest_relevance_counts(conn)
    assessed = sum(v for k, v in relevance.items() if k != "feilet")
    flagged = relevance["hoy"] + relevance["middels"]

    quotes = conn.execute(
        "SELECT COUNT(*) AS n, SUM(f.quote_verified) AS v FROM finding f"
        " JOIN assessment a ON a.id = f.assessment_id WHERE a.context = 'drift'"
    ).fetchone()
    failed = conn.execute(
        "SELECT COUNT(*) FROM assessment WHERE context = 'drift' AND status = 'failed'"
    ).fetchone()[0]

    usage = []
    for r in conn.execute(
        "SELECT substr(created_at, 1, 7) AS month, SUM(input_tokens) AS i, SUM(output_tokens) AS o,"
        " SUM(cost_estimate) AS c, COUNT(*) AS n FROM assessment GROUP BY month ORDER BY month DESC"
        " LIMIT 12"
    ):
        usage.append(
            {
                "month": r["month"],
                "input_tokens": r["i"] or 0,
                "output_tokens": r["o"] or 0,
                "cost": r["c"] or 0.0,
                "assessments": r["n"],
            }
        )

    backtest = conn.execute(
        "SELECT * FROM backtest_run WHERE status = 'ok' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_backtest = None
    if backtest:
        metrics = db.loads(backtest["metrics"], {}) or {}
        last_backtest = {
            **dict(backtest),
            "recall": metrics.get("recall_high_medium"),
            "flagged_share": metrics.get("flagged_share"),
        }

    max_week = max((w["count"] for w in per_week), default=0)
    return {
        "per_week": per_week,
        "max_week": max_week,
        "relevance": relevance,
        "relevance_labels": {**RELEVANCE_LABELS, "feilet": "Feilet"},
        "assessed": assessed,
        "flagged_share": flagged / assessed if assessed else None,
        "quotes_total": quotes["n"] or 0,
        "quotes_verified_share": (quotes["v"] or 0) / quotes["n"] if quotes["n"] else None,
        "failed_assessments": failed,
        "usage": usage,
        "last_backtest": last_backtest,
    }


def tail(path: str | None, lines: int = 200) -> str:
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])
    except OSError:
        return ""
