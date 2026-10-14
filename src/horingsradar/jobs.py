"""Job registry: every CLI job takes the shared lock and is recorded in the `job` table.

A job logs to DATA_DIR/logs/job-<id>.log and to stderr (journald under systemd). SIGINT and
SIGTERM mark the job as cancelled.
"""

from __future__ import annotations

import logging
import signal
import sqlite3
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from horingsradar import db
from horingsradar.config import Settings
from horingsradar.lock import JobLock, pid_alive

log = logging.getLogger("horingsradar")

KINDS = ("run", "report", "backtest", "assess")
TRIGGERS = ("timer", "cli", "web")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class JobSkipped(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class JobCancelled(BaseException):
    """Raised from the SIGTERM handler; SIGINT raises KeyboardInterrupt."""


@dataclass
class JobHandle:
    conn: sqlite3.Connection
    id: int
    kind: str
    triggered_by: str
    summary: dict[str, Any] = field(default_factory=dict)

    def progress(self, **values: Any) -> None:
        self.conn.execute("UPDATE job SET progress = ? WHERE id = ?", (db.dumps(values), self.id))

    def save_summary(self) -> None:
        self.conn.execute(
            "UPDATE job SET summary = ? WHERE id = ?", (db.dumps(self.summary), self.id)
        )


def _insert_job(
    conn: sqlite3.Connection,
    kind: str,
    triggered_by: str,
    args: dict[str, Any],
    status: str,
    pid: int | None,
    summary: dict[str, Any] | None = None,
) -> int:
    now = db.now_iso()
    cur = conn.execute(
        "INSERT INTO job (kind, triggered_by, args, started_at, finished_at, status, pid, summary)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            kind,
            triggered_by,
            db.dumps(args),
            now,
            None if status == "running" else now,
            status,
            pid,
            db.dumps(summary) if summary else None,
        ),
    )
    return int(cur.lastrowid)


def mark_stale_jobs(conn: sqlite3.Connection, current_id: int | None = None) -> int:
    """Mark 'running' jobs whose process is gone as failed."""
    stale = 0
    for row in conn.execute("SELECT id, pid FROM job WHERE status = 'running'").fetchall():
        if row["id"] == current_id or pid_alive(row["pid"]):
            continue
        conn.execute(
            "UPDATE job SET status = 'failed', finished_at = ?, summary = ? WHERE id = ?",
            (db.now_iso(), db.dumps({"error": "process disappeared (stale job)"}), row["id"]),
        )
        stale += 1
    return stale


def _setup_logging(settings: Settings, log_path) -> list[logging.Handler]:
    formatter = logging.Formatter(LOG_FORMAT)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [file_handler]
    root = logging.getLogger("horingsradar")
    if not any(getattr(h, "_horingsradar_console", False) for h in root.handlers):
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        console._horingsradar_console = True  # type: ignore[attr-defined]
        handlers.append(console)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
    return handlers


@contextmanager
def job_context(
    settings: Settings,
    conn: sqlite3.Connection,
    kind: str,
    triggered_by: str = "cli",
    args: dict[str, Any] | None = None,
    pid: int | None = None,
) -> Iterator[JobHandle]:
    """Run a job under the shared lock. Raises JobSkipped if it must not run now."""
    import os

    args = args or {}
    pid = pid or os.getpid()
    if kind not in KINDS or triggered_by not in TRIGGERS:
        raise ValueError(f"invalid job kind/trigger: {kind}/{triggered_by}")

    if kind == "run" and triggered_by == "timer" and db.runs_paused(conn):
        _insert_job(conn, kind, triggered_by, args, "skipped", pid, {"reason": "paused"})
        raise JobSkipped("paused")

    lock = JobLock(settings.lock_path)
    if not lock.acquire():
        _insert_job(conn, kind, triggered_by, args, "skipped", pid, {"reason": "lock_taken"})
        raise JobSkipped("lock_taken")

    handlers: list[logging.Handler] = []
    previous_sigterm = None
    try:
        job_id = _insert_job(conn, kind, triggered_by, args, "running", pid)
        log_path = settings.logs_dir / f"job-{job_id}.log"
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        conn.execute("UPDATE job SET log_path = ? WHERE id = ?", (str(log_path), job_id))
        mark_stale_jobs(conn, current_id=job_id)
        handlers = _setup_logging(settings, log_path)

        def _on_sigterm(signum, frame):  # pragma: no cover - signal path
            raise JobCancelled()

        if hasattr(signal, "SIGTERM"):
            try:
                previous_sigterm = signal.signal(signal.SIGTERM, _on_sigterm)
            except ValueError:  # not main thread
                previous_sigterm = None

        handle = JobHandle(conn, job_id, kind, triggered_by)
        log.info("Job %s started: %s (triggered by %s) %s", job_id, kind, triggered_by, args)
        status = "ok"
        try:
            yield handle
        except (KeyboardInterrupt, JobCancelled):
            status = "cancelled"
            handle.summary.setdefault("error", "cancelled")
            log.warning("Job %s cancelled", job_id)
            raise
        except Exception as exc:
            status = "failed"
            handle.summary["error"] = f"{type(exc).__name__}: {exc}"
            log.error("Job %s failed:\n%s", job_id, traceback.format_exc())
            raise
        finally:
            conn.execute(
                "UPDATE job SET status = ?, finished_at = ?, summary = ? WHERE id = ?",
                (status, db.now_iso(), db.dumps(handle.summary), job_id),
            )
            log.info("Job %s finished: %s %s", job_id, status, handle.summary)
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        root = logging.getLogger("horingsradar")
        for handler in handlers:
            root.removeHandler(handler)
            handler.close()
        lock.release()


def running_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The job that is actually running (its process is alive). Read-only."""
    for row in conn.execute("SELECT * FROM job WHERE status = 'running' ORDER BY id DESC"):
        if pid_alive(row["pid"]):
            return row
    return None
