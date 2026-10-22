"""Admin panel for the operator. Server-rendered Jinja2 with HTMX; no pipeline code runs here.

Every request needs HTTP Basic auth. Every state change is a POST with a CSRF token.
Jobs are started as separate CLI processes (see runner.py).
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

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
from starlette.middleware.base import BaseHTTPMiddleware

from horingsradar import db
from horingsradar.config import Settings
from horingsradar.jobs import running_job
from horingsradar.llm.schema import RELEVANCE_LABELS
from horingsradar.report.build import (
    CATEGORY_LABELS,
    SECTOR_LABELS,
    STATUS_LABELS,
    format_ranges,
    linked_ranges,
    page_link,
)
from horingsradar.web import runner, stats
from horingsradar.web.auth import check_basic_auth, check_csrf, csrf_token

HERE = Path(__file__).parent
FONTS_DIR = HERE.parent / "report" / "fonts"  # same typeface as the PDF report
RELEVANCE_KEYS = ("hoy", "middels", "lav", "ingen", "feilet", "ikke_vurdert")
ESTIMATE_MAX_AGE = timedelta(hours=24)


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings):
        super().__init__(app)
        self.settings = settings
        self._verified: set[str] = set()

    async def dispatch(self, request: Request, call_next):
        header = request.headers.get("authorization")
        key = hashlib.sha256((header or "").encode()).hexdigest()
        if key not in self._verified:
            if not check_basic_auth(
                header, self.settings.web_username, self.settings.web_password_hash
            ):
                return Response(
                    "Innlogging kreves.",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Horingsradar", charset="UTF-8"'},
                )
            self._verified.add(key)
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response


def _fmt_ts(value: str | None) -> str:
    return db.format_ts(value)


def _fmt_date(value: str | None) -> str:
    if not value:
        return "–"
    return datetime.fromisoformat(value[:10]).strftime("%d.%m.%Y")


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value * 100:.0f} %"


def _num(value: int | float | None) -> str:
    if value is None:
        return "–"
    return f"{value:,.0f}".replace(",", " ")


def create_app(settings: Settings) -> FastAPI:
    settings.ensure_dirs()
    app = FastAPI(title="Høringsradar", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(AuthMiddleware, settings=settings)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.mount("/fonts", StaticFiles(directory=FONTS_DIR), name="fonts")
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.filters.update(ts=_fmt_ts, d=_fmt_date, pct=_pct, num=_num, ranges=format_ranges)
    env.globals.update(
        relevance_labels={**RELEVANCE_LABELS, "feilet": "Feilet", "ikke_vurdert": "Ikke vurdert"},
        category_labels=CATEGORY_LABELS,
        sector_labels=SECTOR_LABELS,
        status_labels=STATUS_LABELS,
        loads=db.loads,
        page_link=page_link,
        linked_ranges=linked_ranges,
        job_states={
            "ok": "Ferdig",
            "running": "Kjører",
            "failed": "Feilet",
            "cancelled": "Avbrutt",
            "skipped": "Hoppet over",
        },
        job_kinds={
            "run": "Kjøring",
            "report": "Rapport",
            "assess": "Vurdering",
            "backtest": "Backtest",
        },
        doc_kinds={
            "horingsbrev": "Høringsbrev",
            "horingsnotat": "Høringsnotat",
            "lovutkast": "Lov- eller forskriftsutkast",
            "annet": "Vedlegg",
        },
        doc_states={
            "ok": "Lest",
            "pending": "Venter",
            "unsupported": "Ikke lest",
            "missing": "Finnes ikke",
            "error": "Hentefeil",
            "unreadable": "Kan ikke leses",
        },
    )
    token = csrf_token(settings.web_password_hash, settings.web_username)

    @contextmanager
    def connection() -> Iterator[sqlite3.Connection]:
        conn = db.connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def render(request: Request, name: str, **context: Any) -> HTMLResponse:
        context.setdefault("msg", request.query_params.get("msg"))
        return templates.TemplateResponse(
            request,
            name,
            {"csrf_token": token, **context},
        )

    async def form(request: Request) -> dict[str, str]:
        body = (await request.body()).decode("utf-8", errors="replace")
        return {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}

    async def require_csrf(request: Request) -> dict[str, str] | Response:
        data = await form(request)
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
        last_run = stats.last_job(conn, "run")
        return {
            "paused": db.runs_paused(conn),
            "current": current_view,
            "last_run": last_run,
            "next_run": stats.next_scheduled_run(settings.run_timer_unit),
            "pending": conn.execute(
                "SELECT COUNT(*) FROM hearing WHERE tracked = 1 AND needs_assessment = 1"
            ).fetchone()[0],
        }

    def tracked_hearings(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT h.*, (SELECT a.relevance || '|' || a.status FROM assessment a"
            "  WHERE a.hearing_id = h.id AND a.context = 'drift'"
            "  ORDER BY a.created_at DESC, a.id DESC LIMIT 1) AS latest"
            " FROM hearing h WHERE h.tracked = 1"
            " ORDER BY h.published_date DESC, h.id DESC"
        ).fetchall()
        today = date.today()
        items = []
        for r in rows:
            item = dict(r)
            relevance, _, status = (r["latest"] or "|").partition("|")
            item["relevance"] = (
                "ikke_vurdert" if not status else (relevance if status == "ok" else "feilet")
            )
            item["days_left"] = (
                (date.fromisoformat(r["deadline"]) - today).days if r["deadline"] else None
            )
            items.append(item)
        return items

    def relevance_counts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        counts = {key: 0 for key in RELEVANCE_KEYS}
        for item in items:
            counts[item["relevance"]] += 1
        return [{"key": key, "count": counts[key]} for key in RELEVANCE_KEYS]

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        with connection() as conn:
            last = stats.last_job(conn)
            items = tracked_hearings(conn)
            return render(
                request,
                "index.html",
                **status_context(conn),
                last_job=last,
                log_tail=stats.tail(last["log_path"] if last else None, 60),
                tracked=len(items),
                radar=[seg for seg in relevance_counts(items) if seg["count"]],
            )

    @app.get("/partials/status", response_class=HTMLResponse)
    def status_partial(request: Request):
        with connection() as conn:
            return render(request, "partials/status.html", **status_context(conn))

    # -- hearings ----------------------------------------------------------

    @app.get("/hearings", response_class=HTMLResponse)
    def hearings(request: Request):
        relevance = request.query_params.get("relevans", "")
        if relevance not in RELEVANCE_KEYS:
            relevance = ""
        query = request.query_params.get("q", "").strip()
        with connection() as conn:
            items = tracked_hearings(conn)
        counts = relevance_counts(items)
        shown = [i for i in items if not relevance or i["relevance"] == relevance]
        if query:
            needle = query.casefold()
            shown = [
                i
                for i in shown
                if needle in (i["title"] or "").casefold()
                or needle in (i["department"] or "").casefold()
                or needle == str(i["id"])
            ]
        return render(
            request,
            "hearings.html",
            hearings=shown[:300],
            total=len(items),
            matched=len(shown),
            counts=counts,
            relevance=relevance,
            query=query,
        )

    @app.get("/hearings/{hearing_id}", response_class=HTMLResponse)
    def hearing_detail(request: Request, hearing_id: int):
        with connection() as conn:
            row = conn.execute("SELECT * FROM hearing WHERE id = ?", (hearing_id,)).fetchone()
            if row is None:
                return PlainTextResponse("Ukjent høring.", status_code=404)
            latest = db.latest_assessment(conn, hearing_id, "drift")
            findings = [dict(f) for f in db.findings_for(conn, latest["id"])] if latest else []
            documents = [dict(d) for d in db.documents_for(conn, hearing_id)]
            history = conn.execute(
                "SELECT * FROM assessment WHERE hearing_id = ? ORDER BY created_at DESC, id DESC",
                (hearing_id,),
            ).fetchall()
            return render(
                request,
                "hearing.html",
                hearing=dict(row),
                assessment=dict(latest) if latest else None,
                findings=findings,
                documents=documents,
                doc_urls={d["id"]: d["url"] for d in documents},
                history=[dict(h) for h in history],
                address_list=db.loads(row["address_list"], []) or [],
            )

    # -- reports -----------------------------------------------------------

    @app.get("/reports", response_class=HTMLResponse)
    def reports(request: Request):
        with connection() as conn:
            rows = conn.execute("SELECT * FROM report ORDER BY id DESC LIMIT 200").fetchall()
            return render(request, "reports.html", reports=[dict(r) for r in rows])

    @app.get("/reports/{report_id}/download")
    def download(report_id: int):
        with connection() as conn:
            row = conn.execute("SELECT path FROM report WHERE id = ?", (report_id,)).fetchone()
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
            return render(request, "stats.html", s=stats.statistics(conn, settings))

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
        return render(request, "job_log.html", job=dict(row), log=stats.tail(row["log_path"], 400))

    # -- backtest ----------------------------------------------------------

    def latest_estimate(conn: sqlite3.Connection) -> dict[str, Any] | None:
        for r in conn.execute(
            "SELECT * FROM job WHERE kind = 'backtest' ORDER BY id DESC LIMIT 20"
        ).fetchall():
            args = db.loads(r["args"], {}) or {}
            if args.get("estimate_only"):
                item = dict(r)
                item["args_data"] = args
                item["summary_data"] = db.loads(r["summary"], {}) or {}
                item["progress_data"] = db.loads(r["progress"], {}) or {}
                return item
        return None

    @app.get("/backtest", response_class=HTMLResponse)
    def backtest_page(request: Request):
        with connection() as conn:
            runs = []
            for r in conn.execute("SELECT * FROM backtest_run ORDER BY id DESC LIMIT 20"):
                item = dict(r)
                item["metrics_data"] = db.loads(r["metrics"], {}) or {}
                runs.append(item)
            return render(
                request,
                "backtest.html",
                estimate=latest_estimate(conn),
                runs=runs,
                running=running_job(conn) is not None,
            )

    def _backtest_args(data: dict[str, str]) -> list[str] | str:
        split = data.get("split", "")
        if split not in ("dev", "test"):
            return "Velg dev eller test."
        args = ["backtest", "--split", split]
        limit = data.get("limit", "").strip()
        if limit:
            if not limit.isdigit() or int(limit) < 1:
                return "Utvalg må være et positivt heltall."
            args += ["--limit", limit]
        return args

    @app.post("/actions/backtest/estimate")
    async def backtest_estimate(request: Request):
        data = await require_csrf(request)
        if isinstance(data, Response):
            return data
        args = _backtest_args(data)
        if isinstance(args, str):
            return back("/backtest", args)
        with connection() as conn:
            return start(conn, [*args, "--estimate-only"], "/backtest", "Estimat")

    @app.post("/actions/backtest/start")
    async def backtest_start(request: Request):
        data = await require_csrf(request)
        if isinstance(data, Response):
            return data
        if data.get("confirm") != "ja":
            return back("/backtest", "Backtesten ble ikke startet: bekreftelsen mangler.")
        args = _backtest_args(data)
        if isinstance(args, str):
            return back("/backtest", args)
        with connection() as conn:
            estimate = latest_estimate(conn)
            if (
                estimate is None
                or str(estimate["id"]) != data.get("estimate_job_id")
                or estimate["status"] != "ok"
            ):
                return back("/backtest", "Lag et nytt estimat før backtesten startes.")
            est_args = estimate["args_data"]
            if est_args.get("split") != data.get("split") or (est_args.get("limit") or "") != (
                data.get("limit") or ""
            ):
                return back("/backtest", "Valgene er endret siden estimatet. Lag et nytt estimat.")
            finished = db.parse_ts(estimate["finished_at"])
            if finished is None or datetime.now(UTC) - finished > ESTIMATE_MAX_AGE:
                return back("/backtest", "Estimatet er for gammelt. Lag et nytt estimat.")
            return start(conn, [*args, "--yes"], "/backtest", "Backtest")

    # -- actions -----------------------------------------------------------

    @app.post("/actions/run")
    async def action_run(request: Request):
        data = await require_csrf(request)
        if isinstance(data, Response):
            return data
        with connection() as conn:
            return start(conn, ["run"], "/", "Kjøring")

    @app.post("/actions/report")
    async def action_report(request: Request):
        data = await require_csrf(request)
        if isinstance(data, Response):
            return data
        with connection() as conn:
            return start(conn, ["report"], "/reports", "Rapportgenerering")

    @app.post("/actions/assess/{hearing_id}")
    async def action_assess(request: Request, hearing_id: int):
        data = await require_csrf(request)
        if isinstance(data, Response):
            return data
        with connection() as conn:
            if not conn.execute("SELECT 1 FROM hearing WHERE id = ?", (hearing_id,)).fetchone():
                return back("/hearings", "Ukjent høring.")
            return start(
                conn, ["assess", str(hearing_id)], f"/hearings/{hearing_id}", "Ny vurdering"
            )

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
