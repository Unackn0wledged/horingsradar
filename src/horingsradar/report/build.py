"""PDF report (A4) built with Jinja2 + WeasyPrint.

By default the report contains every tracked hearing that has not been in an earlier report,
plus high/medium hearings that changed since the report that last showed them (see
select_for_report). `--week YYYY-Www` regenerates the hearings published in that ISO week, and
`--weeks N` makes a report of the hearings published in the last N weeks. Neither touches
`reported_in`, so the next operational report is the same with or without them.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

from horingsradar import db
from horingsradar.config import Settings
from horingsradar.llm.schema import RELEVANCE_LABELS, RELEVANCE_ORDER
from horingsradar.quotes import normalize

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
UPCOMING_DAYS = 28

CATEGORY_LABELS = {
    "behandlingsgrunnlag_hjemmel": "Behandlingsgrunnlag og hjemmel",
    "register": "Register",
    "deling_sammenstilling": "Deling og sammenstilling",
    "taushetsplikt": "Taushetsplikt",
    "overvaking_kontroll": "Overvåking og kontroll",
    "automatiserte_avgjorelser_ki": "Automatiserte avgjørelser og KI",
    "saerlige_kategorier": "Særlige kategorier",
    "barn": "Barn",
    "lagring_sletting": "Lagring og sletting",
    "rettigheter_innsyn": "Rettigheter og innsyn",
    "informasjonssikkerhet": "Informasjonssikkerhet",
    "testing_med_personopplysninger": "Testing med personopplysninger",
    "personvernregelverk_tilsyn": "Personvernregelverk og tilsyn",
    "annet": "Annet",
}
SECTOR_LABELS = {
    "helse_omsorg": "Helse og omsorg",
    "justis_politi": "Justis og politi",
    "arbeid_velferd": "Arbeid og velferd",
    "utdanning_forskning": "Utdanning og forskning",
    "barn_familie": "Barn og familie",
    "finans_skatt": "Finans og skatt",
    "digitalisering_forvaltning": "Digitalisering og forvaltning",
    "samferdsel_kommunikasjon": "Samferdsel og kommunikasjon",
    "innvandring_integrering": "Innvandring og integrering",
    "forsvar_sikkerhet": "Forsvar og sikkerhet",
    "naering": "Næring",
    "kultur_medier": "Kultur og medier",
    "kommune_bolig": "Kommune og bolig",
    "annet": "Annet",
}
STATUS_LABELS = {
    "open": "På høring",
    "under_behandling": "Under behandling",
    "ferdigbehandlet": "Ferdigbehandlet",
    "unknown": "Ukjent",
}


def _configure_weasyprint_dlls() -> None:
    """On Windows dev machines, point WeasyPrint at MSYS2's Pango if nothing else is set."""
    if sys.platform == "win32" and "WEASYPRINT_DLL_DIRECTORIES" not in os.environ:
        candidate = Path("C:/msys64/mingw64/bin")
        if candidate.is_dir():
            os.environ["WEASYPRINT_DLL_DIRECTORIES"] = str(candidate)


def parse_week(value: str) -> tuple[date, date]:
    match = WEEK_RE.match(value)
    if not match:
        raise ValueError("Week must be on the form YYYY-Www, for example 2026-W42")
    year, week = int(match.group(1)), int(match.group(2))
    monday = date.fromisocalendar(year, week, 1)
    return monday, monday + timedelta(days=6)


def fmt_date(value: str | date | None) -> str:
    if not value:
        return "–"
    if isinstance(value, str):
        d = db.parse_ts(value).date() if "T" in value else date.fromisoformat(value[:10])
    else:
        d = value
    return d.strftime("%d.%m.%Y")


def format_ranges(ranges: list[list[int]] | list[tuple[int, int]]) -> str:
    parts = [f"{a}–{b}" if a != b else f"{a}" for a, b in ranges]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " og " + parts[-1]


