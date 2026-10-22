# Høringsradar – spesifikasjon (pilot)

## Formål

Spare juristene tid på å oppdage, sortere og lese høringer fra regjeringen.no som har betydning for personvern. Verktøyet er ikke skrivestøtte og skal ikke produsere høringsinnspill.

PoC-en skal dokumentere effekt med tall (se Suksesskriterier), ikke bare se bra ut.

## Omfang

Med i piloten:
- Kilde: høringer på regjeringen.no
- Daglig innhenting og vurdering
- PDF-rapport som hentes ut manuelt og videresendes til pilotjuristene
- Backtest mot historiske høringer
- Enkelt adminpanel på LAN for administratoren

Utenfor piloten:
- Andre kilder (direktorater, særorganer, Stortinget, EU)
- E-post, hastevarsler, web-UI for juristene, tilbakemeldingsknapper
- Skrivestøtte og utkast til høringsinnspill

## Datakilde: regjeringen.no

### Listeside

`https://www.regjeringen.no/no/dokument/hoyringar/id1763/`, paginert med `?page=N`. Sortering på dato: `sortby=1`. Volum: ca. 250–300 høringer per år.

Hver oppføring har tittel, URL, publiseringsdato, dokumenttype, departement, ingress, høringsfrist og status. Listesiden er primærkilde for metadata.

RSS finnes (se `/no/om-nettstedet/hjelp/rss/id476715/`) og kan brukes til å oppdage nye høringer, men verifiser hva feeden inneholder før noe bygges på den.

### Høringsside

Inneholder:
- høringsbrev (HTML)
- vedlegg: høringsnotat, eventuelt lov- eller forskriftsutkast, andre dokumenter
- adresseliste, vanligvis nederst i høringsbrevet
- for avsluttede høringer: publiserte høringssvar med avsendernavn

Kjente variasjoner:
- Bokmål og nynorsk i etiketter: `Høringsfrist`/`Høyringsfrist`, `Høring`/`Høyring`, `På høring`/`På høyring`
- `Opprinnelig utgitt av: <departement>` når ansvaret er flyttet mellom departementer. Lagre begge.
- Statusverdier inkluderer `På høring` og `Under behandling`. Lagre rå streng og en normalisert verdi.
- Datoformat `dd.mm.yyyy`

Strukturen må verifiseres mot ekte sider og lagres som fixtures før parseren skrives.

### Endringer

Høringer kan få nye vedlegg, endret frist eller ny status. Åpne høringer sjekkes på nytt ved hver kjøring. Et nytt eller endret vedlegg (ny sha256) utløser ny vurdering, og rapporten markerer høringen som «oppdatert».

## Datamodell (SQLite)

**hearing**
- id (regjeringen.no-ID), url, title, department, original_department, doc_type, ingress
- published_date, deadline, status_raw, status
- dt_on_address_list (bool)
- dt_responded (bool eller null; brukes i backtest)
- first_seen, last_checked, reported_in (report_id eller null)

**document**
- id, hearing_id, url, title, kind (horingsbrev | horingsnotat | lovutkast | annet), sha256, page_count, text_path

**assessment**
- id, hearing_id, created_at, model, prompt_version, status (ok | failed)
- context (drift | backtest), backtest_run_id (null i drift)
- relevance (hoy | middels | lav | ingen), relevance_reason, sectors (json), summary
- privacy_section (json: om det finnes, og referanse)
- reading_guide (json: sideintervaller per dokument og totalt sidetall)
- input_tokens, output_tokens, raw_response_path

**finding**
- id, assessment_id, document_id, category, description, chapter, page, quote, quote_verified (bool), match_score

**report**
- id, created_at, period_label, path, hearing_count

**backtest_run**
- id, created_at, split, period_from, period_to, model, prompt_version, hearing_count, estimated_cost, actual_input_tokens, actual_output_tokens, results_path

**job**
- id, kind (run | report | backtest | assess), triggered_by (timer | cli | web), started_at, finished_at
- status (running | ok | failed | cancelled | skipped), pid, log_path, summary (json)

**setting**
- key, value. Brukes blant annet til `scheduled_runs_paused`.

