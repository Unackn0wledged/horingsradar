"""Starting EU jobs from the EU panel. Jobs always run as separate `horingsradar.eu` processes.

The regjeringen.no radar's runner (horingsradar.web.runner) starts `horingsradar`; this one
starts the EU command, so a job started here never touches the other instance. Checking for
a running job and cancelling one are shared and used unchanged.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

from horingsradar.config import REPO_ROOT
from horingsradar.eu.config import EUSettings
from horingsradar.web.runner import JobBusy, cancel_job, job_running

__all__ = ["ALLOWED", "JobBusy", "cancel_job", "job_running", "spawn", "start_job"]

# No backtest for the EU radar (out of scope, docs/EU-PLAN.md).
ALLOWED = {"run", "report", "assess"}


def spawn(settings: EUSettings, args: list[str]) -> int:
    """Start `python -m horingsradar.eu <args> --triggered-by web`; returns its pid."""
    if not args or args[0] not in ALLOWED:
        raise ValueError(f"not an allowed job: {args}")
    command = [sys.executable, "-m", "horingsradar.eu", *args, "--triggered-by", "web"]
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


def start_job(settings: EUSettings, conn: sqlite3.Connection, args: list[str]) -> int:
    if job_running(settings, conn):
        raise JobBusy("En jobb kjører allerede.")
    return spawn(settings, args)
