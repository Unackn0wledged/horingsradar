# Installere EU/EØS-radaren ved siden av Høringsradar

EU/EØS-radaren kjører som en egen instans i **samme LXC** som Høringsradar. Den har egen kode, egne data, egen konfigurasjon og egne tjenester, og kan ikke endre Høringsradars filer.

> Forutsetter at Høringsradar allerede er installert etter [`INSTALL-LXC.md`](INSTALL-LXC.md): tjenestebrukeren `horingsradar`, uv og eventuell deploy key finnes.
>
> I dag har EU/EØS-radaren bare et skall for `run` (milepæl 0, se [`EU-PLAN.md`](EU-PLAN.md)). Guiden kan brukes for å teste oppsettet.

| | Høringsradar | EU/EØS-radar |
|---|---|---|
| Kode | `/opt/horingsradar` | `/opt/horingsradar-eu` |
| Data | `/var/lib/horingsradar` | `/var/lib/horingsradar-eu` |
| Kommando | `horingsradar` | `horingsradar-eu` |
| Tjenester | `horingsradar-run.timer`, `horingsradar-web` | `horingsradar-eu-run.timer` |
| Oppdatering | `/opt/horingsradar/deploy/deploy.sh` | `/opt/horingsradar-eu/deploy/eu/deploy.sh` |

## 1. Snapshot først

Ta et snapshot av containeren før du starter, så alt kan rulles tilbake. Kjør på **Proxmox-verten**, og bytt ut `<CT-ID>`:

```bash
pct snapshot <CT-ID> for-eu-installasjon
```

Resten kjøres **inne i containeren** som root.

## 2. Mapper

```bash
install -d -o horingsradar -g horingsradar -m 755 /opt/horingsradar-eu
install -d -o horingsradar -g horingsradar -m 750 /var/lib/horingsradar-eu
```

## 3. Hent koden

Bruk samme adresse som Høringsradar ble klonet fra. Se den med:

```bash
sudo -u horingsradar -H git -C /opt/horingsradar remote get-url origin
```

Klon til EU-mappen, og bytt ut `<adresse>` med svaret over:

```bash
sudo -u horingsradar -H git clone <adresse> /opt/horingsradar-eu
```

Mens EU/EØS-radaren er under utvikling, skal EU-mappen bruke utviklingsbranchen:

```bash
sudo -u horingsradar -H git -C /opt/horingsradar-eu switch feature/eu-eos-kilder
```

Installer avhengighetene. Det lager en egen `.venv` i EU-mappen:

```bash
cd /opt/horingsradar-eu && sudo -u horingsradar -H uv sync --frozen
```

## 4. Konfigurasjon

```bash
cd /opt/horingsradar-eu
sudo -u horingsradar cp .env.eu.example .env
chmod 600 .env && chown horingsradar:horingsradar .env
sudo -u horingsradar nano .env
```

Endre minst disse:

| Variabel | Verdi |
|---|---|
| `RADAR` | `eu` (står allerede der, ikke fjern den) |
| `DATA_DIR` | `/var/lib/horingsradar-eu`. **Aldri** `/var/lib/horingsradar`. |
| `USER_AGENT` | `Horingsradar-EU/0.1 (kontakt: <din e-post>)` |
| `ANTHROPIC_API_KEY` | Gjerne en egen nøkkel, så forbruket holdes adskilt fra Høringsradar |
| `PRICE_INPUT_PER_MTOK` / `PRICE_OUTPUT_PER_MTOK` | Prisene for modellen i `LLM_MODEL`. Fjern `#` foran linjene. |

## 5. Test for hånd

```bash
cd /opt/horingsradar-eu
sudo -u horingsradar -H .venv/bin/horingsradar-eu run
sudo -u horingsradar -H .venv/bin/horingsradar-eu status
```

`status` skal vise `Data: /var/lib/horingsradar-eu` og én jobb med status `ok`. Sjekk at Høringsradar ikke er berørt:

```bash
cd /opt/horingsradar && sudo -u horingsradar -H .venv/bin/horingsradar status
```

## 6. Ukentlig kjøring

```bash
install -m 644 /opt/horingsradar-eu/deploy/eu/systemd/horingsradar-eu-* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now horingsradar-eu-run.timer
systemctl list-timers horingsradar-eu-run.timer
```

Timeren kjører mandag kl. 07:15, etter Høringsradars daglige kjøring.

## 7. Oppdatere

```bash
/opt/horingsradar-eu/deploy/eu/deploy.sh
```

Skriptet oppdaterer bare `/opt/horingsradar-eu` og EU-tjenestene. Det nekter å kjøre mot Høringsradars mapper. Siste linje viser versjonen.

## 8. Fjerne EU-instansen

Høringsradar påvirkes ikke:

```bash
systemctl disable --now horingsradar-eu-run.timer
rm /etc/systemd/system/horingsradar-eu-run.service /etc/systemd/system/horingsradar-eu-run.timer
systemctl daemon-reload
```

Mappene `/opt/horingsradar-eu` og `/var/lib/horingsradar-eu` kan deretter slettes for hånd.
