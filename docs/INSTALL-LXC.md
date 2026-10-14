# Installere Høringsradar i en Proxmox-LXC med Tailscale

Guiden tar deg fra en tom Proxmox-node til en Høringsradar som:

- henter og vurderer høringer hver morgen,
- har adminpanelet tilgjengelig på LAN og via Tailscale,
- kan vises som proof of concept fra en annen lokasjon.

Den er skrevet for Debian 13 (trixie) i en **uprivilegert** container, med Tailscale installert **inne i** containeren. Regn med 45–60 minutter første gang.

> Kommandoer med `#` foran kjøres som root i containeren. Kommandoer med `pve#` kjøres som root på Proxmox-verten. Bytt ut verdier i `<vinkelparenteser>`.

## Innhold

1. [Oversikt](#1-oversikt)
2. [Forberedelser](#2-forberedelser)
3. [Opprett containeren](#3-opprett-containeren)
4. [Gi containeren tilgang til TUN (for Tailscale)](#4-gi-containeren-tilgang-til-tun-for-tailscale)
5. [Grunnoppsett i containeren](#5-grunnoppsett-i-containeren)
6. [Tjenestebruker, mapper og uv](#6-tjenestebruker-mapper-og-uv)
7. [Hent koden](#7-hent-koden)
8. [Konfigurasjon (.env)](#8-konfigurasjon-env)
9. [Første test for hånd](#9-første-test-for-hånd)
10. [systemd: daglig kjøring og adminpanel](#10-systemd-daglig-kjøring-og-adminpanel)
11. [Tailscale](#11-tailscale)
12. [Brannmur (valgfritt, anbefalt)](#12-brannmur-valgfritt-anbefalt)
13. [Daglig drift](#13-daglig-drift)
14. [Oppdatere til ny versjon](#14-oppdatere-til-ny-versjon)
15. [Backup og gjenoppretting](#15-backup-og-gjenoppretting)
16. [Sjekkliste før demo på annen lokasjon](#16-sjekkliste-før-demo-på-annen-lokasjon)
17. [Feilsøking](#17-feilsøking)

---

## 1. Oversikt

```
                 Proxmox-node
 ┌───────────────────────────────────────────────────────────┐
 │  LXC «horingsradar» (Debian 13, uprivilegert)             │
 │                                                           │
 │  systemd-timer 06:15 ──► horingsradar run  ──┐            │
 │                                              │ fil-lås    │
 │  horingsradar-web (adminpanel, :8080) ──► CLI-jobber      │
 │        ▲                ▲                                 │
 │   LAN-IP:8080     Tailscale-IP:8080                       │
 │                                                           │
 │  /opt/horingsradar        kode, .venv, .env (chmod 600)   │
 │  /var/lib/horingsradar    SQLite, cache, PDF-er, logger   │
 └───────────────────────────────────────────────────────────┘
        │ kun utgående: regjeringen.no, api.anthropic.com,
        │ pakkeregistre, GitHub, Tailscale
```

- **Timeren** kjører bare `run`. `report` og `backtest` startes manuelt fra panelet eller terminalen.
- **Panelet** starter jobber som egne prosesser og styrer aldri systemd.
- Det er **ingen portvideresending** fra internett. Tilgang utenfra går via Tailscale.

## 2. Forberedelser

Du trenger:

- Proxmox VE 8 eller 9 med tilgang til internett.
- En **Anthropic API-nøkkel**. Lag en egen nøkkel for piloten og sett gjerne en månedlig utgiftsgrense i Anthropic-konsollen.
- En **Tailscale-konto** (tailnet) og Tailscale på PC-en du skal demonstrere fra.
- Et **git-repo** for koden. Anbefalt er et privat GitHub-repo med en skrivebeskyttet *deploy key*. Det trengs bare utgående trafikk. Et alternativ står i [kapittel 7](#7-hent-koden).

Push koden fra utviklingsmaskinen først, for eksempel:

```bash
git remote add origin git@github.com:<bruker>/horingsradar.git
git push -u origin main
```

## 3. Opprett containeren

### 3.1 Last ned malen

I Proxmox-GUI: velg *local*-lagringen, gå til **CT Templates → Templates** og last ned `debian-13-standard`.

Eller på verten:

```bash
pve# pveam update
pve# pveam available --section system | grep debian-13
pve# pveam download local debian-13-standard_13.<x>-1_amd64.tar.zst
```

### 3.2 Opprett CT

Velg **Create CT** og bruk disse verdiene:

| Felt | Verdi |
|---|---|
| Hostname | `horingsradar` |
| Unprivileged container | ✔ (standard) |
| Nesting | ✔ (standard i nyere PVE, gjør at systemd fungerer best) |
| Template | `debian-13-standard` |
| Disk | 20 GB. PDF-cache og backtest kan bruke noen GB. |
| CPU | 2 kjerner |
| Minne | 2048 MB, swap 1024 MB. Store PDF-er og WeasyPrint trenger litt. |
| Nettverk | `vmbr0`. Bruk statisk IPv4 (for eksempel `192.168.1.50/24`) eller DHCP-reservasjon, så LAN-adressen ligger fast. |
| DNS | Bruk vertens innstillinger |

Når containeren er opprettet, gå til **Options** og sett **Start at boot = Yes**. Ikke start den ennå. TUN legges til først.

Notér **CT-ID** (for eksempel `120`).

## 4. Gi containeren tilgang til TUN (for Tailscale)

Tailscale trenger `/dev/net/tun`. En uprivilegert container har ikke tilgang til den som standard.

**Alternativ A (PVE 8.1 eller nyere, via GUI):** Gå til **CT → Resources → Add → Device Passthrough** og velg Device Path `/dev/net/tun`.

**Alternativ B (alle versjoner, i konfigurasjonsfilen):**

```bash
pve# nano /etc/pve/lxc/<CT-ID>.conf
```

Legg til nederst:

```
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```

Start containeren og åpne konsollen. Bruk enten **Console** i GUI eller `pve# pct enter <CT-ID>`.

```bash
# ls -l /dev/net/tun
crw-rw-rw- 1 nobody nogroup 10, 200 ... /dev/net/tun
```

> Får du ikke TUN til å virke, kan Tailscale kjøre i *userspace networking*-modus. Se [11.6](#116-hvis-tun-ikke-er-tilgjengelig).

## 5. Grunnoppsett i containeren

```bash
# apt update && apt full-upgrade -y
# apt install -y git curl ca-certificates sudo nftables \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fontconfig
```

`libpango…` og `libharfbuzz-subset0` trengs av WeasyPrint for PDF-rapporten (jf. WeasyPrints installasjonsguide for Debian). Selve fontene ligger i repoet, så du trenger ingen fontpakker.

Sett tidssonen, slik at timeren og datoene i panelet følger norsk tid:

```bash
# timedatectl set-timezone Europe/Oslo || ln -sf /usr/share/zoneinfo/Europe/Oslo /etc/localtime
# date
```

Aktiver automatiske sikkerhetsoppdateringer (anbefalt):

```bash
# apt install -y unattended-upgrades
# dpkg-reconfigure -plow unattended-upgrades
```

## 6. Tjenestebruker, mapper og uv

Tjenestebrukeren eier koden og dataene. Hjemmemappen er `/var/lib/horingsradar`, som også er `DATA_DIR`. Der havner også uv-cache og Python-installasjonen til uv.

```bash
# useradd --system --create-home --home-dir /var/lib/horingsradar \
          --shell /bin/bash horingsradar
# chmod 750 /var/lib/horingsradar
# install -d -o horingsradar -g horingsradar -m 755 /opt/horingsradar
```

Installer uv systemomfattende. Prosjektet bruker Python 3.12, som uv henter selv:

```bash
# curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
# uv --version
```

> Vil du ha nøyaktig samme uv-versjon som på utviklingsmaskinen, bruk `https://astral.sh/uv/<versjon>/install.sh`.

## 7. Hent koden

### 7.1 Privat GitHub-repo med deploy key (anbefalt)

Lag en SSH-nøkkel for tjenestebrukeren:

```bash
# install -d -m 700 -o horingsradar -g horingsradar /var/lib/horingsradar/.ssh
# sudo -u horingsradar -H ssh-keygen -t ed25519 -N "" -C "horingsradar-lxc" \
      -f /var/lib/horingsradar/.ssh/id_ed25519
# cat /var/lib/horingsradar/.ssh/id_ed25519.pub
```

Lim inn den offentlige nøkkelen på GitHub under **Repo → Settings → Deploy keys → Add deploy key**. La «Allow write access» være **av**.

```bash
# sudo -u horingsradar -H ssh -o StrictHostKeyChecking=accept-new -T git@github.com   # bekreft vertsnøkkel
# sudo -u horingsradar -H git clone git@github.com:<bruker>/horingsradar.git /opt/horingsradar
```

### 7.2 Alternativ uten GitHub: bart repo i containeren

Dette krever at du kan nå containeren med SSH fra utviklingsmaskinen på LAN.

```bash
# apt install -y openssh-server
# install -d -o horingsradar -g horingsradar /srv/git
# sudo -u horingsradar git init --bare /srv/git/horingsradar.git
```

Legg din offentlige SSH-nøkkel i `/var/lib/horingsradar/.ssh/authorized_keys`. Push deretter fra utviklingsmaskinen:

```bash
git remote add lab horingsradar@<lan-ip>:/srv/git/horingsradar.git
git push lab main
```

Klon i containeren:

```bash
# sudo -u horingsradar -H git clone /srv/git/horingsradar.git /opt/horingsradar
```

### 7.3 Installer avhengigheter

```bash
# cd /opt/horingsradar
# sudo -u horingsradar -H uv sync --frozen
```

Første gang laster uv ned Python 3.12 og alle pakker. Bekreft at alt, også PDF-generering med Pango, virker ved å kjøre testene. De bruker ikke nett eller API:

```bash
# sudo -u horingsradar -H uv run pytest -q
```

## 8. Konfigurasjon (.env)

```bash
# cd /opt/horingsradar
# sudo -u horingsradar cp .env.example .env
# chmod 600 .env && chown horingsradar:horingsradar .env
# sudo -u horingsradar nano .env
```

Endre minst disse:

| Variabel | Verdi |
|---|---|
| `DATA_DIR` | `/var/lib/horingsradar` |
| `USER_AGENT` | `Horingsradar/0.1 (pilot for Datatilsynet; kontakt: <din e-post>)` |
| `ANTHROPIC_API_KEY` | API-nøkkelen |
| `LLM_MODEL` | `claude-sonnet-5-5` (standard) |
| `PRICE_INPUT_PER_MTOK` / `PRICE_OUTPUT_PER_MTOK` | Gjeldende priser for modellen. Brukes bare til estimater. |
| `WEB_BIND` | Settes i [11.4](#114-bind-adminpanelet-til-lan-og-tailscale). Bruk foreløpig `<lan-ip>:8080`. |
| `WEB_USERNAME` | For eksempel `operator` |
| `WEB_PASSWORD_HASH` | Se nedenfor |

Lag passord-hashen. Passordet tastes inn og vises ikke:

```bash
# cd /opt/horingsradar && sudo -u horingsradar -H .venv/bin/horingsradar hash-password
```

Lim hele linjen som starter med `scrypt$` inn som `WEB_PASSWORD_HASH=...`. Bruk et sterkt, unikt passord: knappene i panelet koster penger.

> `.env` skal aldri i git (den står i `.gitignore`). Panelet viser aldri innholdet i den.

## 9. Første test for hånd

Kjør først **uten LLM**. Da hentes høringer og dokumenter, men ingenting sendes til modellen, så det koster ingenting:

```bash
# cd /opt/horingsradar
# sudo -u horingsradar -H .venv/bin/horingsradar run --no-assess
# sudo -u horingsradar -H .venv/bin/horingsradar status
```

Første kjøring henter høringer publisert de siste 90 dagene (`INITIAL_LOOKBACK_DAYS`). Med minst 2 sekunder mellom kall tar det typisk 10–20 minutter. Se fremdriften i loggen:

```bash
# ls -t /var/lib/horingsradar/logs/ | head -1
# tail -f /var/lib/horingsradar/logs/job-<nr>.log
```

Prøv deretter én vurdering med ekte API, for å bekrefte nøkkel, modell og pris:

```bash
# sudo -u horingsradar -H .venv/bin/horingsradar assess <id fra «Siste høringer» i status>
```

Når dette ser bra ut, kan resten vurderes. Det skjer automatisk ved neste `run`, eller med `run` for hånd. Lag deretter første rapport:

```bash
# sudo -u horingsradar -H .venv/bin/horingsradar run
# sudo -u horingsradar -H .venv/bin/horingsradar report
```

PDF-en havner i `/var/lib/horingsradar/reports/` og kan lastes ned fra panelet.

## 10. systemd: daglig kjøring og adminpanel

```bash
# install -m 644 /opt/horingsradar/deploy/systemd/horingsradar-* /etc/systemd/system/
# systemctl daemon-reload
# systemctl enable --now horingsradar-run.timer
# systemctl enable --now horingsradar-web.service
# systemctl list-timers horingsradar-run.timer
# systemctl status horingsradar-web.service
```

- `horingsradar-run.timer` starter `run` hver dag kl. 06:15 (± 10 min). Med `Persistent=true` tas en glemt kjøring igjen etter omstart. Endre tidspunktet i `OnCalendar` og kjør `daemon-reload`.
- **Pause** i panelet stopper ikke timeren. Den fyrer som vanlig, men `run` avslutter straks og registreres som `skipped`. «Kjør nå» virker uansett.
- All logging går til journald. Hver jobb har i tillegg sin egen logg i `/var/lib/horingsradar/logs/`.

## 11. Tailscale

### 11.1 Installer

```bash
# curl -fsSL https://tailscale.com/install.sh | sh
# systemctl enable --now tailscaled
```

### 11.2 Koble til tailnettet

```bash
# tailscale up --hostname=horingsradar --ssh=false
```

Åpne lenken som vises og godkjenn maskinen. Deretter, i Tailscale-admin (**Machines → horingsradar**):

- **Disable key expiry** for denne maskinen. Ellers mister du tilgangen etter 180 dager, kanskje rett før en demo.
- Gi gjerne maskinen en tag, for eksempel `tag:horingsradar`, for ACL-ene i 11.5.

### 11.3 Finn adressen

```bash
# tailscale ip -4
100.x.y.z
```

Med MagicDNS på (standard) heter maskinen `horingsradar` eller `horingsradar.<tailnet>.ts.net`.

### 11.4 Bind adminpanelet til LAN og Tailscale

Panelet binder bare til adressene i `WEB_BIND`, aldri `0.0.0.0`. Legg inn både LAN- og Tailscale-adressen:

```bash
# sudo -u horingsradar nano /opt/horingsradar/.env
WEB_BIND=<lan-ip>:8080,<tailscale-ip>:8080
# systemctl restart horingsradar-web.service
# ss -ltnp | grep 8080
```

Panelet kan binde Tailscale-adressen selv om `tailscaled` ikke er helt oppe ved oppstart (Linux `IP_FREEBIND`). Du trenger derfor ingen spesiell rekkefølge ved boot.

Test fra en maskin i tailnettet: `http://horingsradar:8080`. Nettleseren ber om brukernavn og passord.

### 11.5 Begrens hvem som når panelet (ACL)

Som standard kan alle enheter i tailnettet nå hverandre. I **Access controls** kan du begrense det, for eksempel slik at bare din bruker når port 8080 på `tag:horingsradar`:

```jsonc
{
  "tagOwners": { "tag:horingsradar": ["autogroup:admin"] },
  "grants": [
    { "src": ["<din-epost>"], "dst": ["tag:horingsradar"], "ip": ["tcp:8080"] }
  ]
}
```

Tilpass regelen til din eksisterende policy. Har du allerede en «alle til alle»-regel, må den snevres inn for at dette skal ha effekt.

### 11.6 Hvis TUN ikke er tilgjengelig

Kjør Tailscale i userspace-modus og la Tailscale videresende til panelet på localhost:

```bash
# mkdir -p /etc/systemd/system/tailscaled.service.d
# printf '[Service]\nEnvironment="FLAGS=--tun=userspace-networking"\n' \
      > /etc/systemd/system/tailscaled.service.d/userspace.conf
# systemctl daemon-reload && systemctl restart tailscaled
# tailscale serve --bg --http=8080 http://127.0.0.1:8080
```

Sett da `WEB_BIND=<lan-ip>:8080,127.0.0.1:8080`.

### 11.7 Valgfritt: HTTPS med gyldig sertifikat

Trafikken over Tailscale er allerede kryptert (WireGuard). Vil du likevel ha `https://` i adressefeltet, for eksempel for å slippe advarsler i nettleseren under en demo, aktiver **HTTPS Certificates** i Tailscale-admin og kjør:

```bash
# tailscale serve --bg --https=443 http://127.0.0.1:8080
```

Legg da `127.0.0.1:8080` til i `WEB_BIND`. Panelet er nå tilgjengelig på `https://horingsradar.<tailnet>.ts.net`, bare innenfor tailnettet. Bruk **ikke** `tailscale funnel`, som eksponerer tjenesten mot internett.

## 12. Brannmur (valgfritt, anbefalt)

SPEC sier at containeren bare skal ha utgående trafikk, i tillegg til panelet på LAN og Tailscale. Med nftables:

```bash
# cat > /etc/nftables.conf <<'EOF'
#!/usr/sbin/nft -f
flush ruleset
table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
    ip protocol icmp accept
    ip6 nexthdr ipv6-icmp accept
    iifname "tailscale0" tcp dport 8080 accept
    ip saddr <lan-nett>/24 tcp dport 8080 accept
    # SSH fra LAN, bare hvis du bruker 7.2 eller vil logge inn med SSH:
    # ip saddr <lan-nett>/24 tcp dport 22 accept
    udp dport 41641 accept   # Tailscale direkte forbindelser (valgfritt)
  }
  chain forward { type filter hook forward priority 0; policy drop; }
  chain output  { type filter hook output priority 0; policy accept; }
}
EOF
# systemctl enable --now nftables
# nft list ruleset
```

> Proxmox-brannmuren på containerens nettverkskort er et alternativ. Ikke bruk begge uten å vite hvilken som gjelder.

## 13. Daglig drift

| Oppgave | Hvor |
|---|---|
| Se status, siste og neste kjøring | Panelet, **Status** |
| Lage rapport | Panelet, **Rapporter → Generer rapport**, eller `horingsradar report` |
| Laste ned PDF | Panelet, **Rapporter** |
| Regenerere en uke | `horingsradar report --week 2026-W42` |
| Pause og gjenoppta | Panelet, eller `horingsradar pause` / `horingsradar resume` |
| Vurdere én høring på nytt | Panelet, **Høringer**, eller `horingsradar assess <id>` |
| Backtest | Panelet, **Backtest**: først estimat, deretter bekreftelse. Eller `horingsradar backtest --split dev --limit 20` |
| Logger | Panelet, **Jobber og logg**, eller `journalctl -u horingsradar-run -u horingsradar-web` |

Kjør CLI-kommandoer som tjenestebrukeren fra `/opt/horingsradar`:

```bash
# cd /opt/horingsradar && sudo -u horingsradar -H .venv/bin/horingsradar <kommando>
```

Bare én jobb kan kjøre om gangen, uansett om den er startet fra timeren, panelet eller terminalen. En jobb som finner låsen tatt, registreres som `skipped`.

**Backtest** bruker batch-API-et (halv pris) og kan ta fra noen minutter til flere timer. Første gang må også alle høringer og dokumenter i perioden hentes, med 2 sekunder mellom kall. Det er gratis, men tar tid. Begynn med `--limit 20` for å sjekke oppsettet billig.

## 14. Oppdatere til ny versjon

Push fra utviklingsmaskinen, og kjør deretter dette i containeren:

```bash
# /opt/horingsradar/deploy/deploy.sh
```

Skriptet:

1. avbryter hvis en jobb kjører, med mindre `--force` er gitt,
2. kjører `git pull --ff-only` og `uv sync --frozen` som tjenestebrukeren,
3. kopierer endrede systemd-filer og kjører `daemon-reload`,
4. starter panelet på nytt. Jobber som er startet fra panelet, overlever omstarten.

Bytter du promptversjon eller modell, oppdaterer du `PROMPT_VERSION` og `LLM_MODEL` i `.env`. Promptene er versjonerte filer i `prompts/`.

## 15. Backup og gjenoppretting

All tilstand ligger i `/var/lib/horingsradar` (database, cache, PDF-er, logger). Koden ligger i git, og `.env` er den eneste hemmeligheten.

- **Enklest:** Proxmox-backup av hele containeren (**Datacenter → Backup**, snapshot-modus, daglig eller ukentlig).
- **Mer målrettet:** SQLite-dump og rapporter:

  ```bash
  # sudo -u horingsradar sqlite3 /var/lib/horingsradar/horingsradar.sqlite3 ".backup '/var/lib/horingsradar/backup.sqlite3'"
  ```

  Krever `apt install sqlite3`. Ta vare på `backup.sqlite3`, `reports/` og en kopi av `.env` på et trygt sted.

`http_cache/`, `documents/` og `text/` kan gjenskapes ved å hente på nytt. `.cache/` og `.local/` (uv) kan slettes.

## 16. Sjekkliste før demo på annen lokasjon

Dagen før:

- [ ] `systemctl list-timers` viser at morgenens kjøring gikk, og panelet viser «ok» på siste kjøring.
- [ ] Det finnes en fersk rapport i **Rapporter**. Last ned én PDF lokalt som reserve hvis nettet svikter.
- [ ] Tailscale på demo-PC-en er logget inn i samme tailnet. Test `http://horingsradar:8080` fra et **annet nett** enn hjemme, for eksempel mobil-hotspot.
- [ ] «Key expiry» er slått av for `horingsradar`-maskinen.
- [ ] Du kan brukernavn og passord til panelet.
- [ ] Hvis du skal vise backtest: kjør den på forhånd og vis resultatene. En live-kjøring koster penger og tar tid.

Forslag til forløp i demoen:

1. **Status**: daglig automatikk, siste kjøring, pause og «Kjør nå».
2. **Høringer**: sortert liste med relevans, frist og om Datatilsynet står på adresselisten.
3. Én høring med høy relevans: sammendrag, funn med **verifiserte sitater** og sidetall, leseguide (for eksempel «s. 22–41 av 140») og eget personvernkapittel.
4. **Rapporter**: last ned PDF-en som juristene får.
5. **Statistikk**: andel flagget, verifiserte sitater, API-forbruk og siste backtest (recall).

## 17. Feilsøking

| Symptom | Sjekk |
|---|---|
| Panelet svarer ikke | `systemctl status horingsradar-web`, `journalctl -u horingsradar-web -n 50`. Står `WEB_PASSWORD_HASH` i `.env`? Lytter `ss -ltnp \| grep 8080` på riktige adresser? |
| Når panelet på LAN, men ikke via Tailscale | `tailscale status`, `tailscale ip -4`. Står Tailscale-IP-en i `WEB_BIND`? Slipper brannmuren gjennom `tailscale0`? Stopper ACL-ene trafikken? |
| `tailscaled` starter ikke i containeren | `ls -l /dev/net/tun`. Sjekk punkt 4 eller bruk 11.6. |
| `run` gir «Vurdering hoppet over: ANTHROPIC_API_KEY is not set» | Nøkkelen mangler i `.env`. Høringene står i kø og vurderes ved neste kjøring. |
| Vurderinger feiler med HTTP 401 eller 403 | Ugyldig API-nøkkel eller manglende tilgang til modellen. |
| Vurderinger feiler med HTTP 429 eller 529 | Rate limit eller overbelastning. Klienten prøver igjen selv. Ved varige feil vurderes høringen på nytt neste dag, maks to feilede forsøk per dokumentversjon. |
| PDF-rapport feiler med `cannot load library 'libpango…'` | `apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0`. |
| «En annen jobb kjører allerede» | Se **Status** eller `horingsradar status`. En prosess som har krasjet, markeres automatisk som `failed` neste gang. |
| Mange `skipped` på timer-kjøringer | Er planlagt kjøring pauset? Se **Status**. |
| Feil klokkeslett i panelet eller timeren | `timedatectl`. Tidssonen skal være Europe/Oslo. |
| Hentefeil mot regjeringen.no | Loggen viser URL og status. Nettstedet kan være nede. Kjøringen prøver igjen neste dag. |
