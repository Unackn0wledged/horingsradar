"""Command line interface for the EU/EEA radar: `horingsradar-eu <command>`."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys

from horingsradar import db
from horingsradar.cli import EXIT_CANCELLED, EXIT_SKIPPED
from horingsradar.eu import db as eu_db
from horingsradar.eu.config import EUSettings, get_eu_settings, require_eu_instance
from horingsradar.jobs import TRIGGERS, JobSkipped, job_context

log = logging.getLogger("horingsradar")

# Built in later milestones (docs/EU-PLAN.md).
NOT_BUILT = {
    "backtest": "Backtesten kommer i milepæl 9.",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="horingsradar-eu", description="EU/EØS-radar (pilot)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="hent og vurder nye EU-forslag og høringer")
    p.add_argument(
        "--no-assess", action="store_true", help="hent og trekk ut tekst, men ikke vurder"
    )
    p.add_argument("--triggered-by", choices=TRIGGERS, default="cli", help=argparse.SUPPRESS)

    p = sub.add_parser("assess", help="vurder én sak på nytt")
    p.add_argument("case", help="saks-ID, COM-nummer («COM(2026) 681») eller CELEX (52026PC0681)")
    p.add_argument("--prompt-version", help="for eksempel v2 (standard fra EU_PROMPT_VERSION)")
    p.add_argument("--triggered-by", choices=TRIGGERS, default="cli", help=argparse.SUPPRESS)

    p = sub.add_parser("report", help="lag PDF-rapport for saker som ikke er rapportert før")
    p.add_argument("--triggered-by", choices=TRIGGERS, default="cli", help=argparse.SUPPRESS)
    sub.add_parser("backtest", help="manuell backtest (ikke bygget ennå)")
    sub.add_parser("pause", help="pause planlagt kjøring")
    sub.add_parser("resume", help="gjenoppta planlagt kjøring")
    sub.add_parser("status", help="vis status og siste jobber")
    return parser


def _connect(settings: EUSettings) -> sqlite3.Connection:
    settings.ensure_dirs()
    return eu_db.connect(settings.db_path)


def find_case(conn: sqlite3.Connection, ref: str) -> sqlite3.Row | None:
    """A case by id, COM reference or CELEX number (variants find their proposal)."""
    from horingsradar.eu.sources.eurlex import base_celex

    ref = ref.strip()
    if ref.isdigit():
        return conn.execute("SELECT * FROM eu_case WHERE id = ?", (int(ref),)).fetchone()
    by_com = conn.execute(
        "SELECT * FROM eu_case WHERE REPLACE(com_ref, ' ', '') = ?", (ref.replace(" ", ""),)
    ).fetchone()
    if by_com is not None:
        return by_com
    return conn.execute(
        "SELECT * FROM eu_case WHERE celex = ?", (base_celex(ref.upper()),)
    ).fetchone()


def _assess(
    settings: EUSettings, conn: sqlite3.Connection, job, ref: str, version: str | None
) -> int:
    from horingsradar.eu.assess import assess_case
    from horingsradar.eu.documents import fetch_document
    from horingsradar.eu.sources.eurlex import EurLexClient
    from horingsradar.http import Fetcher
    from horingsradar.llm.client import get_client

    case = find_case(conn, ref)
    if case is None:
        raise SystemExit(f"Fant ingen sak «{ref}». Kjør run først, eller bruk saks-ID/CELEX.")
    doc = conn.execute("SELECT * FROM eu_document WHERE case_id = ?", (case["id"],)).fetchone()
    if doc is None or doc["fetch_status"] != "ok":
        fetcher = Fetcher(settings)
        try:
            url = EurLexClient(fetcher).english_pdfs([case["work_uri"]]).get(case["work_uri"])
            fetch_document(settings, conn, case, url, fetcher)
        finally:
            fetcher.close()
    assessment_id = assess_case(settings, conn, case["id"], get_client(settings), version)
    a = conn.execute("SELECT * FROM eu_assessment WHERE id = ?", (assessment_id,)).fetchone()
    job.summary.update(
        case=case["com_ref"],
        assessment_id=assessment_id,
        status=a["status"],
        relevance=a["relevance"],
        error=a["error"],
    )
    print(f"Vurdering {assessment_id}: {a['status']} {a['relevance'] or ''} {a['error'] or ''}")
    return 0 if a["status"] == "ok" else 1


def _run_job(settings: EUSettings, args: argparse.Namespace) -> int:
    triggered_by = args.triggered_by
    conn = _connect(settings)
    job_args = {
        k: str(v)
        for k, v in vars(args).items()
        if k not in ("command", "triggered_by") and v not in (None, False)
    }
    try:
        with job_context(settings, conn, args.command, triggered_by, job_args) as job:
            if args.command == "assess":
                return _assess(settings, conn, job, args.case, args.prompt_version)
            if args.command == "report":
                from horingsradar.eu.report import build_report

                result = build_report(settings, conn)
                job.summary.update(result)
                print(result["path"])
                return 0
            from horingsradar.eu.pipeline import run

            run(settings, conn, job, assess=not args.no_assess)
    except JobSkipped as exc:
        message = {
            "paused": "Planlagt kjøring er pauset.",
            "lock_taken": "En annen jobb kjører allerede.",
        }.get(exc.reason, exc.reason)
        print(message, file=sys.stderr)
        return 0 if triggered_by == "timer" else EXIT_SKIPPED
    except KeyboardInterrupt:
        return EXIT_CANCELLED
    except Exception:
        return 1
    finally:
        conn.close()
    return 0


def _set_paused(settings: EUSettings, paused: bool) -> int:
    conn = _connect(settings)
    try:
        db.set_setting(conn, "scheduled_runs_paused", "1" if paused else "0")
    finally:
        conn.close()
    print("Planlagt kjøring er " + ("pauset." if paused else "aktiv."))
    return 0


def _status(settings: EUSettings) -> int:
    conn = _connect(settings)
    try:
        print(f"Data: {settings.data_dir}")
        print(f"Planlagt kjøring: {'pauset' if db.runs_paused(conn) else 'aktiv'}")
        from horingsradar.eu.pipeline import counts

        c = counts(conn)
        print(
            f"Saker: {c['cases']}, kandidater: {c['candidates']},"
            f" filtrert bort: {c['filtered_out']}, uten EuroVoc ennå: {c['not_indexed']}"
        )
        print(
            f"EEA-Lex (kandidater): registrert {c['eea_registered']},"
            f" ikke registrert {c['eea_not_registered']}"
        )
        print(
            f"Vurdert: {c['cases_assessed']}, venter: {c['waiting']},"
            f" høy {c['hoy']}, middels {c['middels']}, lav {c['lav']}, ingen {c['ingen']}"
        )
        print("\nSiste jobber:")
        for row in conn.execute("SELECT * FROM job ORDER BY id DESC LIMIT 10"):
            print(
                f"  #{row['id']} {row['kind']:<8} {row['triggered_by']:<5} {row['status']:<9}"
                f" {db.format_ts(row['started_at'])} {row['summary'] or ''}"
            )
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_eu_settings()
    require_eu_instance(settings)
    if args.command in ("run", "assess", "report"):
        return _run_job(settings, args)
    if args.command in NOT_BUILT:
        print(NOT_BUILT[args.command], file=sys.stderr)
        return 2
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if args.command == "pause":
        return _set_paused(settings, True)
    if args.command == "resume":
        return _set_paused(settings, False)
    if args.command == "status":
        return _status(settings)
    return 2


if __name__ == "__main__":
    sys.exit(main())
