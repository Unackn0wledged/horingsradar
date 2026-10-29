# EU/EØS-radar – spesifikasjon (utkast)

> **Status: utkast. Ikke implementert.** Arkitekturen er besluttet i [`EU-ARKITEKTUR.md`](EU-ARKITEKTUR.md).
> Sitatregel og EDPS-regel ble avklart 29.10.2026.

## Formål

En egen radar for EU-lovforslag og EU-høringer som kan være EØS-relevante og berører personvern. Det inkluderer tekniske forslag med personvernkonsekvenser (kryptering, logging, sikkerhetskrav, digital identitet, tilkoblede enheter o.l.). Rene informasjonssikkerhetssaker uten personvernaspekt skal ikke med i rapporten.

Radaren har **egen målgruppe** (brukere som jobber med internasjonale saker), **egen rapport** og **egen instans**. Den deler kodebase med Høringsradar, men ikke database, konfigurasjon, tjenester eller rapporter.

Rammer som gjelder som for Høringsradar:
- Pilot på hjemmelab med egen LLM-API-nøkkel (test, ikke produksjon).
- Formålet er å oppdage, sortere og lese. Verktøyet gir ikke skrivestøtte og lager ikke høringsinnspill.
- Ingen e-post og ingen fristvarsler i piloten.
- Backtest kjøres aldri automatisk, bare manuelt fra panelet eller CLI.

## Regler for EU/EØS-radaren

Disse gjelder som i Høringsradar:
- LLM-en skriver aldri innspill, foreslår ikke standpunkt og vurderer ikke om et forslag er godt eller dårlig. Den gir ingen egne anbefalinger til brukerne.
- Metadata (tittel, referanse, datoer, frister, EØS-status) hentes fra kildene. LLM-en er aldri kilde til dato, frist, referanse eller EØS-status.
- Ved tvil mellom to relevansnivåer velges det høyeste.
- Bare offentlige dokumenter sendes til LLM-en.
- LLM-svar valideres mot Pydantic-modeller, med ett nytt forsøk ved ugyldig svar.
- Alle LLM-kall går gjennom `llm/client.py`. Prompts er versjonerte filer som aldri endres.
- Forespørsler bruker identifiserende User-Agent, ventetid mellom kall, disk-cache og respekterer robots.txt.

Disse er annerledes, og må skrives inn i `CLAUDE.md` og SPEC på branchen før koding:
- **Flere kilder enn regjeringen.no.** Kildespesifikk kode ligger i egne moduler.
- **Forfiltrering er tillatt** (Høringsradar filtrerer ingenting bort). Filteret skal prioritere recall. Antall saker som filtreres bort, telles og vises i statistikken, og recall-settet i backtesten kontrollerer filteret.
- **Sitater fra begrunnelsen, med sidetall.** Cellar har ingen sammendrag av forslagene, så LLM-en leser begrunnelsen i forslaget (se LLM-vurdering). Hvert funn har side og et kort ordrett sitat på engelsk, verifisert i kode mot sidene (med `quotes.py`, som i Høringsradar). Uverifiserte sitater markeres synlig, aldri stille fjernet eller rettet. Sidetallet rettes bare når sitatet står på en annen side. (Endret 29.10.2026 fra «sitat fra tittel eller sammendrag».)
- **EDPS-regel.** En sak med EDPS-uttalelse om forslaget får minst `middels`. Regelen er deterministisk og vises i rapporten som en regel, ikke som modellens vurdering. Det er den eneste regelen som endrer nivået LLM-en har satt, og den kan bare heve det.

## Utviklingsflyt

EU/EØS-radaren utvikles på en egen branch og skal ikke påvirke `main` eller pilotinstansen før den er ferdig testet.

