# EU/EØS-radar – arkitekturvalg

> **Status: besluttet 29.10.2026.** Kravene til selve EU/EØS-radaren står i [`EU-SPEC.md`](EU-SPEC.md).
> Ingenting av dette er implementert ennå.

## Bakgrunn

Høringsradar henter i dag høringer fra regjeringen.no, vurderer personvernrelevans med LLM og lager en PDF-rapport til pilotbrukerne.

En ny radar er planlagt: EU-forslag og EU-høringer med mulig EØS-relevans og personvernrelevans, også tekniske forslag. Den har **en annen målgruppe**: brukere som jobber med internasjonale saker. Rapportene skal ikke slås sammen.

## Beslutning

1. **Samme kodebase, egen modul.** EU/EØS-radaren bygges som en ny pakke ved siden av dagens radar (for eksempel `src/horingsradar/eu/`). Den bruker de felles delene, men dagens radar skrives **ikke** om til en generell pipeline nå. Felles abstraksjoner vurderes først når begge radarene virker.
2. **Egen instans på samme LXC.** Radarene kjører hver for seg og kan ikke påvirke hverandre:

   | Lag | Høringsradar (regjeringen.no) | EU/EØS-radar |
   |---|---|---|
   | Kode | `/opt/horingsradar`, branch `main` | `/opt/horingsradar-eu`, egen checkout |
   | Data og database | `/var/lib/horingsradar` | `/var/lib/horingsradar-eu` |
   | Konfigurasjon | `.env` | egen `.env` |
   | Tjenester | `horingsradar-run.timer`, `horingsradar-web` | `horingsradar-eu-run.timer`, `horingsradar-eu-web` |
   | Adminpanel | egen port | egen port |
   | Fil-lås | i sin `DATA_DIR` | i sin `DATA_DIR` |

3. **Egen database.** Egen `DATA_DIR` gir egen SQLite-database, HTTP-cache, logger, rapporter og lås. Ingen tabeller deles.
4. **Egen rapport.** EU/EØS-radaren lager sin egen PDF i sin egen rapportmappe.
5. **To paneler med lenke mellom seg** i stedet for ett panel med to knapper. Det holder prosessene adskilt.
6. **Egen API-nøkkel** (eget workspace hos LLM-leverandøren) anbefales, så kostnader og grenser ikke blandes.
7. **Timerne går på ulike tidspunkter**, så to kjøringer ikke lager PDF samtidig.

## Svar på de åpne spørsmålene

| Spørsmål | Svar |
|---|---|
| Målgruppe | Brukere som jobber med internasjonale saker. Ikke samme rapport som dagens radar. |
| Livsløp | Åpent. Med egen instans kan radarene senere flyttes hver for seg, for eksempel til hver sin LXC, uten omskriving. |
| Kobling mellom radarene | Ikke nå. Kan legges til senere ved at én radar leser den andres database (bare lesing) og kobler på rettsaktnumre. Det krever ikke felles database. |
| Feiltoleranse | Dagens radar skal være helt uberørt. Det oppnås med egen checkout, egne tjenester og egen `DATA_DIR`. |
| Modularitet i dag | Se analysen under. |

## Analyse av dagens kode (29.10.2026)

### Struktur

```
systemd-timer / panel / terminal
        │  (CLI som egen prosess, felles fil-lås i DATA_DIR)
        ▼
pipeline.run ── RegjeringenSource.discover   listesider
             ├─ refresh_hearing              høringsside, dokumenter, hearing_change
             ├─ extract                      PDF/HTML → tekst med [[side N]]
             └─ assess                       kapittelinndeling → prompt → LLM → Pydantic → sitatsjekk
        ▼
SQLite (hearing, document, assessment, finding, report, report_item, hearing_change, job …)
        ▼
report/build (Jinja2 + WeasyPrint)      backtest (fasit: målvirksomhetens høringssvar)
        ▼
web/app (FastAPI-panel som leser databasen og starter CLI-jobber)
```

Konfigurasjonen leses fra `.env`, og `ENV_FILE` kan peke på en annen fil. Jobber som panelet starter, arver miljøet sitt, så en instans med egen `ENV_FILE` holder seg til sin egen `DATA_DIR`.

### Kildeavhengighet

