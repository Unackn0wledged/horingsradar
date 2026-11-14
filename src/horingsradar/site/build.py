"""Build the static website in DATA_DIR/site.

The site is index.html, data.json (all hearings in the period, filtered and sorted in the browser),
a PDF per hearing, the stylesheet, script and fonts, robots.txt and _headers (Cloudflare Pages).

- Hearings: every tracked hearing published in the last SITE_WEEKS weeks, plus every open one.
  Low and none are included, like in the report. Only operational assessments, never backtest.
- data.json holds only what the report shows. No cost, tokens, raw LLM output or error texts.
- A hearing's PDF is rendered again only when its content changes: the rendered HTML (without the
  generation time) is hashed and compared with DATA_DIR/site_cache/manifest.json.
- The site is built in a sibling directory and swapped in at the end, so `publish` never uploads
  a half-built site and a failed build leaves the previous one in place.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from horingsradar.config import Settings
from horingsradar.llm.schema import RELEVANCE_LABELS
from horingsradar.report.build import (
    DOCUMENT_CHANGE_LABELS,
    STATUS_LABELS,
    HearingView,
    _view,
    fmt_date,
    render_hearing_html,
    write_pdf,
)

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
FONTS_DIR = HERE.parent / "report" / "fonts"
STATIC_FILES = ("site.css", "site.js")
MAX_CHANGES = 10

ROBOTS_TXT = "User-agent: *\nDisallow: /\n"
# Cloudflare Pages reads this file and sets the headers on every response.
HEADERS = """/*
  X-Robots-Tag: noindex, nofollow
  Referrer-Policy: no-referrer
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  Permissions-Policy: camera=(), microphone=(), geolocation=()
  Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
/data.json
  Cache-Control: no-cache
/
  Cache-Control: no-cache
/index.html
  Cache-Control: no-cache
/pdf/*
  Cache-Control: no-cache
"""


def select_hearings(conn: sqlite3.Connection, weeks: int, today: date) -> list[sqlite3.Row]:
    """Tracked hearings published in the last `weeks` weeks, and all open ones."""
    start = (today - timedelta(weeks=weeks)).isoformat()
    return conn.execute(
        "SELECT * FROM hearing WHERE tracked = 1"
        " AND (published_date >= ? OR published_date IS NULL OR status = 'open')"
        " ORDER BY published_date DESC, id DESC",
        (start,),
    ).fetchall()


def _changes(conn: sqlite3.Connection, hearing_id: int) -> list[dict[str, str]]:
    """Deadline, document and relevance changes, newest first, as plain text."""
    items: list[tuple[str, str]] = []
    for r in conn.execute(
        "SELECT * FROM hearing_change WHERE hearing_id = ? ORDER BY detected_at, id", (hearing_id,)
    ):
        if r["kind"] == "deadline":
            items.append(
                (
                    r["detected_at"],
                    f"Ny frist: {fmt_date(r['old_value'])} → {fmt_date(r['new_value'])}",
                )
            )
        elif r["kind"] in DOCUMENT_CHANGE_LABELS:
            label = DOCUMENT_CHANGE_LABELS[r["kind"]]
            items.append((r["detected_at"], f"{label}: «{r['document_title'] or 'uten tittel'}»"))
    previous = None
    for a in conn.execute(
        "SELECT relevance, created_at FROM assessment WHERE hearing_id = ? AND context = 'drift'"
        " AND status = 'ok' ORDER BY created_at, id",
        (hearing_id,),
    ):
        if previous is not None and a["relevance"] != previous:
            old = RELEVANCE_LABELS.get(previous, previous)
            new = RELEVANCE_LABELS.get(a["relevance"], a["relevance"])
            items.append((a["created_at"], f"Relevans endret: {old} → {new}"))
        previous = a["relevance"]
    items.sort(key=lambda item: item[0], reverse=True)
    return [{"at": at, "date": at[:10], "text": text} for at, text in items[:MAX_CHANGES]]


def hearing_data(conn: sqlite3.Connection, view: HearingView) -> dict[str, Any]:
    """One hearing for data.json: only what the report shows, as plain text and https links."""
    row, a = view.row, view.assessment
    ok = a is not None and a["status"] == "ok"
    changes = _changes(conn, view.id)
    privacy = view.privacy_section if ok else None
    return {
        "id": view.id,
        "title": row["title"],
        "url": row["url"],
        "department": row["department"],
        "original_department": row["original_department"],
        "published": row["published_date"],
        "deadline": row["deadline"],
        "status": row["status"] or "unknown",
        "status_label": row["status_raw"]
        or STATUS_LABELS.get(row["status"] or "unknown", "Ukjent"),
        "on_address_list": None
        if row["dt_on_address_list"] is None
        else bool(row["dt_on_address_list"]),
        "relevance": view.relevance,
        "relevance_label": view.relevance_label,
        "relevance_reason": a["relevance_reason"] if ok else None,
        "summary": a["summary"] if ok else None,
        "sectors": view.sectors if ok else [],
        "assessed_at": a["created_at"][:10] if a else None,
        "model": a["model"] if a else None,
        "prompt_version": a["prompt_version"] if a else None,
        "privacy_section": privacy,
        "reading_guide": view.reading_guide if ok else [],
        "reading_share": view.reading_share if ok else None,
        "findings": [
            {
                "category_label": f["category_label"],
                "description": f["description"],
                "document_title": f["document_title"] or f["document_ref"],
                "chapter": f["chapter"],
                "page": f["page"],
                "page_href": f["page_href"],
                "quote": f["quote"],
                "verified": bool(f["quote_verified"]),
            }
            for f in view.findings
        ],
        "documents": [
            {
                "title": d["title"] or "Dokument",
                "url": d["url"],
                "read": d["fetch_status"] == "ok",
                "note": None if d["fetch_status"] == "ok" else (d["note"] or d["fetch_status"]),
            }
            for d in view.documents
        ],
        "notes": view.notes if ok else [],
        "first_seen": row["first_seen"],
        "changed_at": changes[0]["at"] if changes else None,
        "changes": [{"date": c["date"], "text": c["text"]} for c in changes],
        "pdf": f"pdf/{view.id}.pdf",
    }


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(HERE / "templates"),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _asset_version() -> str:
    digest = hashlib.sha256()
    for name in STATIC_FILES:
        digest.update((HERE / "static" / name).read_bytes())
    return digest.hexdigest()[:12]


def _link_or_copy(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


class PdfCache:
    """PDFs of single hearings in DATA_DIR/site_cache, keyed by a hash of their HTML."""

    def __init__(self, settings: Settings):
        self.dir = settings.site_cache_dir / "pdf"
        self.manifest_path = settings.site_cache_dir / "manifest.json"
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            self.manifest: dict[str, str] = json.loads(self.manifest_path.read_text("utf-8"))
        except (OSError, ValueError):
            self.manifest = {}
        self.rendered = self.reused = 0

    def pdf(self, hearing_id: int, html: str, generated_label: str) -> Path:
        # The generation time is the only part of the HTML that changes on every build.
        key = hashlib.sha256(html.replace(generated_label, "").encode("utf-8")).hexdigest()
        path = self.dir / f"{hearing_id}.pdf"
        if self.manifest.get(str(hearing_id)) == key and path.is_file():
            self.reused += 1
            return path
        write_pdf(html, path)
        self.manifest[str(hearing_id)] = key
        self.rendered += 1
        return path

    def save(self, keep: set[int]) -> None:
        for name in list(self.manifest):
            if int(name) not in keep:
                del self.manifest[name]
                (self.dir / f"{name}.pdf").unlink(missing_ok=True)
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.manifest, indent=0, sort_keys=True), encoding="utf-8")
        tmp.replace(self.manifest_path)


def _swap_in(new: Path, target: Path) -> None:
    old = target.with_name(target.name + ".old")
    if old.exists():
        shutil.rmtree(old)
    if target.exists():
        target.rename(old)
    new.rename(target)
    if old.exists():
        shutil.rmtree(old)


def build_site(
    settings: Settings, conn: sqlite3.Connection, now: datetime | None = None
) -> dict[str, Any]:
    """Build DATA_DIR/site. Returns counts for the job summary."""
    now = now or datetime.now().astimezone()
    today = now.date()
    generated_label = now.strftime("%d.%m.%Y kl. %H:%M")
    target = settings.site_dir
    work = target.with_name(target.name + ".new")
    if work.exists():
        shutil.rmtree(work)
    (work / "pdf").mkdir(parents=True)
    (work / "assets" / "fonts").mkdir(parents=True)

    cache = PdfCache(settings)
    hearings = []
    ids: set[int] = set()
    for row in select_hearings(conn, settings.site_weeks, today):
        view = _view(conn, row, today)
        hearings.append(hearing_data(conn, view))
        html = render_hearing_html(conn, view.id, now)
        _link_or_copy(cache.pdf(view.id, html, generated_label), work / "pdf" / f"{view.id}.pdf")
        ids.add(view.id)
    cache.save(ids)

    data = {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_label": generated_label,
        "site_weeks": settings.site_weeks,
        "period_start": (today - timedelta(weeks=settings.site_weeks)).isoformat(),
        "hearings": hearings,
    }
    (work / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    index = (
        _environment()
        .get_template("index.html.j2")
        .render(
            generated_label=generated_label,
            asset_version=_asset_version(),
            site_weeks=settings.site_weeks,
        )
    )
    (work / "index.html").write_text(index, encoding="utf-8")
    for name in STATIC_FILES:
        shutil.copy2(HERE / "static" / name, work / "assets" / name)
    for font in sorted(FONTS_DIR.glob("*.ttf")):
        shutil.copy2(font, work / "assets" / "fonts" / font.name)
    (work / "robots.txt").write_text(ROBOTS_TXT, encoding="utf-8")
    (work / "_headers").write_text(HEADERS, encoding="utf-8")

    _swap_in(work, target)
    counts: dict[str, int] = {}
    for h in hearings:
        counts[h["relevance"]] = counts.get(h["relevance"], 0) + 1
    result = {
        "hearings": len(hearings),
        "pdf_rendered": cache.rendered,
        "pdf_reused": cache.reused,
        "relevance": counts,
        "path": str(target),
    }
    log.info("Site built in %s: %s", target, result)
    return result