Rapporten bruker siste vurdering med `context = drift` per høring. Backtest-vurderinger leses aldri av `report`. Eldre vurderinger beholdes for sammenligning mellom promptversjoner.

## Tekstuttrekk

- PyMuPDF, side for side. Sett inn markøren `[[side N]]` før hver side, slik at modellen kan referere til sider.
- Høringsbrevet hentes fra HTML.
- PDF-er med lite eller ingen tekst per side (skannet) logges som advarsel. Ingen OCR i piloten.

## LLM-vurdering

### Input

Metadata, høringsbrev, høringsnotat og eventuelt lov- eller forskriftsutkast, med sidemarkører. Andre vedlegg tas med hvis det er plass innenfor `MAX_INPUT_TOKENS`.

Dokumenter som er større enn budsjettet deles per kapittel (ut fra overskrifter eller innholdsfortegnelse). Hver del vurderes for seg, med høringsbrev og eventuelt sammendrag inkludert. Funn slås sammen, og høyeste relevansnivå gjelder.

### Output (Pydantic-skjema)

- `relevance`: hoy | middels | lav | ingen
- `relevance_reason`: 1–3 setninger, beskrivende
- `sectors`: 1–3 verdier fra sektorlisten
- `summary`: 3–5 setninger om hva som foreslås, beskrivende
- `findings`: liste med `category`, `description`, `document`, `chapter`, `page`, `quote` (ordrett, maks ca. 40 ord)
- `reading_guide`: relevante sideintervaller per dokument
- `privacy_section`: om høringsnotatet har et eget kapittel om personvern eller personvernkonsekvenser, med referanse

### Relevansskala

- **hoy**: Forslaget etablerer eller endrer behandlingsgrunnlag eller hjemmel for behandling av personopplysninger, registre, deling eller sammenstilling, unntak fra taushetsplikt, overvåking eller kontroll, automatiserte avgjørelser eller profilering, eller behandling av særlige kategorier. Også endringer i personvernregelverket eller i Datatilsynets rolle.
- **middels**: Forslaget medfører behandling av personopplysninger, men uten ny eller endret hjemmel, eller påvirker personvern indirekte i vesentlig grad.
- **lav**: Personvern berøres perifert.
- **ingen**: Personopplysninger berøres ikke.

Ved tvil velges høyeste nivå.

### Funnkategorier

`behandlingsgrunnlag_hjemmel`, `register`, `deling_sammenstilling`, `taushetsplikt`, `overvaking_kontroll`, `automatiserte_avgjorelser_ki`, `saerlige_kategorier`, `barn`, `lagring_sletting`, `rettigheter_innsyn`, `informasjonssikkerhet`, `testing_med_personopplysninger`, `personvernregelverk_tilsyn`, `annet`

### Sektorer

TODO: tilpass til den interne ansvarsfordelingen. Utgangspunkt:

`helse_omsorg`, `justis_politi`, `arbeid_velferd`, `utdanning_forskning`, `barn_familie`, `finans_skatt`, `digitalisering_forvaltning`, `samferdsel_kommunikasjon`, `innvandring_integrering`, `forsvar_sikkerhet`, `naering`, `kultur_medier`, `kommune_bolig`, `annet`

### Regler i prompten

- Beskriv hva som foreslås. Ikke vurder om det er bra, ikke foreslå standpunkt, ikke skriv høringsinnspill.
- Siter ordrett fra kildeteksten. Ikke parafraser i `quote`-feltet.
- Oppgi side ut fra sidemarkørene. Ikke gjett.
- Skriv på bokmål.
- Ingen høringer fra backtest-settet brukes som eksempler i prompten.

### Sitatverifisering

1. Normaliser kildetekst og sitat: whitespace, orddeling med bindestrek ved linjeskift, ligaturer, typografiske anførselstegn.
2. Søk på oppgitt side ±1, deretter i hele dokumentet.
3. rapidfuzz `partial_ratio` ≥ 95 gir verifisert. Lagre scoren.
4. Funnet på en annen side enn oppgitt: rett sidetallet og logg det.
5. Ikke funnet: `quote_verified = false`, vises med tydelig varsel i rapporten.

## Rapport (PDF)

