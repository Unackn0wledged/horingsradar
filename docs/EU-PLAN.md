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
| Første kjøring | COM-forslag fra de siste 6 månedene (endret 31.10.2026 fra 24 måneder; nok til å verifisere funksjonaliteten i piloten). Tallene fra 29.10.2026 under gjelder 24 måneder |
| Relevansskala | Samme fire nivåer som Høringsradar, satt av LLM-en |
| Tekst til LLM-en | Begrunnelsen («explanatory memorandum») fra den engelske PDF-en |
| Sitater | Ordrett sitat med sidetall fra begrunnelsen, verifisert i kode (endret i milepæl 3) |
| EDPS | Flagg, og minst middels (deterministisk regel, vises som regel). Kilde: Rådets dokumenter i Cellar (edps.europa.eu svarer 403), koblet på prosedyre, ellers helt lik forslagstittel. Ingen manuell liste (valgt 29.10.2026) |
| Sektorer | Egen liste med 15 sektorer (`eu/schema.py`) |
| Første vurdering på lab | Gradvis: `run --no-assess`, `assess` på noen få, så `run` |
| Rapport | Saker eldre enn 8 uker som én linje; samme regler for gjentak som Høringsradar |
| Forfilter for EUR-Lex | Filter B: 20 EuroVoc-emner, pluss nøkkelord i tittelen |
| EØS-status | Alternativ A: registrert eller ikke fra EEA-Lex-listen, full status for vedtatte rettsakter |
| Modell | Claude Sonnet 5.5 med `medium` innsats, som Høringsradar, satt i EU-instansens `.env` (endret 31.10.2026 fra liten modell: LLM-en leser hele begrunnelsen, ikke bare tittel og sammendrag) |
| Backtest | Ingen backtest for EU/EØS-radaren. Recall-settet brukes bare i testene for forfilteret (valgt 29.10.2026) |
| Panel | Eget panel på port 8081, med siden «Filtrert bort». Lenke til Høringsradars panel nå, lenken andre veien etter presentasjonen (valgt 29.10.2026) |
| OEIL | Enkel versjon: kort navn, status, komité og hovedhendelser fra prosedyresidene, ingen personnavn. Bare saker med høy eller middels relevans, og ikke vurderte (valgt 29.10.2026) |
| Have Your Say | Bare tilbakemeldingsfrist og lenke for EUR-Lex-kandidater, slått opp på COM-nummer. Høringer uten COM-forslag hentes ikke, og det kobles ikke på tittel (valgt 29.10.2026) |

## Milepæler

| # | Innhold | Status |
|---|---|---|
| 0 | Oppsett og regler: `horingsradar-eu`, `RADAR=eu`-vern, `.env.eu.example`, `deploy/eu/`, regler i CLAUDE.md og SPEC, installasjonsguide | Ferdig |
| 1 | EUR-Lex SPARQL: ekte svar som fixtures, egen datamodell, henting med EuroVoc-filter | Ferdig |
| 2 | EEA-Lex: EØS-status fra listen og faktaark for vedtatte rettsakter | Ferdig |
| 3 | LLM-vurdering av begrunnelsen: egen prompt, eget skjema, sektorliste, sitatsjekk | Ferdig |
| 4 | Egen PDF-rapport | Ferdig |
| 5 | Have Your Say: tilbakemeldingsfrister for EUR-Lex-kandidater | Ferdig |
| 6 | EDPS som relevanssignal, fra Rådets dokumenter i Cellar | Ferdig |
| 7 | OEIL-status for høy og middels | Ferdig |
| 8 | Panel og statistikk | Ferdig |
| 9 | Backtest med recall-sett | Holdt utenfor (29.10.2026) |

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

## Milepæl 4: Rapport

- `eu/report.py` og `eu/templates/eu_report.html.j2`: egen rapport med Høringsradar-rapportens stil (kopiert), fonter og PDF-skriver (gjenbrukt uten endring).
- Seksjoner, regler for gjentak og «Tidligere forslag» som beskrevet i EU-SPEC (Rapport).
- Nye tabeller `eu_report` og `eu_report_item`, og kolonnen `eu_case.reported_in`.
- `horingsradar-eu report`.

