# Høringsradar – samlet plan for milepæl 1–8

Denne planen dekker alle milepælene i `docs/SPEC.md`.

## Avklaringer

| Tema | Beslutning |
|---|---|
| LLM | Anthropic Claude via Messages API med httpx, uten SDK-avhengighet. Standardmodell `claude-sonnet-5-5`. Batch-API brukes for backtest. |
| Arbeidsform | Én samlet plan, deretter autonomt. |
| Første `run` | Henter og vurderer høringer publisert de siste 90 dagene (`INITIAL_LOOKBACK_DAYS=90`). |
| Verifisering lokalt | Kun mock-LLM. Pango er installert via MSYS2, så PDF testes også på Windows. |
| Sektorer | Utgangslisten i SPEC brukes som den er. Den ligger ett sted i koden (`llm/schema.py`), så den er enkel å endre. |
| LXC | Debian 13, uprivilegert container. Fjerntilgang ordnes utenfor containeren (se README). |

## Funn fra ekte sider (fixtures i `tests/fixtures/regjeringen/`)

- **robots.txt** sperrer `?sortby=`, `?type=`, `?from=`, `?to=`, `?consterm=` og `/Utilities/RSSEngine/*`.
  - Listesiden sorterer allerede på dato som standard, så vi bruker bare `?page=N`.
  - RSS brukes ikke, fordi feeden ligger under en sperret sti.
- **Listesiden** har én `li.listItem` per høring, med feltene `span.date` (publisert), `span.type`, `span.department`, `p.excerpts`, `span.event-details` (Høringsfrist/Høyringsfrist) og `span.event-details-open` (Status).
- **Statusverdier**: `På høring`/`På høyring`, `Under behandling`, `Ferdigbehandlet`/`Ferdigbehandla`. Normaliseres til `open`, `under_behandling`, `ferdigbehandlet` eller `unknown`.
- **Høringssiden** har `div.article-info` med type, dato og departement, eventuelt også `Opprinnelig utgitt av: …`. `div.horing-meta` har status og frist.
- **Faktabokser** identifiseres med stabile ID-er:
  - `horingsbrev`: brevet som HTML. Kan inneholde PDF-lenker.
  - `horingsnotater`: PDF-er. Kan også være selve notatet som HTML.
  - `horingsinstanser`: adresselisten.
  - `horingssvar`: lenke til `?showSvar=true`.
  - `horingsresultat`: ignoreres.
- **Publiserte høringssvar** hentes fra `?showSvar=true` (`ul[data-horingssvar-list] li`). «(uten merknader)» lagres separat i `dt_response_no_comments`.
- **Hoder**: HTML-sider har ingen ETag eller Last-Modified, så de caches på tid. PDF-er har ETag og Last-Modified, så de hentes betinget.

## Arkitektur

```
src/horingsradar/
  config.py          .env-lasting + Pydantic-validering
  db.py              SQLite-skjema og spørringer
  lock.py            fil-lås i DATA_DIR (fcntl/msvcrt)
  jobs.py            jobbregister, logg per jobb, fremdrift, SIGINT → cancelled
  http.py            Fetcher: User-Agent, ≥2 s mellom kall, disk-cache, robots.txt (med wildcard)
  sources/regjeringen/  parser for liste, høring og høringssvar + discovery
  extract.py         PyMuPDF side for side med [[side N]], HTML-tekst
  pipeline.py        run: oppdag → oppdater → hent dokumenter → trekk ut → vurder
  llm/client.py      Anthropic Messages + Batches (eneste sted med LLM-kall)
  llm/schema.py      Pydantic-modeller for svaret + JSON-skjema til structured outputs
  llm/prompt.py      lasting av prompts/assess_vN.md
  assess.py          input-bygging, oppdeling per kapittel, sammenslåing, lagring
  quotes.py          normalisering + rapidfuzz-verifisering
  report/            Jinja2 + WeasyPrint, fonter i repo
  backtest.py        prepare → estimat → bekreftelse → batch → metrikker/CSV
  web/               FastAPI-panel, Basic auth, CSRF, HTMX
  cli.py             argparse-kommandoer
prompts/assess_v1.md
deploy/              systemd-filer og deploy.sh
docs/INSTALL-LXC.md  installasjonsguide for Proxmox-LXC
```

