# Installere Høringsradar i en Proxmox-LXC

Guiden tar deg fra en tom Proxmox-node til en Høringsradar som:

- henter og vurderer høringer hver morgen,
- har adminpanelet tilgjengelig på LAN,
- kan vises som proof of concept fra en annen lokasjon via fjerntilgangen du har til nettverket.

Den er skrevet for Debian 13 (trixie) i en **uprivilegert** container. Regn med 45–60 minutter første gang.

> **Slik leser du guiden:** Alle kommandoer kjøres som **root i containeren**, med mindre blokken starter med `# På Proxmox-verten:`. Kopier én kodeblokk om gangen og bytt ut `<vinkelparenteser>` med egne verdier, *inkludert* parentesene: `pct reboot <CT-ID>` blir for eksempel `pct reboot 105`. Blokker merket «Forventet utskrift» viser hva du skal *se*, og skal ikke kjøres.

## Innhold

1. [Oversikt](#1-oversikt)
2. [Forberedelser](#2-forberedelser)
3. [Opprett containeren](#3-opprett-containeren)
4. [Grunnoppsett i containeren](#4-grunnoppsett-i-containeren)
5. [Tjenestebruker, mapper og uv](#5-tjenestebruker-mapper-og-uv)
6. [Hent koden](#6-hent-koden)
7. [Konfigurasjon (.env)](#7-konfigurasjon-env)
8. [Første test for hånd](#8-første-test-for-hånd)
9. [systemd: daglig kjøring og adminpanel](#9-systemd-daglig-kjøring-og-adminpanel)
10. [Brannmur (valgfritt, anbefalt)](#10-brannmur-valgfritt-anbefalt)
11. [Daglig drift](#11-daglig-drift)
12. [Oppdatere til ny versjon](#12-oppdatere-til-ny-versjon)
13. [Backup og gjenoppretting](#13-backup-og-gjenoppretting)
14. [Sjekkliste før demo på annen lokasjon](#14-sjekkliste-før-demo-på-annen-lokasjon)
15. [Feilsøking](#15-feilsøking)

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
 │        ▲                                                  │
 │   LAN-IP:8080                                             │
 │                                                           │
 │  /opt/horingsradar        kode, .venv, .env (chmod 600)   │
 │  /var/lib/horingsradar    SQLite, cache, PDF-er, logger   │
 └───────────────────────────────────────────────────────────┘
        │ kun utgående: regjeringen.no, api.anthropic.com,
        │ pakkeregistre, GitHub
```

- **Timeren** kjører bare `run`. `report` og `backtest` startes manuelt fra panelet eller terminalen.
- **Panelet** starter jobber som egne prosesser og styrer aldri systemd.
- Det er **ingen portvideresending** fra internett. Panelet lytter bare på LAN-adressen. Fjerntilgang ordnes på nettverksnivå, ikke i containeren.

## 2. Forberedelser

Du trenger:

- Proxmox VE 8 eller 9 med tilgang til internett.
- En **Anthropic API-nøkkel**. Lag en egen nøkkel for piloten og sett gjerne en månedlig utgiftsgrense i Anthropic-konsollen.
- **Fjerntilgang** til LAN-et hvis panelet skal vises fra en annen lokasjon, for eksempel VPN på ruteren eller Proxmox-verten.
- Et **git-repo** med koden. Et offentlig repo klones over HTTPS uten nøkler. Et privat repo krever en skrivebeskyttet *deploy key*. Se [kapittel 6](#6-hent-koden).

Endringer pushes fra utviklingsmaskinen med `git push` og hentes til containeren med deployskriptet ([kapittel 12](#12-oppdatere-til-ny-versjon)).

## 3. Opprett containeren

### 3.1 Last ned malen

I Proxmox-GUI: velg *local*-lagringen, gå til **CT Templates → Templates** og last ned `debian-13-standard`.

Eller på verten:

```bash
# På Proxmox-verten:
pveam update
pveam available --section system | grep debian-13
pveam download local debian-13-standard_13.<x>-1_amd64.tar.zst
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
| DNS | Ruterens IP (for eksempel `192.168.1.1`), eventuelt også `1.1.1.1`. Bruk ikke vertens innstillinger hvis Proxmox-verten selv bruker en lokal eller VPN-basert resolver (for eksempel `127.0.0.x` eller `100.100.100.100` i vertens `/etc/resolv.conf`). Den adressen når ikke containeren. |

Når containeren er opprettet, gå til **Options** og sett **Start at boot = Yes**. Notér **CT-ID** (for eksempel `120`).

Start containeren og åpne konsollen. Bruk enten **Console** i GUI eller `pct enter <CT-ID>` på Proxmox-verten.

## 4. Grunnoppsett i containeren

Sjekk først at containeren har nett og DNS:

```bash
ping -c 1 deb.debian.org
```

Svarer den med `1 packets transmitted, 1 received`, er alt i orden. Gir `ip route` ingen utskrift, har containeren ingen IP-adresse. Sett IP og gateway under *CT → Network → net0* (se 3.2), eller kjør `pct set <CT-ID> --net0 name=eth0,bridge=vmbr0,ip=<ip>/24,gw=<ruter-ip>` på verten, og start containeren på nytt. Får du `Temporary failure in name resolution`, ser du [feilsøking](#15-feilsøking) («Temporary failure resolving»). Fiks det før du går videre, ellers feiler alle `apt`-kommandoene.

```bash
apt update && apt full-upgrade -y
apt install -y git curl ca-certificates sudo nftables \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fontconfig
```

`libpango…` og `libharfbuzz-subset0` trengs av WeasyPrint for PDF-rapporten (jf. WeasyPrints installasjonsguide for Debian). Selve fontene ligger i repoet, så du trenger ingen fontpakker.

Sett tidssonen, slik at timeren og datoene i panelet følger norsk tid:

```bash
timedatectl set-timezone Europe/Oslo || ln -sf /usr/share/zoneinfo/Europe/Oslo /etc/localtime
date
```

Aktiver automatiske sikkerhetsoppdateringer (anbefalt):

```bash
apt install -y unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades
```

## 5. Tjenestebruker, mapper og uv

Tjenestebrukeren eier koden og dataene. Hjemmemappen er `/var/lib/horingsradar`, som også er `DATA_DIR`. Der havner også uv-cache og Python-installasjonen til uv.

```bash
useradd --system --create-home --home-dir /var/lib/horingsradar \
        --shell /bin/bash horingsradar
chmod 750 /var/lib/horingsradar
install -d -o horingsradar -g horingsradar -m 755 /opt/horingsradar
```

Installer uv systemomfattende. Prosjektet bruker Python 3.12, som uv henter selv:

```bash
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
uv --version
```

> Vil du ha nøyaktig samme uv-versjon som på utviklingsmaskinen, bruk `https://astral.sh/uv/<versjon>/install.sh`.

## 6. Hent koden

### 6.1 Offentlig repo (enklest)

```bash
sudo -u horingsradar -H git clone https://github.com/<bruker>/horingsradar.git /opt/horingsradar
```

Gå videre til [6.4](#64-installer-avhengigheter).

### 6.2 Privat GitHub-repo med deploy key

Lag en SSH-nøkkel for tjenestebrukeren:

```bash
install -d -m 700 -o horingsradar -g horingsradar /var/lib/horingsradar/.ssh
sudo -u horingsradar -H ssh-keygen -t ed25519 -N "" -C "horingsradar-lxc" \
    -f /var/lib/horingsradar/.ssh/id_ed25519
cat /var/lib/horingsradar/.ssh/id_ed25519.pub
```

Lim inn den offentlige nøkkelen på GitHub under **Repo → Settings → Deploy keys → Add deploy key**. La «Allow write access» være **av**.

```bash
sudo -u horingsradar -H ssh -o StrictHostKeyChecking=accept-new -T git@github.com   # bekreft vertsnøkkel
sudo -u horingsradar -H git clone git@github.com:<bruker>/horingsradar.git /opt/horingsradar
```

### 6.3 Alternativ uten GitHub: bart repo i containeren

Dette krever at du kan nå containeren med SSH fra utviklingsmaskinen på LAN.

```bash
apt install -y openssh-server
install -d -o horingsradar -g horingsradar /srv/git
sudo -u horingsradar git init --bare /srv/git/horingsradar.git
```

Legg din offentlige SSH-nøkkel i `/var/lib/horingsradar/.ssh/authorized_keys`. Push deretter fra utviklingsmaskinen:

```bash
# På utviklingsmaskinen:
git remote add lab horingsradar@<lan-ip>:/srv/git/horingsradar.git
git push lab main
```

Klon i containeren:

```bash
sudo -u horingsradar -H git clone /srv/git/horingsradar.git /opt/horingsradar
```

### 6.4 Installer avhengigheter

```bash
cd /opt/horingsradar
sudo -u horingsradar -H uv sync --frozen
```

Første gang laster uv ned Python 3.12 og alle pakker. Bekreft at alt, også PDF-generering med Pango, virker ved å kjøre testene. De bruker ikke nett eller API:

```bash
sudo -u horingsradar -H uv run pytest -q
```

## 7. Konfigurasjon (.env)

```bash
cd /opt/horingsradar
sudo -u horingsradar cp .env.example .env
chmod 600 .env && chown horingsradar:horingsradar .env
sudo -u horingsradar nano .env
```

Endre minst disse:

| Variabel | Verdi |
|---|---|
| `DATA_DIR` | `/var/lib/horingsradar`. **Må** endres fra standardverdien `./data`, ellers får ikke systemd-tjenestene skrive til databasen. |
| `USER_AGENT` | `Horingsradar/0.1 (kontakt: <din e-post>)` |
| `ANTHROPIC_API_KEY` | API-nøkkelen |
| `LLM_MODEL` | `claude-sonnet-5-5` (standard) |
| `PRICE_INPUT_PER_MTOK` / `PRICE_OUTPUT_PER_MTOK` | Gjeldende priser for modellen. Brukes bare til estimater. |
| `WEB_BIND` | `<lan-ip>:8080`, altså containerens faste LAN-adresse. Panelet binder bare til adressene her, aldri `0.0.0.0`. |
| `WEB_USERNAME` | For eksempel `operator` |
| `WEB_PASSWORD_HASH` | Se nedenfor |

Lag passord-hashen. Passordet tastes inn og vises ikke:

```bash
cd /opt/horingsradar && sudo -u horingsradar -H .venv/bin/horingsradar hash-password
```

Lim hele linjen som starter med `scrypt$` inn som `WEB_PASSWORD_HASH=...`. Bruk et sterkt, unikt passord.

> `.env` skal aldri i git (den står i `.gitignore`). Panelet viser aldri innholdet i den.

## 8. Første test for hånd

Kjør først **uten LLM**. Da hentes høringer og dokumenter, men ingenting sendes til modellen, så det koster ingenting:

```bash
cd /opt/horingsradar
sudo -u horingsradar -H .venv/bin/horingsradar run --no-assess
sudo -u horingsradar -H .venv/bin/horingsradar status
```

Første kjøring henter høringer publisert de siste 90 dagene (`INITIAL_LOOKBACK_DAYS`). Med minst 2 sekunder mellom kall tar det typisk 10–20 minutter. Se fremdriften i loggen:

```bash
ls -t /var/lib/horingsradar/logs/ | head -1
tail -f /var/lib/horingsradar/logs/job-<nr>.log
```

Prøv deretter én vurdering med ekte API, for å bekrefte nøkkel, modell og pris:

```bash
sudo -u horingsradar -H .venv/bin/horingsradar assess <id fra «Siste høringer» i status>
```

Når dette ser bra ut, kan resten vurderes. Det skjer automatisk ved neste `run`, eller med `run` for hånd. Lag deretter første rapport:

```bash
sudo -u horingsradar -H .venv/bin/horingsradar run
sudo -u horingsradar -H .venv/bin/horingsradar report
```

PDF-en havner i `/var/lib/horingsradar/reports/` og kan lastes ned fra panelet.

## 9. systemd: daglig kjøring og adminpanel

```bash
install -m 644 /opt/horingsradar/deploy/systemd/horingsradar-* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now horingsradar-run.timer
systemctl enable --now horingsradar-web.service
systemctl list-timers horingsradar-run.timer
systemctl status horingsradar-web.service
```

- `horingsradar-run.timer` starter `run` hver dag kl. 06:15 (± 10 min). Med `Persistent=true` tas en glemt kjøring igjen etter omstart. Endre tidspunktet i `OnCalendar` og kjør `daemon-reload`.
- **Pause** i panelet stopper ikke timeren. Den fyrer som vanlig, men `run` avslutter straks og registreres som `skipped`. «Kjør nå» virker uansett.
- All logging går til journald. Hver jobb har i tillegg sin egen logg i `/var/lib/horingsradar/logs/`.

Sjekk at panelet lytter på LAN-adressen, og åpne `http://<lan-ip>:8080` i nettleseren. Den ber om brukernavn og passord.

```bash
ss -ltnp | grep 8080
```

## 10. Brannmur (valgfritt, anbefalt)

SPEC sier at containeren bare skal ha utgående trafikk, i tillegg til panelet på LAN. Med nftables:

```bash
cat > /etc/nftables.conf <<'EOF'
#!/usr/sbin/nft -f
flush ruleset
table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
    ip protocol icmp accept
    ip6 nexthdr ipv6-icmp accept
    ip saddr <lan-nett>/24 tcp dport 8080 accept
    # SSH fra LAN, bare hvis du bruker 6.3 eller vil logge inn med SSH:
    # ip saddr <lan-nett>/24 tcp dport 22 accept
  }
  chain forward { type filter hook forward priority 0; policy drop; }
  chain output  { type filter hook output priority 0; policy accept; }
}
EOF
systemctl enable --now nftables
nft list ruleset
```

> Proxmox-brannmuren på containerens nettverkskort er et alternativ. Ikke bruk begge uten å vite hvilken som gjelder.

## 11. Daglig drift

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
cd /opt/horingsradar && sudo -u horingsradar -H .venv/bin/horingsradar <kommando>
```

Bare én jobb kan kjøre om gangen, uansett om den er startet fra timeren, panelet eller terminalen. En jobb som finner låsen tatt, registreres som `skipped`.

**Backtest** bruker batch-API-et (halv pris) og kan ta fra noen minutter til flere timer. Første gang må også alle høringer og dokumenter i perioden hentes, med 2 sekunder mellom kall. Det er gratis, men tar tid. Begynn med `--limit 20` for å sjekke oppsettet billig.

## 12. Oppdatere til ny versjon

Push fra utviklingsmaskinen, og kjør deretter dette i containeren:

```bash
/opt/horingsradar/deploy/deploy.sh
```

Skriptet:

1. avbryter hvis en jobb kjører, med mindre `--force` er gitt,
2. kjører `git pull --ff-only` og `uv sync --frozen` som tjenestebrukeren,
3. kopierer endrede systemd-filer og kjører `daemon-reload`,
4. starter panelet på nytt. Jobber som er startet fra panelet, overlever omstarten.

Bytter du promptversjon eller modell, oppdaterer du `PROMPT_VERSION` og `LLM_MODEL` i `.env`. Promptene er versjonerte filer i `prompts/`.

## 13. Backup og gjenoppretting

All tilstand ligger i `/var/lib/horingsradar` (database, cache, PDF-er, logger). Koden ligger i git, og `.env` er den eneste hemmeligheten.

- **Enklest:** Proxmox-backup av hele containeren (**Datacenter → Backup**, snapshot-modus, daglig eller ukentlig).
- **Mer målrettet:** SQLite-dump og rapporter:

  ```bash
  sudo -u horingsradar sqlite3 /var/lib/horingsradar/horingsradar.sqlite3 ".backup '/var/lib/horingsradar/backup.sqlite3'"
  ```

  Krever `apt install sqlite3`. Ta vare på `backup.sqlite3`, `reports/` og en kopi av `.env` på et trygt sted.

`http_cache/`, `documents/` og `text/` kan gjenskapes ved å hente på nytt. `.cache/` og `.local/` (uv) kan slettes.

## 14. Sjekkliste før demo på annen lokasjon

Dagen før:

- [ ] `systemctl list-timers` viser at morgenens kjøring gikk, og panelet viser «ok» på siste kjøring.
- [ ] Det finnes en fersk rapport i **Rapporter**. Last ned én PDF lokalt som reserve hvis nettet svikter.
- [ ] Fjerntilgangen virker: koble demo-PC-en til nettverket slik du skal gjøre under demoen, og test `http://<lan-ip>:8080` fra et **annet nett** enn hjemme, for eksempel mobil-hotspot.
- [ ] Du kan brukernavn og passord til panelet.
- [ ] Hvis du skal vise backtest: kjør den på forhånd og vis resultatene. En live-kjøring koster penger og tar tid.

Forslag til forløp i demoen:

1. **Status**: daglig automatikk, siste kjøring, pause og «Kjør nå».
2. **Høringer**: sortert liste med relevans, frist og om målvirksomheten står på adresselisten.
3. Én høring med høy relevans: sammendrag, funn med **verifiserte sitater** og sidetall, leseguide (for eksempel «s. 22–41 av 140») og eget personvernkapittel.
4. **Rapporter**: last ned PDF-en som mottakerne får.
5. **Statistikk**: andel flagget, verifiserte sitater, API-forbruk og siste backtest (recall).

## 15. Feilsøking

| Symptom | Sjekk |
|---|---|
| Panelet svarer ikke | `systemctl status horingsradar-web`, `journalctl -u horingsradar-web -n 50`. Står `WEB_PASSWORD_HASH` i `.env`? Lytter `ss -ltnp \| grep 8080` på riktige adresser? |
| «Internal Server Error», eller `unable to open database file` / «Kan ikke åpne databasen» i `journalctl -u horingsradar-web` | `DATA_DIR` i `.env` står ikke på `/var/lib/horingsradar`. Rett den, slett `/opt/horingsradar/data`, kjør `run --no-assess` på nytt og start panelet på nytt. |
| Når panelet på LAN, men ikke utenfra | Fjerntilgangen må slippe trafikk til `<lan-ip>:8080`. Sjekk ruting og tilgangsregler i VPN-en, og at brannmuren i [kapittel 10](#10-brannmur-valgfritt-anbefalt) tillater kildeadressen. |
| `run` gir «Vurdering hoppet over: ANTHROPIC_API_KEY is not set» | Nøkkelen mangler i `.env`. Høringene står i kø og vurderes ved neste kjøring. |
| Vurderinger feiler med HTTP 401 eller 403 | Ugyldig API-nøkkel eller manglende tilgang til modellen. |
| Vurderinger feiler med HTTP 429 eller 529 | Rate limit eller overbelastning. Klienten prøver igjen selv. Ved varige feil vurderes høringen på nytt neste dag, maks to feilede forsøk per dokumentversjon. |
| PDF-rapport feiler med `cannot load library 'libpango…'` | `apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0`. |
| «En annen jobb kjører allerede» | Se **Status** eller `horingsradar status`. En prosess som har krasjet, markeres automatisk som `failed` neste gang. |
| Mange `skipped` på timer-kjøringer | Er planlagt kjøring pauset? Se **Status**. |
| Feil klokkeslett i panelet eller timeren | `timedatectl`. Tidssonen skal være Europe/Oslo. |
| `apt` gir «Temporary failure resolving 'deb.debian.org'» | Containeren mangler DNS eller nett. Kjør `ping -c 1 1.1.1.1`. Feiler den, er IP eller gateway feil under *CT → Network*. Virker den, mangler bare DNS. Kjør da `pct set <CT-ID> --nameserver "<ruterens-ip> 1.1.1.1"` og `pct reboot <CT-ID>` på Proxmox-verten. `/etc/resolv.conf` i containeren skrives over av Proxmox og skal ikke redigeres for hånd. |
| Hentefeil mot regjeringen.no | Loggen viser URL og status. Nettstedet kan være nede. Kjøringen prøver igjen neste dag. |
