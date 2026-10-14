# Høringsradar

Pilot og proof of concept for Datatilsynet.

Hver morgen henter Høringsradar nye og endrede høringer fra regjeringen.no. En LLM vurderer hvor relevante de er for personvern, med verifiserte sitater og sidehenvisninger. Resultatet blir en PDF-rapport som operatøren videresender manuelt til pilotjuristene. Et enkelt adminpanel på LAN og Tailscale lar operatøren styre jobbene og følge med.

Verktøyet beskriver hva som foreslås og hvor det står. Det skriver aldri høringsinnspill og tar ikke stilling til forslagene.

- Krav: [`docs/SPEC.md`](docs/SPEC.md)
- Plan og beslutninger: [`docs/PLAN.md`](docs/PLAN.md)
- Installasjon på Proxmox-LXC med Tailscale: [`docs/INSTALL-LXC.md`](docs/INSTALL-LXC.md)

## Kom i gang (utvikling)

```bash
uv sync
cp .env.example .env        # fyll inn ANTHROPIC_API_KEY m.m.
uv run pytest               # ingen nettverk, mocket LLM
uv run ruff check . && uv run ruff format .
```

PDF-rapporten bruker WeasyPrint, som trenger Pango:

- **Debian/Ubuntu:** `apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0`
- **Windows:** installer MSYS2 og kjør `pacman -S mingw-w64-x86_64-pango`. Koden finner `C:\msys64\mingw64\bin` automatisk, ellers settes `WEASYPRINT_DLL_DIRECTORIES`.

## Kommandoer

| Kommando | Hva den gjør |
|---|---|
| `uv run horingsradar run` | Henter nye og endrede høringer og vurderer dem. `--no-assess` henter uten LLM. |
| `uv run horingsradar assess <id>` | Vurderer én høring på nytt, for eksempel med `--prompt-version v2` (prompt-iterasjon). |
| `uv run horingsradar report` | Lager PDF for alle høringer som ikke er rapportert før. `--week 2026-W42` regenererer en uke. |
| `uv run horingsradar backtest --split dev\|test` | Bare manuelt. Viser kostnadsestimat og krever bekreftelse. Støtter `--limit N`, `--from`/`--to`, `--estimate-only`, `--no-batch` og `--yes`. |
| `uv run horingsradar web` | Starter adminpanelet på `WEB_BIND`. |
| `uv run horingsradar hash-password` | Lager `WEB_PASSWORD_HASH` til `.env`. |
| `uv run horingsradar status` / `pause` / `resume` | Viser status, eller pauser og gjenopptar planlagt kjøring. |

Alle jobber tar en felles fil-lås i `DATA_DIR`. Bare én jobb kan kjøre om gangen, uansett om den startes fra timer, panel eller terminal.

## Hvordan det virker

1. **Discovery.** Listesiden `/no/dokument/hoyringar/id1763/` leses side for side, nyeste først.
   - Første kjøring går 90 dager tilbake, senere kjøringer til forrige kjøring minus 14 dager.
   - Åpne høringer sjekkes på nytt hver gang.
2. **Henting.** Høringsside, høringsbrev, høringsnotat og vedlegg hentes høflig:
   - identifiserende User-Agent og minst 2 sekunder mellom kall,
   - disk-cache, og robots.txt respekteres.
   - Et nytt eller endret dokument (ny sha256) gir ny vurdering og merkes «oppdatert» i rapporten.
3. **Tekst.** PDF-er trekkes ut side for side med `[[side N]]`-markører. Høringsbrevet hentes fra HTML.
4. **Vurdering.** Dette er `prompts/assess_v1.md` med strukturert JSON-output, validert med Pydantic.
   - Ugyldig svar gir ett nytt forsøk. Feiler det igjen, markeres vurderingen som feilet.
   - Store høringer deles per kapittel, og høyeste relevansnivå gjelder.
   - Hvert sitat verifiseres mot kildeteksten (rapidfuzz ≥ 95). Uverifiserte sitater markeres synlig.
5. **Rapport.** Jinja2 + WeasyPrint gir en A4-PDF med:
   - oversikt,
   - frister som nærmer seg,
   - detaljer for høy og middels relevans,
   - én linje per høring for lav og ingen relevans.
6. **Backtest.** Startes bare manuelt. Datatilsynets publiserte høringssvar brukes som fasit. Den gir recall, andel flagget, forvirringsmatrise og lister til manuell gjennomgang.

Metadata (tittel, datoer, frist, departement, status og adresseliste) kommer alltid fra HTML-en, aldri fra LLM-en. Ingen høring filtreres bort.

## Struktur

```
src/horingsradar/
  config.py            .env-lasting og validering
  db.py                SQLite-skjema
  http.py              høflig henting, cache, robots.txt
  sources/regjeringen/ parser og crawler (all kildespesifikk kode)
  extract.py           PDF/HTML til tekst med sidemarkører
  llm/                 client.py (eneste LLM-kall), schema.py, prompt.py
  assess.py, chunking.py, quotes.py
  pipeline.py          run
  report/              PDF-rapport, maler og fonter (OFL)
  backtest.py
  jobs.py, lock.py     jobbregister og fil-lås
  web/                 adminpanel (FastAPI, Jinja2, HTMX vendret)
prompts/               versjonerte prompts – endre aldri en eksisterende versjon
deploy/                systemd-filer og deploy.sh
tests/                 pytest med lagrede fixtures fra regjeringen.no
```

## Lisenser

Fontene i `src/horingsradar/report/fonts/` er Source Sans 3 (SIL Open Font License). htmx i `src/horingsradar/web/static/` er BSD Zero Clause License.
