"""The EU/EEA radar's admin panel. Server-rendered Jinja2 with HTMX; no pipeline code runs here.

Same rules as the regjeringen.no radar's panel, whose login, CSRF, styles and job cancelling
are reused unchanged: every request needs HTTP Basic auth, every state change is a POST with a
CSRF token, and jobs are started as separate `horingsradar.eu` processes (see runner.py).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from horingsradar import db
from horingsradar.eu import db as eu_db
from horingsradar.eu.assess import DOC_TYPE_LABELS
from horingsradar.eu.config import EUSettings
from horingsradar.eu.report import EEA_LABELS, case_view
from horingsradar.eu.sources import eealex, haveyoursay, oeil
from horingsradar.eu.sources.eurlex import eurlex_url
from horingsradar.eu.web import runner, stats
from horingsradar.jobs import running_job
from horingsradar.llm.schema import RELEVANCE_LABELS
from horingsradar.report.build import CATEGORY_LABELS
from horingsradar.web import stats as shared_stats
from horingsradar.web.app import AuthMiddleware
from horingsradar.web.auth import check_csrf, csrf_token

HERE = Path(__file__).parent
SHARED_WEB = HERE.parent.parent / "web"  # the other panel's styles and HTMX, used as they are
FONTS_DIR = HERE.parent.parent / "report" / "fonts"
RELEVANCE_KEYS = ("hoy", "middels", "lav", "ingen", "feilet", "ikke_vurdert")
CASE_SORTS = {
    "nyeste": "Forslagsdato, nyeste først",
    "relevans": "Relevans, høy til lav",
    "frist": "Åpen frist på Have Your Say, nærmeste først",
}
EEA_FILTERS = {
    "registrert": "Registrert i EEA-Lex",
    "ikke": "Ikke registrert",
    "ukjent": "Ikke sjekket",
}
PHASES = {
    "discover": "Henter forslag fra EUR-Lex",
    "edps": "Henter EDPS-uttalelser",
    "enrich": "Henter EuroVoc og prosedyrer",
    "eea": "Sjekker EEA-Lex",
    "have_your_say": "Slår opp på Have Your Say",
    "documents": "Henter engelske PDF-er",
    "assess": "Vurderer saker",
    "oeil": "Slår opp i OEIL",
    "done": "Avslutter",
}


def _fmt_ts(value: str | None) -> str:
    return db.format_ts(value)


def _fmt_date(value: str | None) -> str:
    if not value:
        return "–"
    return datetime.fromisoformat(value[:10]).strftime("%d.%m.%Y")


def _num(value: int | float | None) -> str:
    if value is None:
        return "–"
    return f"{value:,.0f}".replace(",", " ")


def latest_relevance(conn: sqlite3.Connection) -> dict[int, str]:
    """Case id -> relevance of its latest assessment ("feilet" when that one failed)."""
    rows = conn.execute(
        "SELECT a.case_id, a.status, a.relevance FROM eu_assessment a WHERE a.id ="
        " (SELECT MAX(b.id) FROM eu_assessment b WHERE b.case_id = a.case_id)"
    ).fetchall()
    return {r["case_id"]: (r["relevance"] if r["status"] == "ok" else "feilet") for r in rows}


def open_deadlines(conn: sqlite3.Connection, today: date) -> dict[int, str]:
    """Case id -> nearest open Have Your Say deadline on `today`."""
    rows = conn.execute(
        "SELECT c.id, MIN(p.deadline) AS deadline FROM eu_case c"
        " JOIN eu_hys_publication p ON p.initiative_id = c.hys_initiative_id"
        " WHERE p.status = ? AND p.deadline >= ? GROUP BY c.id",
        (haveyoursay.OPEN, today.isoformat()),
    ).fetchall()
    return {r["id"]: r["deadline"] for r in rows}


def create_app(settings: EUSettings) -> FastAPI:
    settings.ensure_dirs()
    app = FastAPI(title="EU/EØS-radar", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(AuthMiddleware, settings=settings)
    app.mount("/static", StaticFiles(directory=SHARED_WEB / "static"), name="static")
    app.mount("/fonts", StaticFiles(directory=FONTS_DIR), name="fonts")
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.filters.update(ts=_fmt_ts, d=_fmt_date, num=_num)
    env.globals.update(
        relevance_labels={**RELEVANCE_LABELS, "feilet": "Feilet", "ikke_vurdert": "Ikke vurdert"},
        category_labels=CATEGORY_LABELS,
        doc_type_labels=DOC_TYPE_LABELS,
        eea_labels=EEA_LABELS,
        oeil_status_label=oeil.status_label,
        phases=PHASES,
        loads=db.loads,
        eurlex_url=eurlex_url,
        other_panel_url=settings.other_panel_url,
        job_states={
            "ok": "Ferdig",
            "running": "Kjører",
            "failed": "Feilet",
            "cancelled": "Avbrutt",
            "skipped": "Hoppet over",
        },
        job_kinds={"run": "Kjøring", "report": "Rapport", "assess": "Vurdering"},
        triggers={"timer": "Timer", "cli": "Terminal", "web": "Panelet"},
        skip_reasons={
            "paused": "planlagt kjøring var pauset",
            "lock_taken": "en annen jobb kjørte",
        },
    )
    token = csrf_token(settings.web_password_hash, settings.web_username)

    @contextmanager
    def connection() -> Iterator[sqlite3.Connection]:
        conn = eu_db.connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def render(request: Request, name: str, **context: Any) -> HTMLResponse:
        context.setdefault("msg", request.query_params.get("msg"))
        return templates.TemplateResponse(request, name, {"csrf_token": token, **context})

    async def require_csrf(request: Request) -> dict[str, str] | Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        data = {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return PlainTextResponse("Ugyldig opprinnelse.", status_code=403)
        supplied = request.headers.get("x-csrf-token") or data.get("csrf_token")
        if not check_csrf(supplied, settings.web_password_hash, settings.web_username):
            return PlainTextResponse("Ugyldig eller manglende CSRF-token.", status_code=403)
        return data

    def back(path: str, message: str) -> RedirectResponse:
        return RedirectResponse(f"{path}?msg={quote(message)}", status_code=303)

    def start(conn: sqlite3.Connection, args: list[str], path: str, label: str) -> RedirectResponse:
        try:
            runner.start_job(settings, conn, args)
        except runner.JobBusy as exc:
            return back(path, str(exc))
        return back(path, f"{label} er startet.")

    # -- status ------------------------------------------------------------

    def status_context(conn: sqlite3.Connection) -> dict[str, Any]:
        current = running_job(conn)
        current_view = None
        if current:
            current_view = dict(current)
            current_view["progress_data"] = db.loads(current["progress"], {}) or {}
            current_view["args_data"] = db.loads(current["args"], {}) or {}
        return {
            "paused": db.runs_paused(conn),
            "current": current_view,
            "last_run": shared_stats.last_job(conn, "run"),
            "next_run": shared_stats.next_scheduled_run(settings.run_timer_unit),
            "pending": conn.execute(
                "SELECT COUNT(*) FROM eu_case WHERE candidate = 1 AND needs_assessment = 1"
            ).fetchone()[0],
        }

    def relevance_counts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        latest = latest_relevance(conn)
        counts = dict.fromkeys(RELEVANCE_KEYS, 0)
        for (case_id,) in conn.execute("SELECT id FROM eu_case WHERE candidate = 1"):
            counts[latest.get(case_id, "ikke_vurdert")] += 1
        return [{"key": key, "count": counts[key]} for key in RELEVANCE_KEYS]

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        with connection() as conn:
            last = shared_stats.last_job(conn)
            radar = relevance_counts(conn)
            return render(
                request,
                "index.html",
                **status_context(conn),
                last_job=last,
                log_tail=shared_stats.tail(last["log_path"] if last else None, 60),
                candidates=sum(seg["count"] for seg in radar),
                radar=[seg for seg in radar if seg["count"]],
                counts=stats.counts(conn),
            )

    @app.get("/partials/status", response_class=HTMLResponse)
    def status_partial(request: Request):
        with connection() as conn:
            return render(request, "partials/status.html", **status_context(conn))

    # -- cases -------------------------------------------------------------

    def candidate_rows(conn: sqlite3.Connection, today: date) -> list[dict[str, Any]]:
        latest = latest_relevance(conn)
        deadlines = open_deadlines(conn, today)
        edps_cases = {r[0] for r in conn.execute("SELECT DISTINCT case_id FROM eu_edps_link")}
        items = []
        for r in conn.execute(
            "SELECT * FROM eu_case WHERE candidate = 1 ORDER BY date_document DESC, id DESC"
        ):
            item = dict(r)
            item["relevance"] = latest.get(r["id"], "ikke_vurdert")
            item["deadline"] = deadlines.get(r["id"])
            item["edps"] = r["id"] in edps_cases
            item["eea_label"] = (
                EEA_LABELS.get(r["eea_status"], r["eea_status"])
                if r["eea_status"]
                else "Ikke sjekket"
            )
            items.append(item)
        return items

    def matches(
        item: dict[str, Any], relevance: str, eea: str, edps: bool, open_only: bool, q: str
    ) -> bool:
        if relevance and item["relevance"] != relevance:
            return False
        status = item["eea_status"]
        if eea == "registrert" and (not status or status == eealex.NOT_REGISTERED):
            return False
        if eea == "ikke" and status != eealex.NOT_REGISTERED:
            return False
        if eea == "ukjent" and status:
            return False
        if edps and not item["edps"]:
            return False
        if open_only and not item["deadline"]:
            return False
        if q:
            haystack = " ".join(
                str(item[k] or "")
                for k in ("title", "com_ref", "celex", "oeil_title", "procedure_ref")
            ).lower()
            return all(word in haystack for word in q.lower().split())
        return True

    def sort_cases(items: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
        if sort == "relevans":
            rank = {key: i for i, key in enumerate(RELEVANCE_KEYS)}
            return sorted(items, key=lambda i: rank[i["relevance"]])
        if sort == "frist":
            return sorted(items, key=lambda i: (i["deadline"] is None, i["deadline"] or ""))
        return items

    @app.get("/cases", response_class=HTMLResponse)
    def cases(request: Request):
        params = request.query_params
        relevance = params.get("relevans", "")
        relevance = relevance if relevance in RELEVANCE_KEYS else ""
        eea = params.get("eos", "")
        eea = eea if eea in EEA_FILTERS else ""
        edps = params.get("edps") == "1"
        open_only = params.get("frist") == "1"
        query = params.get("q", "").strip()
        sort = params.get("sorter", "")
        sort = sort if sort in CASE_SORTS else next(iter(CASE_SORTS))
        with connection() as conn:
            items = candidate_rows(conn, date.today())
        shown = sort_cases(
            [i for i in items if matches(i, relevance, eea, edps, open_only, query)], sort
        )
        counts = dict.fromkeys(RELEVANCE_KEYS, 0)
        for i in items:
            counts[i["relevance"]] += 1
        current = {
            "relevans": relevance,
            "eos": eea,
            "edps": "1" if edps else "",
            "frist": "1" if open_only else "",
            "q": query,
            "sorter": sort,
        }

        def link(**changes: str) -> str:
            merged = {**current, **changes}
            return "/cases?" + urlencode({k: v for k, v in merged.items() if v})

        return render(
            request,
            "cases.html",
            cases=shown,
            total=len(items),
            matched=len(shown),
            counts=[{"key": k, "count": counts[k]} for k in RELEVANCE_KEYS],
            relevance=relevance,
            eea=eea,
            eea_filters=EEA_FILTERS,
            edps=edps,
            open_only=open_only,
            query=query,
            sort=sort,
            sorts=CASE_SORTS,
            link=link,
        )

    @app.get("/cases/{case_id}", response_class=HTMLResponse)
    def case_detail(request: Request, case_id: int):
        with connection() as conn:
            row = conn.execute("SELECT * FROM eu_case WHERE id = ?", (case_id,)).fetchone()
            if row is None:
                return PlainTextResponse("Ukjent sak.", status_code=404)
            view = case_view(conn, row, date.today())
            assessments = [
                dict(a)
                for a in conn.execute(
                    "SELECT * FROM eu_assessment WHERE case_id = ? ORDER BY id DESC", (case_id,)
                )
            ]
            changes = [
                dict(c)
                for c in conn.execute(
                    "SELECT * FROM eu_case_change WHERE case_id = ? ORDER BY id DESC", (case_id,)
                )
            ]
            sources = [
                dict(s)
                for s in conn.execute(
                    "SELECT * FROM eu_case_source WHERE case_id = ? ORDER BY id", (case_id,)
                )
            ]
            events = [
                dict(e)
                for e in conn.execute(
                    "SELECT * FROM eu_oeil_event WHERE case_id = ? ORDER BY date DESC, id DESC",
                    (case_id,),
                )
            ]
            return render(
                request,
                "case.html",
                v=view,
                case=dict(row),
                reasons=db.loads(row["prefilter"], {}) or {},
                assessments=assessments,
                changes=changes,
                sources=sources,
                oeil_events=events,
                hys_url=view.hys_url,
            )

    @app.get("/filtered", response_class=HTMLResponse)
    def filtered(request: Request):
        query = request.query_params.get("q", "").strip()
        with connection() as conn:
            labels = dict(conn.execute("SELECT id, label FROM eu_eurovoc").fetchall())
            rows = conn.execute(
                "SELECT * FROM eu_case WHERE candidate = 0 ORDER BY date_document DESC, id DESC"
            ).fetchall()
        items = []
        for r in rows:
            item = dict(r)
            item["eurovoc_labels"] = [labels.get(c, c) for c in (db.loads(r["eurovoc"], []) or [])]
            if query:
                haystack = " ".join(
                    [
                        r["title"] or "",
                        r["com_ref"] or "",
                        r["celex"] or "",
                        *item["eurovoc_labels"],
                    ]
                ).lower()
                if not all(word in haystack for word in query.lower().split()):
                    continue
            items.append(item)
        return render(request, "filtered.html", items=items, total=len(rows), query=query)

    # -- reports -----------------------------------------------------------

    @app.get("/reports", response_class=HTMLResponse)
    def reports(request: Request):
        with connection() as conn:
            rows = conn.execute("SELECT * FROM eu_report ORDER BY id DESC LIMIT 200").fetchall()
            return render(request, "reports.html", reports=[dict(r) for r in rows])

    @app.get("/reports/{report_id}/download")
    def download(report_id: int):
        with connection() as conn:
            row = conn.execute("SELECT path FROM eu_report WHERE id = ?", (report_id,)).fetchone()
        if row is None:
            return PlainTextResponse("Ukjent rapport.", status_code=404)
        path = Path(row["path"]).resolve()
        reports_dir = settings.reports_dir.resolve()
        if reports_dir not in path.parents or not path.is_file():
            return PlainTextResponse("Filen finnes ikke.", status_code=404)
        return FileResponse(path, media_type="application/pdf", filename=path.name)

    # -- statistics, jobs, logs ---------------------------------------------

    @app.get("/stats", response_class=HTMLResponse)
    def statistics(request: Request):
        with connection() as conn:
            return render(request, "stats.html", s=stats.statistics(conn))

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(request: Request):
        with connection() as conn:
            rows = conn.execute("SELECT * FROM job ORDER BY id DESC LIMIT 100").fetchall()
        items = []
        for r in rows:
            item = dict(r)
            item["summary_data"] = db.loads(r["summary"], {}) or {}
            item["args_data"] = db.loads(r["args"], {}) or {}
            items.append(item)
        return render(request, "jobs.html", jobs=items)

    @app.get("/jobs/{job_id}/log", response_class=HTMLResponse)
    def job_log(request: Request, job_id: int):
        with connection() as conn:
            row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return PlainTextResponse("Ukjent jobb.", status_code=404)
        return render(
            request, "job_log.html", job=dict(row), log=shared_stats.tail(row["log_path"], 400)
        )

    # -- actions -----------------------------------------------------------

    @app.post("/actions/run")
    async def action_run(request: Request):
        data = await require_csrf(request)
        if isinstance(data, Response):
            return data
        args = ["run", "--no-assess"] if data.get("no_assess") == "1" else ["run"]
        label = "Kjøring uten vurdering" if len(args) == 2 else "Kjøring"
        with connection() as conn:
            return start(conn, args, "/", label)

    @app.post("/actions/report")
    async def action_report(request: Request):
        data = await require_csrf(request)
        if isinstance(data, Response):
            return data
        with connection() as conn:
            return start(conn, ["report"], "/reports", "Rapportgenerering")

    @app.post("/actions/assess/{case_id}")
    async def action_assess(request: Request, case_id: int):
        data = await require_csrf(request)
        if isinstance(data, Response):
            return data
        with connection() as conn:
            if not conn.execute("SELECT 1 FROM eu_case WHERE id = ?", (case_id,)).fetchone():
                return back("/cases", "Ukjent sak.")
            return start(conn, ["assess", str(case_id)], f"/cases/{case_id}", "Ny vurdering")

    @app.post("/actions/pause")
    async def action_pause(request: Request):
        data = await require_csrf(request)
        if isinstance(data, Response):
            return data
        with connection() as conn:
            db.set_setting(conn, "scheduled_runs_paused", "1")
        return back("/", "Planlagt kjøring er pauset.")

    @app.post("/actions/resume")
    async def action_resume(request: Request):
        data = await require_csrf(request)
        if isinstance(data, Response):
            return data
        with connection() as conn:
            db.set_setting(conn, "scheduled_runs_paused", "0")
        return back("/", "Planlagt kjøring er gjenopptatt.")

    @app.post("/actions/cancel/{job_id}")
    async def action_cancel(request: Request, job_id: int):
        data = await require_csrf(request)
        if isinstance(data, Response):
            return data
        with connection() as conn:
            message = runner.cancel_job(conn, job_id)
        return back("/", message)

    return app
