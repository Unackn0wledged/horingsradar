"""The EU/EEA radar's PDF report: its own template, the pilot report's look and PDF writer.

What a report shows (docs/EU-SPEC.md, Rapport):
- candidates not shown before; those dated within the last 8 weeks in full, older ones as one
  line under "Tidligere forslag";
- high/medium cases shown before whose new assessment changed relevance or findings, again in
  full and marked; a case first shown without an assessment is shown again once it has one;
- high/medium cases shown before where only the procedure or EEA status changed, a
  corrigendum appeared, or a Have Your Say feedback period opened or moved: one line each;
- low/none cases are not shown again; a new assessment with the same result is not a change.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from horingsradar import db
from horingsradar.eu.assess import DOC_TYPE_LABELS
from horingsradar.eu.schema import EU_SECTOR_LABELS
from horingsradar.eu.sources import eealex, haveyoursay
from horingsradar.eu.sources.eurlex import eurlex_url
from horingsradar.llm.schema import RELEVANCE_LABELS, RELEVANCE_ORDER
from horingsradar.quotes import normalize
from horingsradar.report.build import CATEGORY_LABELS, fmt_date, write_pdf

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
FONTS_DIR = HERE.parent / "report" / "fonts"
RECENT = timedelta(weeks=8)

PROCEDURE_LABELS = {
    "PROP_PENDING": "Til behandling",
    "PROP_ADOPTED": "Vedtatt",
    "PROP_REJECTED": "Avvist",
    "WITHDRAWN": "Trukket",
}
EEA_LABELS = {
    "PROPOSED": "Registrert i EEA-Lex som mulig EØS-relevant",
    "SCRUTINY": "Til vurdering i EØS/EFTA-statene",
    "DRAFT_JCD": "Utkast til EØS-komitébeslutning",
    "JCD_PENDING": "EØS-komitébeslutning vedtatt, venter på ikrafttredelse",
    "IN_FORCE": "Innlemmet i EØS-avtalen og i kraft",
    "NO_LONGER_IN_FORCE": "Innlemmet i EØS-avtalen, ikke lenger i kraft",
    eealex.NOT_REGISTERED: "Ikke registrert i EEA-Lex",
}


@dataclass
class Changes:
    """What changed for a case since the report that last showed it."""

    since: str
    relevance: tuple[str, str] | None = None
    new_findings: int = 0
    dropped_findings: int = 0
    events: list[dict[str, str]] = field(default_factory=list)  # status changes and corrigenda

    @property
    def material(self) -> bool:
        """Worth showing the full analysis again (a status change alone is not)."""
        return bool(self.relevance or self.new_findings or self.dropped_findings)

    @property
    def tags(self) -> list[str]:
        tags = []
        if self.relevance:
            tags.append(
                "Endret relevans" if self.relevance[0] != "ikke_vurdert" else "Ny vurdering"
            )
        elif self.new_findings:
            tags.append("Nye funn")
        elif self.dropped_findings:
            tags.append("Endrede funn")
        tags += sorted({e["tag"] for e in self.events})
        return tags


@dataclass
class CaseView:
    row: dict[str, Any]
    assessment: dict[str, Any] | None
    findings: list[dict[str, Any]]
    document: dict[str, Any] | None
    eurovoc: list[str]
    today: date
    changes: Changes | None = None
    hys: dict[str, Any] | None = None  # the linked Have Your Say initiative
    hys_publications: list[dict[str, Any]] = field(default_factory=list)

    @property
    def id(self) -> int:
        return int(self.row["id"])

    @property
    def relevance(self) -> str:
        if self.assessment is None:
            return "ikke_vurdert"
        return self.assessment["relevance"] if self.assessment["status"] == "ok" else "feilet"

    @property
    def relevance_label(self) -> str:
        return {**RELEVANCE_LABELS, "feilet": "Feilet", "ikke_vurdert": "Ikke vurdert"}.get(
            self.relevance, self.relevance
        )

    @property
    def sort_key(self) -> tuple:
        """Highest relevance first, then open Have Your Say deadlines (nearest first), then the
        newest proposal."""
        d = self.row["date_document"]
        newest = -date.fromisoformat(d).toordinal() if d else 0
        deadline = self.open_deadline
        return (
            -RELEVANCE_ORDER.get(self.relevance, -1),
            0 if deadline else 1,
            date.fromisoformat(deadline).toordinal() if deadline else 0,
            newest,
        )

    def _is_open(self, p: dict[str, Any]) -> bool:
        return (
            p["status"] == haveyoursay.OPEN
            and bool(p["deadline"])
            and date.fromisoformat(p["deadline"]) >= self.today
        )

    @property
    def open_deadline(self) -> str | None:
        """The nearest deadline of a feedback period that is open on the report date."""
        deadlines = [p["deadline"] for p in self.hys_publications if self._is_open(p)]
        return min(deadlines) if deadlines else None

    @property
    def hys_url(self) -> str | None:
        return haveyoursay.PAGE_URL.format(path=self.hys["path"]) if self.hys else None

    @property
    def feedback(self) -> list[str]:
        """Feedback periods on Have Your Say: the open ones, and the proposal's own period."""
        lines = []
        for p in self.hys_publications:
            label = _stage_label(p)
            if p["status"] == haveyoursay.OPEN and p["deadline"]:
                state = "åpen til" if self._is_open(p) else "lukket"
                lines.append(f"Tilbakemelding på {label} {state} {fmt_date(p['deadline'])}")
            elif p["stage"] == haveyoursay.ADOPTION and p["status"] == "CLOSED":
                lines.append(f"Tilbakemelding på {label} lukket {fmt_date(p['deadline'])}")
            elif p["stage"] == haveyoursay.ADOPTION and p["status"] == haveyoursay.UPCOMING:
                planned = haveyoursay.planned_label(p["planned_period"])
                lines.append(
                    f"Tilbakemelding på {label} planlagt {planned}"
                    if planned
                    else f"Tilbakemelding på {label} kommer"
                )
        if self.hys and not lines:
            lines.append("Ingen åpen tilbakemelding")
        return lines

    @property
    def recent(self) -> bool:
        d = self.row["date_document"]
        return bool(d) and date.fromisoformat(d) >= self.today - RECENT

    @property
    def title(self) -> str:
        return self.row["title"]

    @property
    def reference(self) -> str:
        return self.row["com_ref"] or self.row["celex"]

    @property
    def eurlex_url(self) -> str:
        return eurlex_url(self.row["celex"])

    @property
    def doc_type(self) -> str:
        label = DOC_TYPE_LABELS.get(self.row["doc_type"], self.row["doc_type"] or "–")
        return label.removeprefix("Forslag til ").capitalize()

    @property
    def sectors(self) -> list[str]:
        if not self.assessment or self.assessment["status"] != "ok":
            return []
        return [EU_SECTOR_LABELS.get(s, s) for s in db.loads(self.assessment["sectors"], [])]

    @property
    def procedure(self) -> str:
        status = PROCEDURE_LABELS.get(self.row["procedure_status"] or "", "")
        ref = self.row["procedure_ref"] or ""
        return ", ".join(p for p in (status, ref) if p) or "–"

    @property
    def eea_label(self) -> str:
        status = self.row["eea_status"]
        return EEA_LABELS.get(status, "Ikke sjekket") if status else "Ikke sjekket"

    @property
    def eea_derived(self) -> bool:
        return bool(self.row["eea_status_derived"]) and self.row["eea_status"] == "PROPOSED"

    @property
    def eea_url(self) -> str | None:
        key = self.row["eea_lex_id"]
        return f"{eealex.BASE}/eea-lex/{key.lower()}" if key else None

    @property
    def eea_areas(self) -> list[str]:
        return db.loads(self.row["eea_areas"], []) or []

    def page_href(self, page: int | None) -> str | None:
        """The page in the English PDF; PDF viewers honour #page=N (physical pages)."""
        if not self.document or not self.document.get("url"):
            return None
        return f"{self.document['url']}#page={page}" if page else self.document["url"]

    @property
    def not_assessed_reason(self) -> str:
        if self.assessment and self.assessment["status"] != "ok":
            return f"Vurderingen feilet: {self.assessment['error']}"
        doc = self.document
        if doc is None:
            return "Teksten er ikke hentet ennå."
        if doc["fetch_status"] != "ok":
            return doc["note"] or "Teksten kunne ikke hentes."
        return "Venter på vurdering."

    @property
    def notes(self) -> list[str]:
        return db.loads(self.assessment["notes"], []) if self.assessment else []