## Milepæler

1. **Parser**: `sources/regjeringen/parse.py` mot lagrede fixtures. Tester for bokmål og nynorsk, `Opprinnelig utgitt av`, statusnormalisering, adresseliste, dokumentlenker, inline høringsnotat og høringssvar.
2. **Henting og `run` uten LLM**:
   - `http.py` med throttling (felles klokke), disk-cache (`DATA_DIR/http_cache`), betinget GET og robots-sjekk.
   - Datamodell i SQLite.
   - `run` blar i listesider til eldste dato på siden er før grensen. Grensen er 90 dager ved første kjøring, ellers siste vellykkede discovery minus 14 dager.
   - Høringer med status `open` (og alle nye) sjekkes på nytt hver kjøring. Ny sha256 på et dokument gir ny vurdering og merket «oppdatert».
   - Kjøringen er idempotent.
3. **Tekstuttrekk**:
   - `[[side N]]` før hver side, tekst lagres i `DATA_DIR/text/`.
   - Sider med under 50 tegn telles. Er over halvparten av sidene slik, logges en advarsel om skannet dokument.
   - Høringsbrev og inline-notat fra HTML.
4. **LLM og `assess <id>`**:
   - Prompt v1 på bokmål og `output_config.format` med JSON-skjema. Svaret valideres med Pydantic. Ved ugyldig svar gjøres ett nytt forsøk; feiler det igjen, settes status `failed`.
   - Dokumenter merkes `D1…Dn` i prompten, og modellen refererer til dem.
   - Er input større enn `MAX_INPUT_TOKENS`, deles den per kapittel (PDF-ens innholdsfortegnelse eller nummererte overskrifter). Høyeste relevansnivå gjelder.
   - Sitater verifiseres på oppgitt side ±1 og deretter i hele dokumentet, med `partial_ratio` ≥ 95. Står sitatet på en annen side, rettes sidetallet og det logges.
   - Ved refusal brukes `fallbacks: "default"`, og modellen som faktisk svarte lagres.
5. **Backtest**:
   - `backtest --split dev|test [--limit N] [--from --to] [--yes] [--estimate-only]`.
   - `prepare` (discovery, henting, uttrekk og fasit) koster ingenting. Deretter vises estimatet, som er kalibrert mot faktiske tokens fra tidligere vurderinger når slike finnes.
   - Kjøringen starter først etter bekreftelse og går via Batch-API. Svar caches per input-hash.
   - Output er CSV og `summary.json` + `summary.md` i `DATA_DIR/backtests/<id>/`.
   - Selve backtesten kjøres ikke av meg. Den startes manuelt av deg.
6. **Rapport**:
   - Uten argumenter tas alle høringer med `tracked=1` som ikke er rapportert, pluss de som er vurdert på nytt siden.
   - `--week YYYY-Www` regenererer uken etter publiseringsdato og endrer ikke `reported_in`.
   - Fem seksjoner som i SPEC. Fonter (Source Sans 3, OFL) ligger i repoet.
7. **Jobber og panel**:
   - Felles fil-lås og `job`-tabell. `run --triggered-by timer` blir `skipped` ved pause eller tatt lås.
   - FastAPI-panel med status, handlinger, høringer, rapporter, statistikk, backtest (estimat → bekreftelse) og logg.
   - Panelet starter CLI-en med `subprocess` og sender SIGINT ved avbryt.
8. **Drift**: systemd-filer (`horingsradar-run.service/.timer`, `horingsradar-web.service`), `deploy/deploy.sh`, README og `docs/INSTALL-LXC.md`.

## Tillegg til datamodellen i SPEC

- **`hearing`**:
  - `tracked`: oppdaget av `run`, med i driftsrapporter.
  - `doc_type_raw`, `address_list` (JSON), `dt_response_no_comments`.
  - `content_changed_at`: når et dokument sist fikk ny hash.
