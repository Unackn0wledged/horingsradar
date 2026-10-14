"""The daily `run`: discover new hearings, refresh open ones, fetch documents and assess."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from typing import Any

from horingsradar import db
from horingsradar.assess import (
    FAILED_ATTEMPTS_BEFORE_PAUSE,
    assess_hearing,
    failed_attempts_since_change,
)
from horingsradar.config import Settings
from horingsradar.http import Fetcher, FetchError
from horingsradar.jobs import JobHandle
from horingsradar.llm.client import LLMClient, get_client
from horingsradar.sources.regjeringen.crawler import RegjeringenSource

log = logging.getLogger(__name__)

CHECK_STATUSES = ("open", "unknown")


def discovery_cutoff(settings: Settings, conn: sqlite3.Connection, today: date) -> date:
    last = db.get_setting(conn, "last_discovery_date")
    if last is None:
        return today - timedelta(days=settings.initial_lookback_days)
    return date.fromisoformat(last) - timedelta(days=settings.discovery_overlap_days)


def hearings_to_check(conn: sqlite3.Connection, extra: set[int]) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM hearing WHERE tracked = 1 AND (status IN (?, ?) OR status IS NULL"
        " OR last_checked IS NULL)",
        CHECK_STATUSES,
    ).fetchall()
    return sorted({r["id"] for r in rows} | extra, reverse=True)


def pending_assessments(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        "SELECT h.id FROM hearing h WHERE h.tracked = 1 AND h.needs_assessment = 1"
        " AND h.last_checked IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM document d WHERE d.hearing_id = h.id AND d.removed = 0"
        "                 AND d.fetch_status IN ('error', 'pending'))"
        " ORDER BY h.published_date DESC, h.id DESC"
    ).fetchall()
    return [
        r["id"]
        for r in rows
        if failed_attempts_since_change(conn, r["id"]) < FAILED_ATTEMPTS_BEFORE_PAUSE
    ]


def run(
    settings: Settings,
    conn: sqlite3.Connection,
    job: JobHandle,
    fetcher: Fetcher | None = None,
    client: LLMClient | None = None,
    today: date | None = None,
    assess: bool = True,
) -> dict[str, Any]:
    today = today or date.today()
    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(settings)
    summary = job.summary
    try:
        source = RegjeringenSource(settings, conn, fetcher)
        open_before = {
            r["id"]
            for r in conn.execute(
                "SELECT id FROM hearing WHERE tracked = 1 AND status IN (?, ?)", CHECK_STATUSES
            )
        }
        cutoff = discovery_cutoff(settings, conn, today)
        log.info("Discovering hearings published on or after %s", cutoff)
        stats = source.discover(since=cutoff, tracked=True, progress=job.progress)
        db.set_setting(conn, "last_discovery_date", today.isoformat())
        summary.update(cutoff=cutoff.isoformat(), list_pages=stats.pages, new=len(stats.new_ids))

        to_check = hearings_to_check(conn, set(stats.new_ids) | open_before)
        changed = errors = 0
        for i, hearing_id in enumerate(to_check, start=1):
            job.progress(phase="refresh", done=i - 1, total=len(to_check), hearing=hearing_id)
            try:
                result = source.refresh_hearing(hearing_id)
            except FetchError as exc:
                errors += 1
                log.error("Could not refresh hearing %s: %s", hearing_id, exc)
                continue
            if result.changed and hearing_id not in stats.new_ids:
                changed += 1
                log.info(
                    "Hearing %s changed: %s new, %s changed, %s removed documents",
                    hearing_id,
                    result.new_documents,
                    result.changed_documents,
                    result.removed_documents,
                )
        summary.update(checked=len(to_check), changed=changed, fetch_errors=errors)
        summary["http_requests"] = fetcher.request_count
        job.save_summary()

        pending = pending_assessments(conn)
        summary["pending_assessments"] = len(pending)
        if not assess or not pending:
            return summary
        if client is None:
            if not settings.anthropic_api_key:
                message = "ANTHROPIC_API_KEY is not set"
                log.warning("Skipping assessment of %s hearings: %s", len(pending), message)
                summary["assessment_skipped"] = message
                return summary
            client = get_client(settings)
        assessed = failed = 0
        for i, hearing_id in enumerate(pending, start=1):
            job.progress(phase="assess", done=i - 1, total=len(pending), hearing=hearing_id)
            assessment_id = assess_hearing(settings, conn, hearing_id, client)
            status = conn.execute(
                "SELECT status FROM assessment WHERE id = ?", (assessment_id,)
            ).fetchone()["status"]
            if status == "ok":
                assessed += 1
            else:
                failed += 1
            summary.update(assessed=assessed, assessment_failed=failed)
            job.save_summary()
        job.progress(phase="done", done=len(pending), total=len(pending))
        return summary
    finally:
        if own_fetcher:
            fetcher.close()
