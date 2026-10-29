"""Run the EU panel with uvicorn on one socket per address in WEB_BIND (the EU instance's .env)."""

from __future__ import annotations

import uvicorn

from horingsradar.eu import db as eu_db
from horingsradar.eu.config import EUSettings
from horingsradar.eu.web.app import create_app
from horingsradar.web.server import bind_socket


def serve(settings: EUSettings) -> None:
    if not settings.web_password_hash:
        raise SystemExit(
            "WEB_PASSWORD_HASH er ikke satt. Lag en hash med `horingsradar-eu hash-password`"
            " og legg den i EU-instansens .env."
        )
    if not settings.web_binds:
        raise SystemExit("WEB_BIND er tom.")
    eu_db.connect(settings.db_path).close()  # fail at startup, not on the first request
    app = create_app(settings)
    config = uvicorn.Config(
        app,
        log_level=settings.log_level.lower(),
        server_header=False,
        proxy_headers=False,
        access_log=False,
    )
    sockets = [bind_socket(host, port) for host, port in settings.web_binds]
    for host, port in settings.web_binds:
        print(f"EU-panelet lytter på http://{host}:{port}", flush=True)
    uvicorn.Server(config).run(sockets=sockets)