Funn underveis:
- Høringsradar (piloten) ser ut til å ha samme hull som EU-rapporten nå tetter: en høring som sto under «Ikke vurdert» og senere får en vurdering, vises ikke igjen, fordi grunnlaget ikke har noen vurdering å sammenligne med. Ikke rettet i piloten før presentasjonen.

## Milepæl 5: Have Your Say

- `eu/sources/haveyoursay.py`: oppslag på COM-nummer (`findInitiativePathByReference`) og initiativet med faser og frister (`groupInitiatives/{id}`). Bruker pilotens `Fetcher` uendret, med 5 sekunder mellom kall.
- Nytt steg i `run` etter EEA-Lex: kandidater uten kobling slås opp, og initiativet kobles bare hvis en fase har samme COM-nummer. Uten treff slås forslaget opp igjen til det er 12 uker gammelt. Koblede initiativer hentes på nytt så lenge en fase er åpen eller kommende. Feil stopper bare dette steget.
- Nye tabeller `eu_hys_initiative` og `eu_hys_publication`, nye kolonner i `eu_case` (`hys_initiative_id`, `hys_status`, `hys_checked_at`) og kilden `hys` i `eu_case_source`. En periode som åpner eller en flyttet frist logges som `hys_feedback`.
- Rapporten viser fristen i oversikten og analysen, sorterer åpne frister først innen samme relevans, og gir én linje når fristen endres på en tidligere rapportert høy eller middels sak. `status` viser hvor mange kandidater som har en side og hvor mange som er åpne.
- Testdata i `tests/fixtures/haveyoursay/`: oppslag, robots.txt og tre initiativer (EU KIDS Act, Cloud and AI Development Act, Brussel Ia med planlagt fase), uten saksbehandlerfelt og bare med engelske vedlegg. Falskt endepunkt i `tests/eu_fakes.py`.

Funn underveis:
- Brukerne skal plukke opp EØS-relevante forslag. Høringer uten COM-forslag (call for evidence, konsultasjoner, gjennomføringsrettsakter) ble derfor valgt bort i denne omgang. Se «Ikke i scope» i EU-SPEC for tallene om de skal tas inn senere.
- Oppslaget på COM-nummer virker uansett tema. Temafilteret fra det opprinnelige utkastet ville mistet CSAM (Home affairs) og EHDS (Public health).
- Søkelisten er usortert og overlapper mellom sidene; den brukes ikke.
- Dokumentene for call for evidence er ofte DOCX, og nedlastingsruten for vedlegg er ikke verifisert.
- Noen titler har U+FFFD i selve kilden. Rapporten bruker EUR-Lex-tittelen.
- Initiativ-JSON-en navngir saksbehandlerne. Bare fasefeltene lagres.

## Milepæl 6: EDPS

- `eu/sources/edps.py`: SPARQL mot Cellar etter Rådets dokumenter som oversender EDPS-uttalelser, med prosedyre og forslagenes CELEX-nummer. Bruker EUR-Lex-klienten og pilotens `Fetcher` uendret.
- Nytt steg i `run` rett etter henting av forslag: dokumentene lagres i `eu_edps`, og koblingene i `eu_edps_link` (prosedyre, ellers helt lik tittel). Alle lagrede dokumenter prøves på nytt hver kjøring, så forslag som kommer senere også kobles.
- `eu/rules.py`: EDPS-regelen. Brukes når en vurdering lagres (`assess.py`), og som en ny vurderingsrad uten LLM-kall når uttalelsen kommer etter vurderingen. Bare hever, aldri senker; `llm_relevance` beholder modellens nivå.
- Et forslag med uttalelse blir kandidat (grunn «edps»), og endringen `edps` logges.
- Rapporten viser merket «EDPS», lenke til Rådets dokument, regelen når den har hevet nivået, antall kandidater med uttalelse og at listen ikke er fullstendig. `status` viser antallet.
- Testdata i `tests/fixtures/edps/`: ekte SPARQL-svar for 24 måneder (31 dokumenter), og de tre forslagene fra juni 2026 som tre av dem gjelder (Cloud and AI, Eurojust, Europol) med prosedyrer. I det falske endepunktet er EDPS av som standard, så eksisterende tester ikke endres.

