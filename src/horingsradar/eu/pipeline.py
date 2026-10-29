"""`horingsradar-eu run`: fetch EU proposals, prefilter them and store every one.

Milestone 1: EUR-Lex only, no LLM. Every proposal is stored, also the ones the prefilter
leaves out, so nothing disappears silently and the statistics can show what was filtered.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from typing import Any

from horingsradar import db
from horingsradar.eu import db as eu_db
from horingsradar.eu.config import EUSettings
from horingsradar.eu.prefilter import Prefilter, load_keywords
from horingsradar.eu.sources import eurlex
from horingsradar.eu.sources.eurlex import WITHDRAWN, EurLexClient, Proposal
from horingsradar.http import Fetcher
from horingsradar.jobs import JobHandle

log = logging.getLogger(__name__)

LAST_DISCOVERY_KEY = "eu_last_discovery_date"
# Procedures that are finished; their status is not looked up again.
FINAL_STATUSES = ("PROP_ADOPTED", "PROP_REJECTED", WITHDRAWN)


def discovery_since(settings: EUSettings, conn: sqlite3.Connection, today: date) -> date:
    last = db.get_setting(conn, LAST_DISCOVERY_KEY)
    if last:
        return date.fromisoformat(last) - timedelta(days=settings.eu_discovery_overlap_days)
    return today - timedelta(days=settings.eu_initial_lookback_days)


def upsert_proposal(
    conn: sqlite3.Connection, p: Proposal, prefilter: Prefilter
) -> tuple[int, bool, bool]:
    """Store or update one proposal. Returns (case id, is new, is a candidate for the first time).

    Corrigenda (52025PC0828R(01)) and extra parts (52025PC0101(01)) belong to the same case as
    the proposal, keyed by its base CELEX number. They are kept as sources of the case, a new
    one is logged as a change, and the proposal's own fields win over theirs. EuroVoc concepts
    are merged. Once a candidate, always a candidate: later index changes never drop a case.
    """
    base = eurlex.base_celex(p.celex)
    is_variant = base != p.celex
    now = db.now_iso()
    row = conn.execute("SELECT * FROM eu_case WHERE celex = ?", (base,)).fetchone()
    known = db.loads(row["eurovoc"], []) if row else []
    eurovoc = sorted(set(known) | set(p.eurovoc))
    reasons = prefilter.reasons(p.title, eurovoc)
    hit = any(reasons.values())

    if row is None:
        cur = conn.execute(
            "INSERT INTO eu_case (celex, com_ref, work_uri, title, doc_type, date_document,"
            " eea_flag, eurovoc, indexed, candidate, prefilter, needs_assessment,"
            " source_modified, first_seen, last_checked)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                base,
                p.com_ref,
                p.work_uri,
                p.title,
                p.doc_type,
                p.date_document,
                p.eea_flag,
                db.dumps(eurovoc),
                int(bool(eurovoc)),
                int(hit),
                db.dumps(reasons),
                int(hit),
                p.modified,
                now,
                now,
            ),
        )
        case_id = int(cur.lastrowid)
        first_time = hit
    else:
        case_id = int(row["id"])
        first_time = hit and not row["candidate"]
        if first_time:
            eu_db.record_change(conn, case_id, "candidate", "0", "1")
            log.info("%s is now a candidate: %s", p.com_ref or p.celex, reasons)
        own = row if is_variant else None  # a variant does not overwrite the proposal's fields
        conn.execute(
            "UPDATE eu_case SET com_ref = ?, work_uri = ?, title = ?, doc_type = ?,"
            " date_document = ?, eea_flag = ?, source_modified = ?, eurovoc = ?, indexed = ?,"
            " candidate = ?, prefilter = ?,"
            " needs_assessment = CASE WHEN ? THEN 1 ELSE needs_assessment END,"
            " last_checked = ? WHERE id = ?",
            (
                own["com_ref"] if own else p.com_ref,
                (own["work_uri"] or p.work_uri) if own else p.work_uri,
                own["title"] if own else p.title,
                own["doc_type"] if own else p.doc_type,
                own["date_document"] if own else p.date_document,
                own["eea_flag"] if own else p.eea_flag,
                own["source_modified"] if own else p.modified,
                db.dumps(eurovoc),
                int(bool(eurovoc)),
                int(row["candidate"] or hit),
                db.dumps(reasons) if hit or not row["candidate"] else row["prefilter"],
                int(first_time),
                now,
                case_id,
            ),
        )

    added = conn.execute(
        "INSERT OR IGNORE INTO eu_case_source (case_id, source, source_id, url, first_seen)"
        " VALUES (?, 'eurlex', ?, ?, ?)",
        (case_id, p.celex, p.url, now),
    ).rowcount
    if added and is_variant and row is not None:
        eu_db.record_change(conn, case_id, "variant", None, p.celex)
        log.info("%s: new variant %s", p.com_ref or base, p.celex)
    return case_id, row is None, first_time


def update_labels(conn: sqlite3.Connection, client: EurLexClient) -> int:
    """Fetch English labels for the candidates' EuroVoc concepts not seen before."""
    known = {r["id"] for r in conn.execute("SELECT id FROM eu_eurovoc")}
    seen: set[str] = set()
    for r in conn.execute("SELECT eurovoc FROM eu_case WHERE candidate = 1"):
        seen.update(db.loads(r["eurovoc"], []))
    missing = sorted(seen - known)
    if not missing:
        return 0
    labels = client.labels(missing)
    conn.executemany(
        "INSERT OR REPLACE INTO eu_eurovoc (id, label) VALUES (?, ?)",
        [(c, labels.get(c)) for c in missing],
    )
    return len(missing)


