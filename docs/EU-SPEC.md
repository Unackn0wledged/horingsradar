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
- **Sitater fra tittel eller sammendrag.** Høringsradar krever dokument, side og verifisert ordrett sitat per funn. EU/EØS-radaren vurderer tittel og sammendrag uten sidetall, så hvert funn har i stedet et kort ordrett sitat fra tittel eller sammendrag. Sitatet verifiseres i kode mot den teksten (med `quotes.py`). Uverifiserte sitater markeres synlig, aldri stille fjernet eller rettet.
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
- Senere kjøringer henter det som er nytt eller endret siden forrige kjøring, med overlapp.

## Designprinsipp: billig først, LLM sist

LLM brukes bare til relevansvurdering av et forfiltrert utvalg. Henting, filtrering, kobling mellom kilder og oppslag av EØS-status gjøres deterministisk uten LLM.

```
hent → forfiltrer → dedupliser/koble (CELEX/COM) → LLM-vurdering → EØS-status → rapport
```

## Kilder

| Kilde | Innhold | Tilgang | Rolle |
|---|---|---|---|
| EUR-Lex / Cellar | COM-forslag, vedtatte rettsakter, EuroVoc-indeksering, EØS-merking | SPARQL-endepunkt (åpent). Web service med gratis registrering | Primærkilde for forslag |
| Have Your Say (Kommisjonen) | Initiativer, «call for evidence», offentlige konsultasjoner, tilbakemeldingsperioder | JSON-endepunkter bak nettsiden (ikke offisielt dokumentert) | Primærkilde for høringer |
| EEA-Lex (EFTA) | EØS-status per rettsakt, inkl. forslag med mulig EØS-relevans | Faktaark per CELEX: `https://www.efta.int/eea-lex/<celex>` (skraping) | Berikelse: EØS-status |
| EDPS | Uttalelser om lovforslag som berører personvern | RSS / nettside | Sterkt relevanssignal |
| Legislative Observatory (OEIL) | Prosedyrestatus i Europaparlamentet | RSS / prosedyresider | Valgfri berikelse: status |

**Må verifiseres før implementering** (hent ekte svar og lagre som fixtures, som for regjeringen.no):
- Have Your Say: faktiske JSON-endepunkter, paginering og felter (tema, type, tilbakemeldingsperiode).
- EEA-Lex: at URL-mønsteret per CELEX er stabilt, og hvordan forslag uten CELEX identifiseres. Faktaark for forslag kan ha egne node-ID-er.
- EUR-Lex SPARQL: riktige predikater for EuroVoc-deskriptorer, dokumenttype og EØS-relevans-merking.
- OEIL: om fagområdekodene for personvern og cybersikkerhet kan brukes som filter i RSS.
- Robots.txt og bruksvilkår for skraping, særlig EEA-Lex og Have Your Say.

## Forfiltrering (uten LLM)

**Prinsipp: forfiltreringen prioriterer recall, ikke presisjon.** Saker som filtreres bort her, når aldri LLM-en og blir aldri synlige. Filteret skal derfor også slippe gjennom tekniske og sikkerhetsrelaterte saker. LLM-vurderingen avgjør om saken har personvernrelevans. Ekstra kandidater koster lite. Tapte saker koster mye.

**EUR-Lex:** filtrer i spørringen på
- dokumenttype: COM-forslag (lovgivningsforslag)
- EuroVoc-deskriptorer, minst: `data protection`, `protection of privacy`, `personal data`, `data security`, `computer security`, `electronic identification`, `information technology`
- valgfritt: merking «Text with EEA relevance»

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
- Et treff i EDPS vises i rapporten og gir minst `middels` (se EDPS-regel under Regler).

## LLM-vurdering

- Input: tittel, sammendrag/abstract og kildemetadata. Ikke fulltekst.
- Fulltekst hentes bare for saker som allerede er vurdert som relevante, og bare hvis det trengs for et kort sammendrag.
- **Skala: samme fire nivåer som Høringsradar** (`hoy`, `middels`, `lav`, `ingen`), satt av LLM-en. Ingen numerisk score.
- Output (JSON, validert med Pydantic): relevansnivå, kort begrunnelse, sektor og funn (se Sitater under Regler).
- **Sektor** fra en egen sektorliste for EU-saker (ikke Høringsradars norske liste). Listen foreslås og avklares i milepæl 3.
- Relevans vurderes mot personvern. Prompten sier eksplisitt at tekniske forslag er relevante når de påvirker behandling av personopplysninger, for eksempel krav til eller svekkelse av kryptering, logging og lagring av metadata, sikkerhetskrav til systemer som behandler personopplysninger, avviks- og hendelsesrapportering som omfatter personopplysninger, digital identitet, sporing, tilkoblede enheter og datadeling.
- Bare saker som er ren informasjonssikkerhet uten kobling til personopplysninger, vurderes som `ingen`. Ved tvil velges det høyeste nivået.
- **EDPS:** en EDPS-uttalelse vises som et eget flagg og gir minst `middels` (se EDPS-regel under Regler). Både LLM-ens nivå og det endelige nivået lagres, så det er sporbart hva regelen har endret.
- Egen prompt i `prompts/` (for eksempel `eu_assess_v1.md`), versjonert som før. Versjonen velges med `EU_PROMPT_VERSION`.
- Liten, billig modell via `LLM_MODEL` i EU-instansens `.env`. Forventet volum er noen titalls kandidater i uka.
- Cache resultater per sak og input-hash, så ingen sak vurderes to ganger med samme input.

## EØS-status (uten LLM)

For saker med CELEX: slå opp EEA-Lex og hent
- status, f.eks. «Proposed act with possible EEA relevance», «Act under scrutiny by EEA EFTA», «Draft JCD under consideration» eller «Incorporated»
- område i EØS-avtalen (vedlegg, f.eks. XI)

Saker uten CELEX får status «ukjent / ikke registrert i EEA-Lex». EUs egen «EEA relevant»-merking betyr ikke at EFTA-statene vurderer saken som relevant. EEA-Lex er fasiten.

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
2. EEA-Lex-oppslag per CELEX.
3. LLM-vurdering med egen prompt og eget skjema.
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
