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
| Tekst til LLM-en | Begrunnelsen («explanatory memorandum») fra den engelske PDF-en |
| Sitater | Ordrett sitat med sidetall fra begrunnelsen, verifisert i kode (endret i milepæl 3) |
| EDPS | Flagg, og minst middels (deterministisk regel, vises som regel) |
| Sektorer | Egen liste med 15 sektorer (`eu/schema.py`) |
| Første vurdering på lab | Gradvis: `run --no-assess`, `assess` på noen få, så `run` |
| Forfilter for EUR-Lex | Filter B: 20 EuroVoc-emner, pluss nøkkelord i tittelen |
| EØS-status | Alternativ A: registrert eller ikke fra EEA-Lex-listen, full status for vedtatte rettsakter |
| Modell | Liten modell, satt i EU-instansens `.env` |

## Milepæler

| # | Innhold | Status |
|---|---|---|
| 0 | Oppsett og regler: `horingsradar-eu`, `RADAR=eu`-vern, `.env.eu.example`, `deploy/eu/`, regler i CLAUDE.md og SPEC, installasjonsguide | Ferdig |
| 1 | EUR-Lex SPARQL: ekte svar som fixtures, egen datamodell, henting med EuroVoc-filter | Ferdig |
| 2 | EEA-Lex: EØS-status fra listen og faktaark for vedtatte rettsakter | Ferdig |
| 3 | LLM-vurdering av begrunnelsen: egen prompt, eget skjema, sektorliste, sitatsjekk | Ferdig (EDPS-regelen kommer med EDPS-data i milepæl 6) |
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

## Milepæl 2: EEA-Lex

- `eu/sources/eealex.py`: listen sortert etter forslagsdato og faktaark for vedtatte rettsakter. Egen tilkobling med høyst 2 omdirigeringer, pilotens `Fetcher` ellers uendret.
- EU-instansen har minst 5 sekunder mellom alle kall (`REQUEST_INTERVAL_SECONDS`), slik EEA-Lex ber om i robots.txt. Lavere verdier avvises.
- Nye felt i `eu_case` (EEA-Lex-nøkkel, status, om den er utledet, område, JCD, sjekket), lagt til med `ALTER TABLE` så eksisterende databaser får dem.
- Status bare for kandidater. Statusendringer logges. 429, omdirigeringsløkke eller nektelse stopper steget uten å gjette.
- Testdata i `tests/fixtures/eealex/`: to listesider, faktaark for GDPR (i kraft) og AI Act (under vurdering), robots.txt.

Funn underveis:
- Faktaark for forslag (numeriske nøkler) sender videre til seg selv, og å følge løkken ga 429. Status for forslag utover «registrert» er derfor ikke tilgjengelig. Valgt: alternativ A.
- robots.txt sperrer filtrerte lister og søk, og sperrer ClaudeBot (Anthropics søkerobot for trening). EU-radaren har egen User-Agent og er ikke ClaudeBot.

## Milepæl 3: LLM-vurdering

- `eu/documents.py`: den engelske PDF-en for kandidater (SPARQL finner den, pilotens `Fetcher` henter den), uttrekk med `[[side N]]`, ny tabell `eu_document`.
- `eu/text.py`: begrunnelsen er sidene fra «EXPLANATORY MEMORANDUM» til siden der rettsakten begynner; kuttes over 90 000 tegn.
- `eu/schema.py`, `eu/assess.py`, `prompts/assess_eu_v1.md`: relevans, begrunnelse, sammendrag, sektorer og funn med side og ordrett sitat. Gjenbruker pilotens LLM-klient, promptlasting, personvernkategorier, avvisning av egne anbefalinger og sitatsjekk, uten å endre dem.
- Nye tabeller `eu_assessment` (med både LLM-ens og endelig nivå, for EDPS-regelen) og `eu_finding`.
- `horingsradar-eu run --no-assess` og `horingsradar-eu assess <saks-ID, COM-nummer eller CELEX>`.
- Testdata: to ekte forslag i `tests/fixtures/eurlex/pdf/` (rådsbeslutningen COM(2026) 558, og de første 17 sidene av EU KIDS Act gjenskapt med originalteksten for å holde filen liten).
- En test i Høringsradar (`test_replaced_document_is_one_changed_document`) var ustabil på raske maskiner: rapporten og endringene fikk samme millisekund. Rettet i testen; Høringsradars kode er uendret.
- Ekte kjøring uten LLM 29.10.2026: alle 112 PDF-er hentet på 11 minutter (5 sekunder mellom kall). Begrunnelsen funnet i 111; den siste manglet overskriften og startet med «CONTEXT OF THE PROPOSAL», som nå også gjenkjennes. Ingen LLM-kall er gjort lokalt.