| Del | Bundet til |
|---|---|
| `sources/regjeringen/` | regjeringen.no (som tiltenkt) |
| `pipeline.py` | regjeringen.no: oppretter `RegjeringenSource` direkte og bruker regjeringen-statuser |
| `db.py` (`hearing`) | regjeringen.no: ID, statusverdier, adresseliste og `dt_*`-felt |
| `backtest.py` | regjeringen.no og målvirksomhetens høringssvar som fasit |
| `prompts/assess_v1.md`, `llm/schema.py` | dagens målgruppe: norske kategorier og sektorer, leseguide med sidetall, funn med ordrette sitater fra lange PDF-er |
| Rapportmal og panelvisninger | dagens målgruppe |
| `extract.py` | litt: bruker `html_to_text` fra regjeringen-parseren |

**Generisk allerede:** `http.py` (User-Agent, ventetid, cache, robots.txt), `llm/client.py` (Messages og Batches), `lock.py`, `jobs.py`, `quotes.py`, `chunking.py`, promptlasting, innlogging, CSRF og jobbstarter i panelet, PDF-produksjon og fonter.

Det finnes ingen nøkkelordmekanisme i dag. Dagens radar vurderer alt den finner.

### Felles og radarspesifikt

- **Deles uendret:** HTTP-henting, LLM-klient, lås, jobber, sitatverifisering, PDF-uttrekk, promptlasting, innlogging og jobbstarter, PDF-produksjon.
- **Eget per radar:** kilder og parsere, datamodell, prompt og svarskjema, pipeline-steg, rapportmal, panelvisninger, backtest og statistikk.

Radarene er ulike av natur. Dagens radar leser noen få lange dokumenter i fulltekst og gir sitater med sidetall. EU/EØS-radaren vurderer mange kandidater ut fra metadata og sammendrag, etter et deterministisk filter og med berikelse fra andre kilder. En felles «radar»-abstraksjon ville blitt tynn nå.

### Refaktorering og omfang

| Vei | Omfang | Berører dagens radar |
|---|---|---|
| A. Generell pipeline med radarer som moduler | Stort | Ja: pipeline, database, assess, rapport, panel og backtest |
| **B. EU/EØS som egen modul (valgt)** | Middels til stort, nesten bare ny kode | Lite: CLI-registrering, lenke i panelet, eventuelt små generaliseringer i felles kode |

### Kjøremodell

- **(a) Én prosess med flere radarer:** dårligst. Felles lås og database gjør at en lang EU-jobb blokkerer den daglige kjøringen, som da blir «hoppet over».
- **(b) Samme kodebase som egne instanser (valgt):** virker nesten uten endringer i dag.
- **(c) Separate applikasjoner:** duplisert kode uten gevinst nå. (b) kan flyttes til egen LXC senere.

## Risiko for dagens radar, og tiltak

| Risiko | Tiltak |
|---|---|
| Endringer i felles kode gir feil i dagens radar | Ikke endre modulene til dagens radar. Legg til ny kode. Hele testsuiten må være grønn før merge. |
| Felles lås eller database | Egen `DATA_DIR` per instans. |
| EU-arbeid når piloten før det er ferdig | Egen checkout i `/opt/horingsradar-eu`. Piloten kjører bare `main`. |
| Forbruk blandes | Egen API-nøkkel og egen statistikk. |
| En deploy av `main` tar med seg EU-kode | All EU-kode på branchen `feature/eu-eos-kilder` til den er ferdig testet. Merge via pull request. |
| Ressurser på samme LXC (CPU, minne, disk) | Timere på ulike tidspunkter. Følg med på diskplass. |

## Sikring og rollback

- **Tag `pilot-presentasjon`** peker på versjonen som var i drift før EU-arbeidet startet (`e33c7d5`). Den er det faste punktet for rollback av koden.
- **Snapshot av containeren i Proxmox** før hver deploy som kan påvirke piloten. Det dekker også database, `.env` og systemd-filer, som ikke ligger i git. Kjøres på Proxmox-verten:

  ```bash
  pct snapshot <CT-ID> <navn>
  ```

  Tilbakerulling:

  ```bash
  pct rollback <CT-ID> <navn>
  ```

- **Ingen deploy til piloten** før presentasjonen av piloten, utenom feilrettinger i piloten selv.