def page_link(url: str | None, page: int | None) -> str | None:
    """Link to a page in a source document.

    PDF viewers and browsers honour `#page=N`, which counts physical pages, the same pages as our
    `[[side N]]` markers. Documents that are part of the hearing web page (the letter, inline notat)
    have no pages, so they link to the page itself.
    """
    if not url:
        return None
    if urlparse(url).path.lower().endswith(".pdf"):
        base = url.split("#")[0]
        return f"{base}#page={page}" if page and page > 0 else base
    return url


def linked_ranges(
    url: str | None, ranges: list[list[int]] | list[tuple[int, int]]
) -> list[dict[str, Any]]:
    """Page ranges as labels, each linked to its first page."""
    return [
        {"label": f"{a}–{b}" if a != b else f"{a}", "href": page_link(url, a)} for a, b in ranges
    ]


@dataclass
class Changes:
    """What changed for a hearing since the operational report that last showed it."""

    since: str  # created_at of that report
    deadline: tuple[str | None, str | None] | None = None
    relevance: tuple[str, str] | None = None
    documents: list[dict[str, Any]] = field(default_factory=list)
    new_findings: int = 0
    dropped_findings: int = 0

    @property
    def material(self) -> bool:
        """Worth showing the full analysis again (a deadline alone is not)."""
        return bool(self.relevance or self.documents or self.new_findings or self.dropped_findings)

    @property
    def tags(self) -> list[str]:
        tags = []
        if self.deadline:
            tags.append("Ny frist")
        kinds = {d["kind"] for d in self.documents}
        for kind, label in DOCUMENT_CHANGE_LABELS.items():
            if kind in kinds:
                tags.append(label)
        if self.relevance:
            tags.append("Endret relevans")
        if not self.relevance and self.new_findings:
            tags.append("Nye funn")
        elif not self.relevance and self.dropped_findings:
            tags.append("Endrede funn")
        return tags


DOCUMENT_CHANGE_LABELS = {
    "document_added": "Nytt dokument",
    "document_changed": "Endret dokument",
    "document_removed": "Fjernet dokument",
}


@dataclass
class HearingView:
    row: dict[str, Any]
    assessment: dict[str, Any] | None
    findings: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    changes: Changes | None = None  # set when shown again after an earlier report
    today: date = field(default_factory=date.today)

    @property
    def id(self) -> int:
        return int(self.row["id"])

    @property
    def updated(self) -> bool:
        return self.changes is not None

    @property
    def relevance(self) -> str:
        if self.assessment is None:
            return "ikke_vurdert"
        if self.assessment["status"] != "ok":
            return "feilet"
        return self.assessment["relevance"]

    @property
    def relevance_label(self) -> str:
        return RELEVANCE_LABELS.get(
            self.relevance,
            {"feilet": "Feilet", "ikke_vurdert": "Ikke vurdert"}.get(
                self.relevance, self.relevance
            ),
        )

    @property
    def sort_key(self) -> tuple:
        rank = RELEVANCE_ORDER.get(self.relevance, -1)
        deadline = self.row.get("deadline") or "9999-12-31"
        return (-rank, deadline, self.row.get("title") or "")

    @property
    def days_left(self) -> int | None:
        if not self.row.get("deadline"):
            return None
        return (date.fromisoformat(self.row["deadline"]) - self.today).days

    @property
    def sectors(self) -> list[str]:
        if not self.assessment or not self.assessment.get("sectors"):
            return []
        return [SECTOR_LABELS.get(s, s) for s in db.loads(self.assessment["sectors"], [])]

    def document_url(self, document_id: int | None) -> str | None:
        return next((d["url"] for d in self.documents if d["id"] == document_id), None)

    @property
    def reading_guide(self) -> list[dict[str, Any]]:
        """Per document: title, total pages and the page ranges, each linked to its first page."""
        if not self.assessment:
            return []
        guide = db.loads(self.assessment.get("reading_guide"), {}) or {}
        entries = []
        for doc in guide.get("documents", []):
            if not doc.get("ranges"):
                continue
            url = self.document_url(doc.get("document_id"))
            entries.append(
                {
                    "title": doc["title"],
                    "page_count": doc.get("page_count"),
                    "ranges": linked_ranges(url, doc["ranges"]),
                }
            )
        return entries

    @property
    def reading_share(self) -> str | None:
        if not self.assessment:
            return None
        guide = db.loads(self.assessment.get("reading_guide"), {}) or {}
        if not guide.get("total_pages"):
            return None
        return f"{guide.get('relevant_pages', 0)} av {guide['total_pages']} sider"

    @property
    def privacy_section(self) -> dict[str, Any] | None:
        """None when not assessed; otherwise whether a privacy chapter exists, and where."""
        if not self.assessment:
            return None
        p = db.loads(self.assessment.get("privacy_section"), {}) or {}
        if not p.get("exists"):
            return {"exists": False}
        url = self.document_url(p.get("document_id"))
        return {
            "exists": True,
            "chapter": p.get("chapter"),
            "pages": linked_ranges(url, [p["pages"]]) if p.get("pages") else [],
            "document_title": p.get("document_title"),
        }

    @property
    def unread_documents(self) -> list[str]:
        return [
            f"{d['title']} ({d['note'] or d['fetch_status']})"
            for d in self.documents
            if d["fetch_status"] not in ("ok",)
        ]

    @property
    def notes(self) -> list[str]:
        if not self.assessment:
            return []
        return db.loads(self.assessment.get("notes"), []) or []


