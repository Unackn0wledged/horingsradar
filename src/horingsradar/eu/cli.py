"""Command line interface for the EU/EEA radar: `horingsradar-eu <command>`."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys

from horingsradar import db
from horingsradar.cli import EXIT_CANCELLED, EXIT_SKIPPED
from horingsradar.eu.config import EUSettings, get_eu_settings, require_eu_instance
from horingsradar.jobs import TRIGGERS, JobSkipped, job_context

log = logging.getLogger("horingsradar")

# Built in later milestones (docs/EU-PLAN.md).
NOT_BUILT = {
    "report": "Rapporten kommer i milepæl 4.",
    "backtest": "Backtesten kommer i milepæl 9.",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="horingsradar-eu", description="EU/EØS-radar (pilot)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="hent og vurder nye EU-forslag og høringer")
    p.add_argument("--triggered-by", choices=TRIGGERS, default="cli", help=argparse.SUPPRESS)

    sub.add_parser("report", help="lag PDF-rapport (ikke bygget ennå)")
    sub.add_parser("backtest", help="manuell backtest (ikke bygget ennå)")
    sub.add_parser("pause", help="pause planlagt kjøring")
    sub.add_parser("resume", help="gjenoppta planlagt kjøring")
    sub.add_parser("status", help="vis status og siste jobber")
    return parser


def _connect(settings: EUSettings) -> sqlite3.Connection:
    settings.ensure_dirs()
    return db.connect(settings.db_path)


def _run(settings: EUSettings, triggered_by: str) -> int:
    conn = _connect(settings)
    try:
        with job_context(settings, conn, "run", triggered_by) as job:
            log.info("EU/EØS-radaren har ingen kilder ennå (milepæl 0).")
            job.summary.update(sources=0)
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
    if args.command == "run":
        return _run(settings, args.triggered_by)
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