- **Branch:** `feature/eu-eos-kilder`, opprettet fra oppdatert `main`.
- **Aldri commit eller push til `main`** i dette arbeidet. Alle endringer ligger på branchen.
- Små, avgrensede commits med beskrivende meldinger. Push jevnlig.
- **Hold branchen oppdatert:** når `main` endres, kjør `git merge main` på branchen og løs konflikter der.
- **Offentlig repo:** dokumentasjon og commit-meldinger holdes nøytrale (ingen navn på virksomhet eller personer).

### Egen instans i stedet for feature-flagg

Det opprinnelige feature-flagget `eu_source_enabled` er tatt ut. Radaren har egen instans, så det å starte eller stoppe instansen er bryteren. Høringsradar kjører ingen EU-kode.

| | Høringsradar | EU/EØS-radar |
|---|---|---|
| Kode | `/opt/horingsradar` (`main`) | `/opt/horingsradar-eu` (branchen, senere `main`) |
| Data | `/var/lib/horingsradar` | `/var/lib/horingsradar-eu` |
| Konfigurasjon | `.env` | egen `.env` (eksempel: `.env.eu.example`) |
| Kommando | `horingsradar` | `horingsradar-eu` (krever `RADAR=eu`) |
| Tjenester | `horingsradar-run.timer`, `horingsradar-web` | `horingsradar-eu-run.timer`, `horingsradar-eu-web` |
| Systemd-filer og deploy | `deploy/systemd/`, `deploy/deploy.sh` | `deploy/eu/systemd/`, `deploy/eu/deploy.sh` |
| Panel | egen port | egen port, lenke mellom panelene |

- Databasemigreringer for EU/EØS-radaren gjelder bare dens egen database.
- Endringer i felles kode (`http.py`, `llm/client.py`, `jobs.py`, `lock.py` o.l.) skal være bakoverkompatible og dekkes av Høringsradars tester.
- EU-instansen har egne systemd-filer og eget deploy-skript i `deploy/eu/`. De ligger utenfor `deploy/systemd/`, så pilotens deploy-skript installerer dem aldri. EU-tjenesten kan bare skrive til `/var/lib/horingsradar-eu`.

### Merge til main

1. Branchen er oppdatert mot `main`, og hele testsuiten er grønn.
2. EU/EØS-instansen har kjørt på lab med ekte data.
3. Pull request fra `feature/eu-eos-kilder` mot `main`. Gå gjennom diffen, særlig endringer i felles kode.
4. Snapshot av containeren før piloten deployes med den nye `main`.
5. Etter deploy: verifiser at Høringsradar kjører som før (statussiden, en kjøring, en rapport).

Tilbakerulling: rollback til snapshot, eller sjekk ut forrige versjon (for eksempel taggen `pilot-presentasjon`) i `/opt/horingsradar`.

## Kjøring

- **Ukentlig**, mandag kl. 07:15 (etter Høringsradars daglige kjøring). `report` og `backtest` startes manuelt.
- **Første kjøring:** COM-forslag fra de siste 24 månedene (`EU_INITIAL_LOOKBACK_DAYS=730`) og alle høringer på Have Your Say som er åpne.
- Senere kjøringer går tilbake til forrige kjøring minus 60 dager (`EU_DISCOVERY_OVERLAP_DAYS`), fordi EuroVoc-indekseringen kommer dager eller uker etter at et forslag er publisert.
- Forslag hentes i vinduer på 30 dager. Én spørring over flere måneder tar minutter på et kaldt endepunkt, over tidsgrensen, mens et vindu på 30 dager svarer på rundt ett sekund. Første kjøring tar rundt 2 minutter.

## Designprinsipp: billig først, LLM sist

LLM brukes bare til relevansvurdering av et forfiltrert utvalg. Henting, filtrering, kobling mellom kilder og oppslag av EØS-status gjøres deterministisk uten LLM.

```
hent → forfiltrer → dedupliser/koble (CELEX/COM) → LLM-vurdering → EØS-status → rapport
```

## Kilder

