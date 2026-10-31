# Installere EU/EØS-radaren ved siden av Høringsradar

EU/EØS-radaren kjører som en egen instans i **samme LXC** som Høringsradar. Den har egen kode, egne data, egen konfigurasjon og egne tjenester, og kan ikke endre Høringsradars filer.

> Forutsetter at Høringsradar allerede er installert etter [`INSTALL-LXC.md`](INSTALL-LXC.md): tjenestebrukeren `horingsradar`, uv og eventuell deploy key finnes.
>
> Status for hver del står i [`EU-PLAN.md`](EU-PLAN.md).

| | Høringsradar | EU/EØS-radar |
|---|---|---|
| Kode | `/opt/horingsradar` | `/opt/horingsradar-eu` |
| Data | `/var/lib/horingsradar` | `/var/lib/horingsradar-eu` |
| Kommando | `horingsradar` | `horingsradar-eu` |
| Tjenester | `horingsradar-run.timer`, `horingsradar-web` | `horingsradar-eu-run.timer`, `horingsradar-eu-web` |
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
| `PRICE_INPUT_PER_MTOK` / `PRICE_OUTPUT_PER_MTOK` | Prisene for modellen i `LLM_MODEL`. Eksempelfilen har prisene for Sonnet 5.5. Sjekk dem mot Anthropics prisliste. |

## 5. Første kjøring, gradvis

Kjør først **uten LLM**. Da hentes forslag, EDPS-uttalelser, EØS-status, frister fra Have Your Say, status i Europaparlamentet og tekst, men ingenting sendes til modellen, så det koster ingenting. Første kjøring går 6 måneder tilbake (`EU_INITIAL_LOOKBACK_DAYS=183`). Med 5 sekunder mellom kall tar den anslagsvis 10–15 minutter (uten vurdering slås alle kandidater opp i OEIL; senere bare høy og middels):

```bash
cd /opt/horingsradar-eu
sudo -u horingsradar -H .venv/bin/horingsradar-eu run --no-assess
sudo -u horingsradar -H .venv/bin/horingsradar-eu status
```

`status` skal vise `Data: /var/lib/horingsradar-eu`, antall saker og kandidater, hvor mange kandidater som har en side på Have Your Say, og at kandidatene venter på vurdering.

Vurder deretter noen få saker for hånd, for å sjekke nøkkel, modell, pris og prompt. Bytt ut `<COM-nummer>` med et nummer fra en kandidat, for eksempel `"COM(2026) 681"` (med anførselstegn):

```bash
sudo -u horingsradar -H .venv/bin/horingsradar-eu assess <COM-nummer>
```

Når det ser bra ut, vurderes resten, og første rapport lages:

```bash
sudo -u horingsradar -H .venv/bin/horingsradar-eu run
sudo -u horingsradar -H .venv/bin/horingsradar-eu report
```

Siste linje fra `report` er stien til PDF-en i `/var/lib/horingsradar-eu/reports/`.

Sjekk at Høringsradar ikke er berørt:

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

## 7. Adminpanel

EU-radaren har sitt eget panel på port 8081. Lag et passord og legg hashen i EU-instansens `.env`:

```bash
cd /opt/horingsradar-eu
sudo -u horingsradar -H .venv/bin/horingsradar-eu hash-password
```

Skriv inn passordet to ganger. Kopier linjen som starter med `scrypt$` og lim den inn etter `WEB_PASSWORD_HASH=` i `.env`:

```bash
sudo -u horingsradar nano /opt/horingsradar-eu/.env
```

Sett også `WEB_BIND` til LXC-ens LAN-adresse med port 8081 (for eksempel `<LXC-ens IP>:8081`), og gjerne `OTHER_PANEL_URL` til adressen til Høringsradars panel (for eksempel `http://<LXC-ens IP>:8080`). Bytt ut `<LXC-ens IP>` med adressen containeren har på nettet ditt.

Har du satt opp brannmuren fra [`INSTALL-LXC.md`](INSTALL-LXC.md) (nftables), må port 8081 åpnes også. Sjekk først:

```bash
nft list ruleset | grep 'dport 8080'
```

Gir det ingen linje, er nftables-regelen ikke i bruk, og du kan hoppe over neste kommando. Gir det en linje, legger disse kommandoene til 8081, sjekker filen og laster brannmuren på nytt:

```bash
sed -i 's/dport 8080 accept/dport { 8080, 8081 } accept/' /etc/nftables.conf
nft -c -f /etc/nftables.conf && systemctl restart nftables
nft list ruleset | grep 'dport'
```

Siste kommando skal vise en linje med `tcp dport { 8080, 8081 } accept`. Bruker du Proxmox-brannmuren i stedet, åpner du 8081 fra LAN der.

Start panelet:

```bash
systemctl enable --now horingsradar-eu-web.service
systemctl status horingsradar-eu-web.service --no-pager
```

Åpne `http://<LXC-ens IP>:8081` i nettleseren og logg inn med brukernavnet i `WEB_USERNAME` (standard `operator`) og passordet. Panelet kan starte kjøring (med eller uten vurdering), rapport og ny vurdering av én sak, pause og gjenoppta den ukentlige kjøringen og avbryte en jobb. Det styrer ikke systemd og endrer ikke `.env`.

## 8. Oppdatere

```bash
/opt/horingsradar-eu/deploy/eu/deploy.sh
```

Skriptet oppdaterer bare `/opt/horingsradar-eu` og EU-tjenestene. Det nekter å kjøre mot Høringsradars mapper. Siste linje viser versjonen.

## 9. Fjerne EU-instansen

Høringsradar påvirkes ikke:

```bash
systemctl disable --now horingsradar-eu-run.timer horingsradar-eu-web.service
rm /etc/systemd/system/horingsradar-eu-run.service /etc/systemd/system/horingsradar-eu-run.timer /etc/systemd/system/horingsradar-eu-web.service
systemctl daemon-reload
```

Mappene `/opt/horingsradar-eu` og `/var/lib/horingsradar-eu` kan deretter slettes for hånd.
