
# Høringsradar – plan for nettside til brukerne

Status: M0–M3 er bygget. Neste er M4 (tilgang og lansering). Gjelder bare Høringsradar (regjeringen.no); EU/EØS-radaren er ikke med.

Installasjonsveiledningen er [INSTALL-NETTSIDE.md](INSTALL-NETTSIDE.md).

## Mål

Brukerne skal kunne slå opp høringer selv på en nettside, i stedet for at administratoren sender PDF-rapporter på e-post. Siden viser det samme som rapporten (relevans, begrunnelse, sammendrag, funn med kontrollerte sitater, sider å lese) og lar brukeren filtrere, søke, velge periode og laste ned en PDF av én høring med samme layout som rapporten.

Den ukentlige PDF-rapporten og adminpanelet fortsetter som i dag.

## Avgjørelser

| Tema | Valg | Begrunnelse |
|---|---|---|
| Hosting | Statisk side på **Cloudflare Pages** (gratis) | Ingen server å drifte eller sikre; ingen kode kjører på nettsiden |
| Domene | Eget domene, siden på et subdomene, f.eks. `horinger.<domene>` | Holder plass til andre tjenester senere uten å blande dem |
| Hvem lager siden | Lab-serveren, etter hver `run` | Data og vurderinger blir på lab; repoet er offentlig og skal ikke inneholde dem |
| Publisering | **Direkte opplasting** fra lab til Pages | Bare utgående trafikk fra lab, som i dag. Ikke Pages sin Git-integrasjon, siden dataene ikke ligger i Git |
| Tilgang | **Cloudflare Access** med engangskode på e-post, økt på 1 måned | Ingen passord å administrere; gratis opp til 50 brukere |
| Søk og filter | I nettleseren mot én JSON-fil | Noen hundre høringer er lite; ingen database trengs |
| PDF av én høring | Lages på lab med en **egen mal** basert på rapportmalen | Lik layout som rapporten; nettleserens «Skriv ut» blir ikke likt |
| Personlige valg | «Følger», «lest» og valgt periode lagres i nettleseren | Ingen brukerdata på server |
| Opplastingsverktøy | Cloudflares **`wrangler`**, kjørt fra `publish` | Den støttede måten; krever Node.js på lab (godkjent i M0) |
| Periode | `SITE_WEEKS` = **26 uker**, alltid alle åpne høringer | Avgjort i M0 |

Lab-serveren eksponeres aldri. Adminpanelet blir på LAN.

### Avgjørelser i M0 (2026-11-14)

1. Regelen «ingen UI for brukerne» i `CLAUDE.md` og `docs/SPEC.md` er endret: en lesbar, statisk nettside bak innlogging er tillatt. Fortsatt ingen e-post, varsler, tilbakemeldinger eller knapper som starter jobber eller koster penger.
2. Arbeidsgivers regler for sideprosjekter: avklart av administrator.
3. Opplastingsverktøy: `wrangler`. Node.js på lab er godkjent som ny avhengighet.
4. `SITE_WEEKS` = 26 uker.

Cloudflare-oppsettet i installasjonsveiledningen del 2–6 er gjort: domene aktivt, Pages-prosjekt med plassholder, eget subdomene aktivt, Access foran alle tre adresser, engangskoden når frem til jobb-e-post, og API-nøkkel med bare Pages-rettighet er laget.

## Arkitektur

```
Lab (LXC, bare utgående trafikk)
  systemd-timer ─► horingsradar run ─► horingsradar site ─► horingsradar publish
                                        │                    │
                     DATA_DIR/site/     │  index.html        │  opplasting med API-nøkkel
                                        │  data.json         │  (kun rettighet til Pages)
                                        │  pdf/<id>.pdf      ▼
                                                     Cloudflare Pages
                                                           │
                                         Cloudflare Access (engangskode på e-post)
                                                           │
                                                 horinger.<domene>  ◄── brukerne
```

