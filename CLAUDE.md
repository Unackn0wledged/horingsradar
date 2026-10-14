# Høringsradar

Pilot / proof of concept for Datatilsynet. En daglig jobb henter nye høringer fra regjeringen.no, lar en LLM vurdere personvernrelevans og lager en PDF-rapport som videresendes manuelt til jurister i piloten. Et enkelt adminpanel på LAN lar operatøren styre jobbene og se status. Kjører i en Debian-LXC på privat lab. Ikke produksjon.

Krav og detaljer står i `docs/SPEC.md`. Les den før du jobber med datamodell, parsing, LLM-vurdering, rapport eller backtest.

## Harde regler

- Eneste kilde er regjeringen.no. Ikke implementer andre kilder, men hold kildespesifikk kode i `sources/`.
- Ingen e-post, varsler eller tilbakemeldingsfunksjon i piloten. Output er PDF-filer på disk. Eneste web-UI er adminpanelet for operatøren (se SPEC); ingen UI for juristene.
- LLM-en skal aldri skrive høringsinnspill, foreslå standpunkt eller vurdere om et forslag er godt eller dårlig. Den beskriver hva som foreslås og hvor det står. Modellen gir aldri egne anbefalinger til juristene («vi anbefaler», «juristene bør» o.l. avvises). Å gjengi hva høringsdokumentene selv sier er tillatt, også «det anbefales» og «Datatilsynet bør» når det står i kildeteksten (sjekkes i kode).
- Metadata (tittel, publisert, frist, departement, status, adresseliste) parses fra HTML. LLM-en er aldri kilde til dato, frist eller departement.
- Ingen høring filtreres bort. Alle vurderes og vises i rapporten; lav og ingen relevans som én linje med begrunnelse.
- Hvert funn har dokument, kapittel/side og ordrett sitat. Sitatet verifiseres i kode mot kildeteksten. Uverifiserte sitater markeres synlig i rapporten, aldri stille fjernet eller rettet.
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

<!-- CLI-kommandoene finnes ikke før milepæl 2–6 er bygget. Oppdater lista når de endres. -->

## Konvensjoner

- Kode, identifikatorer, kommentarer og commit-meldinger på engelsk. Prompts og rapporttekst på norsk bokmål (kildedokumentene kan være nynorsk).
- Konfigurasjon via miljøvariabler i `.env` (gitignored), lastet i `config.py`. Hold `.env.example` oppdatert. Hemmeligheter skal aldri i kode, logg eller commit.
- All tilstand ligger i `DATA_DIR` utenfor repoet (default `./data` lokalt, `/var/lib/horingsradar` på lab): SQLite, HTTP-cache, nedlastede dokumenter, uttrukket tekst, rå LLM-svar og rapporter.
- Prompts er versjonerte filer i `prompts/` (`assess_v1.md`, `assess_v2.md` osv.). Endre aldri en eksisterende versjon; lag en ny. Modell og promptversjon lagres med hver vurdering.
- Alle LLM-kall går gjennom `llm/client.py`. Leverandør og modell velges med `LLM_PROVIDER` og `LLM_MODEL`.
- LLM-svar valideres mot Pydantic-modeller. Ugyldig svar gir ett nytt forsøk; feiler det igjen, logges det og vurderingen markeres som feilet. Ikke fyll inn manglende felter selv.
- Forespørsler mot regjeringen.no: identifiserende User-Agent fra config, minst 2 sekunder mellom kall, disk-cache, respekter robots.txt.
- Høringer identifiseres med regjeringen.no-ID-en (tallet i `/idNNNNNNN/`). Alle steg skal være idempotente.
- Alle jobber (`run`, `report`, `backtest`, `assess`) tar en felles fil-lås i `DATA_DIR`. Kun én jobb om gangen, uansett om den startes fra timer, panel eller terminal.
- Adminpanelet starter jobber ved å kjøre CLI-en som egen prosess. Ingen pipeline-kode kjører i web-prosessen, og panelet styrer aldri systemd.
- Tester bruker lagrede HTML- og PDF-fixtures i `tests/fixtures/` og mocket LLM. Ingen tester mot nett eller ekte API.
- Parseren må tåle både bokmål og nynorsk (`Høringsfrist`/`Høyringsfrist`, `På høring`/`På høyring`) og datoformat `dd.mm.yyyy`.

## Arbeidsform

- Bygg i milepælsrekkefølgen i `docs/SPEC.md`. Lag en plan for hver milepæl og få den godkjent før du koder.
- Kjør `uv run pytest` og `uv run ruff check .` før du sier at noe er ferdig.
- Ikke gjett på HTML-strukturen til regjeringen.no. Hent en ekte side, lagre den som fixture og bygg parseren mot den.

## Drift

Debian-LXC på Proxmox. systemd-timer kjører kun `run`, daglig; `report` og `backtest` kjøres manuelt. Adminpanelet kjører som egen systemd-tjeneste. Kun utgående trafikk. WeasyPrint krever Pango fra apt; følg WeasyPrints installasjonsguide for Debian. Deploy på LXC: `git pull --ff-only && uv sync --frozen`.