def _stage_label(p: dict[str, Any]) -> str:
    """What the feedback is on, after "Tilbakemelding på"."""
    if p["stage"] == haveyoursay.ADOPTION:
        return "forslaget"
    if p["stage"] == "OPC_LAUNCHED" or p["type"] == "OPC_LAUNCHED":
        return "den offentlige konsultasjonen"
    if (p["type"] or "").startswith("CFE_"):
        return "innspillsfasen (call for evidence)"
    if p["stage"] == "ISC_WORKFLOW":
        return "utkastet"
    return "initiativet"


def _latest_assessment(conn: sqlite3.Connection, case_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM eu_assessment WHERE case_id = ? ORDER BY id DESC LIMIT 1", (case_id,)
    ).fetchone()
    return dict(row) if row else None


def _view(conn: sqlite3.Connection, row: sqlite3.Row, today: date) -> CaseView:
    a = _latest_assessment(conn, row["id"])
    findings = []
    if a and a["status"] == "ok":
        for f in conn.execute(
            "SELECT * FROM eu_finding WHERE assessment_id = ? ORDER BY id", (a["id"],)
        ):
            item = dict(f)
            item["category_label"] = CATEGORY_LABELS.get(f["category"], f["category"])
            findings.append(item)
    doc = conn.execute("SELECT * FROM eu_document WHERE case_id = ?", (row["id"],)).fetchone()
    ids = db.loads(row["eurovoc"], [])
    labels = {
        r["id"]: r["label"]
        for r in conn.execute(
            f"SELECT id, label FROM eu_eurovoc WHERE id IN ({', '.join('?' * len(ids))})", ids
        )
    }
    view = CaseView(
        dict(row),
        a,
        findings,
        dict(doc) if doc else None,
        [labels[i] for i in ids if labels.get(i)],
        today,
    )
    if row["hys_initiative_id"]:
        hys = conn.execute(
            "SELECT * FROM eu_hys_initiative WHERE id = ?", (row["hys_initiative_id"],)
        ).fetchone()
        view.hys = dict(hys) if hys else None
        view.hys_publications = [
            dict(p)
            for p in conn.execute(
                "SELECT * FROM eu_hys_publication WHERE initiative_id = ? ORDER BY id",
                (row["hys_initiative_id"],),
            )
        ]
    for f in view.findings:
        f["page_href"] = view.page_href(f["page"])
    return view


