"""Run the admin panel with uvicorn on one socket per address in WEB_BIND."""

from __future__ import annotations

import contextlib
import socket
import sys

import uvicorn

from horingsradar.config import Settings
from horingsradar.web.app import create_app

IP_FREEBIND = 15  # Linux: allow binding the Tailscale address before tailscaled is up


def bind_socket(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if sys.platform.startswith("linux"):
        level = socket.IPPROTO_IPV6 if family == socket.AF_INET6 else socket.IPPROTO_IP
        with contextlib.suppress(OSError):
            sock.setsockopt(level, IP_FREEBIND, 1)
    sock.bind((host, port))
    sock.set_inheritable(True)
    return sock


def serve(settings: Settings) -> None:
    if not settings.web_password_hash:
        raise SystemExit(
            "WEB_PASSWORD_HASH er ikke satt. Lag en hash med `uv run horingsradar hash-password`"
            " og legg den i .env."
        )
    if not settings.web_binds:
        raise SystemExit("WEB_BIND er tom.")
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
        print(f"Adminpanel lytter på http://{host}:{port}", flush=True)
    uvicorn.Server(config).run(sockets=sockets)