def _view(conn: sqlite3.Connection, row: sqlite3.Row, today: date) -> HearingView:
    assessment = db.latest_assessment(conn, row["id"], "drift")
    a = dict(assessment) if assessment else None
    findings = (
        [dict(f) for f in db.findings_for(conn, a["id"])] if a and a["status"] == "ok" else []
    )
    for f in findings:
        f["category_label"] = CATEGORY_LABELS.get(f["category"], f["category"])
        f["page_href"] = page_link(f.get("document_url"), f.get("page"))
    documents = [dict(d) for d in db.documents_for(conn, row["id"])]
    return HearingView(dict(row), a, findings, documents, None, today)


def _quote_key(quote: str | None) -> str:
    return normalize(quote or "")


def _baseline(conn: sqlite3.Connection, hearing_id: int, report_id: int) -> dict[str, Any] | None:
    """The assessment and deadline the given report showed for this hearing."""
    report = conn.execute("SELECT created_at FROM report WHERE id = ?", (report_id,)).fetchone()
    if report is None:
        return None
    item = conn.execute(
        "SELECT assessment_id, deadline FROM report_item WHERE report_id = ? AND hearing_id = ?",
        (report_id, hearing_id),
    ).fetchone()
    if item is not None:
        return {
            "since": report["created_at"],
            "assessment_id": item["assessment_id"],
            "deadline": item["deadline"],
            "known_deadline": True,
        }
    # Reports made before report_item existed: the latest assessment at report time.
    a = conn.execute(
        "SELECT id FROM assessment WHERE hearing_id = ? AND context = 'drift' AND created_at <= ?"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (hearing_id, report["created_at"]),
    ).fetchone()
    return {
        "since": report["created_at"],
        "assessment_id": a["id"] if a else None,
        "deadline": None,
        "known_deadline": False,
    }