EVENT_TEXTS = {
    "procedure_status": ("Ny status", "Prosedyren: {old} → {new}"),
    "eea_status": ("Ny EØS-status", "EØS-status: {old} → {new}"),
    "variant": ("Rettelse", "Ny rettelse eller tilleggsdel publisert ({new})"),
}


def _event(change: sqlite3.Row) -> dict[str, str] | None:
    if change["kind"] == "hys_feedback":
        old, new = change["old_value"], change["new_value"]
        text = (
            f"Frist for tilbakemelding på Have Your Say: {fmt_date(old)} → {fmt_date(new)}"
            if old
            else f"Åpen for tilbakemelding på Have Your Say til {fmt_date(new)}"
        )
        return {"tag": "Ny frist", "text": text, "date": change["detected_at"]}
    if change["kind"] not in EVENT_TEXTS:
        return None
    tag, template = EVENT_TEXTS[change["kind"]]
    labels = PROCEDURE_LABELS if change["kind"] == "procedure_status" else EEA_LABELS
    return {
        "tag": tag,
        "text": template.format(
            old=labels.get(change["old_value"] or "", change["old_value"] or "–"),
            new=labels.get(change["new_value"] or "", change["new_value"] or "–"),
        ),
        "date": change["detected_at"],
    }


def compare(conn: sqlite3.Connection, view: CaseView, report_id: int) -> Changes | None:
    """Changes since the report `report_id` showed the case; None if there is no baseline."""
    report = conn.execute("SELECT created_at FROM eu_report WHERE id = ?", (report_id,)).fetchone()
    item = conn.execute(
        "SELECT * FROM eu_report_item WHERE report_id = ? AND case_id = ?", (report_id, view.id)
    ).fetchone()
    if report is None or item is None:
        return None
    changes = Changes(since=report["created_at"])
    for c in conn.execute(
        "SELECT * FROM eu_case_change WHERE case_id = ? AND detected_at > ? ORDER BY id",
        (view.id, report["created_at"]),
    ):
        event = _event(c)
        if event:
            changes.events.append(event)
    current = view.assessment if view.assessment and view.assessment["status"] == "ok" else None
    if current is None or current["id"] == item["assessment_id"]:
        return changes
    old = (
        conn.execute(
            "SELECT * FROM eu_assessment WHERE id = ?", (item["assessment_id"],)
        ).fetchone()
        if item["assessment_id"]
        else None
    )
    if old is None or old["status"] != "ok":
        changes.relevance = ("ikke_vurdert", current["relevance"])
        return changes
    if old["relevance"] != current["relevance"]:
        changes.relevance = (old["relevance"], current["relevance"])
    old_quotes = {
        normalize(f["quote"] or "")
        for f in conn.execute("SELECT quote FROM eu_finding WHERE assessment_id = ?", (old["id"],))
    }
    new_quotes = {normalize(f["quote"] or "") for f in view.findings}
    for f in view.findings:
        f["is_new"] = normalize(f["quote"] or "") not in old_quotes
    changes.new_findings = len(new_quotes - old_quotes)
    changes.dropped_findings = len(old_quotes - new_quotes)
    return changes