def update_procedures(conn: sqlite3.Connection, client: EurLexClient) -> int:
    """Look up procedure and status for candidates whose procedure is not finished.

    Returns the number of status changes.
    """
    placeholders = ", ".join("?" * len(FINAL_STATUSES))
    rows = conn.execute(
        "SELECT id, work_uri, procedure_ref, procedure_status FROM eu_case"
        f" WHERE candidate = 1 AND work_uri IS NOT NULL AND (procedure_status IS NULL"
        f" OR procedure_status NOT IN ({placeholders}))",
        FINAL_STATUSES,
    ).fetchall()
    if not rows:
        return 0
    found = client.dossiers([r["work_uri"] for r in rows])
    changes = 0
    for r in rows:
        procedure, status = found.get(r["work_uri"], (None, None))
        if r["procedure_status"] and status and status != r["procedure_status"]:
            eu_db.record_change(conn, r["id"], "procedure_status", r["procedure_status"], status)
            log.info("Case %s: procedure status %s -> %s", r["id"], r["procedure_status"], status)
            changes += 1
        conn.execute(
            "UPDATE eu_case SET procedure_ref = COALESCE(?, procedure_ref),"
            " procedure_status = COALESCE(?, procedure_status) WHERE id = ?",
            (procedure, status, r["id"]),
        )
    return changes


def run(
    settings: EUSettings,
    conn: sqlite3.Connection,
    job: JobHandle,
    fetcher: Fetcher | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    today = today or date.today()
    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(settings)
    summary = job.summary
    try:
        client = EurLexClient(fetcher)
        prefilter = Prefilter(load_keywords(settings.eu_keywords_file))
        since = discovery_since(settings, conn, today)
        log.info("Fetching EU proposals dated %s or later", since)

        fetched = new = new_candidates = 0
        for p in client.proposals_since(since, today):
            _, is_new, first_time_candidate = upsert_proposal(conn, p, prefilter)
            fetched += 1
            new += int(is_new)
            new_candidates += int(first_time_candidate)
            if fetched % 50 == 0:
                job.progress(phase="discover", done=fetched)
        db.set_setting(conn, LAST_DISCOVERY_KEY, today.isoformat())
        summary.update(
            since=since.isoformat(), fetched=fetched, new=new, new_candidates=new_candidates
        )
        job.save_summary()

        job.progress(phase="enrich", done=0, total=2)
        summary["eurovoc_labels"] = update_labels(conn, client)
        summary["status_changes"] = update_procedures(conn, client)
        summary.update(counts(conn))
        summary["http_requests"] = fetcher.request_count
        job.progress(phase="done", done=2, total=2)
        return summary
    finally:
        if own_fetcher:
            fetcher.close()


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Totals over all stored cases, for the job summary and `status`."""
    row = conn.execute(
        "SELECT COUNT(*) AS cases, COALESCE(SUM(candidate), 0) AS candidates,"
        " COALESCE(SUM(CASE WHEN indexed = 0 THEN 1 ELSE 0 END), 0) AS not_indexed,"
        " COALESCE(SUM(CASE WHEN candidate = 0 THEN 1 ELSE 0 END), 0) AS filtered_out"
        " FROM eu_case"
    ).fetchone()
    return {k: int(v) for k, v in dict(row).items()}
