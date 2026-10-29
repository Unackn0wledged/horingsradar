# Høringsradar

Pilot / proof of concept. En daglig jobb henter nye høringer fra regjeringen.no, lar en LLM vurdere personvernrelevans og lager en PDF-rapport som videresendes manuelt til brukerne i piloten. Et enkelt adminpanel på LAN lar administratoren styre jobbene og se status. Kjører i en Debian-LXC på privat lab. Ikke produksjon.

En egen EU/EØS-radar (EU-forslag og EU-høringer, egen målgruppe og egen rapport) bygges i `src/horingsradar/eu/` og kjører som egen instans med egen database. Se `docs/EU-ARKITEKTUR.md`.

Krav og detaljer står i `docs/SPEC.md` (Høringsradar) og `docs/EU-SPEC.md` (EU/EØS-radar). Les den som gjelder før du jobber med datamodell, parsing, LLM-vurdering, rapport eller backtest.

## Harde regler

- Høringsradar har én kilde: regjeringen.no. EU/EØS-radaren har kildene i `docs/EU-SPEC.md`. Ikke implementer andre kilder. Kildespesifikk kode ligger i egne moduler.
- Ingen e-post, varsler eller tilbakemeldingsfunksjon i piloten. Output er PDF-filer på disk. Eneste web-UI er adminpanelet for administratoren (se SPEC); ingen UI for brukerne.
- LLM-en skal aldri skrive høringsinnspill, foreslå standpunkt eller vurdere om et forslag er godt eller dårlig. Den beskriver hva som foreslås og hvor det står. Modellen gir aldri egne anbefalinger til brukerne («vi anbefaler» o.l. avvises). Å gjengi hva høringsdokumentene selv sier er tillatt, også «det anbefales» og råd rettet til målvirksomheten («… bør») når det står i kildeteksten (sjekkes i kode).
- Metadata (tittel, publisert, frist, departement, status, adresseliste) parses fra HTML. LLM-en er aldri kilde til dato, frist eller departement. For EU/EØS-radaren gjelder det samme for referanse, datoer, frister og EØS-status.
- Høringsradar: ingen høring filtreres bort. Alle vurderes og vises i rapporten; lav og ingen relevans som én linje med begrunnelse.
- EU/EØS-radaren: forfiltrering uten LLM er tillatt og skal prioritere recall. Antall som filtreres bort, telles og vises i statistikken. Det som vurderes, vises i rapporten som over.
- Hvert funn har dokument, kapittel/side og ordrett sitat. Sitatet verifiseres i kode mot kildeteksten. Uverifiserte sitater markeres synlig i rapporten, aldri stille fjernet eller rettet.
- EU/EØS-radaren: LLM-en leser begrunnelsen («explanatory memorandum») fra den engelske PDF-en av forslaget. Hvert funn har side og ordrett sitat fra begrunnelsen, verifisert i kode som over.
- EU/EØS-radaren: en EDPS-uttalelse om forslaget gir minst middels. Regelen er deterministisk, vises i rapporten som regel, og både LLM-ens og endelig nivå lagres. Ingen annen kode endrer nivået LLM-en har satt.
- Ved tvil mellom to relevansnivåer velges det høyeste.
- Kun offentlige dokumenter sendes til LLM-en.
- Backtest startes kun manuelt av brukeren, via CLI eller adminpanelet, alltid med kostnadsestimat og bekreftelse. Den skal aldri kalles fra `run`, systemd-timer, cron eller annen automatikk, og backtest-vurderinger skal aldri havne i driftsrapportene.

## Stack

Python 3.12, uv (`pyproject.toml` + `uv.lock`), httpx, selectolax, PyMuPDF, Pydantic, sqlite3 (stdlib), Jinja2 + WeasyPrint, rapidfuzz, pytest, ruff. Adminpanel: FastAPI + uvicorn, Jinja2-maler og HTMX (vendret i repoet, ingen CDN). Spør før du legger til andre avhengigheter.

## Kommandoer

- `uv sync` – installer avhengigheter
- `uv run pytest` – tester (ingen nettverk)
- `uv run ruff check . && uv run ruff format .`
- `uv run horingsradar run` – hent nye og endrede høringer og vurder dem
- `uv run horingsradar assess <id>` – vurder én høring på nytt (prompt-iterasjon)
- `uv run horingsradar report` – PDF for alle høringer som ikke er rapportert før
- `uv run horingsradar backtest --split dev|test` – kun manuelt; viser kostnadsestimat og krever bekreftelse
- `uv run horingsradar web` – start adminpanelet lokalt
- `uv run horingsradar-eu run [--no-assess]|assess <sak>|report|status|pause|resume|web` – EU/EØS-radaren. Krever `RADAR=eu` i `.env` (se `.env.eu.example`). `horingsradar-eu hash-password` lager passord-hash til EU-panelet.