def compare(conn: sqlite3.Connection, view: HearingView, base: dict[str, Any]) -> Changes:
    """Changes since the baseline. Marks findings that are new with `is_new`."""
    since = base["since"]
    changes = Changes(since=since)
    rows = conn.execute(
        "SELECT * FROM hearing_change WHERE hearing_id = ? AND detected_at > ? ORDER BY detected_at, id",
        (view.id, since),
    ).fetchall()

    current_deadline = view.row.get("deadline")
    if base["known_deadline"]:
        old_deadline = base["deadline"]
    else:
        logged = [r for r in rows if r["kind"] == "deadline"]
        old_deadline = logged[0]["old_value"] if logged else current_deadline
    if old_deadline and current_deadline and old_deadline != current_deadline:
        changes.deadline = (old_deadline, current_deadline)

    # A document removed and added again under the same title (typically a new URL) is one
    # changed document, not a new one plus a removed one.
    doc_rows = [r for r in rows if r["kind"].startswith("document_")]
    added = {r["document_title"] for r in doc_rows if r["kind"] == "document_added"}
    removed = {r["document_title"] for r in doc_rows if r["kind"] == "document_removed"}
    replaced = added & removed
    seen_docs: set[tuple[str, str]] = set()
    for r in doc_rows:
        kind = "document_changed" if r["document_title"] in replaced else r["kind"]
        if (kind, r["document_title"]) not in seen_docs:
            seen_docs.add((kind, r["document_title"]))
            changes.documents.append(
                {
                    "kind": kind,
                    "label": DOCUMENT_CHANGE_LABELS.get(kind, kind),
                    "title": r["document_title"],
                    "date": r["detected_at"],
                }
            )

    if view.assessment and base["assessment_id"] and view.assessment["id"] != base["assessment_id"]:
        old = conn.execute(
            "SELECT * FROM assessment WHERE id = ?", (base["assessment_id"],)
        ).fetchone()
        if old is not None and old["status"] == "ok":
            if (
                view.assessment["status"] == "ok"
                and old["relevance"] != view.assessment["relevance"]
            ):
                changes.relevance = (old["relevance"], view.assessment["relevance"])
            old_quotes = {_quote_key(f["quote"]) for f in db.findings_for(conn, old["id"])}
            new_quotes = {_quote_key(f["quote"]) for f in view.findings}
            for f in view.findings:
                f["is_new"] = _quote_key(f["quote"]) not in old_quotes
            changes.new_findings = len(new_quotes - old_quotes)
            changes.dropped_findings = len(old_quotes - new_quotes)
    return changes


def select_for_report(
    conn: sqlite3.Connection, today: date
) -> tuple[list[HearingView], list[HearingView]]:
    """Hearings for the next operational report.

    Returns (full, deadline_only):
    - full: never reported before, or high/medium with a new assessment that changed something
      (relevance, findings or documents) since the report that last showed it;
    - deadline_only: high/medium hearings shown before whose only change is the deadline.
    Hearings that are now low/none are not shown again, and a re-assessment with the same
    result is not an update.
    """
    full, deadline_only = [], []
    for row in conn.execute("SELECT * FROM hearing WHERE tracked = 1"):
        view = _view(conn, row, today)
        if row["reported_in"] is None:
            full.append(view)
            continue
        if view.relevance not in ("hoy", "middels"):
            continue
        base = _baseline(conn, view.id, row["reported_in"])
        if base is None:
            continue
        changes = compare(conn, view, base)
        if changes.material:
            view.changes = changes
            full.append(view)
        elif changes.deadline:
            view.changes = changes
            deadline_only.append(view)
    return sorted(full, key=lambda v: v.sort_key), sorted(deadline_only, key=lambda v: v.sort_key)


def period_start(weeks: int, today: date) -> date:
    return today - timedelta(weeks=weeks)


def select_week(conn: sqlite3.Connection, start: date, end: date, today: date) -> list[HearingView]:
    rows = conn.execute(
        "SELECT * FROM hearing WHERE tracked = 1 AND published_date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return sorted((_view(conn, r, today) for r in rows), key=lambda v: v.sort_key)


def upcoming_deadlines(
    conn: sqlite3.Connection, exclude: set[int], today: date
) -> list[HearingView]:
    limit = today + timedelta(days=UPCOMING_DAYS)
    rows = conn.execute(
        "SELECT * FROM hearing WHERE tracked = 1 AND reported_in IS NOT NULL AND status = 'open'"
        " AND deadline BETWEEN ? AND ? ORDER BY deadline",
        (today.isoformat(), limit.isoformat()),
    ).fetchall()
    views = []
    for row in rows:
        if row["id"] in exclude:
            continue
        view = _view(conn, row, today)
        if view.relevance in ("hoy", "middels"):
            views.append(view)
    return views


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(HERE / "templates"),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["date"] = fmt_date
    return env


def render_html(context: dict[str, Any]) -> str:
    return _environment().get_template("report.html.j2").render(**context)


def write_pdf(html: str, path: Path) -> None:
    _configure_weasyprint_dlls()
    from weasyprint import HTML

    path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html, base_url=str(HERE)).write_pdf(path)


