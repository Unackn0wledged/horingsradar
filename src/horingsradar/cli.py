"""Command line interface: `horingsradar <command>`."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from datetime import date

from horingsradar import db
from horingsradar.config import get_settings
from horingsradar.jobs import TRIGGERS, JobSkipped, job_context

log = logging.getLogger("horingsradar")

EXIT_SKIPPED = 3
EXIT_CANCELLED = 130


def _date(value: str) -> date:
    return date.fromisoformat(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="horingsradar", description="Høringsradar (pilot)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_trigger(p: argparse.ArgumentParser) -> None:
        p.add_argument("--triggered-by", choices=TRIGGERS, default="cli", help=argparse.SUPPRESS)

    p = sub.add_parser("run", help="hent nye og endrede høringer og vurder dem")
    p.add_argument(
        "--no-assess", action="store_true", help="hent og trekk ut tekst, men ikke vurder"
    )
    add_trigger(p)

    p = sub.add_parser("assess", help="vurder én høring på nytt")
    p.add_argument("hearing_id", type=int)
    p.add_argument("--prompt-version", help="for eksempel v2 (standard fra PROMPT_VERSION)")
    p.add_argument(
        "--refresh", action="store_true", help="hent høringssiden og dokumentene på nytt først"
    )
    add_trigger(p)

    p = sub.add_parser("report", help="lag PDF-rapport")
    p.add_argument("--week", help="regenerer en uke, for eksempel 2026-W42")
    add_trigger(p)

    p = sub.add_parser("backtest", help="manuell backtest med kostnadsestimat og bekreftelse")
    p.add_argument("--split", choices=("dev", "test"))
    p.add_argument("--from", dest="period_from", type=_date)
    p.add_argument("--to", dest="period_to", type=_date)
    p.add_argument("--limit", type=int, help="kjør på et utvalg på N høringer")
    p.add_argument("--yes", action="store_true", help="hopp over bekreftelsen")
    p.add_argument("--estimate-only", action="store_true", help="bare forbered og vis estimat")
    p.add_argument(
        "--no-batch", action="store_true", help="bruk vanlige kall i stedet for batch-API"
    )
    add_trigger(p)

    p = sub.add_parser("web", help="start adminpanelet")

    sub.add_parser("hash-password", help="lag passord-hash til WEB_PASSWORD_HASH")
    sub.add_parser("pause", help="pause planlagt kjøring")
    sub.add_parser("resume", help="gjenoppta planlagt kjøring")
    sub.add_parser("status", help="vis status og siste jobber")
    return parser


def _confirm(estimate) -> bool:
    from horingsradar.config import get_settings as _settings

    print(estimate.describe(_settings()))
    if not sys.stdin.isatty():
        print("Ingen terminal for bekreftelse. Bruk --yes for å starte.")
        return False
    answer = input("Start backtest og bruk API-et? Skriv 'ja' for å bekrefte: ").strip().lower()
    return answer in ("ja", "j", "yes", "y")


def _run_job(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.ensure_dirs()
    conn = db.connect(settings.db_path)
    job_args = {
        k: str(v)
        for k, v in vars(args).items()
        if k not in ("command", "triggered_by") and v not in (None, False)
    }
    try:
        with job_context(settings, conn, args.command, args.triggered_by, job_args) as job:
            if args.command == "run":
                from horingsradar.pipeline import run

                run(settings, conn, job, assess=not args.no_assess)
            elif args.command == "assess":
                from horingsradar.assess import assess_hearing
                from horingsradar.http import Fetcher
                from horingsradar.llm.client import get_client
                from horingsradar.sources.regjeringen.crawler import RegjeringenSource

                row = conn.execute(
                    "SELECT id FROM hearing WHERE id = ?", (args.hearing_id,)
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO hearing (id, url, title, tracked, first_seen) VALUES (?, ?, ?, 0, ?)",
                        (
                            args.hearing_id,
                            f"{settings.regjeringen_base_url}/no/dokumenter/id{args.hearing_id}/",
                            f"Høring {args.hearing_id}",
                            db.now_iso(),
                        ),
                    )
                has_docs = conn.execute(
                    "SELECT 1 FROM document WHERE hearing_id = ? LIMIT 1", (args.hearing_id,)
                ).fetchone()
                if args.refresh or not has_docs:
                    fetcher = Fetcher(settings)
                    try:
                        RegjeringenSource(settings, conn, fetcher).refresh_hearing(
                            args.hearing_id, max_age=0 if args.refresh else None
                        )
                    finally:
                        fetcher.close()
                assessment_id = assess_hearing(
                    settings, conn, args.hearing_id, get_client(settings), args.prompt_version
                )
                a = conn.execute(
                    "SELECT * FROM assessment WHERE id = ?", (assessment_id,)
                ).fetchone()
                job.summary.update(
                    hearing_id=args.hearing_id,
                    assessment_id=assessment_id,
                    status=a["status"],
                    relevance=a["relevance"],
                    error=a["error"],
                )
                print(
                    f"Vurdering {assessment_id}: {a['status']} {a['relevance'] or ''} {a['error'] or ''}"
                )
                if a["status"] != "ok":
                    return 1
            elif args.command == "report":
                from horingsradar.report.build import build_report

                result = build_report(settings, conn, week=args.week)
                job.summary.update(result)
                print(result["path"])
            elif args.command == "backtest":
                from horingsradar.backtest import run_backtest
                from horingsradar.llm.client import get_client

                if not args.split and not (args.period_from and args.period_to):
                    raise SystemExit("Bruk --split dev|test eller både --from og --to")
                if args.triggered_by != "cli" and not (args.yes or args.estimate_only):
                    raise SystemExit("Backtest fra panelet krever eksplisitt bekreftelse (--yes)")
                summary = run_backtest(
                    settings,
                    conn,
                    job,
                    get_client(settings),
                    split=args.split,
                    period_from=args.period_from,
                    period_to=args.period_to,
                    limit=args.limit,
                    use_batch=not args.no_batch,
                    confirm=(lambda est: True) if args.yes else _confirm,
                    estimate_only=args.estimate_only,
                )
                if args.estimate_only:
                    print(f"Estimat: {summary.get('estimate')}")
                elif summary.get("result"):
                    print(f"Backtest ikke startet: {summary['result']}")
                if summary.get("results_path"):
                    print(f"Resultater: {summary['results_path']}")
    except JobSkipped as exc:
        message = {
            "paused": "Planlagt kjøring er pauset.",
            "lock_taken": "En annen jobb kjører allerede.",
        }.get(exc.reason, exc.reason)
        print(message, file=sys.stderr)
        return 0 if args.triggered_by == "timer" else EXIT_SKIPPED
    except KeyboardInterrupt:
        return EXIT_CANCELLED
    except SystemExit:
        raise
    except Exception:
        return 1
    finally:
        conn.close()
    return 0


def _web() -> int:
    from horingsradar.web.server import serve

    serve(get_settings())
    return 0


def _hash_password() -> int:
    from horingsradar.web.auth import hash_password

    first = getpass.getpass("Passord: ")
    second = getpass.getpass("Gjenta passord: ")
    if not first or first != second:
        print("Passordene er tomme eller ulike.", file=sys.stderr)
        return 1
    print(hash_password(first))
    return 0


def _set_paused(paused: bool) -> int:
    settings = get_settings()
    settings.ensure_dirs()
    conn = db.connect(settings.db_path)
    db.set_setting(conn, "scheduled_runs_paused", "1" if paused else "0")
    print("Planlagt kjøring er " + ("pauset." if paused else "aktiv."))
    return 0


def _status() -> int:
    settings = get_settings()
    settings.ensure_dirs()
    conn = db.connect(settings.db_path)
    print(f"Planlagt kjøring: {'pauset' if db.runs_paused(conn) else 'aktiv'}")
    print(
        f"Høringer i drift: {conn.execute('SELECT COUNT(*) FROM hearing WHERE tracked = 1').fetchone()[0]}"
    )
    for row in conn.execute("SELECT * FROM job ORDER BY id DESC LIMIT 10"):
        print(
            f"#{row['id']} {row['kind']:<8} {row['triggered_by']:<5} {row['status']:<9}"
            f" {row['started_at']} {row['summary'] or ''}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in ("run", "assess", "report", "backtest"):
        return _run_job(args)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if args.command == "web":
        return _web()
    if args.command == "hash-password":
        return _hash_password()
    if args.command == "pause":
        return _set_paused(True)
    if args.command == "resume":
        return _set_paused(False)
    if args.command == "status":
        return _status()
    return 2


if __name__ == "__main__":
    sys.exit(main())