- `site` og `publish` tar samme fil-lås som de andre jobbene.
- Feiler `run`, publiseres ingenting. Feiler `site` eller `publish`, er `run` likevel ok; feilen vises i panelet, og forrige versjon blir liggende.
- Pages tar vare på tidligere opplastinger; tilbakerulling er ett klikk.

## Innhold på siden

Basert på mockupen (lokalt, ikke i repoet):

- **Periode:** publisert siste 1, 2, 3, 4 eller 8 uker, eller alle. Gjelder liste, tall og filtre.
- **Tall øverst:** høy relevans og åpne; høy/middels med frist innen 3 uker; nye eller endret siden forrige besøk; åpne totalt. Klikkbare som snarveier.
- **Faner:** Aktuelle (åpne for høring), Nye, Alle, Følger.
- **Filtre:** søk, relevans, sektor, departement, målvirksomheten på adresselisten. Sortering på relevans, frist eller dato.
- **Liste:** høy og middels som kort; lav og ingen samlet i en sammenleggbar liste. Merker for Ny, Endret og adresseliste. Ulest-prikk.
- **Høringsside** (dekker hele vinduet, med «Tilbake til oversikten»): begrunnelse, sammendrag, sider å lese med lenke til riktig side, funn med sitat og kontrollmerke, dokumenter, fakta (departement, datoer, status, adresseliste), knappene «Åpne på regjeringen.no», «Følg», «Kopier lenke» og «Last ned PDF».
- **Bunntekst:** privat prosjekt, ikke en offisiell tjeneste; vurderingene er KI-generert og må kontrolleres mot kilden. Ingen navn eller logo fra arbeidsgiver.
- Ingenting som koster penger eller starter jobber. `noindex` og `robots.txt` som stenger søkemotorer ute.

## Milepæler

Hver milepæl får en egen plan som godkjennes før koding, som før.

### M0 – Avklaringer og regler (ferdig 2026-11-14)
- Endre «ingen UI for brukerne» i `CLAUDE.md` og `docs/SPEC.md` til å tillate en lesbar, statisk nettside bak innlogging. Fortsatt ingen e-post eller varsler.
- Sjekke arbeidsgivers regler for sideprosjekter og offentlige ytringer.
- Velge opplastingsverktøy og godkjenne eventuell ny avhengighet.
- Kjøpe domene og sette opp Cloudflare etter installasjonsveiledningen, del 2–6.

### M1 – PDF for én høring (ferdig 2026-11-14)
- Ny mal `report/templates/hearing.html.j2`. Stilen og makroene (fakta, funn, leseguide) er flyttet fra `report.html.j2` til `_style.html.j2` og `_macros.html.j2`, som begge malene bruker. Samlerapporten ser ut som før.
- Uten det som bare gjelder samlerapporten: oversiktstabell, tellinger, periodelinje, «Frister som nærmer seg», endringer siden forrige rapport og «Ny»-merker. Lav og ingen får full visning, ikke én linje.
- Topptekst med «Generert», «Vurdert» (dato for vurderingen) og modell/prompt, og samme merknad om KI og kontroll av sitater. «Hentet <dato>» ble droppet fordi den ville bli utdatert i en PDF som bare lages på nytt ved endringer.
- Feilet vurdering vises uten den interne feilteksten.
- Kommando `horingsradar hearing-pdf <id> [--out <fil>]`. Leser bare databasen, tar ikke jobb-låsen; `site` i M2 tar låsen.
- Endrer ikke `report`, `report_item` eller `reported_in`.
- Tester: høy, middels, lav, ingen, feilet og ikke vurdert; uverifisert sitat er synlig merket; ingen samlerapport-seksjoner; bare driftsvurderinger; ukjent eller ikke fulgt høring gir feil.