Som standard tar rapporten med alle høringer som ikke har vært i en tidligere rapport, pluss høringer som er oppdatert siden. `--week YYYY-Www` regenererer en bestemt periode.

Innhold:
1. **Topptekst**: periode, tidspunkt for generering, modell og promptversjon, og en merknad om at innholdet er KI-generert sortering som må verifiseres mot kilden.
2. **Oversiktstabell**, sortert på relevans og deretter frist: relevans, tittel, departement, publisert, frist, dager igjen, sektor, om Datatilsynet står på adresselisten.
3. **Frister som nærmer seg**: åpne høringer med høy eller middels relevans fra tidligere rapporter, med frist innen 28 dager.
4. **Detaljer for høy og middels**: tittel med lenke, metadata, sammendrag, funn (kategori, beskrivelse, dokument, kapittel og side, sitat, verifiseringsstatus), leseguide (for eksempel «s. 22–41 og 88–95 av 140»), og om høringsnotatet har eget personvernkapittel.
5. **Lav og ingen**: én linje per høring med kort begrunnelse.

Utforming:
- A4, Jinja2-mal med CSS, generert med WeasyPrint.
- Fonter med åpen lisens ligger i repoet, slik at PDF-en blir lik lokalt og på lab.
- Relevansnivå vises med tekst, ikke bare farge.
- Alle lenker er klikkbare.

## Backtest

Backtesten måler om modellen og prompten rangerer høringer riktig, med Datatilsynets faktiske høringssvar som fasit. Den tester verktøyet, ikke Datatilsynet.

### Når

- Kun manuelt, startet av brukeren med `horingsradar backtest` eller fra adminpanelet. Aldri fra `run`, systemd-timer, cron eller annen automatikk.
- Aktuelt før piloten starter (kalibrering), og når modell eller promptversjon byttes. Uten slike endringer gir en ny kjøring i praksis samme resultat. Utover det: sjelden, for eksempel årlig.

### Sikring

- Før kjøring vises antall høringer, estimert antall tokens og estimert kostnad. Kjøringen krever bekreftelse; `--yes` hopper over den.
- `--limit N` kjører på et utvalg for å teste oppsettet billig.
- Vurderinger lagres med `context = backtest` og en `backtest_run_id`. De påvirker aldri `reported_in` eller driftsrapportene.
- Resultater skrives til `DATA_DIR/backtests/<run_id>/`.

### Oppsett

- **Periode**: avsluttede høringer 2024–2025 (ca. 575). Kan overstyres med `--from` og `--to`.
- **Fasit**: `dt_responded` er sann når Datatilsynet står blant de publiserte høringssvarene på høringssiden. Fasiten er støyete: noen svar sendes utenom skjemaet, og at Datatilsynet ikke svarte kan skyldes kapasitet.
- **Split**: 2024 er dev-sett og brukes til å justere prompt og skala. 2025 er testsett og kjøres først når prompten er låst. Tallene som rapporteres kommer fra testsettet.
- **Kjøring**: samme pipeline som i drift. Cache alt. Bruk batch-API hvis leverandøren tilbyr det.
- **Output**: CSV per høring og et kort sammendrag med:
  - recall for høy + middels mot `dt_responded`
  - fordeling per relevansnivå (andel flagget)
  - forvirringsmatrise
  - andel verifiserte sitater
  - leseguide: relevante sider delt på totalt sidetall
  - lister til manuell gjennomgang: høy relevans uten svar, og svar fra Datatilsynet men lav eller ingen relevans

## Suksesskriterier

- Recall ≥ 90 % for høy + middels mot høringer Datatilsynet svarte på (testsettet)
- Andelen som flagges som høy eller middels er lav nok til at sorteringen sparer tid. Terskel settes etter dev-kjøringen.
- Alle sitater er verifisert eller synlig markert
- Leseguiden peker på en klar minoritet av sidene i relevante høringer
- Tilbakemelding fra pilotjuristene etter 6–8 uker

## Adminpanel

Enkelt webgrensesnitt for administratoren, nådd via LAN-IP eller Tailscale. Ikke for juristene.

### Innhold