| Kilde | Innhold | Tilgang | Rolle |
|---|---|---|---|
| EUR-Lex / Cellar | COM-forslag, vedtatte rettsakter, EuroVoc-indeksering, EØS-merking | SPARQL-endepunkt `publications.europa.eu/webapi/rdf/sparql` (åpent, tillatt i robots.txt). EUR-Lex' egne nettsider er bak bot-beskyttelse og hentes ikke; lenkene er for mennesker | Primærkilde for forslag |
| Have Your Say (Kommisjonen) | Initiativer, «call for evidence», offentlige konsultasjoner, tilbakemeldingsperioder | JSON-endepunkter bak nettsiden (ikke offisielt dokumentert) | Primærkilde for høringer |
| EEA-Lex (EFTA) | EØS-status per rettsakt, inkl. forslag med mulig EØS-relevans | Listen sortert etter forslagsdato, og faktaark per CELEX for vedtatte rettsakter (skraping, 5 sekunder mellom kall). Se EØS-status | Berikelse: EØS-status |
| EDPS | Uttalelser om lovforslag som berører personvern | RSS / nettside | Sterkt relevanssignal |
| Legislative Observatory (OEIL) | Prosedyrestatus i Europaparlamentet | RSS / prosedyresider | Valgfri berikelse: status |

**Må verifiseres før implementering** (hent ekte svar og lagre som fixtures, som for regjeringen.no):
- Have Your Say: faktiske JSON-endepunkter, paginering og felter (tema, type, tilbakemeldingsperiode).
- ~~EEA-Lex~~: verifisert 29.10.2026, se EØS-status.
- ~~EUR-Lex SPARQL~~: verifisert 29.10.2026, se Forfiltrering og [`EU-PLAN.md`](EU-PLAN.md).
- OEIL: om fagområdekodene for personvern og cybersikkerhet kan brukes som filter i RSS.
- Robots.txt og bruksvilkår for skraping, særlig EEA-Lex og Have Your Say.

## Forfiltrering (uten LLM)

**Prinsipp: forfiltreringen prioriterer recall, ikke presisjon.** Saker som filtreres bort her, når aldri LLM-en og blir aldri synlige. Filteret skal derfor også slippe gjennom tekniske og sikkerhetsrelaterte saker. LLM-vurderingen avgjør om saken har personvernrelevans. Ekstra kandidater koster lite. Tapte saker koster mye.

**EUR-Lex:** alle COM-forslag (forordning, direktiv, beslutning) hentes og lagres. Et forslag er kandidat hvis det har ett av EuroVoc-emnene under (filter B), eller et nøkkelord i tittelen.

| Gruppe | EuroVoc-emner (ID) |
|---|---|
| Personvern og sikkerhet | data protection (5181), personal data (5595), protection of privacy (2828), information security (c_04ae3ba8), computer crime (3027), electronic signature (6370), data processing (4488), data-processing law (3028), artificial intelligence (3030), biometrics (7410) |
| Bredere digitalt | digital technology (7219), Internet (6088), cross-frontier data flow (3011), data transmission (4502), information storage (3024), data collection (6030), disclosure of information (5334), exchange of information (616), e-Health (c_31da5694), digital single market (c_6f7bbe63) |

- Emnene i det opprinnelige utkastet («data security», «computer security», «electronic identification», «information technology») finnes ikke slik i EuroVoc. Listen over er sjekket mot hvilke emner recall-settet faktisk har. Cyber Resilience Act fanges for eksempel bare av information security, computer crime og digital technology.
- Hele recall-settet fanges av filteret. Over 24 måneder (29.10.2026) gir det 112 kandidater av 578 forslag. Emnet exchange of information (616) alene gir mange svake kandidater; vurder å justere etter milepæl 3.
- **EØS-merkingen («Text with EEA relevance») lagres, men brukes ikke som filter.** NIS2, eIDAS2, AI Act og CSAM har den ikke. EEA-Lex er fasiten.
- Forslag som ennå ikke har fått EuroVoc-emner, vurderes på tittelen og ses på nytt i senere kjøringer (overlappen på 60 dager).
- Når et forslag først er kandidat, forblir det kandidat, selv om emnene endres senere.