@dataclass
class Selection:
    full: list[CaseView] = field(default_factory=list)  # recent, or shown again
    older: list[CaseView] = field(default_factory=list)  # new but older than 8 weeks: one line
    events_only: list[CaseView] = field(default_factory=list)  # only status changes

    @property
    def shown(self) -> list[CaseView]:
        return self.full + self.older + self.events_only


def select_for_report(conn: sqlite3.Connection, today: date) -> Selection:
    sel = Selection()
    for row in conn.execute("SELECT * FROM eu_case WHERE candidate = 1"):
        view = _view(conn, row, today)
        if row["reported_in"] is None:
            (sel.full if view.recent else sel.older).append(view)
            continue
        changes = compare(conn, view, row["reported_in"])
        if changes is None:
            continue
        first_assessment = bool(changes.relevance and changes.relevance[0] == "ikke_vurdert")
        if view.relevance not in ("hoy", "middels") and not first_assessment:
            continue
        if changes.material:
            view.changes = changes
            sel.full.append(view)
        elif changes.events:
            view.changes = changes
            sel.events_only.append(view)
    for part in (sel.full, sel.older, sel.events_only):
        part.sort(key=lambda v: v.sort_key)
    return sel


def period_counts(conn: sqlite3.Connection, since: str | None) -> dict[str, int]:
    """Proposals fetched since the previous report: all, candidates and filtered out."""
    row = conn.execute(
        "SELECT COUNT(*) AS fetched, COALESCE(SUM(candidate), 0) AS candidates"
        " FROM eu_case WHERE first_seen > ?",
        (since or "",),
    ).fetchone()
    return {
        "fetched": int(row["fetched"]),
        "candidates": int(row["candidates"]),
        "filtered_out": int(row["fetched"]) - int(row["candidates"]),
    }