def build_report(
    settings: Settings,
    conn: sqlite3.Connection,
    week: str | None = None,
    now: datetime | None = None,
    weeks: int | None = None,
) -> dict[str, Any]:
    now = now or datetime.now().astimezone()
    today = now.date()
    deadline_only: list[HearingView] = []
    if weeks:
        start = period_start(weeks, today)
        views = select_week(conn, start, today, today)
        unit = "uke" if weeks == 1 else "uker"
        period_label = (
            f"Siste {weeks} {unit} ({fmt_date(start)}–{fmt_date(today)}), publisert i perioden"
        )
        kind = "period"
    elif week:
        start, end = parse_week(week)
        views = select_week(conn, start, end, today)
        iso_year, iso_week = int(week[:4]), int(week[-2:])
        period_label = (
            f"Uke {iso_week} {iso_year} ({fmt_date(start)}–{fmt_date(end)}), publisert i perioden"
        )
        kind = "week"
    else:
        views, deadline_only = select_for_report(conn, today)
        previous = conn.execute(
            "SELECT created_at FROM report WHERE kind = 'drift' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        iso = today.isocalendar()
        since = f"{fmt_date(previous['created_at'])}–" if previous else "til og med "
        period_label = f"Uke {iso.week} {iso.year} ({since}{fmt_date(today)})"
        kind = "drift"

    in_report = {v.id for v in views} | {v.id for v in deadline_only}
    upcoming = upcoming_deadlines(conn, in_report, today) if kind == "drift" else []
    detailed = [v for v in views if v.relevance in ("hoy", "middels")]
    brief = [v for v in views if v.relevance in ("lav", "ingen")]
    not_assessed = [v for v in views if v.relevance in ("feilet", "ikke_vurdert")]
    models = sorted(
        {
            f"{v.assessment['model']} / prompt {v.assessment['prompt_version']}"
            for v in views
            if v.assessment
        }
    )
    context = {
        "period_label": period_label,
        "generated_at": now.strftime("%d.%m.%Y kl. %H:%M"),
        "models": models,
        "views": views,
        "detailed": detailed,
        "brief": brief,
        "not_assessed": not_assessed,
        "upcoming": upcoming,
        "deadline_only": deadline_only,
        "relevance_labels": RELEVANCE_LABELS,
        "status_labels": STATUS_LABELS,
        "kind": kind,
        "fonts_dir": (HERE / "fonts").as_uri(),
    }
    html = render_html(context)
    stamp = now.strftime("%Y-%m-%d_%H%M")
    label = f"siste-{weeks}-uker" if weeks else week
    name = f"horingsradar_{label}_{stamp}.pdf" if label else f"horingsradar_{stamp}.pdf"
    path = settings.reports_dir / name
    write_pdf(html, path)
    cur = conn.execute(
        "INSERT INTO report (created_at, period_label, path, hearing_count, kind) VALUES (?, ?, ?, ?, ?)",
        (db.now_iso(), period_label, str(path), len(views), kind),
    )
    report_id = int(cur.lastrowid)
    shown = views + deadline_only
    if kind == "drift" and shown:
        conn.executemany(
            "UPDATE hearing SET reported_in = ? WHERE id = ?", [(report_id, v.id) for v in shown]
        )
        # The baseline for the next report: what this report showed.
        conn.executemany(
            "INSERT OR REPLACE INTO report_item (report_id, hearing_id, assessment_id, deadline)"
            " VALUES (?, ?, ?, ?)",
            [
                (
                    report_id,
                    v.id,
                    v.assessment["id"] if v.assessment else None,
                    v.row.get("deadline"),
                )
                for v in shown
            ],
        )
    log.info("Report %s written to %s (%s hearings)", report_id, path, len(views))
    return {
        "report_id": report_id,
        "path": str(path),
        "hearings": len(views),
        "detailed": len(detailed),
        "upcoming": len(upcoming),
        "deadline_changes": len(deadline_only),
        "kind": kind,
    }