**Have Your Say:** filtrer på tema
- «Justice and fundamental rights»
- «Digital economy and society»

Temaene er brede. Nøkkelordfilteret på tittel og sammendrag er derfor obligatorisk for Have Your Say, ikke et supplement.

**EDPS:** alle nye uttalelser om lovforslag regnes automatisk som kandidater.

**Nøkkelordliste** (konfigurerbar i EU-instansens `.env` eller en egen fil, engelsk). Høringsradar har ingen nøkkelordmekanisme i dag, så denne er ny:
`personal data, data protection, privacy, GDPR, ePrivacy, surveillance, monitoring, tracking, biometric, facial recognition, profiling, identity, eID, digital identity wallet, age verification, data sharing, data access, data space, data retention, metadata, logging, encryption, end-to-end, lawful access, cybersecurity, NIS, incident reporting, connected devices, IoT, cloud, AI, algorithm, automated decision, interoperability, health data, location data, vehicle data, smart meter, child sexual abuse detection`

## Kobling og deduplisering

- Nøkler: CELEX-nummer og COM-nummer (f.eks. `COM(2023) 905`). Have Your Say-initiativer kan mangle begge. Koble i så fall på tittel eller referanse når forslaget senere publiseres i EUR-Lex.
- Én sak = én post i databasen, med lenker til alle kilder den er funnet i.
- Rettelser (`52025PC0828R(01)`) og tilleggsdeler (`52025PC0101(01)`) har eget CELEX-nummer, men hører til samme sak som forslaget (grunn-CELEX uten tillegg). De lagres som kilder til saken, en ny variant logges som endring, og EuroVoc-emnene slås sammen. Forslagets egne felt (tittel, dato) går foran variantens.
- Et treff i EDPS vises i rapporten og gir minst `middels` (se EDPS-regel under Regler).

## LLM-vurdering