### M2 – Statisk side (ferdig 2026-11-14)
- Ny modul `src/horingsradar/site/` med Jinja2-maler, CSS og egen JS (vendret i repoet, ingen CDN, ingen byggesteg).
- Kommando `horingsradar site` bygger `DATA_DIR/site/`: `index.html`, `data.json`, `assets/`, `pdf/<id>.pdf`, `robots.txt` og `_headers`. Kommandoen skriver ut stien.
- Tar med høringer fra en innstillbar periode (`SITE_WEEKS`, standard 26 uker), pluss alle åpne. Alle relevansnivåer tas med, også lav, ingen, feilet og ikke vurdert.
- PDF for én høring lages bare på nytt når innholdet endrer seg (hash av den gjengitte HTML-en i `DATA_DIR/site_cache/manifest.json`), så et daglig bygg tar sekunder.
- Bygges i `DATA_DIR/site.new` og byttes inn til slutt. Feiler bygget, blir forrige side liggende.
- Bare driftsvurderinger; aldri backtest-vurderinger.
- Tar jobb-låsen, som de andre jobbene.
- Tester: innholdet i `data.json` mot fixtures, lav/ingen er med, ingen hemmeligheter eller interne felt (kostnad, tokens, rå svar) i output.

### M3 – Publisering (ferdig 2026-11-14)
- Kommando `horingsradar publish [--preview]` bygger først og laster deretter opp `DATA_DIR/site/` til Pages-prosjektet med wrangler. `--preview` går til en forhåndsvisning og rører ikke produksjon. `--no-build` laster opp siden som allerede er bygget.
- Kjøres automatisk etter hver vellykket `run` når `SITE_ENABLED=true`, og kan startes fra panelet («Publiser nettside» og «Publiser forhåndsvisning»). Panelet viser tidspunkt og resultat for siste publisering.
- Nye variabler i `.env.example`: `SITE_ENABLED`, `SITE_WEEKS`, `SITE_BASE_URL`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_PAGES_PROJECT`, `CLOUDFLARE_PAGES_BRANCH` og `WRANGLER_BIN`. De to siste ble lagt til under bygging.
- API-nøkkelen logges aldri. Den gis til wrangler bare i miljøet, og wrangler-utskrift renses for nøkkelen før den logges.
- Tester med falsk opplasting; ingen nettverk.

### M4 – Tilgang og lansering
- Cloudflare Access foran eget domene, `*.pages.dev` og forhåndsvisninger.
- Test med én bruker: kommer e-posten frem, eller havner den i søppelpost eller karantene? Be IT hvitliste `noreply@notify.cloudflare.com` ved behov.
- Prøveperiode med noen få brukere før alle får tilgang.

### M5 – Justeringer etter prøveperioden
- Endringer etter tilbakemeldinger. Ta stilling til om PDF på e-post kan fases ut.

## Åpne spørsmål

Ingen åpne spørsmål gjenstår.

Avgjort i M0: opplastingsverktøy (`wrangler`), innlogging (Cloudflare Access) og periode (`SITE_WEEKS` = 26 uker). Avgjort i M3: publisering rett etter hver vellykket `run`, når `SITE_ENABLED=true`.

## Risiko

| Risiko | Tiltak |
|---|---|
| Engangskoden stoppes av spamfilter på jobb | Test med én bruker først; be IT hvitliste avsenderen; lang økt gir få e-poster |
| `*.pages.dev`-adressen er åpen selv om eget domene er beskyttet | Egen Access-regel for `*.pages.dev` og for forhåndsvisninger (se installasjonsveiledningen, del 5) |
| API-nøkkelen kommer på avveie | Nøkkelen har bare rettighet til Pages; ligger i `.env` med `chmod 600`; kan trekkes tilbake i Cloudflare |
| Siden oppfattes som en offisiell tjeneste | Tydelig bunntekst; ingen navn eller logo fra arbeidsgiver |
| Feil i en vurdering blir lest uten kontroll | Samme merknad som i rapporten; sitater er kontrollert og uverifiserte er merket |

## Ikke med

- EU/EØS-radaren.
- Varsler (e-post, RSS, push).
- Kommentarer, tilbakemeldinger eller andre brukerdata på server.
- Knapper som starter jobber eller koster penger.
- Hosting på lab eller VPS.

