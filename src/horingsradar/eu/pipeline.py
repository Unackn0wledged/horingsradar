"""`horingsradar-eu run`: fetch EU proposals, prefilter them and store every one.

Milestone 1: EUR-Lex only, no LLM. Every proposal is stored, also the ones the prefilter
leaves out, so nothing disappears silently and the statistics can show what was filtered.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date, timedelta
from typing import Any

from horingsradar import db
from horingsradar.eu import db as eu_db
from horingsradar.eu.assess import assess_case, pending_assessments
from horingsradar.eu.config import EUSettings
from horingsradar.eu.documents import update_documents
from horingsradar.eu.prefilter import Prefilter, load_keywords
from horingsradar.eu.sources import eealex, eurlex, haveyoursay
from horingsradar.eu.sources.eurlex import WITHDRAWN, EurLexClient, Proposal
from horingsradar.http import Fetcher
from horingsradar.jobs import JobHandle
from horingsradar.llm.client import LLMClient, get_client

log = logging.getLogger(__name__)

LAST_DISCOVERY_KEY = "eu_last_discovery_date"
# Procedures that are finished; their status is not looked up again.
FINAL_STATUSES = ("PROP_ADOPTED", "PROP_REJECTED", WITHDRAWN)
# EEA statuses that will not change any more; not looked up again.
EEA_FINAL = ("IN_FORCE", "NO_LONGER_IN_FORCE")
# Upper bound on EEA-Lex list pages per run (60 rows each, 5 seconds apart).
MAX_EEA_LIST_PAGES = 40
YEAR_RE = re.compile(r"\((\d{4})\)")
# Proposals without a Have Your Say page are looked up again until they are this old.
HYS_RETRY = timedelta(weeks=12)


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


def scan_eea_list(
    client: eealex.EEALexClient, stop_year: int
) -> tuple[dict[str, eealex.ListEntry], bool, int]:
    """Read the EEA-Lex list (newest proposals first) until every row on a page is older than
    `stop_year`. Returns (COM reference -> entry, whether the scan finished, pages read)."""
    by_com: dict[str, eealex.ListEntry] = {}
    for page in range(MAX_EEA_LIST_PAGES):
        try:
            entries = client.list_page(page)
        except eealex.EEALexUnavailable as exc:
            log.warning("EEA-Lex stopped: %s", exc)
            return by_com, False, page
        for entry in entries:
            for ref in entry.com_refs:
                by_com.setdefault(ref, entry)
        years = [int(m.group(1)) for e in entries for r in e.com_refs if (m := YEAR_RE.search(r))]
        if not entries or (years and max(years) < stop_year):
            return by_com, True, page + 1
    log.warning("EEA-Lex: stopped after %s list pages", MAX_EEA_LIST_PAGES)
    return by_com, False, MAX_EEA_LIST_PAGES


def update_eea(conn: sqlite3.Connection, client: eealex.EEALexClient) -> dict[str, Any]:
    """EEA status for candidates whose status can still change.

    - In the list with a numeric key: a proposal registered in EEA-Lex, status PROPOSED
      (derived from the list; proposal fact sheets are not available).
    - In the list with a CELEX key: an adopted act; status, area and JCD from its fact sheet.
    - Not in the list: NOT_REGISTERED, but only when the list was read to the end.
    """
    placeholders = ", ".join("?" * len(EEA_FINAL))
    cases = conn.execute(
        "SELECT * FROM eu_case WHERE candidate = 1 AND com_ref IS NOT NULL"
        f" AND (eea_status IS NULL OR eea_status NOT IN ({placeholders}))",
        EEA_FINAL,
    ).fetchall()
    if not cases:
        return {"eea_checked": 0}
    stop_year = min(int(c["date_document"][:4]) for c in cases if c["date_document"])
    by_com, complete, pages = scan_eea_list(client, stop_year)
    checked = changes = 0
    sheets_ok = True
    now = db.now_iso()
    for case in cases:
        entry = by_com.get(case["com_ref"])
        if entry is None:
            if not complete:
                continue
            values = (None, eealex.NOT_REGISTERED, 1, None, None)
        elif not entry.is_act:
            values = (entry.key, "PROPOSED", 1, None, None)
        else:
            if not sheets_ok:
                continue
            try:
                sheet = client.factsheet(entry.key)
            except eealex.EEALexUnavailable as exc:
                log.warning("EEA-Lex fact sheets stopped: %s", exc)
                sheets_ok = False
                continue
            if sheet is None or sheet.status is None:
                values = (entry.key, case["eea_status"], case["eea_status_derived"], None, None)
            else:
                values = (entry.key, sheet.status, 0, db.dumps(sheet.areas), sheet.jcd)
        lex_id, status, derived, areas, jcd = values
        if case["eea_status"] and status and status != case["eea_status"]:
            eu_db.record_change(conn, case["id"], "eea_status", case["eea_status"], status)
            log.info("%s: EEA status %s -> %s", case["com_ref"], case["eea_status"], status)
            changes += 1
        conn.execute(
            "UPDATE eu_case SET eea_lex_id = COALESCE(?, eea_lex_id), eea_status = ?,"
            " eea_status_derived = ?, eea_areas = COALESCE(?, eea_areas),"
            " eea_jcd = COALESCE(?, eea_jcd), eea_checked_at = ? WHERE id = ?",
            (lex_id, status, derived, areas, jcd, now, case["id"]),
        )
        checked += 1
    result = {"eea_checked": checked, "eea_list_pages": pages, "eea_status_changes": changes}
    if not complete or not sheets_ok:
        result["eea_lex"] = "ufullstendig (se loggen)"
    return result


def _hys_lookup_due(case: sqlite3.Row, today: date) -> bool:
    """A proposal's Have Your Say page comes a couple of weeks after adoption. One without a
    page is looked up again until it is HYS_RETRY old; an older one is looked up once."""
    if case["hys_status"] is None:
        return True
    if case["hys_status"] == "found":
        return False
    d = case["date_document"]
    return bool(d) and date.fromisoformat(d) >= today - HYS_RETRY


def _feedback_events(
    before: dict[int, sqlite3.Row], publications: list[haveyoursay.Publication]
) -> list[tuple[str | None, str]]:
    """(old deadline, new deadline) for each feedback period that opened or moved."""
    events: list[tuple[str | None, str]] = []
    for p in publications:
        if p.status != haveyoursay.OPEN or not p.deadline:
            continue
        old = before.get(p.id)
        if old is None or old["status"] != haveyoursay.OPEN:
            events.append((None, p.deadline))
        elif old["deadline"] != p.deadline:
            events.append((old["deadline"], p.deadline))
    return events


def _save_initiative(
    conn: sqlite3.Connection, init: haveyoursay.Initiative, new_case_id: int | None = None
) -> int:
    """Store the initiative and its stages, and log the feedback periods that opened or moved
    for each case linked to it (all of them are new for a case linked just now)."""
    now = db.now_iso()
    before = {
        r["id"]: r
        for r in conn.execute(
            "SELECT * FROM eu_hys_publication WHERE initiative_id = ?", (init.id,)
        )
    }
    conn.execute(
        "INSERT INTO eu_hys_initiative (id, path, reference, short_title, first_seen, checked_at)"
        " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET path = excluded.path,"
        " reference = excluded.reference, short_title = excluded.short_title,"
        " checked_at = excluded.checked_at",
        (init.id, init.path, init.reference, init.short_title, now, now),
    )
    conn.execute("DELETE FROM eu_hys_publication WHERE initiative_id = ?", (init.id,))
    conn.executemany(
        "INSERT OR REPLACE INTO eu_hys_publication (id, initiative_id, type, stage, reference,"
        " published, deadline, status, planned_period, is_current)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                p.id,
                init.id,
                p.type,
                p.stage,
                p.reference,
                p.published,
                p.deadline,
                p.status,
                p.planned_period,
                int(p.is_current),
            )
            for p in init.publications
        ],
    )
    changes = 0
    linked = conn.execute("SELECT id FROM eu_case WHERE hys_initiative_id = ?", (init.id,))
    for (case_id,) in linked.fetchall():
        for old, new in _feedback_events(
            {} if case_id == new_case_id else before, init.publications
        ):
            eu_db.record_change(conn, case_id, "hys_feedback", old, new)
            changes += 1
    return changes


def update_hys(
    conn: sqlite3.Connection, client: haveyoursay.HaveYourSayClient, today: date
) -> dict[str, Any]:
    """Have Your Say feedback periods for candidates, found by their COM number.

    - Candidates not linked yet are looked up (see _hys_lookup_due). The initiative found must
      have a stage with the same COM number, or it is not linked.
    - Linked initiatives are fetched again while a stage is open or still to come.
    The portal never changes which cases are candidates or their relevance.
    """
    looked_up = linked = refreshed = changes = 0
    fetched: set[int] = set()
    result: dict[str, Any] = {}
    try:
        cases = conn.execute(
            "SELECT * FROM eu_case WHERE candidate = 1 AND com_ref IS NOT NULL ORDER BY id"
        ).fetchall()
        for case in cases:
            if not _hys_lookup_due(case, today):
                continue
            path = client.find(case["com_ref"])
            looked_up += 1
            init = client.initiative(path) if path else None
            if init is None or not init.has_com_ref(case["com_ref"]):
                if init is not None:
                    log.warning(
                        "%s: Have Your Say %s has no stage with that COM number; not linked",
                        case["com_ref"],
                        init.id,
                    )
                conn.execute(
                    "UPDATE eu_case SET hys_status = ?, hys_checked_at = ? WHERE id = ?",
                    ("mismatch" if init else "not_found", db.now_iso(), case["id"]),
                )
                continue
            conn.execute(
                "UPDATE eu_case SET hys_initiative_id = ?, hys_status = 'found',"
                " hys_checked_at = ? WHERE id = ?",
                (init.id, db.now_iso(), case["id"]),
            )
            conn.execute(
                "INSERT OR IGNORE INTO eu_case_source (case_id, source, source_id, url, first_seen)"
                " VALUES (?, 'hys', ?, ?, ?)",
                (
                    case["id"],
                    f"{init.id}/{haveyoursay.api_ref(case['com_ref'])}",
                    init.url,
                    db.now_iso(),
                ),
            )
            changes += _save_initiative(conn, init, new_case_id=case["id"])
            fetched.add(init.id)
            linked += 1
            log.info("%s: Have Your Say %s", case["com_ref"], init.path)
        still_open = conn.execute(
            "SELECT i.* FROM eu_hys_initiative i WHERE i.id IN"
            " (SELECT hys_initiative_id FROM eu_case WHERE candidate = 1)"
            " AND EXISTS (SELECT 1 FROM eu_hys_publication p WHERE p.initiative_id = i.id"
            "   AND p.status IN (?, ?)) ORDER BY i.id",
            (haveyoursay.OPEN, haveyoursay.UPCOMING),
        ).fetchall()
        for row in still_open:
            if row["id"] in fetched:
                continue
            changes += _save_initiative(conn, client.initiative(row["path"]))
            fetched.add(row["id"])
            refreshed += 1
    except haveyoursay.HaveYourSayUnavailable as exc:
        log.warning("Have Your Say stopped: %s", exc)
        result["have_your_say"] = "ufullstendig (se loggen)"
    result.update(
        hys_looked_up=looked_up,
        hys_new_links=linked,
        hys_refreshed=refreshed,
        hys_feedback_changes=changes,
    )
    return result


def assess_pending(
    settings: EUSettings, conn: sqlite3.Connection, job: JobHandle, client: LLMClient | None
) -> dict[str, Any]:
    """Assess candidates that wait for an assessment and have their text."""
    pending = pending_assessments(conn)
    result: dict[str, Any] = {"pending_assessments": len(pending)}
    if not pending:
        return result
    if client is None:
        if not settings.anthropic_api_key:
            message = "ANTHROPIC_API_KEY is not set"
            log.warning("Skipping assessment of %s cases: %s", len(pending), message)
            result["assessment_skipped"] = message
            return result
        client = get_client(settings)
    assessed = failed = 0
    for i, case_id in enumerate(pending, start=1):
        job.progress(phase="assess", done=i - 1, total=len(pending), case=case_id)
        assessment_id = assess_case(settings, conn, case_id, client)
        status = conn.execute(
            "SELECT status FROM eu_assessment WHERE id = ?", (assessment_id,)
        ).fetchone()["status"]
        assessed += int(status == "ok")
        failed += int(status != "ok")
        result.update(assessed=assessed, assessment_failed=failed)
        job.summary.update(result)
        job.save_summary()
    return result


def run(
    settings: EUSettings,
    conn: sqlite3.Connection,
    job: JobHandle,
    fetcher: Fetcher | None = None,
    today: date | None = None,
    eea_fetcher: Fetcher | None = None,
    client: LLMClient | None = None,
    assess: bool = True,
) -> dict[str, Any]:
    today = today or date.today()
    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(settings)
    own_eea_fetcher = eea_fetcher is None
    summary = job.summary
    try:
        eurlex_client = EurLexClient(fetcher)
        prefilter = Prefilter(load_keywords(settings.eu_keywords_file))
        since = discovery_since(settings, conn, today)
        log.info("Fetching EU proposals dated %s or later", since)

        fetched = new = new_candidates = 0
        for p in eurlex_client.proposals_since(since, today):
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

        job.progress(phase="enrich", done=0, total=5)
        summary["eurovoc_labels"] = update_labels(conn, eurlex_client)
        summary["status_changes"] = update_procedures(conn, eurlex_client)
        job.save_summary()

        job.progress(phase="eea", done=2, total=5)
        eea_fetcher = eea_fetcher or eealex.make_fetcher(settings)
        summary.update(update_eea(conn, eealex.EEALexClient(eea_fetcher)))
        job.save_summary()

        job.progress(phase="have_your_say", done=3, total=5)
        summary.update(update_hys(conn, haveyoursay.HaveYourSayClient(fetcher), today))
        job.save_summary()

        job.progress(phase="documents", done=4, total=5)
        summary.update(update_documents(settings, conn, eurlex_client, fetcher))
        summary["http_requests"] = fetcher.request_count + eea_fetcher.request_count
        job.save_summary()

        if assess:
            summary.update(assess_pending(settings, conn, job, client))
        summary.update(counts(conn))
        job.progress(phase="done", done=5, total=5)
        return summary
    finally:
        if own_fetcher:
            fetcher.close()
        if own_eea_fetcher and eea_fetcher is not None:
            eea_fetcher.close()


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Totals over all stored cases, for the job summary and `status`."""
    row = conn.execute(
        "SELECT COUNT(*) AS cases, COALESCE(SUM(candidate), 0) AS candidates,"
        " COALESCE(SUM(CASE WHEN indexed = 0 THEN 1 ELSE 0 END), 0) AS not_indexed,"
        " COALESCE(SUM(CASE WHEN candidate = 0 THEN 1 ELSE 0 END), 0) AS filtered_out,"
        " COALESCE(SUM(CASE WHEN candidate = 1 AND eea_status IS NOT NULL"
        "   AND eea_status != 'NOT_REGISTERED' THEN 1 ELSE 0 END), 0) AS eea_registered,"
        " COALESCE(SUM(CASE WHEN candidate = 1 AND eea_status = 'NOT_REGISTERED'"
        "   THEN 1 ELSE 0 END), 0) AS eea_not_registered"
        " FROM eu_case"
    ).fetchone()
    result = {k: int(v) for k, v in dict(row).items()}
    # Relevance of each candidate's latest successful assessment.
    latest = conn.execute(
        "SELECT a.relevance FROM eu_assessment a WHERE a.status = 'ok' AND a.id ="
        " (SELECT MAX(b.id) FROM eu_assessment b WHERE b.case_id = a.case_id AND b.status = 'ok')"
    ).fetchall()
    result["cases_assessed"] = len(latest)
    for level in ("hoy", "middels", "lav", "ingen"):
        result[level] = sum(1 for r in latest if r["relevance"] == level)
    hys = conn.execute(
        "SELECT COUNT(*) AS linked, COALESCE(SUM(EXISTS (SELECT 1 FROM eu_hys_publication p"
        "   WHERE p.initiative_id = c.hys_initiative_id AND p.status = 'OPEN')), 0) AS open"
        " FROM eu_case c WHERE c.candidate = 1 AND c.hys_initiative_id IS NOT NULL"
    ).fetchone()
    result["hys_linked"], result["hys_open"] = int(hys["linked"]), int(hys["open"])
    result["waiting"] = int(
        conn.execute(
            "SELECT COUNT(*) FROM eu_case WHERE candidate = 1 AND needs_assessment = 1"
        ).fetchone()[0]
    )
    return result
