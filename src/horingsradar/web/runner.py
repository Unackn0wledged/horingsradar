"""Starting and cancelling jobs from the panel. Jobs always run as separate CLI processes."""

from __future__ import annotations

import contextlib
import os
import signal
import sqlite3
import subprocess
import sys
import time

from horingsradar import db
from horingsradar.config import REPO_ROOT, Settings
from horingsradar.jobs import running_job
from horingsradar.lock import JobLock, pid_alive

ALLOWED = {"run", "report", "assess", "backtest"}


class JobBusy(Exception):
    pass


def job_running(settings: Settings, conn: sqlite3.Connection) -> bool:
    return running_job(conn) is not None or JobLock(settings.lock_path).is_locked_elsewhere()


def spawn(settings: Settings, args: list[str]) -> int:
    """Start `horingsradar <args> --triggered-by web` in a new process; returns its pid."""
    if not args or args[0] not in ALLOWED:
        raise ValueError(f"not an allowed job: {args}")
    command = [sys.executable, "-m", "horingsradar", *args, "--triggered-by", "web"]
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    spawn_log = (settings.logs_dir / "web-spawn.log").open("ab")
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": spawn_log, "stderr": spawn_log}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, cwd=str(REPO_ROOT), env=os.environ.copy(), **kwargs)
    finally:
        spawn_log.close()
    return process.pid


def start_job(settings: Settings, conn: sqlite3.Connection, args: list[str]) -> int:
    if job_running(settings, conn):
        raise JobBusy("En jobb kjører allerede.")
    return spawn(settings, args)


def cancel_job(conn: sqlite3.Connection, job_id: int, wait_seconds: float = 5.0) -> str:
    row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
    if row is None or row["status"] != "running":
        return "Jobben kjører ikke."
    pid = row["pid"]
    if pid_alive(pid):
        sig = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
        with contextlib.suppress(OSError):
            os.kill(pid, sig)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            status = conn.execute("SELECT status FROM job WHERE id = ?", (job_id,)).fetchone()[0]
            if status != "running" or not pid_alive(pid):
                break
            time.sleep(0.2)
    status = conn.execute("SELECT status FROM job WHERE id = ?", (job_id,)).fetchone()[0]
    if status == "running" and not pid_alive(pid):
        conn.execute(
            "UPDATE job SET status = 'cancelled', finished_at = ? WHERE id = ?",
            (db.now_iso(), job_id),
        )
        status = "cancelled"
    if status == "running":
        return "Avbrudd er sendt. Jobben avslutter så snart den kan."
    return f"Jobben er {'avbrutt' if status == 'cancelled' else status}."