def hys_counts(conn: sqlite3.Connection, today: date) -> dict[str, int]:
    """Candidates with a Have Your Say page, and those open for feedback on the report date."""
    row = conn.execute(
        "SELECT COUNT(*) AS linked, COALESCE(SUM(EXISTS (SELECT 1 FROM eu_hys_publication p"
        "   WHERE p.initiative_id = c.hys_initiative_id AND p.status = ? AND p.deadline >= ?)),"
        " 0) AS open FROM eu_case c WHERE c.candidate = 1 AND c.hys_initiative_id IS NOT NULL",
        (haveyoursay.OPEN, today.isoformat()),
    ).fetchone()
    return {"linked": int(row["linked"]), "open": int(row["open"])}


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(HERE / "templates"),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["date"] = fmt_date
    return env


def build_report(settings, conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now().astimezone()
    today = now.date()
    previous = conn.execute("SELECT created_at FROM eu_report ORDER BY id DESC LIMIT 1").fetchone()
    since = previous["created_at"] if previous else None
    sel = select_for_report(conn, today)
    full = sel.full
    detailed = [v for v in full if v.relevance in ("hoy", "middels")]
    brief = [v for v in full if v.relevance in ("lav", "ingen")]
    not_assessed = [v for v in full if v.relevance in ("feilet", "ikke_vurdert")]
    models = sorted(
        {
            f"{v.assessment['model']} / prompt {v.assessment['prompt_version']}"
            for v in sel.shown
            if v.assessment
        }
    )
    period_label = (
        f"{fmt_date(since)}–{fmt_date(today)}" if since else f"til og med {fmt_date(today)}"
    )
    context = {
        "period_label": period_label,
        "generated_at": now.strftime("%d.%m.%Y kl. %H:%M"),
        "models": models,
        "counts": period_counts(conn, since),
        "hys_counts": hys_counts(conn, today),
        "full": full,
        "detailed": detailed,
        "brief": brief,
        "not_assessed": not_assessed,
        "older": sel.older,
        "events_only": sel.events_only,
        "recent_since": today - RECENT,
        "relevance_labels": RELEVANCE_LABELS,
        "fonts_dir": FONTS_DIR.as_uri(),
    }
    html = _environment().get_template("eu_report.html.j2").render(**context)
    path = settings.reports_dir / f"horingsradar-eu_{now.strftime('%Y-%m-%d_%H%M')}.pdf"
    write_pdf(html, path)
    shown = sel.shown
    cur = conn.execute(
        "INSERT INTO eu_report (created_at, period_label, path, case_count) VALUES (?, ?, ?, ?)",
        (db.now_iso(), period_label, str(path), len(shown)),
    )
    report_id = int(cur.lastrowid)
    conn.executemany(
        "UPDATE eu_case SET reported_in = ? WHERE id = ?", [(report_id, v.id) for v in shown]
    )
    conn.executemany(
        "INSERT OR REPLACE INTO eu_report_item"
        " (report_id, case_id, assessment_id, procedure_status, eea_status) VALUES (?, ?, ?, ?, ?)",
        [
            (
                report_id,
                v.id,
                v.assessment["id"] if v.assessment and v.assessment["status"] == "ok" else None,
                v.row["procedure_status"],
                v.row["eea_status"],
            )
            for v in shown
        ],
    )
    log.info("EU report %s written to %s (%s cases)", report_id, path, len(shown))
    return {
        "report_id": report_id,
        "path": str(path),
        "cases": len(shown),
        "detailed": len(detailed),
        "older": len(sel.older),
        "status_changes": len(sel.events_only),
    }
