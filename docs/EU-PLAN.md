# EU/EØS-radar – plan og status

Milepælene for EU/EØS-radaren. Krav i [`EU-SPEC.md`](EU-SPEC.md), arkitektur i [`EU-ARKITEKTUR.md`](EU-ARKITEKTUR.md).

Arbeidsform:
- Plan per milepæl, godkjent før koding.
- Alt arbeid på branchen `feature/eu-eos-kilder`. Ingenting til `main` før merge via pull request.
- Høringsradars kode og drift røres ikke. Endringer i felles kode skal være bakoverkompatible og dekkes av Høringsradars tester.

## Avklaringer

| Tema | Beslutning |
|---|---|
| Målgruppe og rapport | Egen målgruppe (internasjonale saker), egen PDF |
| Instans | Egen instans på samme LXC: egen checkout, `DATA_DIR`, database, `.env`, tjenester og panel |
| Kjøring | Ukentlig, mandag kl. 07:15 |
| Første kjøring | COM-forslag fra de siste 24 månedene, og alle åpne høringer på Have Your Say |
| Relevansskala | Samme fire nivåer som Høringsradar, satt av LLM-en |
| Sitater | Kort ordrett sitat fra tittel eller sammendrag, verifisert i kode |
| EDPS | Flagg, og minst middels (deterministisk regel, vises som regel) |
| Sektorer | Egen liste for EU-saker, foreslås i milepæl 3 |
| Forfilter for EUR-Lex | Filter B: 20 EuroVoc-emner, pluss nøkkelord i tittelen |
| Modell | Liten modell, satt i EU-instansens `.env` |

## Milepæler

| # | Innhold | Status |
|---|---|---|
| 0 | Oppsett og regler: `horingsradar-eu`, `RADAR=eu`-vern, `.env.eu.example`, `deploy/eu/`, regler i CLAUDE.md og SPEC, installasjonsguide | Ferdig |
| 1 | EUR-Lex SPARQL: ekte svar som fixtures, egen datamodell, henting med EuroVoc-filter | Ferdig |
| 2 | EEA-Lex: EØS-status per CELEX | Ikke startet |
| 3 | LLM-vurdering: egen prompt, eget skjema, sektorliste, sitatsjekk, EDPS-regel | Ikke startet |
| 4 | Egen PDF-rapport | Ikke startet |
| 5 | Have Your Say og kobling mot EUR-Lex | Ikke startet |
| 6 | EDPS som relevanssignal | Ikke startet |
| 7 | OEIL-status (valgfritt) | Ikke startet |
| 8 | Panel og statistikk | Ikke startet |
| 9 | Backtest med recall-sett | Ikke startet |

## Milepæl 0: Oppsett og regler

- Ny pakke `src/horingsradar/eu/` med kommandoen `horingsradar-eu` (`run`, `report`, `backtest`, `status`, `pause`, `resume`). `run` er et skall som tar låsen og registrerer jobben. `report` og `backtest` sier fra at de ikke er bygget ennå.
- `eu/config.py` utvider de felles innstillingene med `RADAR`, `EU_INITIAL_LOOKBACK_DAYS` og `EU_PROMPT_VERSION`. `horingsradar-eu` nekter å starte uten `RADAR=eu`.
- `.env.eu.example` med egen `DATA_DIR`, port 8081, egen timer og liten modell.
- `deploy/eu/`: ukentlig `horingsradar-eu-run.service/.timer` som bare kan skrive til `/var/lib/horingsradar-eu`, og `deploy/eu/deploy.sh` som bare oppdaterer EU-instansen.
- Regler i `CLAUDE.md` og `SPEC.md`, oppdatert `EU-SPEC.md`, ny [`INSTALL-EU.md`](INSTALL-EU.md).
- Utenfor `eu/` er bare `pyproject.toml` (ny kommando) og dokumentasjon endret. Høringsradars kode er uendret.

## Milepæl 1: EUR-Lex

- `eu/sources/eurlex.py`: spørringer mot Cellar SPARQL-endepunktet og tolking av svarene. Bruker pilotens `Fetcher` uendret (JSON bestilles med `format`-parameteren).
- Henting i vinduer på 30 dager: én spørring over 24 måneder tok 121 sekunder på et kaldt endepunkt, over tidsgrensen på 60 sekunder.
- `eu/db.py`: `eu_case`, `eu_case_source`, `eu_case_change` og `eu_eurovoc`, bare i EU-instansens database. Alle forslag lagres, også de som filtreres bort.
- `eu/prefilter.py`: filter B og nøkkelordene. Forkortelser (AI, NIS, eID, IoT) må stå med store bokstaver som egne ord.
- Prosedyrenummer og status (`PROP_PENDING`, `PROP_ADOPTED`, `PROP_REJECTED`, `WITHDRAWN`) for kandidater, slått opp på nytt til prosedyren er avsluttet. Statusendringer logges.
- Rettelser og tilleggsdeler samles under forslaget sitt.
- Testdata i `tests/fixtures/eurlex/`: ekte svar for september og oktober 2026, recall-settet, prosedyrer (også et trukket forslag) og EuroVoc-navn.

Funn underveis:
- EØS-merkingen i EUR-Lex mangler på NIS2, eIDAS2, AI Act og CSAM. Den brukes ikke som filter.
- Cellar har ingen sammendrag for forslag. Teksten til LLM-en avklares i milepæl 3.
- Ekte kjøring 29.10.2026 (24 måneder): 610 forslag, 578 saker etter sammenslåing av varianter, 112 kandidater, 42 kall, under 2 minutter.