<!-- CLI-kommandoene finnes ikke før milepæl 2–6 er bygget. Oppdater lista når de endres. -->

## Konvensjoner

- Kode, identifikatorer, kommentarer og commit-meldinger på engelsk. Prompts og rapporttekst på norsk bokmål (kildedokumentene kan være nynorsk).
- Konfigurasjon via miljøvariabler i `.env` (gitignored), lastet i `config.py`. Hold `.env.example` og `.env.eu.example` oppdatert. Hemmeligheter skal aldri i kode, logg eller commit.
- All tilstand ligger i `DATA_DIR` utenfor repoet (default `./data` lokalt, `/var/lib/horingsradar` på lab; EU/EØS-radaren har egen `DATA_DIR`, `/var/lib/horingsradar-eu` på lab): SQLite, HTTP-cache, nedlastede dokumenter, uttrukket tekst, rå LLM-svar og rapporter.
- Prompts er versjonerte filer i `prompts/` (`assess_v1.md`, `assess_v2.md` osv.; EU/EØS-radaren `assess_eu_v1.md` osv.). Endre aldri en eksisterende versjon; lag en ny. Modell og promptversjon lagres med hver vurdering.
- Alle LLM-kall går gjennom `llm/client.py`. Leverandør og modell velges med `LLM_PROVIDER` og `LLM_MODEL`.
- LLM-svar valideres mot Pydantic-modeller. Ugyldig svar gir ett nytt forsøk; feiler det igjen, logges det og vurderingen markeres som feilet. Ikke fyll inn manglende felter selv.
- Forespørsler mot regjeringen.no: identifiserende User-Agent fra config, minst 2 sekunder mellom kall, disk-cache, respekter robots.txt.
- Høringer identifiseres med regjeringen.no-ID-en (tallet i `/idNNNNNNN/`). Alle steg skal være idempotente.
- Alle jobber (`run`, `report`, `backtest`, `assess`) tar en felles fil-lås i `DATA_DIR`. Kun én jobb om gangen per instans, uansett om den startes fra timer, panel eller terminal.
- Adminpanelet starter jobber ved å kjøre CLI-en som egen prosess. Ingen pipeline-kode kjører i web-prosessen, og panelet styrer aldri systemd.
- Tester bruker lagrede HTML- og PDF-fixtures i `tests/fixtures/` og mocket LLM. Ingen tester mot nett eller ekte API.
- Parseren må tåle både bokmål og nynorsk (`Høringsfrist`/`Høyringsfrist`, `På høring`/`På høyring`) og datoformat `dd.mm.yyyy`.

## Arbeidsform

- Bygg i milepælsrekkefølgen i `docs/SPEC.md`. Lag en plan for hver milepæl og få den godkjent før du koder.
- EU/EØS-radaren: milepælene i `docs/EU-PLAN.md`, bare på branchen `feature/eu-eos-kilder`. Aldri commit EU-arbeid til `main`. Endringer i felles kode skal være bakoverkompatible og dekkes av Høringsradars tester.
- Kjør `uv run pytest` og `uv run ruff check .` før du sier at noe er ferdig.
- Ikke gjett på strukturen til kildene (HTML, JSON, SPARQL-svar). Hent et ekte svar, lagre det som fixture og bygg parseren mot det. Sjekk robots.txt og bruksvilkår før en ny kilde tas i bruk.

## Drift

Debian-LXC på Proxmox. systemd-timer kjører kun `run`, daglig; `report` og `backtest` kjøres manuelt. Adminpanelet kjører som egen systemd-tjeneste. Kun utgående trafikk. WeasyPrint krever Pango fra apt; følg WeasyPrints installasjonsguide for Debian. Deploy på LXC: `git pull --ff-only && uv sync --frozen`.

EU/EØS-radaren kjører på samme LXC som egen instans: kode i `/opt/horingsradar-eu`, data i `/var/lib/horingsradar-eu`, egne systemd-filer i `deploy/eu/systemd/` (ukentlig `run`) og eget deploy-skript `deploy/eu/deploy.sh`. Se `docs/INSTALL-EU.md`. Pilotens filer i `deploy/systemd/` og `deploy/deploy.sh` brukes ikke for EU-instansen.
