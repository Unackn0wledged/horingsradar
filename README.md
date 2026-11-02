# Høringsradar

![Høringsradar: en radar som finner høringer og fargelegger dem etter personvernrelevans](docs/images/horingsradar-radar.gif)

Pilot og proof of concept.

Hver morgen henter Høringsradar nye og endrede høringer fra regjeringen.no. En LLM vurderer hvor relevante de er for personvern og viser til sitater og sidetall som kontrolleres mot dokumentene. Resultatet er en PDF-rapport som administratoren sender videre til pilotbrukerne. Jobbene styres fra et enkelt adminpanel på LAN og Tailscale.

Verktøyet beskriver hva som foreslås og hvor det står. Det skriver aldri høringsinnspill og tar ikke stilling til forslagene.

**Målvirksomheten** er virksomheten verktøyet følger med for. Rapporten viser om den står på adresselisten, og backtesten bruker høringssvarene den har publisert som fasit.

- [Krav](docs/SPEC.md)
- [Plan og beslutninger](docs/PLAN.md)
- [Installasjon på Proxmox-LXC](docs/INSTALL-LXC.md)

<details>
<summary><h2>Hvordan det virker &nbsp;🔽</h2><hr></summary>

En kjøring går gjennom fem steg:

1. **Finne høringer.** Høringslisten på regjeringen.no (`/no/dokument/hoyringar/id1763/`) leses side for side, nyeste først. Første kjøring går 90 dager tilbake, senere kjøringer til forrige kjøring minus 14 dager. Åpne høringer sjekkes hver gang.
2. **Hente dokumenter.** Høringsside, høringsbrev, høringsnotat og vedlegg hentes med identifiserende User-Agent, minst 2 sekunder mellom kall, disk-cache og respekt for robots.txt. Et nytt eller endret dokument (ny sha256) gir ny vurdering. Endret frist og endrede dokumenter logges.
3. **Trekke ut tekst.** PDF-er leses side for side med `[[side N]]`-markører. Høringsbrevet hentes fra HTML.
4. **Vurdere.** LLM-en følger `prompts/assess_v1.md` og svarer med JSON som valideres med Pydantic.
   - Ugyldig svar gir ett nytt forsøk. Feiler det igjen, markeres vurderingen som feilet.
   - Store høringer deles per kapittel, og det høyeste relevansnivået gjelder.
   - Hvert sitat kontrolleres mot kildeteksten (rapidfuzz ≥ 95). Uverifiserte sitater markeres synlig.
5. **Lage rapport.** Jinja2 og WeasyPrint lager en A4-PDF med oversikt, frister som nærmer seg, detaljer for høy og middels relevans og én linje per høring med lav eller ingen relevans.

### Endringer siden forrige rapport

Har en høring med høy eller middels relevans vært med i en tidligere rapport, viser neste rapport hva som er endret:

- merker i oversikten, for eksempel «Ny frist», «Nytt dokument» og «Endret relevans»
- en boks med før og etter i analysen
- «Ny» på nye funn

Endret frist alene gir én linje i en egen seksjon. Høringer med lav eller ingen relevans vises ikke på nytt, og en ny vurdering med samme resultat regnes ikke som en endring.

### Backtest

Startes bare manuelt. Målvirksomhetens publiserte høringssvar brukes som fasit. Resultatet er recall, andel flagget, forvirringsmatrise og lister til manuell gjennomgang.

### Faste regler

- Metadata (tittel, datoer, frist, departement, status og adresseliste) kommer alltid fra HTML-en, aldri fra LLM-en.
- Ingen høring filtreres bort.

</details>

<details>
<summary><h2>Kommandoer &nbsp;🔽</h2><hr></summary>

Alle kommandoer kjøres som `uv run horingsradar <kommando>`.

| Kommando | Hva den gjør |
|---|---|
| `run` | Henter nye og endrede høringer og vurderer dem. `--no-assess` henter uten LLM. |
| `assess <id>` | Vurderer én høring på nytt, for eksempel med `--prompt-version v2`. |
| `report` | Lager PDF for høringer som ikke er rapportert før. `--week 2026-W42` lager en uke på nytt. `--weeks 6` tar med de siste seks ukene uten å merke noe som rapportert. |
| `backtest --split dev\|test` | Bare manuelt, med kostnadsestimat og bekreftelse. Valg: `--limit N`, `--from`/`--to`, `--estimate-only`, `--no-batch` og `--yes`. |
| `web` | Starter adminpanelet på `WEB_BIND`. |
| `hash-password` | Lager `WEB_PASSWORD_HASH` til `.env`. |
| `status`, `pause`, `resume` | Viser status, eller pauser og gjenopptar planlagt kjøring. |

Bare én jobb kjører om gangen. Alle jobber tar en felles fil-lås i `DATA_DIR`, enten de startes fra timer, panel eller terminal.

</details>

<details>
<summary><h2>Kom i gang (utvikling) &nbsp;🔽</h2><hr></summary>

```bash
uv sync
cp .env.example .env        # fyll inn ANTHROPIC_API_KEY m.m.
uv run pytest               # ingen nettverk, mocket LLM
uv run ruff check . && uv run ruff format .
```

PDF-rapporten bruker WeasyPrint, som trenger Pango:

- **Debian/Ubuntu:** `apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0`
- **Windows:** installer MSYS2 og kjør `pacman -S mingw-w64-x86_64-pango`. Koden finner `C:\msys64\mingw64\bin` automatisk. Ellers settes `WEASYPRINT_DLL_DIRECTORIES`.

</details>

<details>
<summary><h2>Tilgang utenfra (Tailscale) &nbsp;🔽</h2><hr></summary>

Adminpanelet lytter bare på adressene i `WEB_BIND`, og det er ingen portvideresending fra internett. For tilgang fra et annet sted kan Tailscale settes opp på én av to måter:

- **På Proxmox-verten** som subnet router for LAN-et. Anbefalt hvis Tailscale allerede kjører der. Containeren trenger ingenting ekstra, og `WEB_BIND` er bare LAN-adressen, for eksempel `192.168.1.50:8080`. Enheter i tailnettet når panelet på samme adresse.
- **Inne i containeren.** Den uprivilegerte containeren må få tilgang til `/dev/net/tun`, og Tailscale-adressen legges til i `WEB_BIND`, for eksempel `192.168.1.50:8080,100.x.y.z:8080`. Panelet kan binde adressen selv om `tailscaled` ikke er oppe ennå ved oppstart.

Begrens gjerne hvem i tailnettet som når port 8080 med Tailscale-ACL-er. Bruk aldri `tailscale funnel`, som åpner tjenesten mot internett.

</details>

<details>
<summary><h2>Struktur &nbsp;🔽</h2><hr></summary>

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

</details>

<details>
<summary><h2>Lisenser &nbsp;🔽</h2><hr></summary>

- Fontene i `src/horingsradar/report/fonts/` er Source Sans 3 (SIL Open Font License).
- htmx i `src/horingsradar/web/static/` er BSD Zero Clause License.

</details>
