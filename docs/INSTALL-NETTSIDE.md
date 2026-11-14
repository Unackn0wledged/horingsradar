# Installere nettsiden for Høringsradar

Guiden setter opp den statiske nettsiden fra [PLAN-NETTSIDE.md](PLAN-NETTSIDE.md):

- et eget domene med siden på et subdomene, f.eks. `horinger.<domene>`,
- Cloudflare Pages som serverer siden,
- Cloudflare Access som slipper inn bare brukerne du har lagt inn,
- opplasting fra lab, automatisk etter hver vellykket kjøring hvis du slår det på (del 9).

Lab-serveren får ingen ny innkommende trafikk. Del 2–6 gjøres i nettleseren og kan gjøres før noe er bygget. Del 7–9 gjøres på lab, med koden fra repoet installert. Regn med en kveld første gang, pluss ventetid på domenet.

> **Slik leser du guiden:** Kommandoer kjøres som **root i containeren** (`root@horingsradar`), med mindre blokken starter med `# På utviklingsmaskinen:`. Bytt ut `<vinkelparenteser>` med egne verdier, *inkludert* parentesene. Blokker merket «Forventet utskrift» skal ikke kjøres.
>
> **Menyene i Cloudflare endres ofte.** Finner du ikke en side, trykk **Ctrl K** (søkefeltet øverst til venstre) og skriv navnet, f.eks. `Workers & Pages`, `Bulk redirects` eller `API Tokens`. Menyveiene under er sjekket mot Cloudflares dokumentasjon i november 2026.

## Innhold

