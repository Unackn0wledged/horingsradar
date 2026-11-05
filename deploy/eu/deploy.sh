#!/usr/bin/env bash
# Oppdaterer EU/EØS-radaren på LXC-en. Kjøres som root:  /opt/horingsradar-eu/deploy/eu/deploy.sh
#   git pull --ff-only && uv sync --frozen, oppdaterer EU-instansens systemd-filer og starter
#   EU-panelet på nytt hvis det finnes. Rører ikke Høringsradar (/opt/horingsradar).
# Avbryter hvis en EU-jobb kjører (fil-låsen er tatt), med mindre --force er gitt.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/horingsradar-eu}"
DATA_DIR="${DATA_DIR:-/var/lib/horingsradar-eu}"
SERVICE_USER="${SERVICE_USER:-horingsradar}"
FORCE="${1:-}"

if [[ $EUID -ne 0 ]]; then
  echo "Kjør som root (sudo $0)." >&2
  exit 1
fi

if [[ "$APP_DIR" == "/opt/horingsradar" || "$DATA_DIR" == "/var/lib/horingsradar" ]]; then
  echo "Dette skriptet er for EU-instansen. Høringsradar oppdateres med /opt/horingsradar/deploy/deploy.sh." >&2
  exit 1
fi

if [[ "$FORCE" != "--force" && -e "$DATA_DIR/horingsradar.lock" ]]; then
  if ! flock -n "$DATA_DIR/horingsradar.lock" true; then
    echo "En EU-jobb kjører (låsen er tatt). Vent til den er ferdig, eller bruk --force." >&2
    exit 2
  fi
fi

as_app() { sudo -u "$SERVICE_USER" -H "$@"; }

cd "$APP_DIR"
echo "==> git pull --ff-only"
as_app git -C "$APP_DIR" pull --ff-only

echo "==> uv sync --frozen"
as_app bash -c "cd '$APP_DIR' && uv sync --frozen"

echo "==> systemd-filer (bare EU-instansens)"
changed=0
for unit in deploy/eu/systemd/*.service deploy/eu/systemd/*.timer; do
  target="/etc/systemd/system/$(basename "$unit")"
  if ! cmp -s "$unit" "$target"; then
    install -m 0644 "$unit" "$target"
    changed=1
    echo "    oppdatert $(basename "$unit")"
  fi
done
if [[ $changed -eq 1 ]]; then
  systemctl daemon-reload
fi

if [[ -e /etc/systemd/system/horingsradar-eu-web.service ]]; then
  echo "==> starter EU-panelet på nytt"
  systemctl restart horingsradar-eu-web.service
fi
systemctl --no-pager --lines=0 status horingsradar-eu-run.timer || true
echo "Ferdig. Versjon: $(as_app git -C "$APP_DIR" log -1 --format='%h %s')"