- **Input: tittel, metadata og begrunnelsen («explanatory memorandum»)** fra den engelske PDF-en av forslaget, ikke rettsakten selv. Begrunnelsen har kontekst, rettsgrunnlag, høringer og konsekvensutredning, grunnleggende rettigheter og forklaring av bestemmelsene.
- PDF-en finnes med SPARQL (første item i den engelske PDF-manifestasjonen) og hentes gjennom pilotens `Fetcher`. Teksten trekkes ut med pilotens PyMuPDF-uttrekk, med `[[side N]]`.
- Begrunnelsen er sidene fra «EXPLANATORY MEMORANDUM» (eller «CONTEXT OF THE PROPOSAL», som noen forslag starter med) til siden der rettsakten begynner («Proposal for a …» øverst på siden). Over 90 000 tegn kuttes den, og det står i prompten og i dokumentets merknad. Uten overskriften brukes de første sidene, og det merkes.
- Målt 29.10.2026 på alle 112 kandidater over 24 måneder: begrunnelsen funnet i 111, gjennomsnitt 26 000 tegn (3 000–90 000), én kuttet. Til sammen rundt 3 millioner tegn, omtrent 1 million input-tokens for første vurdering.
- Bare kandidater får hentet tekst. En sak uten engelsk PDF ennå venter og prøves igjen ved neste kjøring.
- **Skala: samme fire nivåer som Høringsradar** (`hoy`, `middels`, `lav`, `ingen`), satt av LLM-en. Ingen numerisk score.
- Output (JSON, validert med Pydantic): relevansnivå, kort begrunnelse, sektor og funn (se Sitater under Regler).
- **Sektor** (1–3) fra en egen liste for EU-saker, godkjent 29.10.2026: digitale tjenester og plattformer, cybersikkerhet, digital identitet og tillitstjenester, kunstig intelligens, dataøkonomi og datadeling, helse, justis/politi/rettshåndhevelse, grensekontroll/migrasjon/visum, finans og betaling, transport og kjøretøy, energi og miljø, arbeidsliv og trygd, forbrukervern og indre marked, internasjonale avtaler og samarbeid, annet (`eu/schema.py`).
- **Funn** bruker Høringsradars personvernkategorier.
- Relevans vurderes mot personvern. Prompten sier eksplisitt at tekniske forslag er relevante når de påvirker behandling av personopplysninger, for eksempel krav til eller svekkelse av kryptering, logging og lagring av metadata, sikkerhetskrav til systemer som behandler personopplysninger, avviks- og hendelsesrapportering som omfatter personopplysninger, digital identitet, sporing, tilkoblede enheter og datadeling.
- Bare saker som er ren informasjonssikkerhet uten kobling til personopplysninger, vurderes som `ingen`. Ved tvil velges det høyeste nivået.
- **EDPS:** en EDPS-uttalelse vises som et eget flagg og gir minst `middels` (se EDPS-regel under Regler). Både LLM-ens nivå og det endelige nivået lagres, så det er sporbart hva regelen har endret.
- Egen prompt `prompts/assess_eu_v1.md`, versjonert som før (lastes som versjon `eu_v1`). Versjonen velges med `EU_PROMPT_VERSION`. Prompten nevner ingen virksomhet.
- Ugyldig svar gir ett nytt forsøk, deretter lagres vurderingen som feilet. Etter to feilede forsøk venter saken til teksten endres. Avvisningen av modellens egne anbefalinger er den samme som i Høringsradar.
- Liten, billig modell via `LLM_MODEL` i EU-instansens `.env`. Forventet volum er noen titalls kandidater i uka.
- En sak vurderes når den blir kandidat og har tekst; den vurderes ikke på nytt med mindre teksten endres eller `assess` kjøres for hånd. Input-hash, modell, promptversjon, tokens, kostnadsestimat og rått svar lagres med hver vurdering.

## EØS-status (uten LLM)

**Hva EEA-Lex gir en automatisk klient** (sjekket 29.10.2026):
- robots.txt tillater `/eea-lex/`, ber om 5 sekunder mellom kall og sperrer filtrerte lister (`?f[`) og søk (`?keyword=`). EU-instansen holder minst 5 sekunder mellom alle kall.
- **Listen sortert etter forslagsdato** (60 rader per side) har EEA-Lex-nøkkel, COM-nummer og tittel, men ikke status.
- **Forslag** har numeriske nøkler (for eksempel `329709` for COM(2026) 569). Faktaarkene deres sender videre til seg selv og hentes ikke. Vi prøver ikke å komme rundt det.
- **Vedtatte rettsakter** har CELEX som nøkkel. Faktaarkene deres gir status, rettslig status, område i EØS-avtalen og JCD.

**Slik settes status for kandidater:**
1. Listen leses fra de nyeste forslagene og bakover til alle rader på en side er eldre enn den eldste kandidaten som fortsatt kan endre status (høyst 40 sider). Kobling på COM-nummer.
2. **I listen med numerisk nøkkel:** «Proposed act with possible EEA relevance». Statusen er utledet av at forslaget står i listen, og merkes som utledet i rapporten.
3. **I listen med CELEX-nøkkel:** status, område og JCD fra faktaarket.
4. **Ikke i listen:** «Ikke registrert i EEA-Lex», men bare når listen ble lest helt. Det er ikke det samme som at saken ikke er EØS-relevant.
5. Statusendringer logges. «I kraft» og «ikke lenger i kraft» slås ikke opp igjen.
6. Ved 429, omdirigeringsløkke (høyst 2 omdirigeringer) eller annen nektelse stopper EEA-Lex-steget for kjøringen. Resten av kjøringen fullføres, og ingenting gjettes.