1. [Oversikt](#1-oversikt)
2. [Kjøp domene](#2-kjøp-domene)
3. [Legg domenet til i Cloudflare](#3-legg-domenet-til-i-cloudflare)
4. [Opprett Pages-prosjektet](#4-opprett-pages-prosjektet)
5. [Cloudflare Access: hvem slipper inn](#5-cloudflare-access-hvem-slipper-inn)
6. [API-nøkkel for opplasting](#6-api-nøkkel-for-opplasting)
7. [Opplastingsverktøy på lab](#7-opplastingsverktøy-på-lab)
8. [Konfigurasjon (.env) og første publisering](#8-konfigurasjon-env-og-første-publisering)
9. [Automatisk publisering](#9-automatisk-publisering)
10. [Daglig drift og tilbakerulling](#10-daglig-drift-og-tilbakerulling)
11. [Sjekkliste før brukerne får tilgang](#11-sjekkliste-før-brukerne-får-tilgang)
12. [Feilsøking](#12-feilsøking)

**Rekkefølge:** Del 2 og 3 starter ventetiden på domenet. Mens du venter, kan du gjøre del 4.1, 5.1–5.3, 5.5 og 6. Del 4.2, 5.4 og 5.8 krever at domenet er **Active** i Cloudflare.

---

## 1. Oversikt

```
 Lab (LXC «horingsradar»)                          Cloudflare
 ┌──────────────────────────────────┐             ┌───────────────────────────────┐
 │ timer ► run ► site ► publish ────┼─ utgående ─►│ Pages-prosjekt «horingsradar» │
 │                                  │ (API-nøkkel)│        ▲                      │
 │ DATA_DIR/site/  index.html       │             │ Access (engangskode på e-post)│
 │                 data.json        │             │        ▲                      │
 │                 pdf/<id>.pdf     │             └────────┼──────────────────────┘
 └──────────────────────────────────┘                      │
                                              horinger.<domene>  ◄── brukerne
```

- **Domenet** kjøpes hos en registrar (firmaet du kjøper domenet hos), men DNS styres av Cloudflare.
- **Pages** lagrer filene og tar vare på tidligere versjoner.
- **Access** står foran siden. Uten godkjent e-postadresse kommer ingen inn.
- Cloudflare-kontoen kan brukes til flere tjenester senere. Domenet, teamnavnet i Access og reglene gjelder hele kontoen.

## 2. Kjøp domene

1. Kjøp domenet hos en registrar som selger `.no`-domener. Privatpersoner kan registrere `.no`-domener. Du trenger **ikke** webhotell; Cloudflare serverer siden.
2. Skru på tofaktorinnlogging på registrar-kontoen.
3. Vent til domenet står som **Active** hos registraren. Rett etter kjøpet står det ofte som «Pending Registration» i noen minutter til noen timer.
4. Mange registrarer har **registrar lock** (domenelås). Låsen hindrer at domenet flyttes eller endres uten lov, og den stopper også bytte av navnetjenere. Den skal være **av** mens du gjør del 3.4, og **på** igjen når domenet er aktivt i Cloudflare.

## 3. Legg domenet til i Cloudflare

### 3.1 Konto

Opprett en gratis konto på `dash.cloudflare.com` og skru på tofaktorinnlogging (profilen øverst til høyre).

### 3.2 Legg til domenet

1. **Domains** > **Onboard a domain** (eller **Add a domain**). Skriv inn domenet.
2. **Additional configuration**:
   - **AI search, agent and training policies:** sett alt til **Block**. Siden skal ikke indekseres eller brukes til trening, og det gjelder også fremtidige subdomener. Kan endres senere.
   - **Enable Bot Preference Sync:** på.
   - **Import DNS records:** *Automatic* er greit.
3. Velg planen **Free**.
4. **Review your DNS records:** du trenger ikke legge til noe. Varslene om MX, `www` og rotdomenet kan du se bort fra; vi bruker bare `horinger`, og den oppføringen lager Pages selv i del 4.2. Står det poster som registraren har lagt inn (en «parkeringsside»), kan de slettes.
5. **Continue to activation.** Cloudflare viser to navnetjenere, f.eks. `ada.ns.cloudflare.com` og `bob.ns.cloudflare.com`.

### 3.3 Valgfritt: si at domenet ikke sender e-post

Et domene som aldri sender e-post, bør si det, så ingen kan sende falske e-poster fra `@<domene>`. Domenet > **DNS** > **Records** > **Add record**:

| Type | Name | Content |
|---|---|---|
| TXT | `@` | `v=spf1 -all` |
| TXT | `_dmarc` | `v=DMARC1; p=reject;` |

### 3.4 Bytt navnetjenere hos registraren

1. Slå av **registrar lock** (del 2.4).
2. Åpne domenet hos registraren og finn innstillingen for navnetjenere. Den heter gjerne **Nameservers**, **Change nameservers**, **Navnetjenere** eller **DNS-servere**.
3. Velg egne navnetjenere (**Use custom nameservers**), skriv inn de to fra Cloudflare, fjern de gamle og lagre.
4. Ved bytte på `.no` tester Norid at de nye navnetjenerne svarer for domenet. Cloudflare svarer med en gang domenet er lagt til i 3.2, så testen skal gå gjennom. Får du en generell feilmelding og feltene nullstilles, se [Feilsøking](#12-feilsøking).

### 3.5 Vent på aktivering

Norid lagrer byttet med en gang, men det publiseres først ved neste oppdatering av `.no`-sonen. Cloudflare sjekker selv og setter domenet til **Active**, vanligvis innen 1–2 timer (inntil et døgn). Du får e-post når det skjer. **Check nameservers now** på domenets **Overview** sjekker med en gang.

Kontroll fra en maskin med `dig`:

```bash
# På utviklingsmaskinen:
dig +short NS <domene>
```

Forventet utskrift: de to Cloudflare-navnetjenerne. Er utskriften tom, er sonen ikke publisert ennå.

Når domenet er **Active**: slå på **registrar lock** igjen.

## 4. Opprett Pages-prosjektet

Prosjektet opprettes med **direkte opplasting**. Det kan ikke kobles til Git senere, og det skal det heller ikke: dataene ligger på lab, ikke i repoet.

### 4.1 Prosjekt med plassholder (domenet trenger ikke være aktivt)

Lag en mappe med en `index.html` («Kommer snart», med `<meta name="robots" content="noindex,nofollow">`) og en `robots.txt`:

```
User-agent: *
Disallow: /
```

1. **Workers & Pages** ligger på kontonivå, ikke inne på domenet. Gå tilbake med **Back to Domains**, eller bruk **Ctrl K** og skriv `Workers & Pages`.
2. **Create application** > **Get started** under Pages > **Drag and drop your files**.
3. Prosjektnavn: `horingsradar`. Navnet blir også adressen `horingsradar.pages.dev`.
4. Dra inn **hele mappen** (Cloudflare vil ha en mappe, ikke enkeltfiler) og velg **Deploy site**.

Plassholderen ligger nå åpent på `horingsradar.pages.dev`. Gjør del 5.5 snart, og last aldri opp ekte data før Access er på plass.

### 4.2 Eget domene (domenet må være Active)

Gjør del 5.4 først, så adressen er beskyttet fra første sekund.

1. **Workers & Pages** (Ctrl K) > velg prosjektet `horingsradar`.
2. Fanen **Custom domains** (ved siden av Deployments, Metrics og Settings).
3. **Set up a domain** (eller **Set up a custom domain**).
4. **Domain:** skriv `horinger.<domene>` > **Continue**.
5. Cloudflare viser DNS-oppføringen den vil lage: **CNAME** `horinger` → `horingsradar.pages.dev`. Du skal ikke gjøre noe med den selv. Velg **Activate domain**.
6. Status går fra **Initializing** / **Verifying** til **Active**, vanligvis i løpet av noen minutter. Cloudflare lager HTTPS-sertifikatet samtidig. Last siden på nytt for å se status.
7. Kontroll: domenet > **DNS** > **Records** skal ha en CNAME-oppføring for `horinger` med oransje sky (**Proxied**).

Ikke lag `horinger`-oppføringen for hånd i DNS først; da kan siden svare med feil 522.

Får du valget **Begin DNS transfer** eller **Begin CNAME setup**, er domenet ikke aktivt i Cloudflare ennå. Velg **Cancel** og vent på del 3.5.

## 5. Cloudflare Access: hvem slipper inn

Access må dekke **tre** adresser:

| Adresse | Hvem skal inn |
|---|---|
| `horinger.<domene>` | Brukerne (og deg) |
| `horingsradar.pages.dev` | Bare deg (brukerne trenger den aldri) |
| `*.horingsradar.pages.dev` (forhåndsvisninger) | Bare deg |

Beskyttes bare én, er siden fortsatt åpen via de andre.

### 5.1 Slå på Zero Trust

1. Velg **Zero Trust** i menyen.
2. **Teamnavn:** et unikt navn for kontoen, f.eks. `<domene-uten-punktum>` eller `<navn>-access`. Det blir adressen til innloggingssiden (`<teamnavn>.cloudflareaccess.com`) og gjelder alle tjenester på kontoen. Navnet må være ledig hos alle Cloudflare-kunder; får du «already been claimed», prøv et annet. Kan endres senere under **Zero Trust** > **Settings**.
3. **Plan:** velg **Zero Trust Free** (opptil 50 brukere). Cloudflare krever betalingsinformasjon også for gratisplanen, men belaster ikke. Kontroller at planen er Free og totalen **$0** før du bekrefter. PayPal godtas ofte som alternativ til kort.

### 5.2 Engangskode på e-post

1. **Zero Trust** > **Integrations** > **Identity providers**.
2. Står **One-time PIN** i listen, er du ferdig. Hvis ikke: **Add new identity provider** > **One-time PIN**. Ingen innstillinger trengs.

La MFA-metodene under **Access controls** > **Access settings** stå av. Engangskoden er nok.

### 5.3 To gjenbrukbare regler

Reglene lages én gang og brukes på flere applikasjoner.

**Zero Trust** > **Access controls** > **Policies** > **Add a policy**:

| Felt | Regel 1 | Regel 2 |
|---|---|---|
| **Policy name** | `Høringsradar – brukere` | `Bare administrator` |
| **Action** | Allow | Allow |
| **Policy session duration** | Same as application session duration | Same as application session duration |
| **Include** | *Emails* med hver brukers adresse, og din egen. Eller *Emails ending in* `@<jobbdomene>` hvis alle med den adressen skal inn | *Emails* med din egen adresse |

Velg **Save** for hver.

### 5.4 Applikasjon for eget domene (domenet må være Active)

Dette er en **ny** applikasjon, ved siden av de to for `pages.dev` fra 5.5. Lag den gjerne **før** del 4.2, så `horinger.<domene>` er beskyttet fra første sekund.

**Zero Trust** > **Access controls** > **Applications** > **Create new application** > **Self-hosted and private**:

1. **Destinations** > **Public hostnames**: **Subdomain** `horinger`, **Domain** `<domene>` fra listen, **Path** tomt.
2. **Access policies** > **Add existing policy**: velg `Høringsradar – brukere`. Logger du inn med en annen adresse enn jobbadressen, legg også til `Bare administrator`.
3. **Login methods**: bare **One-time PIN**. Slå på **Apply instant authentication**, så går brukeren rett til e-postfeltet.
4. **Session duration**: **1 month**. Da trenger brukerne ny kode omtrent én gang i måneden per nettleser.
5. **Application name** (under **Details**, eller øverst på siden): `Høringsradar`. Navnet vises på innloggingssiden.
6. **Create** (eller **Save**).

Under **Applications** skal det nå være tre applikasjoner: `horingsradar.pages.dev`, forhåndsvisningene (`*.horingsradar.pages.dev`) og `Høringsradar` (`horinger.<domene>`).

Uten denne applikasjonen vises innloggingssiden, men innloggingen virker ikke.

### 5.5 `pages.dev` og forhåndsvisninger (domenet trenger ikke være aktivt)

1. **Workers & Pages** > `horingsradar` > **Settings** > **General** > **Preview access** > **Restrict previews** (i eldre dashbord: **Enable access policy**). Cloudflare lager da en Access-applikasjon for forhåndsvisningene (`*.horingsradar.pages.dev`).
2. Velg **Manage** på den. Du havner under **Zero Trust** > **Access controls** > **Applications**. Velg applikasjonen > **Configure** (eller **Edit**).
3. Under **Public hostname**: slett stjernen (`*`) i **Subdomain**, endre **Application name** til `horingsradar.pages.dev` og lagre. Applikasjonen beskytter nå `horingsradar.pages.dev`.
4. Tilbake i Pages-prosjektet > **Settings** > **General** > **Preview access**: velg **Restrict previews** på nytt. Nå lages en ny applikasjon for forhåndsvisningene.
5. Under **Access controls** > **Applications** skal det være to applikasjoner for `pages.dev`: én for `horingsradar.pages.dev` og én for `*.horingsradar.pages.dev`.
6. Åpne begge og sett **Access policies** til `Bare administrator` (fjern regelen Cloudflare la inn automatisk) og **Identity providers** til **One-time PIN**.

### 5.6 Test

1. Åpne `https://horinger.<domene>` i et privat vindu. Du skal få innloggingssiden med navnet `Høringsradar`.
2. Skriv en adresse som **ikke** er godkjent. Siden sier at koden er sendt, men ingen e-post kommer. Det er riktig.
3. Skriv din egen adresse og logg inn med koden. Koden gjelder i 10 minutter og kan brukes én gang.
4. Gjenta for `https://horingsradar.pages.dev` og en forhåndsvisningsadresse (vises under **Deployments** i Pages-prosjektet).

### 5.7 E-post på jobb

Koden sendes fra `noreply@notify.cloudflare.com`. Før alle får tilgang:

- Test med én bruker. Kommer e-posten i innboksen, i søppelpost eller i karantene?
- Stoppes den, be IT hvitliste avsenderen. Sikkerhetsfiltre som åpner lenker og koder for å skanne dem, kan bruke opp koden før brukeren ser den; hvitlisting hjelper også mot det.

### 5.8 Valgfritt: send `pages.dev` videre til eget domene (domenet må være Active)

`horingsradar.pages.dev` kan ikke slettes. Med en videresending havner alle som åpner den, på `horinger.<domene>`. Access-applikasjonene fra 5.5 beholdes som ekstra sikring.

1. **Ctrl K** > `Bulk redirects` (siden ligger på kontonivå).
2. **Create Bulk Redirect List** > navn `horingsradar-pages-dev` > **Next** > **Or, manually add URL redirects**:
   - **Source URL:** `horingsradar.pages.dev`
   - **Target URL:** `https://horinger.<domene>`
   - **Status:** `301`
   - Huk av **Preserve query string**, **Subpath matching** og **Preserve path suffix**.
   - **Ikke** huk av **Include subdomains**. Da ville også forhåndsvisningene blitt sendt videre, og `publish --preview` kunne ikke lenger testes.
3. **Next** > **Continue to Redirect Rules** > gi regelen et navn, velg listen > **Save and Deploy**.
4. Test i privat vindu: `https://horingsradar.pages.dev/robots.txt` skal havne på `https://horinger.<domene>/robots.txt` og vise innloggingssiden.

## 6. API-nøkkel for opplasting

Lab trenger en nøkkel som **bare** kan laste opp til Pages. Bruk en **kontonøkkel** (account token), som hører til kontoen og ikke til din personlige bruker.

1. **Manage account** > **Account API Tokens** > **Create Token**.
2. **Token name:** bytt det tilfeldige navnet til `horingsradar-publish`.
3. **Permission policies:** velg **Start from scratch** (*Custom*). Ikke bruk malene; de gir langt flere rettigheter enn nødvendig.
4. Omfang **Entire Account** er greit. Søk etter `pages` og huk av **Edit** på raden **Pages** under **Developer Platform**. Ikke *Account Custom Pages* eller *Access: Custom Pages*; det er noe annet. Ingen andre rettigheter.
5. **Token expiration:** **1 year**, med påminnelse i kalenderen om å fornye.
6. **Client IP address filtering:** la feltet stå tomt. Lab-ens offentlige IP-adresse kan endre seg, og da ville opplastingen stoppe.
7. **Review token** > kontroller at oppsummeringen bare viser *Pages Write* for din konto (Edit vises som Write, og inkluderer lesing) > **Create Token**. Kopier nøkkelen til en passordbehandler; den vises bare én gang.
8. **Account ID** står på domenets **Overview** i høyre kolonne under **API**, og under **Workers & Pages**.

Finner du ikke *Cloudflare Pages* blant rettighetene for kontonøkler, lag nøkkelen under profilen din i stedet: **My Profile** > **API Tokens**, med de samme valgene.

Nøkkelen skal bare ligge i `.env` på lab. Den skal aldri i repoet, i chat eller i logger. Kommer den på avveie: slett den under **API Tokens** og lag en ny.

## 7. Opplastingsverktøy på lab

> Opplastingsverktøyet er `wrangler`. Det installeres med npm og kjøres av tjenestebrukeren `horingsradar`.

`wrangler` krever en støttet Node.js-versjon (Current, Active eller Maintenance LTS).

```bash
apt update && apt install -y nodejs npm
node --version
```

Er versjonen eldre enn den eldste støttede LTS-versjonen, installer Node.js fra NodeSource i stedet, etter deres veiledning for Debian. Installasjonen av wrangler skriver en tydelig feilmelding hvis Node.js er for gammel.

```bash
npm install -g wrangler
wrangler --version
```

Sjekk at tjenestebrukeren finner den:

```bash
sudo -u horingsradar bash -c 'which wrangler && wrangler --version'
```

Finner den ingenting, sett `WRANGLER_BIN` til full sti (del 8).

Ikke kjør `wrangler login`. Lab bruker API-nøkkelen fra `.env`, ikke en innlogging.

## 8. Konfigurasjon (.env) og første publisering

Legg til i `/opt/horingsradar/.env`. `.env.example` har alle variablene.

```
SITE_ENABLED=false
SITE_WEEKS=26
SITE_BASE_URL=https://horinger.<domene>
CLOUDFLARE_ACCOUNT_ID=<account-id>
CLOUDFLARE_API_TOKEN=<nøkkel>
CLOUDFLARE_PAGES_PROJECT=horingsradar
CLOUDFLARE_PAGES_BRANCH=main
WRANGLER_BIN=wrangler
```

- `SITE_ENABLED`: `true` bygger og publiserer siden etter hver vellykket `run` (del 9). `false` gjør ingenting automatisk; du starter `site` og `publish` selv.
- `SITE_WEEKS`: hvor mange uker tilbake siden tar med. Åpne høringer tas alltid med.
- `SITE_BASE_URL`: adressen til siden. Brukes bare til å vise adressen i panelet.
- `CLOUDFLARE_ACCOUNT_ID`: Account ID fra del 6.
- `CLOUDFLARE_API_TOKEN`: API-nøkkelen fra del 6.
- `CLOUDFLARE_PAGES_PROJECT`: navnet på Pages-prosjektet (`horingsradar`).
- `CLOUDFLARE_PAGES_BRANCH`: produksjonsgrenen til Pages-prosjektet. For prosjekt med direkte opplasting er den `main`. Sjekk under **Workers & Pages** > prosjektet > **Settings**, og bruk navnet som står der. Et annet navn gir forhåndsvisning i stedet for produksjon.
- `WRANGLER_BIN`: kommandoen for wrangler. Standard er `wrangler`. Bruk full sti, f.eks. `/usr/local/bin/wrangler`, hvis `which wrangler` ikke finner den (del 7).

```bash
chmod 600 /opt/horingsradar/.env
```

Wrangler skriver sin konfigurasjon og logg i `/var/lib/horingsradar/wrangler/`, fordi tjenesten bare kan skrive der. Telemetri er av. Du trenger ikke gjøre noe med det.

Bygg siden for hånd og se på den:

```bash
sudo -u horingsradar bash -c 'cd /opt/horingsradar && uv run horingsradar site'
ls /var/lib/horingsradar/site
```

Kommandoen skriver ut stien til siden. Forventet listing: `index.html`, `data.json`, `assets`, `pdf`, `robots.txt` og `_headers`.

Publiser til forhåndsvisning først. Den rører ikke produksjon:

```bash
sudo -u horingsradar bash -c 'cd /opt/horingsradar && uv run horingsradar publish --preview'
```

Åpne adressen som skrives ut. Du skal få innloggingssiden fra del 5.5. Sjekk også i panelet, under «Nettside for brukerne», at siste publisering står som ok med forhåndsvisning. Ser alt riktig ut, publiser til produksjon:

```bash
sudo -u horingsradar bash -c 'cd /opt/horingsradar && uv run horingsradar publish'
```

`publish` bygger siden først, så `site` trenger du ikke kjøre før. Sjekk i panelet at siste publisering står som ok med produksjon.

## 9. Automatisk publisering

Sett i `.env`:

```
SITE_ENABLED=true
```

Fra nå bygges og publiseres siden etter hver vellykket `run`. Timeren er den samme som før; ingen nye systemd-filer.

- Feiler `run`, publiseres ingenting, og forrige versjon av siden blir liggende.
- Feiler bygging eller opplasting, er `run` likevel ok. Feilen vises i panelet, og forrige versjon av siden blir liggende.

Panelet viser tidspunkt og resultat for siste publisering. Knappene **Publiser nettside** og **Publiser forhåndsvisning** starter `publish` og `publish --preview` som egne prosesser. Knappene vises når Cloudflare-innstillingene er fylt ut.

## 10. Daglig drift og tilbakerulling

- **Se siste publisering:** panelet under «Nettside for brukerne», eller Pages-prosjektet > **Deployments**.
- **Rulle tilbake:** Pages-prosjektet > **Deployments** > velg en tidligere versjon > **Rollback to this deployment**. Neste vellykkede publisering legger seg over igjen; sett `SITE_ENABLED=false` hvis du vil holde den gamle versjonen.
- **Legge til eller fjerne brukere:** **Zero Trust** > **Access controls** > **Policies** > `Høringsradar – brukere`. Endringen gjelder alle applikasjoner som bruker regelen, fra neste innlogging. En bruker du fjerner, mister tilgang når økten går ut.
- **Stenge en bruker ute med en gang:** **Zero Trust** > **Team & Resources** > **Users** > brukeren > **Revoke**.
- **Stenge siden helt:** fjern eget domene under **Custom domains**, eller bytt regelen på applikasjonen til `Bare administrator`.
- **Fornye API-nøkkelen:** lag ny nøkkel (del 6), bytt i `.env`, slett den gamle.

## 11. Sjekkliste før brukerne får tilgang

- [ ] Domenet er **Active** i Cloudflare, og registrar lock er slått på igjen.
- [ ] `https://horinger.<domene>` krever innlogging i privat vindu.
- [ ] `https://horingsradar.pages.dev` krever innlogging, eller sender videre til eget domene (5.8).
- [ ] En forhåndsvisningsadresse krever innlogging og slipper bare inn deg.
- [ ] En adresse som ikke er godkjent, får ingen kode.
- [ ] Én testbruker har fått koden i innboksen på jobb.
- [ ] `https://horinger.<domene>/robots.txt` stenger søkemotorer ute.
- [ ] Bunnteksten sier at siden er privat og KI-generert, uten navn eller logo fra arbeidsgiver.
- [ ] «Last ned PDF» gir samme layout som rapporten.
- [ ] API-nøkkelen har bare Pages-rettighet og står ikke i noen logg (`grep -r <starten av nøkkelen> /var/lib/horingsradar/logs` gir ingen treff).
- [ ] Snapshot av LXC-en er tatt før `SITE_ENABLED=true`.

## 12. Feilsøking

| Symptom | Sannsynlig årsak | Løsning |
|---|---|---|
| Registraren gir «An issue was encountered while updating the domain nameservers», og feltene nullstilles | **Registrar lock** er på, domenet er ikke ferdig registrert, eller Norids test av navnetjenerne feilet | Slå av registrar lock (del 2.4); vent til domenet er Active hos registraren; sjekk at domenet er lagt til i Cloudflare (3.2) før byttet. Hjelper ikke det, be registrarens support sjekke svaret fra Norid |
| Domenet står som «Pending» i Cloudflare | `.no`-sonen er ikke publisert med de nye navnetjenerne ennå | Vent; **Check nameservers now**; kontroller med `dig +short NS <domene>` |
| Pages viser **Begin DNS transfer** / **Begin CNAME setup** | Domenet er ikke Active i Cloudflare ennå | **Cancel**, vent på del 3.5 og prøv igjen |
| Siden svarer med feil 522 | `horinger`-oppføringen ble laget for hånd før domenet ble lagt til i Pages | Slett oppføringen i DNS og legg til domenet på nytt (4.2) |
| «This authentication domain has already been claimed» | Teamnavnet er tatt av en annen Cloudflare-kunde | Velg et annet teamnavn (5.1) |
| Zero Trust ber om kort for Free | Slik er det for alle | Kontroller Free og $0; eller bruk PayPal (5.1) |
| Innloggingssiden vises, men innloggingen feiler | Mangler Access-applikasjon for eget domene | Del 5.4 |
| Siden åpner uten innlogging på `pages.dev` | Del 5.5 er ikke fullført | Fullfør 5.5 og test i privat vindu |
| Brukeren får ingen kode | Adressen er ikke i regelen, eller e-posten stoppes på jobb | Sjekk regelen (5.3); se i søppelpost og karantene; be IT hvitliste `noreply@notify.cloudflare.com` |
| Koden er «allerede brukt» | Sikkerhetsfilter har åpnet e-posten | Hvitlisting hos IT |
| `publish` feiler med 401/403 | Feil eller utløpt nøkkel, mangler Pages-rettighet, eller feil Account ID | Lag ny nøkkel (del 6) og sjekk `.env` |
| `publish` feiler fordi `wrangler` ikke finnes | Del 7 er ikke gjort, eller `PATH` mangler npm-mappen for tjenestebrukeren | `which wrangler` som `horingsradar`-brukeren. Finner den ingenting, sett `WRANGLER_BIN` til full sti (del 8) |
| `publish` går til forhåndsvisning, ikke produksjon | `CLOUDFLARE_PAGES_BRANCH` er ikke produksjonsgrenen i Pages-prosjektet | Sjekk **Settings** i prosjektet og sett navnet der (ofte `main`) |
| `wrangler` feiler med at den ikke kan skrive | Tjenesten kan bare skrive i `/var/lib/horingsradar`, og `wrangler/`-mappen der har feil eier | Sjekk at `/var/lib/horingsradar/wrangler/` finnes og eies av `horingsradar` |
| Panelet viser feil under «Nettside for brukerne», men `run` er ok | Bygging eller opplasting feilet etter kjøringen | Les feilen i panelet og kjør `publish` på nytt. Forrige versjon ligger fortsatt ute |
| Siden viser gamle data | `SITE_ENABLED=false`, eller siste `run` eller publisering feilet | Se jobbloggen og «Nettside for brukerne» i panelet |