Funn underveis:
- edps.europa.eu svarer 403 på alle kall fra radaren, også robots.txt. Sammendragene i EU-tidende og EDPS-hendelsene i Cellar stopper i 2023. Rådets dokumenter er eneste maskinlesbare kilde etter det, og de er ikke fullstendige.
- 14 av 31 dokumenter mangler prosedyre. Tittelen gjentar forslagets tittel ordrett og gir sikre treff (Eurojust, Europol, European Investigation Order).
- `procedure_ref` finnes bare for kandidater. Koblingen bruker derfor forslagenes CELEX-nummer fra samme prosedyre, hentet i samme spørring, så også forslag som ikke er kandidater kan kobles.
- European Investigation Order (omarbeiding, COM(2026) 313) slapp ikke gjennom forfilteret, men blir kandidat gjennom EDPS-uttalelsen. Vurder å se på nøkkelordlisten for strafferettslig samarbeid.

## Milepæl 7: OEIL

- `eu/sources/oeil.py`: henter og tolker prosedyresiden i OEIL (kort navn, status, ansvarlig komité, hovedhendelser). Personnavn leses ikke. Pilotens `Fetcher` uendret, 5 sekunder mellom kall.
- Nytt steg sist i `run`, etter vurderingen: kandidater med prosedyrenummer som er høy, middels eller ikke vurdert, og som ikke er avsluttet i OEIL. 404 prøves igjen neste kjøring.
- Nye kolonner `eu_case.oeil_ref`, `oeil_title`, `oeil_status`, `oeil_committees`, `oeil_checked_at`, ny tabell `eu_oeil_event`, og endringen `oeil_status`.
- Rapporten har raden «Europaparlamentet» og én linje ved ny status. `status` viser hvor mange kandidater som har OEIL-status.
- Testdata i `tests/fixtures/oeil/`: ekte sider for EU KIDS Act, Europol, AI Act (avsluttet, felles komité), CSAM (mange hendelser) og EUCCS (henvisning ikke avgjort), og en ekte 404-side. Navn på saksordførere, skyggeordførere og kommissær er erstattet med «NAVN Fjernet».

Funn underveis:
- OEIL har ingen robots.txt. Parlamentets juridiske merknad svarer 403 til radaren og ble lest for hånd av brukeren.
- Parlamentets Open Data API var ustabilt (500 på halvparten av kallene, 13 MB feilmelding) og mangler statusteksten.
- Nye NLE-prosedyrer finnes ikke i OEIL før Parlamentet skal gi samtykke; 8 av 22 kandidatprosedyrer i testdataene er NLE.
- Hovedhendelsene i OEIL tar ikke med trilogmøter. CSAM står fortsatt med «Awaiting Parliament's position in 1st reading».

## Milepæl 8: Panel og statistikk

- `src/horingsradar/eu/web/`: FastAPI-app med egne maler (status, saker, sak, filtrert bort, rapporter, statistikk, jobber og logg), egen jobbstart (`python -m horingsradar.eu run|run --no-assess|report|assess <id>`), server og statistikk.
- Gjenbrukt uendret fra Høringsradar: `web/auth.py`, `AuthMiddleware`, `runner.job_running` og `cancel_job`, `server.bind_socket`, `stats.next_scheduled_run`, `last_job` og `tail`, stilark, HTMX og fonter.
- `report._view` heter nå `case_view` og brukes av både rapporten og saksiden.
- Nye kommandoer `horingsradar-eu web` og `horingsradar-eu hash-password` (den siste trenger ikke `RADAR=eu`). Ny innstilling `OTHER_PANEL_URL`.
- `deploy/eu/systemd/horingsradar-eu-web.service` (bare skrivetilgang til `/var/lib/horingsradar-eu`), INSTALL-EU avsnitt 7.
- Tester i `tests/test_eu_web.py`: innlogging, alle sider med tom og fylt database, filtre og sortering, saksiden, «Filtrert bort», statistikk, CSRF, jobbstart (mocket), opptatt lås, nedlasting av rapport, at bare EU-jobber kan startes.

Funn underveis:
- Pilotens maler og jobbstart er bundet til høringene og `horingsradar`-kommandoen, så EU-panelet har egne. Utseendet er det samme.
- Med EDPS-testdataene blir Cloud and AI Development Act kandidat og får åpen frist på Have Your Say; fristfilteret viser derfor to saker.
