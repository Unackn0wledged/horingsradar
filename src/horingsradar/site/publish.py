"""Upload DATA_DIR/site to Cloudflare Pages with wrangler (direct upload).

Production: `--branch CLOUDFLARE_PAGES_BRANCH` (the project's production branch, default main).
Preview: `--branch preview`, which gets its own address and never touches production.

The API token goes to wrangler through its environment only. It is never put on the command
line, in the log, in the job summary or in the database, and it is removed from any wrangler
output before that is logged or stored.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from typing import Any

from horingsradar import db
from horingsradar.config import Settings

log = logging.getLogger(__name__)

PREVIEW_BRANCH = "preview"
LAST_PUBLISH_KEY = "site_last_publish"
URL_RE = re.compile(r"https://[A-Za-z0-9.-]+\.pages\.dev[^\s'\"]*")
OUTPUT_TAIL = 2000

Runner = Callable[..., subprocess.CompletedProcess]


class PublishError(Exception):
    pass


def check_config(settings: Settings) -> None:
    missing = [
        name
        for name, value in (
            ("CLOUDFLARE_ACCOUNT_ID", settings.cloudflare_account_id),
            ("CLOUDFLARE_API_TOKEN", settings.cloudflare_api_token),
            ("CLOUDFLARE_PAGES_PROJECT", settings.cloudflare_pages_project),
        )
        if not value
    ]
    if missing:
        raise PublishError(f"Mangler i .env: {', '.join(missing)}")


def redact(text: str, settings: Settings) -> str:
    token = settings.cloudflare_api_token
    return text.replace(token, "[skjult]") if token else text


def wrangler_command(settings: Settings, preview: bool, message: str) -> list[str]:
    executable = shutil.which(settings.wrangler_bin) or settings.wrangler_bin
    return [
        executable,
        "pages",
        "deploy",
        str(settings.site_dir),
        f"--project-name={settings.cloudflare_pages_project}",
        f"--branch={PREVIEW_BRANCH if preview else settings.cloudflare_pages_branch}",
        f"--commit-message={message}",
        "--commit-dirty=true",
    ]


def wrangler_env(settings: Settings) -> dict[str, str]:
    """Environment for wrangler. Its home and logs live in DATA_DIR, the only writable place
    under the systemd sandbox (ProtectHome, ProtectSystem=strict)."""
    home = settings.data_dir / "wrangler"
    (home / "logs").mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLOUDFLARE_", "CF_"))}
    env.update(
        CLOUDFLARE_API_TOKEN=settings.cloudflare_api_token,
        CLOUDFLARE_ACCOUNT_ID=settings.cloudflare_account_id,
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / "config"),
        WRANGLER_LOG_PATH=str(home / "logs"),
        WRANGLER_SEND_METRICS="false",
        NO_COLOR="1",
        CI="true",
    )
    return env


def record(conn: sqlite3.Connection, result: dict[str, Any]) -> None:
    db.set_setting(conn, LAST_PUBLISH_KEY, db.dumps(result))


def last_publish(conn: sqlite3.Connection) -> dict[str, Any] | None:
    return db.loads(db.get_setting(conn, LAST_PUBLISH_KEY), None)


def build_and_publish(
    settings: Settings, conn: sqlite3.Connection, run: Runner = subprocess.run
) -> dict[str, Any]:
    """After a successful `run`: build and publish. A failure is logged and recorded for the
    panel but does not fail the run; the previous version of the site stays online."""
    from horingsradar.site.build import build_site

    try:
        built = build_site(settings, conn)
    except Exception as exc:
        error = f"Bygging av nettsiden feilet: {exc}"
        log.error("Website not built: %s", error)
        record(conn, {"at": db.now_iso(), "preview": False, "ok": False, "error": error})
        return {"ok": False, "error": error[:500]}
    try:
        published = publish_site(settings, conn, run=run)  # records the outcome itself
    except Exception as exc:
        error = redact(str(exc), settings)
        log.error("Website not published: %s", error)
        return {"ok": False, "error": error[:500]}
    return {"ok": True, "hearings": built["hearings"], "url": published.get("url")}


def publish_site(
    settings: Settings,
    conn: sqlite3.Connection,
    preview: bool = False,
    run: Runner = subprocess.run,
) -> dict[str, Any]:
    """Upload the built site. Records the outcome (also failures) for the panel."""
    result: dict[str, Any] = {"at": db.now_iso(), "preview": preview, "ok": False}
    try:
        check_config(settings)
        site = settings.site_dir
        if not (site / "index.html").is_file() or not (site / "data.json").is_file():
            raise PublishError("Nettsiden er ikke bygget. Kjør `horingsradar site` først.")
        if shutil.which(settings.wrangler_bin) is None:
            raise PublishError(
                f"Fant ikke `{settings.wrangler_bin}`. Installer wrangler (se INSTALL-NETTSIDE.md, del 7)."
            )
        label = "forhåndsvisning" if preview else "produksjon"
        command = wrangler_command(settings, preview, f"Høringsradar ({label})")
        log.info("Uploading %s to Cloudflare Pages (%s)", site, label)
        try:
            completed = run(
                command,
                cwd=str(settings.data_dir),
                env=wrangler_env(settings),
                capture_output=True,
                text=True,
                timeout=settings.wrangler_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PublishError(
                f"wrangler svarte ikke innen {settings.wrangler_timeout_seconds:.0f} sekunder"
            ) from exc
        output = redact(f"{completed.stdout or ''}\n{completed.stderr or ''}".strip(), settings)
        for line in output.splitlines():
            log.info("wrangler: %s", line)
        if completed.returncode != 0:
            raise PublishError(
                f"wrangler feilet (kode {completed.returncode}): {output[-OUTPUT_TAIL:]}"
            )
        urls = URL_RE.findall(output)
        result.update(ok=True, url=urls[-1] if urls else None)
        log.info("Site published (%s): %s", label, result["url"] or "no URL in output")
        return result
    except Exception as exc:
        result["error"] = redact(str(exc), settings)
        raise
    finally:
        record(conn, result)
