"""Discovery and refresh of hearings from regjeringen.no into SQLite. All steps are idempotent."""

from __future__ import annotations

import hashlib
import logging
import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from horingsradar import db
from horingsradar.config import Settings
from horingsradar.extract import extract_html, extract_pdf, text_path_for, write_text
from horingsradar.http import Fetcher, FetchError
from horingsradar.sources.regjeringen.parse import (
    DocumentLink,
    ListingItem,
    parse_hearing,
    parse_listing,
    parse_responses,
)

log = logging.getLogger(__name__)

LIST_PATH = "/no/dokument/hoyringar/id1763/"
DATATILSYNET = "Datatilsynet"
PERMANENT_STATUSES = {404, 410}
UNSUPPORTED_NOTE = "Filformatet leses ikke i piloten"
MAX_RESPONSE_PAGES = 20

ProgressFn = Callable[..., None]


def _noop(**_: object) -> None:
    return None


@dataclass
class DiscoveryStats:
    pages: int = 0
    seen: int = 0
    new_ids: list[int] = field(default_factory=list)


@dataclass
class RefreshResult:
    hearing_id: int
    changed: bool = False
    new_documents: int = 0
    changed_documents: int = 0
    removed_documents: int = 0
    transient_errors: int = 0


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


class RegjeringenSource:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, fetcher: Fetcher):
        self.settings = settings
        self.conn = conn
        self.fetcher = fetcher
        self.base = settings.regjeringen_base_url.rstrip("/")

    def list_url(self, page: int) -> str:
        url = self.base + LIST_PATH
        return url if page == 1 else f"{url}?page={page}"

    # -- discovery ---------------------------------------------------------

    def discover(
        self,
        since: date,
        until: date | None = None,
        tracked: bool = True,
        max_pages: int | None = None,
        progress: ProgressFn = _noop,
    ) -> DiscoveryStats:
        """Walk the listing (newest first) until a whole page is older than `since`.

        Hearings published in [since, until] are inserted; known hearings on the walked pages
        get their listing metadata refreshed.
        """
        stats = DiscoveryStats()
        max_pages = max_pages or self.settings.max_list_pages
        page_no = 1
        while page_no <= max_pages:
            progress(phase="discovery", page=page_no)
            result = self.fetcher.get(
                self.list_url(page_no), max_age=self.settings.html_cache_max_age_seconds
            )
            page = parse_listing(result.text, self.base + LIST_PATH)
            stats.pages += 1
            if not page.items:
                break
            for item in page.items:
                stats.seen += 1
                in_range = item.published_date is None or (
                    item.published_date >= since and (until is None or item.published_date <= until)
                )
                if self._upsert_listing(item, tracked=tracked, insert=in_range):
                    stats.new_ids.append(item.id)
            dated = [i.published_date for i in page.items if i.published_date]
            if dated and max(dated) < since:
                break
            if not page.next_url:
                break
            page_no += 1
        else:
            log.warning("Stopped discovery at max_pages=%s", max_pages)
        log.info(
            "Discovery: %s pages, %s items seen, %s new",
            stats.pages,
            stats.seen,
            len(stats.new_ids),
        )
        return stats

    def _upsert_listing(self, item: ListingItem, tracked: bool, insert: bool) -> bool:
        exists = self.conn.execute(
            "SELECT tracked FROM hearing WHERE id = ?", (item.id,)
        ).fetchone()
        if exists is None:
            if not insert:
                return False
            self.conn.execute(
                "INSERT INTO hearing (id, url, title, department, doc_type, ingress, published_date,"
                " deadline, status_raw, status, tracked, needs_assessment, first_seen)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.url,
                    item.title,
                    item.department,
                    item.doc_type,
                    item.excerpt,
                    _iso(item.published_date),
                    _iso(item.deadline),
                    item.status_raw,
                    item.status,
                    int(tracked),
                    int(tracked),
                    db.now_iso(),
                ),
            )
            return True
        promote = tracked and not exists["tracked"] and insert
        self.conn.execute(
            "UPDATE hearing SET title = ?, department = COALESCE(?, department),"
            " doc_type = COALESCE(?, doc_type), ingress = COALESCE(ingress, ?),"
            " published_date = COALESCE(?, published_date), deadline = COALESCE(?, deadline),"
            " status_raw = COALESCE(?, status_raw),"
            " status = CASE WHEN ? = 'unknown' THEN status ELSE ? END,"
            " tracked = CASE WHEN ? THEN 1 ELSE tracked END,"
            " needs_assessment = CASE WHEN ? THEN 1 ELSE needs_assessment END"
            " WHERE id = ?",
            (
                item.title,
                item.department,
                item.doc_type,
                item.excerpt,
                _iso(item.published_date),
                _iso(item.deadline),
                item.status_raw,
                item.status,
                item.status,
                int(promote),
                int(promote),
                item.id,
            ),
        )
        return False

    # -- hearing page ------------------------------------------------------

    def refresh_hearing(
        self, hearing_id: int, *, max_age: float | None = None, fetch_responses: bool = True
    ) -> RefreshResult:
        row = self.conn.execute("SELECT * FROM hearing WHERE id = ?", (hearing_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown hearing {hearing_id}")
        max_age = self.settings.html_cache_max_age_seconds if max_age is None else max_age
        page_result = self.fetcher.get(row["url"], max_age=max_age)
        page = parse_hearing(page_result.text, row["url"])

        self.conn.execute(
            "UPDATE hearing SET title = ?, doc_type = COALESCE(?, doc_type),"
            " department = COALESCE(?, department), original_department = ?,"
            " ingress = COALESCE(?, ingress), published_date = COALESCE(?, published_date),"
            " deadline = COALESCE(?, deadline), status_raw = COALESCE(?, status_raw),"
            " status = CASE WHEN ? = 'unknown' THEN status ELSE ? END,"
            " address_list = ?, dt_on_address_list = ?, last_checked = ? WHERE id = ?",
            (
                page.title or row["title"],
                page.doc_type,
                page.department,
                page.original_department,
                page.ingress,
                _iso(page.published_date),
                _iso(page.deadline),
                page.status_raw,
                page.status,
                page.status,
                db.dumps(page.address_list),
                int(page.dt_on_address_list),
                db.now_iso(),
                hearing_id,
            ),
        )

        result = self._sync_documents(hearing_id, page.documents, max_age)
        if result.changed:
            self.conn.execute(
                "UPDATE hearing SET content_changed_at = ?,"
                " needs_assessment = CASE WHEN tracked = 1 THEN 1 ELSE needs_assessment END"
                " WHERE id = ?",
                (db.now_iso(), hearing_id),
            )
        if fetch_responses and page.responses_url and page.status != "open":
            try:
                self._update_responses(hearing_id, page.responses_url, max_age)
            except FetchError as exc:
                log.warning("Could not fetch responses for %s: %s", hearing_id, exc)
        return result

    def _sync_documents(
        self, hearing_id: int, links: list[DocumentLink], max_age: float
    ) -> RefreshResult:
        result = RefreshResult(hearing_id)
        existing = {
            r["url"]: r
            for r in self.conn.execute("SELECT * FROM document WHERE hearing_id = ?", (hearing_id,))
        }
        seen: set[str] = set()
        now = db.now_iso()
        for link in links:
            seen.add(link.url)
            row = existing.get(link.url)
            if row is None:
                cur = self.conn.execute(
                    "INSERT INTO document (hearing_id, url, title, kind, section, position,"
                    " fetch_status, first_seen) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (hearing_id, link.url, link.title, link.kind, link.section, link.position, now),
                )
                doc_id = int(cur.lastrowid)
                old_sha = None
                was_removed = False
            else:
                doc_id = row["id"]
                old_sha = row["sha256"]
                was_removed = bool(row["removed"])
                self.conn.execute(
                    "UPDATE document SET title = ?, kind = ?, section = ?, position = ?,"
                    " removed = 0 WHERE id = ?",
                    (link.title, link.kind, link.section, link.position, doc_id),
                )
            outcome = self._store_content(hearing_id, doc_id, link, old_sha, max_age)
            if outcome == "transient":
                result.transient_errors += 1
            elif outcome == "changed" or was_removed:
                result.changed = True
                if old_sha is None and row is None:
                    result.new_documents += 1
                else:
                    result.changed_documents += 1
        for url, row in existing.items():
            if url not in seen and not row["removed"]:
                self.conn.execute("UPDATE document SET removed = 1 WHERE id = ?", (row["id"],))
                result.removed_documents += 1
                result.changed = True
        return result

    def _store_content(
        self, hearing_id: int, doc_id: int, link: DocumentLink, old_sha: str | None, max_age: float
    ) -> str:
        """Fetch/extract one document. Returns 'changed', 'unchanged' or 'transient'."""
        text_path = text_path_for(self.settings.text_dir, hearing_id, doc_id)
        if link.is_inline:
            extracted = extract_html(link.inline_html or "")
            sha = hashlib.sha256(extracted.text.encode("utf-8")).hexdigest()
            if sha == old_sha and text_path.is_file():
                return "unchanged"
            write_text(text_path, extracted.text)
            self._update_doc(doc_id, sha, 1, text_path, "ok", None)
            return "changed"

        try:
            fetched = self.fetcher.get(link.url, max_age=max_age)
        except FetchError as exc:
            if exc.status in PERMANENT_STATUSES:
                self.conn.execute(
                    "UPDATE document SET fetch_status = 'missing', note = ? WHERE id = ?",
                    (f"Dokumentet finnes ikke lenger ({exc.status})", doc_id),
                )
                log.warning("Document %s is gone: %s", link.url, exc)
                return "changed" if old_sha else "unchanged"
            self.conn.execute(
                "UPDATE document SET fetch_status = 'error', note = ? WHERE id = ?",
                (str(exc), doc_id),
            )
            log.warning("Transient error fetching %s: %s", link.url, exc)
            return "transient"

        sha = hashlib.sha256(fetched.content).hexdigest()
        if sha == old_sha and (text_path.is_file() or not self._is_pdf(link.url, fetched)):
            return "unchanged"
        suffix = Path(urlparse(link.url).path).suffix.lower() or ".bin"
        file_path = self.settings.documents_dir / str(hearing_id) / f"{doc_id}{suffix}"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(fetched.content)

        if not self._is_pdf(link.url, fetched):
            self._update_doc(
                doc_id, sha, None, None, "unsupported", f"{UNSUPPORTED_NOTE} ({suffix})"
            )
            log.info("Unsupported document format %s (%s)", suffix, link.url)
            return "changed"
        try:
            extracted = extract_pdf(file_path)
        except Exception as exc:  # corrupt or encrypted PDF
            self._update_doc(doc_id, sha, None, None, "unreadable", f"Kunne ikke leses: {exc}")
            log.warning("Could not read PDF %s: %s", link.url, exc)
            return "changed"
        write_text(text_path, extracted.text)
        note = (
            f"{extracted.low_text_pages} av {extracted.page_count} sider uten tekst (skannet?)"
            if extracted.probably_scanned
            else None
        )
        self._update_doc(doc_id, sha, extracted.page_count, text_path, "ok", note)
        return "changed"

    @staticmethod
    def _is_pdf(url: str, fetched) -> bool:
        content_type = (fetched.content_type or "").lower()
        return (
            "pdf" in content_type or url.lower().endswith(".pdf") or fetched.content[:5] == b"%PDF-"
        )

    def _update_doc(
        self,
        doc_id: int,
        sha: str,
        page_count: int | None,
        text_path: Path | None,
        status: str,
        note: str | None,
    ) -> None:
        self.conn.execute(
            "UPDATE document SET sha256 = ?, page_count = ?, text_path = ?, fetch_status = ?,"
            " note = ?, last_changed = ? WHERE id = ?",
            (
                sha,
                page_count,
                str(text_path) if text_path else None,
                status,
                note,
                db.now_iso(),
                doc_id,
            ),
        )

    # -- responses ---------------------------------------------------------

    def _update_responses(self, hearing_id: int, url: str, max_age: float) -> None:
        first = parse_responses(self.fetcher.get(url, max_age=max_age).text)
        entries = list(first.entries)
        if first.total and len(entries) < first.total and entries:
            pages = min(math.ceil(first.total / len(entries)), MAX_RESPONSE_PAGES)
            for page_no in range(2, pages + 1):
                more = parse_responses(
                    self.fetcher.get(f"{url}&page={page_no}", max_age=max_age).text
                )
                if not more.entries:
                    break
                entries.extend(more.entries)
        dt = next(
            (
                e
                for e in entries
                if e.name.lower() == DATATILSYNET.lower()
                or e.name.lower().startswith(DATATILSYNET.lower() + " ")
            ),
            None,
        )
        self.conn.execute(
            "UPDATE hearing SET dt_responded = ?, dt_response_no_comments = ? WHERE id = ?",
            (int(dt is not None), int(dt.no_comments) if dt else None, hearing_id),
        )