| Kode | Status i EEA-Lex |
|---|---|
| `PROPOSED` | Proposed act with possible EEA relevance |
| `SCRUTINY` | Act under scrutiny by EEA EFTA |
| `DRAFT_JCD` | Draft Joint Committee Decision (JCD) under consideration |
| `JCD_PENDING` | Entry into force of Joint Committee Decision (JCD) pending |
| `IN_FORCE` | Incorporated into the EEA Agreement and in force |
| `NO_LONGER_IN_FORCE` | Incorporated into the EEA Agreement but no longer in force |
| `NOT_REGISTERED` | Ikke registrert i EEA-Lex |

EUs egen «EEA relevant»-merking betyr ikke at EFTA-statene vurderer saken som relevant. EEA-Lex er fasiten. Status for forslag utover «registrert» krever tilgang til faktaarkene for forslag; det kan legges til hvis EFTA gir slik tilgang.

## Rapport

**Egen PDF**, ikke en seksjon i Høringsradars rapport. Skrives til EU-instansens rapportmappe. Samme visuelle stil og fonter. Per sak:
- tittel og referanse (COM/CELEX)
- kilde(r) med lenker
- publiseringsdato og eventuell tilbakemeldingsfrist (Have Your Say)
- relevansnivå, begrunnelse og sektor
- EØS-status fra EEA-Lex
- flagg hvis EDPS har avgitt uttalelse

Sortering som i Høringsradar: relevans og deretter frist. Lav og ingen relevans som én linje med begrunnelse.

## Panel og statistikk

- Eget panel for EU-instansen på egen port, med samme oppbygning som Høringsradar (status, saker, rapporter, statistikk, backtest, jobber). Lenke til det andre panelet.
- Ingen bryter for kilden i panelet. Panelet endrer ikke konfigurasjon.
- Statistikk per kilde: antall hentet, antall etter forfiltrering, antall vurdert, antall relevante, LLM-kall og tokens.

## Backtest

- Bare manuell kjøring, med kostnadsestimat og bekreftelse.
- Fast **recall-sett** av kjente, tekniske forslag med personvernrelevans som skal fanges opp. Minimum: NIS2, Cyber Resilience Act, eIDAS2 / EU Digital Identity Wallet, CSAM-forordningen («chat control»), European Health Data Space, Data Act og AI Act.
- Backtesten rapporterer hvilke saker som falt ut, og i hvilket steg (forfilter eller LLM).
- Merk: sakene i recall-settet er velkjente og trolig kjent for modellen fra treningen. Settet tester derfor mest forfilteret. Supplér gjerne med mindre kjente saker.

## Implementeringsrekkefølge

0. Branch. Nye regler i `CLAUDE.md` og SPEC (flere kilder, forfiltrering, sitatregel, EDPS-regel). Kommandoen `horingsradar-eu`, `.env.eu.example`, systemd-filer og deploy-skript i `deploy/eu/`. Status føres i [`EU-PLAN.md`](EU-PLAN.md).
1. EUR-Lex SPARQL-henting med EuroVoc-filter. Egen datamodell i egen database.
2. EEA-Lex: liste over registrerte forslag, og faktaark for vedtatte rettsakter.
3. LLM-vurdering av begrunnelsen med egen prompt, eget skjema og egen sektorliste.
4. Egen PDF-rapport.
5. Have Your Say-henting og kobling mot EUR-Lex-saker.
6. EDPS som relevanssignal.
7. OEIL-status, valgfritt.
8. Panel og statistikk.
9. Backtest med recall-sett.

## Ikke i scope

- Fulltekstanalyse av alle forslag
- Automatisk innsending eller utkast til innspill
- E-post og fristvarsler
- Ren informasjonssikkerhet uten personvernaspekt, i rapporten. Slike saker skal fortsatt slippe gjennom forfilteret og vurderes av LLM-en.
- Andre EU-kilder (Rådets register, komitologiregisteret) – vurderes etter piloten
- Kobling mot Høringsradar – vurderes senere (se arkitekturvalget)
