"""The English PDF of each candidate: fetched, text extracted, explanatory memorandum located."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path
from typing import Any

from horingsradar import db
from horingsradar.eu.config import EUSettings
from horingsradar.eu.sources.eurlex import EurLexClient
from horingsradar.eu.text import memorandum
from horingsradar.extract import extract_pdf, split_pages, write_text
from horingsradar.http import Fetcher, FetchError

log = logging.getLogger(__name__)

NO_ENGLISH_PDF = "Ingen engelsk PDF i Cellar ennå"


def pdf_path(settings: EUSettings, case_id: int) -> Path:
    return settings.documents_dir / "eu" / f"{case_id}.pdf"


def text_path(settings: EUSettings, case_id: int) -> Path:
    return settings.text_dir / "eu" / f"{case_id}.txt"


def _save(conn: sqlite3.Connection, case_id: int, url: str, status: str, **values: Any) -> None:
    now = db.now_iso()
    columns = ["url", "fetch_status", *values]
    conn.execute(
        f"INSERT INTO eu_document (case_id, first_seen, {', '.join(columns)})"
        f" VALUES (?, ?, {', '.join('?' * len(columns))})"
        f" ON CONFLICT (case_id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in columns)}",
        (case_id, now, url, status, *values.values()),
    )


def fetch_document(
    settings: EUSettings,
    conn: sqlite3.Connection,
    case: sqlite3.Row,
    url: str | None,
    fetcher: Fetcher,
) -> str:
    """Fetch and extract one case's English PDF. Returns the document's fetch status."""
    case_id = int(case["id"])
    if not url:
        _save(conn, case_id, "", "missing", note=NO_ENGLISH_PDF)
        return "missing"
    try:
        fetched = fetcher.get(url)
    except FetchError as exc:
        _save(conn, case_id, url, "error", note=str(exc))
        log.warning("%s: could not fetch the English PDF: %s", case["com_ref"], exc)
        return "error"
    if fetched.content[:5] != b"%PDF-":
        _save(conn, case_id, url, "unreadable", note=f"Ikke en PDF ({fetched.content_type})")
        return "unreadable"
    path = pdf_path(settings, case_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(fetched.content)
    try:
        extracted = extract_pdf(path)
    except Exception as exc:  # corrupt or encrypted PDF
        _save(conn, case_id, url, "unreadable", note=f"Kunne ikke leses: {exc}")
        return "unreadable"
    memo = memorandum(split_pages(extracted.text))
    write_text(text_path(settings, case_id), extracted.text)
    notes = []
    if not memo.found:
        notes.append("Fant ikke «Explanatory memorandum»; de første sidene er brukt")
    if memo.truncated:
        notes.append(f"Begrunnelsen er kuttet etter side {memo.last_page}")
    if extracted.probably_scanned:
        notes.append("Mange sider uten tekst (skannet?)")
    _save(
        conn,
        case_id,
        url,
        "ok",
        sha256=hashlib.sha256(fetched.content).hexdigest(),
        page_count=extracted.page_count,
        text_path=str(text_path(settings, case_id)),
        memo_first_page=memo.first_page,
        memo_last_page=memo.last_page,
        memo_found=int(memo.found),
        memo_truncated=int(memo.truncated),
        memo_chars=memo.chars,
        note="; ".join(notes) or None,
        last_changed=db.now_iso(),
    )
    log.info(
        "%s: memorandum on pages %s-%s of %s (%s characters)",
        case["com_ref"],
        memo.first_page,
        memo.last_page,
        extracted.page_count,
        memo.chars,
    )
    return "ok"


def update_documents(
    settings: EUSettings, conn: sqlite3.Connection, client: EurLexClient, fetcher: Fetcher
) -> dict[str, int]:
    """English PDFs for candidates that do not have one yet (or whose last try failed)."""
    cases = conn.execute(
        "SELECT c.* FROM eu_case c LEFT JOIN eu_document d ON d.case_id = c.id"
        " WHERE c.candidate = 1 AND c.work_uri IS NOT NULL"
        " AND (d.id IS NULL OR d.fetch_status != 'ok')"
        " ORDER BY c.date_document DESC, c.id DESC"
    ).fetchall()
    if not cases:
        return {"documents_fetched": 0}
    urls = client.english_pdfs([c["work_uri"] for c in cases])
    statuses: dict[str, int] = {}
    for case in cases:
        status = fetch_document(settings, conn, case, urls.get(case["work_uri"]), fetcher)
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "documents_fetched": statuses.get("ok", 0),
        "documents_missing": statuses.get("missing", 0),
        "documents_failed": statuses.get("error", 0) + statuses.get("unreadable", 0),
    }