- **Status**: om planlagt kjøring er aktiv eller pauset, siste kjøring (tidspunkt, resultat, antall nye og endrede høringer, feil), neste planlagte kjøring, og eventuell jobb som kjører nå, med fremdrift.
- **Handlinger**:
  - Pause og gjenoppta planlagt kjøring
  - Kjør nå (`run`)
  - Generer rapport (`report`)
  - Backtest: velg split og eventuelt `--limit`, vis estimat (antall høringer, tokens, kostnad), start først etter eksplisitt bekreftelse
  - Avbryt kjørende jobb
  - Vurder én høring på nytt (`assess <id>`) fra høringslisten
- **Rapporter**: liste over genererte PDF-er med nedlasting.
- **Høringer**: siste høringer med relevans, departement, frist og lenke til regjeringen.no og til vurderingen.
- **Statistikk**:
  - høringer behandlet per uke
  - fordeling per relevansnivå (andel flagget)
  - andel verifiserte sitater og antall feilede vurderinger
  - API-forbruk per måned: tokens og estimert kostnad
  - siste backtest: dato, modell, promptversjon, recall og andel flagget
- **Logg**: siste linjer fra siste jobb.

Ikke med: redigering av prompts, konfigurasjon eller API-nøkkel, brukerhåndtering, tilgang for juristene.

### Teknisk

- FastAPI + uvicorn med server-renderte Jinja2-maler. HTMX ligger i repoet (ingen CDN) og brukes til automatisk oppdatering av status og jobbfremdrift. Ingen JS-byggesteg.
- Statistikk vises som tall, enkle tabeller og CSS-søyler, uten grafbibliotek.
- Pause er flagget `scheduled_runs_paused` i `setting`. Timeren fortsetter å fyre; `run` startet av timeren avslutter straks og registrerer jobben som `skipped` når flagget er satt. Manuell «Kjør nå» går uansett.
- Panelet kjører aldri pipeline-kode selv og styrer aldri systemd. Handlinger starter CLI-en som egen prosess med `triggered_by = web`. Hver jobb registreres i `job` med logg i `DATA_DIR/logs/`.
- Jobblåsen ligger i CLI-en (fil-lås i `DATA_DIR`) og gjelder alle inngangsveier. Panelet avviser nye jobber mens en kjører. Timer-startet `run` som finner låsen tatt, registreres som `skipped`.
- Avbryt sender SIGINT til jobbprosessen og markerer jobben `cancelled`. Allerede brukte tokens telles med i forbruket.
- Kostnad regnes fra faktiske tokens og priser satt i `.env` (`PRICE_INPUT_PER_MTOK`, `PRICE_OUTPUT_PER_MTOK`), og vises som estimat.

### Sikkerhet

- Bindes til LAN- og Tailscale-grensesnittet (`WEB_BIND`). Ingen portvideresending.
- HTTP Basic auth med brukernavn og passord-hash fra `.env`. Knappene koster penger, og alt på LAN-et kan nå panelet.
- Alle handlinger er POST med CSRF-token. GET endrer aldri tilstand.
- Panelet viser aldri API-nøkkelen eller annet innhold fra `.env`.

## Drift

- Debian-LXC på Proxmox, Python via uv.
- systemd service og timer kjører kun `run`, daglig. `report` og `backtest` kjøres manuelt.
- `horingsradar-web.service` kjører adminpanelet som tjenestebrukeren.
- Kun utgående trafikk: regjeringen.no, LLM-API, pakkeregistre.
- `.env` på LXC-en har `chmod 600` og eies av tjenestebrukeren.
- Logging til journald.

## Milepæler

1. Parser for listeside og høringsside mot lagrede fixtures, med tester.
2. Henting, cache, throttling, SQLite og idempotent `run` uten LLM.
3. PDF-tekstuttrekk med sidemarkører.
4. LLM-klient, prompt v1, skjemavalidering, sitatverifisering og `assess <id>`.
5. Backtest på dev-settet, iterasjon på prompten, låsing, deretter kjøring på testsettet.
6. PDF-rapport.
7. Jobbregister og jobblås, deretter adminpanelet.
8. Drift: systemd-filer (timer for `run` og tjeneste for panelet), deploy-skript og README.