- **`document`**: `section` (faktaboks), `etag`, `last_modified`, `fetch_status`, `note`.
- **`assessment`**: `input_chars`, `cost_estimate`, `batch`, `error`, `chunk_count`.
- **`backtest_run`**: `status`, `metrics` (JSON).
- **`job`**: `progress` (JSON), `args`.

## Status (23.10.2026)

Milepæl 1–4 og 6–8 er ferdige og i drift på lab. Milepæl 5 (backtest) er bygget og testet, men kjøringene gjenstår. De startes manuelt av administratoren.

### Lagt til etter den opprinnelige planen

- **Validering av egne anbefalinger:**
  - Bare modellens egen stemme avvises («vi anbefaler», «juristene bør»).
  - «Det anbefales» og «utvalget anbefaler» er tillatt som gjengivelse av kilden.
  - «Datatilsynet bør/burde» er tillatt når det står i høringsdokumentene. Det sjekkes mot kildeteksten i `assess.check_against_sources`.
- **Rapporten:**
  - Hver rad i oversikten hopper til analysen («Gå til analyse»), og analysene lenker tilbake.
  - Sidetall i funn, leseguide og personvernkapittel lenker til PDF-en med `#page=N`. Det gjelder også i panelet.
- **Endringer siden forrige rapport:**
  - Tabellen `hearing_change` logger endringer i frist og dokumenter. Status spores ikke.
  - Tabellen `report_item` lagrer hvilken vurdering og frist hver rapport viste. Det er grunnlaget for sammenligningen.
  - Høy og middels med en ny vurdering som har endret relevans, funn eller dokumenter, vises igjen med:
    - merker i oversikten,
    - en boks med før og etter,
    - «Ny» på nye funn.
  - Endret frist alene gir én linje i en egen seksjon.
  - Lav og ingen vises ikke igjen.
  - En ny vurdering med samme resultat regnes ikke som en endring.
  - Ingen ekstra LLM-kall.
- **Adminpanelet:**
  - Nytt utseende: blekk på arkivpapir, relevans som eneste farge, Source Sans 3 fra repoet.
  - Responsivt for mobil.
  - Radarstripe på statussiden.
  - Høringslisten kan filtreres på relevans og fritekst. Filteret bruker GET og endrer ingenting.
  - Høringslisten kan sorteres på publisert, relevans og frist, begge veier (`?sorter=`):
    - Høringer uten verdien det sorteres på, står alltid nederst.
    - Stigende frist viser kommende frister først og deretter passerte, sist passerte først.
    - Like verdier står nyeste først.
- **Drift:**
  - `DATA_DIR` gjøres absolutt.
  - Tydelig feil hvis databasen ikke kan åpnes. Panelet sjekker dette ved oppstart.
  - Installasjonsguiden har blokker som er trygge å kopiere og lime inn, og nevner ikke Tailscale (se README).

### Gjenstår

1. Tilpass sektorlisten (`llm/schema.py` og `report/build.py`, TODO i SPEC).
2. Backtest på dev-settet:
   - Start med `--limit 20`, deretter hele 2024.
   - Iterer med `prompts/assess_v2.md` osv.
   - Lås prompten og kjør testsettet (2025).
3. Sett terskelen for «andel flagget» etter dev-kjøringen (SPEC, Suksesskriterier).
4. Mulige forbedringer som er diskutert, men ikke bestemt:
   - endringsmerker og tidslinje også i panelet,
   - kostnadstak per måned,
   - batch-API for daglig `run`,
   - løpende treffsikkerhet i drift (svar fra Datatilsynet registreres etter fristen),
   - sammenligning av promptversjoner side om side,
   - backtest-resultater i panelet,
   - `report --reset`.

## Ting som bevisst ikke er med

- OCR, andre kilder, e-post og UI for jurister (jf. SPEC).
- Word-vedlegg (.docx/.odt) leses ikke. De registreres som dokument med merknaden «ikke lest», og det vises i rapporten.
- Lenker til andre nettsteder (for eksempel hoering.udir.no) følges ikke.
