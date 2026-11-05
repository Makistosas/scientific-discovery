# AI mokslinių atradimų agentas

Agentas nuolat analizuoja viešas biomedicinos duomenų bazes ir ieško **taikinio–ligos galimybių, kurios turi
stiprių priežastinių įrodymų, bet dar nėra pakankamai ištirtos**. Pavyzdžiui, jis gali pastebėti, kad
tam tikro baltymo aktyvumo slopinimas galėtų būti perspektyvus konkrečiai ligai. Kiekvienai galimybei jis
parengia išsamią mokslinę hipotezę: įrodymus, kiekvieno jų patikimumo įvertinimą (A–D), bendrą
pasitikėjimo lygį, alternatyvius paaiškinimus ir eksperimentus, kurie hipotezę patvirtintų arba paneigtų.

> _English summary below._

## Duomenų šaltiniai

| Šaltinis | Kam naudojamas |
|---|---|
| **Open Targets Platform** (GraphQL API) | Kandidatų paieška. Platforma jungia daugiau kaip 20 šaltinių: GWAS, ClinVar, retų variantų naštos testus, ClinGen, Gene2Phenotype, Orphanet, COSMIC, IntOGen, ChEMBL klinikinius duomenis, IMPC pelių modelius, Reactome, CRISPR, Expression Atlas, Europe PMC. Iš jos imami ir poveikio krypties duomenys (LoF/GoF → rizika/apsauga), vaistingumas, saugumas ir genetinis konservatyvumas |
| **Europe PMC** | Kiek publikacijų mini porą pavadinime arba santraukoje (ištirtumas), nauji straipsniai, santraukos |
| **PubMed** (NCBI E-utilities) | Nepriklausomas publikacijų skaičiaus patikrinimas `[tiab]` |
| **bioRxiv / medRxiv** | Preprintai: randami per Europe PMC, o bioRxiv API patikrina, ar vėliau buvo paskelbti žurnaluose |
| **ChEMBL** | Junginių bioaktyvumas (pChEMBL), įrankiniai junginiai, žinomi veikimo mechanizmai |
| **UniProt** | Baltymo funkcija, lokalizacija, susijusios ligos |
| **RCSB PDB** | Eksperimentinės struktūros, ar yra struktūrų su ligandais, skyra |
| **ClinicalTrials.gov** (API v2) | Klinikiniai tyrimai, sustabdyti tyrimai ir sustabdymo priežastys (neigiami rezultatai) |
| **Patentai** (Europe PMC / EPO) | Komercinio susidomėjimo signalas |
| **Claude** (Anthropic API, nebūtina) | Hipotezių rašymas ir agentiškas papildomų įrodymų tyrimas |

## Kaip tai veikia

```
 Open Targets ──► 1. Kandidatai (iki kelių šimtų taikinio–ligos porų, balai pagal įrodymų tipą)
                     │   įrodymų stiprumas, preliminarus ištirtumas, vaistingumas, saugumas
                     ▼
                  2. Praturtinimas (lygiagrečiai, kiekvienam geriausiam kandidatui)
                     │   OT įrodymų įrašai → patikimumas A–D, poveikio kryptis
                     │   Europe PMC + PubMed + preprintai + patentai + ClinicalTrials → ištirtumas
                     │   ChEMBL + PDB + UniProt → vaistingumas ir mechanizmas
                     ▼
                  3. Balai ir reitingas:  galimybė = įrodymai × spraga × vaistingumas × saugumas × kryptis
                     ▼
                  4. Hipotezė + savikritika (taisyklėmis arba su Claude)
                     │   pasitikėjimas (GRADE principu), alternatyvūs paaiškinimai, eksperimentai
                     ▼
                  5. Ataskaitos (Markdown + JSON) ir nuolatinė stebėsena (SQLite atmintis, perspėjimai)
```

### Balai

* **Įrodymai** (0–1): „noisy-OR“ per nepriklausomus priežastinius įrodymų tipus (genetika, kuruota genetika,
  somatinės mutacijos, gyvūnų modeliai, keliai/CRISPR, raiška). Kelių tipų sutapimas balą didina.
  Literatūra ir klinika čia neįskaičiuojamos, nes jos rodo ištirtumą, o ne priežastingumą.
* **Ištirtumas → spraga = 1 − ištirtumas**: publikacijos (įskaitant tėvinę ligą, kad ligos potipiai nesukurtų
  netikrų „atradimų“), klinikiniai tyrimai, patentai, preprintai ir aukščiausia klinikinė stadija šiai ligai.
  Taikiniai, kurie šiai ligai jau yra 3 fazėje arba patvirtinti, pagal nutylėjimą atmetami (`--include-known`
  juos palieka).
* **Vaistingumas**: Open Targets tractability kategorijos, ChEMBL junginiai (pChEMBL ≥ 6), PDB struktūros su
  ligandais ir cheminiai zondai. Aktyvinimas (agonizmas) fermentams ir kitiems ne receptorių tipo taikiniams
  vertinamas kaip sunkesnis.
* **Saugumo rizika**: žinomi saugumo signalai, ar genas būtinas ląstelėms (DepMap), ar netoleruoja funkcijos
  praradimo (gnomAD LOEUF), pleiotropija.
* **Kryptis**: iš žmogaus genetikos ir klinikos (LoF→rizika ⇒ aktyvinti; GoF→rizika arba LoF→apsauga ⇒
  slopinti). Kiekvieno šaltinio įtaka apribota, kad 60 ClinVar įrašų nenusvertų vieno naštos testo.
* **GWAS lokusas ir raiškos kolokalizacija**:
  * Kiekvienam GWAS lokusui tikrinami visų lokuso genų L2G balai, todėl matyti konkuruojantys genai, pvz.,
    MUC5AC lokuse – MUC5B.
  * Tikrinama ir kolokalizacija su eQTL/pQTL. Jei rizikos alelis mažina geno raišką, siūloma aktyvinti, o jei
    didina – slopinti (Mendelinės randomizacijos logika). Pvz., DPP9 IPF atveju kryptis nustatoma iš
    fibroblastų eQTL.
  * Splaisingo QTL patvirtina geną, bet krypčiai nenaudojami. Priešingi ženklai skirtinguose audiniuose
    pažymimi kaip atskira alternatyva.
* **Santykinis dėmesys ir tendencija**:
  * Bendrų paminėjimų skaičius lyginamas su visos ligos literatūra, todėl 30 straipsnių retoje ligoje sveria
    daugiau nei Alzheimerio ligos tyrimuose.
  * Paskutinių 3 metų publikacijos lyginamos su ankstesniais 3 metais, atsižvelgiant į visos srities augimą.
    Sparčiai augantis dėmesys reiškia, kad kiti tyrėjai jau artėja.
* **Raiška audiniuose ir ląstelėse** (GTEx, Tabula Sapiens, PRIDE per Open Targets):
  * Ligai aktualūs audiniai nustatomi pagal ligos pavadinimą, QTL kolokalizacijos audinius arba
    terapinę sritį.
  * Tikrinama, ar taikinys ten išreikštas. Silpna raiška tampa alternatyva „veikia per kitą audinį“.
  * Pateikiami didžiausios raiškos ląstelių tipai: tai kandidatai eksperimentams.
  * Fono užterštumas atmetamas: raiška turi siekti bent 2 % paties geno maksimumo.
* **Genetinis saugumas (PheWAS)**:
  * Slopinimas imituoja LoF nešiotojus, aktyvinimas – GoF. Pagal kitų ligų genetiką su anotuota kryptimi
    nustatoma, kurias ligas tokia moduliacija padidintų (šalutinis poveikis) ar sumažintų (papildomos
    indikacijos). Pvz., TYK2 slopinimas psoriazei → imunodeficitas 35.
  * Pridedami pelių knockout letališkumas ir šį taikinį veikiančių vaistų įspėjimai bei reikšmingi FAERS
    reiškiniai.
  * Su tiriamąja liga susiję požymiai (pvz., hipocholesterolemija PCSK9 atveju) atmetami: tai tikslinis
    poveikis.
* **Sakiniai iš literatūros**:
  * Europe PMC teksto analizės sakiniai (per Open Targets), kuriuose kartu minimi taikinys ir liga.
  * Pirmiausia imami rezultatų skyriai, iš kiekvieno straipsnio po vieną sakinį, pasikartojimai pašalinami.
  * Neigiami rezultatai pažymimi. Jie sustiprina alternatyvą „nepaskelbti ar neigiami rezultatai“.
  * Užuominos „slopinimas naudingas / kenkia“ nustatomos pagal raktinius žodžius. Jos nėra kryptis, o tik
    nuoroda, ką perskaityti.
  * Įrankis `read_full_text` perskaito atviros prieigos straipsnio pilną tekstą.
* **Hipotezių kokybės patikra** (be AI, `analysis/quality.py`):
  * Tikrinama, ar cituojami esami įrodymų ID ir aptarti prieštaraujantys įrodymai.
  * Ar yra bent 2 alternatyvos su testais ir bent 2 eksperimentai su rezultatais „jei teisinga“ ir „jei
    klaidinga“.
  * Ar aptarti saugumas (įskaitant genetiškai numatomą šalutinį poveikį), neapibrėžtumai ir priežastis, kodėl
    pora mažai ištirta.
  * Ar kryptis neprieštarauja įrodymams, ar patikimumas nėra aukštesnis, nei leidžia įrodymų lygiai, ir ar
    nėra perdėtų teiginių.
  * Balas (0–100) rodomas kiekvienoje ataskaitoje. MCP jį grąžina įrankiu `discovery_check_hypothesis` ir
    išsaugant hipotezę.
* **Etalonas**: `discovery-agent benchmark` matuoja, ar balas aukščiau iškelia klinikinius taikinius
  20-yje ligų. Dabar AUC 0,61, top-10 – 4,8 karto dažniau, nei atsitiktinai. Žr.
  [`docs/BENCHMARK.md`](docs/BENCHMARK.md).

### Patikimumas ir savikritika

* Kiekvienas įrodymas gauna **A–D** įvertinimą su paaiškinimu. A – stipri žmogaus genetika (pvz., naštos
  testo p ≤ 2,5·10⁻⁶, ClinVar ≥ 2★, ClinGen „Definitive“) arba patvirtinti vaistai; B – kuruoti, bet
  netiesioginiai įrodymai; C – modeliai ir koreliacijos; D – teksto gavyba, preprintai, patentai.
* **Pasitikėjimas** vertinamas GRADE principu. Pradinis lygis priklauso nuo geriausio priežastinio įrodymo.
  Lygis kyla, kai sutampa keli įrodymų tipai ir kryptis nuosekli. Lygis krenta, kai yra tik vienas įrodymų
  tipas, kryptis neaiški, yra prieštaraujančių įrodymų (pvz., tyrimai sustabdyti dėl neveiksmingumo) arba
  remiamasi tik monogeniniais variantais.
* **Alternatyvūs paaiškinimai** parenkami pagal tai, kokie įrodymai yra:
  * kitas priežastinis genas tame pačiame GWAS lokuse;
  * monogeninis potipis;
  * atvirkštinis priežastingumas raiškos duomenyse;
  * rūšių skirtumai;
  * pleiotropija;
  * nepaskelbti neigiami rezultatai (mažas ištirtumas ne visada reiškia galimybę);
  * teksto gavybos artefaktai;
  * negrįžtama žala;
  * neteisinga moduliavimo kryptis.

  Prie kiekvieno nurodoma, kaip jį patikrinti.

## Įdiegimas

```bash
pip install -e .
```

AI sluoksniui (Claude):

```bash
pip install -e ".[ai]"
```

```bash
export ANTHROPIC_API_KEY=...
```

## Naudojimas

Galimybės ligai (kurie taikiniai turi įrodymų, bet mažai tirti):

```bash
discovery-agent discover --disease "idiopathic pulmonary fibrosis" --top 10
```

Naujos indikacijos žinomam taikiniui:

```bash
discovery-agent discover --target LRRK2 --top 10
```

Išsami hipotezė vienai porai:

```bash
discovery-agent hypothesis --target DPP9 --disease "idiopathic pulmonary fibrosis"
```

Hipotezes rašo Claude, o tyrėjo agentas prieš tai pats ieško papildomų įrodymų:

```bash
discovery-agent discover --disease "amyotrophic lateral sclerosis" --investigate --ai-top 3
```

Nuolatinė stebėsena (ciklas) arba vienkartinis paleidimas cron'ui ar CI:

```bash
discovery-agent watch config/watchlist.example.yaml
```

```bash
discovery-agent watch config/watchlist.example.yaml --once
```

Perspėjimai ir ID paieška:

```bash
discovery-agent alerts --db data/discovery.sqlite
```

```bash
discovery-agent search "pulmonary fibrosis"
```

Kalba: `--lang lt` (numatyta) arba `--lang en`. Rezultatai įrašomi į `reports/<laikas>_<užklausa>/`:
`report.md` (reitingas ir visos hipotezės), `result.json` (visi duomenys), `hypotheses/*.md` (po vieną failą
kiekvienai hipotezei).

### Pavyzdys (tikri duomenys, 2026-11-04)

`discovery-agent discover --disease "idiopathic pulmonary fibrosis" --top 5 --enrich 8`

| # | Taikinys | Galimybė | Įrodymai | Pagrindas | Spraga | Kryptis | Publ. |
|---|---|---|---|---|---|---|---|
| 1 | AKAP13 | 0.393 | 0.64 | genetika 0.64 | 0.78 | nenustatyta | 2 |
| 2 | PARN | 0.339 | 0.96 | genetika 0.87, kuruota genetika 0.85 | 0.49 | ⬆ aktyvinimas | 24 |
| 3 | MUC5AC | 0.330 | 0.83 | genetika 0.83 | 0.50 | nenustatyta | 17 |
| 4 | DPP9 | 0.320 | 0.71 | genetika 0.71 | 0.55 | nenustatyta | 8 |
| 5 | SFTPA2 | 0.306 | 0.98 | genetika 0.86, kuruota genetika 0.81 | 0.49 | ⬆ aktyvinimas | 20 |

Pavyzdžiui, MUC5AC hipotezėje agentas pats iškelia alternatyvą „kitas priežastinis genas tame pačiame
lokuse“ ir įvardija už 21 kb esantį MUC5B. Tai teisingas įspėjimas: MUC5B promotoriaus variantas yra gerai
žinomas IPF rizikos veiksnys.
PARN atveju jis pažymi „monogeninio potipio“ ribotumą ir sumažina pasitikėjimo lygį.

## AI sluoksnis (Claude)

* **Sintezatorius**: iš įrodymų rinkinio parašo hipotezę griežtos JSON schemos forma (structured outputs).
  Modelis gali cituoti tik įrodymų ID, kurie yra rinkinyje. Neegzistuojantys ID pašalinami ir parodomi
  ataskaitoje kaip įspėjimas.
* **Tyrėjas (agentas)**: Claude gauna įrankius tiems patiems API: Europe PMC paiešką ir santraukas, Open
  Targets įrodymus ir taikinių profilius, ChEMBL ir ClinicalTrials.gov. Jis ieško neigiamų rezultatų,
  tikrina kryptį ir mechanizmą, o radinius įrašo kaip naujus įrodymus. Radinys priimamas tik tada, kai jo
  nuorodos (PMID, DOI, NCT ID, URL) buvo grąžintos įrankio toje pačioje sesijoje. Taip apsisaugoma nuo
  išgalvotų šaltinių.
* Modelis: `claude-opus-5-5` (keičiamas per `DISCOVERY_MODEL` arba `--model`). Naudojamas adaptyvus mąstymas,
  o pastangų lygis nustatomas per `--effort` (numatyta `high`). Įjungti serverio pusės atsarginiai modeliai
  (`fallbacks: "default"`): jei saugumo klasifikatorius klaidingai atmeta biomedicininę užklausą, ją
  automatiškai perima rekomenduojamas modelis.
* Kaina: AI taikomas tik `--ai-top` geriausioms galimybėms. Stebėsenos režime AI taikomas tik naujoms arba
  pasikeitusioms galimybėms. Be `--ai` sistema veikia visiškai be API rakto: hipotezes tada rašo taisyklės.

## Vietinis MCP serveris: be API rakto, su jūsų prenumerata

Jei turite **Claude Pro/Max** (Claude Desktop arba Claude Code) ar **ChatGPT** prenumeratą (Codex CLI), API
raktas nereikalingas. Sistema veikia kaip vietinis **MCP serveris jūsų kompiuteryje**:

* **Mąsto jūsų kliento modelis** (pvz., Claude Claude Desktop programoje), todėl naudojamas jūsų prenumeratos
  limitas, o ne apmokestinamas API.
* **Serveris pateikia duomenis ir atmintį**:
  * visą atradimų konvejerį (9 šaltiniai, A–D įvertinimai);
  * tyrimo įrankius (literatūra, santraukos, Open Targets įrodymai, ChEMBL, klinikiniai tyrimai);
  * **vietinę SQLite duomenų bazę** (`~/.local/share/discovery-agent/discovery.sqlite`), kurioje saugomi
    įrodymų rinkiniai, jūsų hipotezės, stebėsenos paleidimai ir perspėjimai, o Markdown ataskaitos – šalia
    duomenų bazės.

> **Išsami diegimo instrukcija** (macOS, Windows, Linux; patikrinimas; problemų sprendimas):
> [`docs/MCP_DIEGIMAS.md`](docs/MCP_DIEGIMAS.md). Norite, kad viską įdiegtų ir patikrintų **Codex** agentas?
> Jam skirtas žingsnis po žingsnio planas yra [`docs/codex-install-claude-mcp.md`](docs/codex-install-claude-mcp.md).

```bash
pip install -e ".[mcp]"
```

Patikrinkite įdiegimą. Turi būti `9/9 checks passed`:

```bash
discovery-agent mcp --self-test --online
```

**Claude Code**: repozitorijoje jau yra [`.mcp.json`](.mcp.json). Atidarius projektą su Claude Code, serverį
pakaks patvirtinti. Kad serveris būtų pasiekiamas visuose projektuose:

```bash
claude mcp add --scope user scientific-discovery -- discovery-agent mcp
```

**Claude Desktop**: įrašykite serverį į konfigūraciją. Komanda pasidaro atsarginę kopiją ir kitų serverių
neliečia. Po to visiškai perkraukite programą:

```bash
discovery-agent mcp --install claude-desktop
```

**Codex CLI (OpenAI)**: įklijuokite į `~/.codex/config.toml`:

```bash
discovery-agent mcp --print-config codex
```

**Naudojimas.** Tiesiog paprašykite, pvz.: *„Rask mažai ištirtas terapines galimybes idiopatinei plaučių
fibrozei ir parašyk 3 hipotezes“*. Galima naudoti ir paruoštus užklausų šablonus (MCP prompts):

* `investigate_disease` – visas ciklas ligai;
* `write_hypothesis` – viena taikinio–ligos pora;
* `weekly_review` – stebėsenos sąrašo peržiūra ir naujų pokyčių tyrimas.

Claude Code juos rodo kaip `/mcp__scientific-discovery__investigate_disease`, Claude Desktop – meniu „+“.

| Įrankis | Ką daro |
|---|---|
| `discovery_find_opportunities` | Galimybių reitingas ligai arba naujos indikacijos taikiniui |
| `discovery_investigate_pair` | **Viskas vienu kvietimu**, suspaustai: balai, kryptis (su kolokalizacija), lokuso konkurentai, įvertinti įrodymai, sustabdyti tyrimai, saugumas, vaistai, alternatyvos, svarbiausi santraukų sakiniai su neigiamų rezultatų žymomis (~1,8 tūkst. žetonų) |
| `discovery_analyze_pair` | Pilnas įrodymų rinkinys (E1, E2… su A–D) ir taisyklėmis paremtas hipotezės juodraštis |
| `discovery_search_literature`, `discovery_read_abstracts` | Europe PMC straipsniai, preprintai, patentai, santraukos |
| `discovery_check_hypothesis` | Hipotezės juodraščio kokybės patikra be AI: įrodymų ID, prieštaravimai, alternatyvos su testais, eksperimentai su laukiamais rezultatais, saugumas, kryptis, patikimumas, perdėti teiginiai |
| `discovery_read_full_text` | Atviros prieigos straipsnio pilnas tekstas: sakiniai apie taikinį pagal skyrius, su neigiamų rezultatų žymomis |
| `discovery_get_open_targets_evidence`, `discovery_get_target_profile`, `discovery_get_chembl_profile`, `discovery_search_clinical_trials` | Įrodymai, taikinių profiliai, junginiai, klinikiniai tyrimai |
| `discovery_search_entities` | Ligų, genų ir vaistų ID paieška |
| `discovery_save_hypothesis` | Įrašo hipotezę į DB ir `.md` failą. Patikrina, ar cituojami įrodymų ID yra rinkinyje, o PMID, DOI ir NCT ID – ar tikrai egzistuoja |
| `discovery_list_hypotheses`, `discovery_get_hypothesis` | Išsaugotos hipotezės |
| `discovery_run_watchlist`, `discovery_list_alerts`, `discovery_status` | Stebėsena, perspėjimai ir būsena (stebėsenos sąrašas – `~/.local/share/discovery-agent/watchlist.yaml`) |

**Reguliarus paleidimas be API.**
* Deterministinė stebėsena (`discovery-agent watch …`, žr. žemiau) veikia per cron ir be jokio modelio.
* AI peržiūrą galima paleisti per Claude Code neinteraktyviu režimu. Jis naudoja jūsų prisijungimą, todėl
  sunaudoja prenumeratos limitą. Pavyzdys:

  ```bash
  claude -p "Atlik savaitinę peržiūrą su scientific-discovery įrankiais: discovery_run_watchlist, ištirk naujas galimybes ir išsaugok hipotezes." --allowedTools "mcp__scientific-discovery"
  ```

Duomenų katalogą galima pakeisti per `--data-dir` arba `DISCOVERY_DATA_DIR`.

## Claude Managed Agents versija (debesyje, be savo serverio)

Katalogas [`managed_agents/`](managed_agents/README.md) aprašo tą patį agentą kaip **Claude Managed Agents**
diegimą. Anthropic jį paleidžia kas pirmadienį savo debesyje. Vadovas (`claude-opus-5-5`) paleidžia šį
konvejerį iš repozitorijos, naujas galimybes išdalija tyrėjams (`claude-sonnet-5-5`), patikrina jų
nuorodas ir parašo hipotezes bei santrauką. Būsena tarp paleidimų laikoma Managed Agents atmintyje, o
kiekvienam paleidimui taikomas $20 biudžetas. Resursai aprašyti failais ir diegiami su `ant apply`;
`discovery-agent managed run|latest|pause|unpause|answer|serve-webhook` valdo paleidimus iš jūsų kompiuterio.

## Nuolatinė stebėsena

`watch` režimas kiekvienam stebėjimui:

1. paleidžia atradimų konvejerį;
2. palygina rezultatą su ankstesniu paleidimu: naujos top-K galimybės, iškritusios, reikšmingi balo pokyčiai;
3. ieško **naujų publikacijų, preprintų ir klinikinių tyrimų** apie geriausias galimybes nuo paskutinio
   paleidimo;
4. įrašo viską į SQLite atmintį ir sugeneruoja santrauką (`digest-*.md`);
5. jei nustatytas `notify.webhook_url` (Slack/Teams suderinamas), išsiunčia perspėjimą.

Pirmas paleidimas sukuria pradinį vaizdą be perspėjimų. Konfigūracijos pavyzdys yra
[`config/watchlist.example.yaml`](config/watchlist.example.yaml). Cron pavyzdys (kasdien 06:15):

```cron
15 6 * * * cd /path/to/scientific-discovery && discovery-agent watch config/watchlist.yaml --once
```

## Konfigūracija (aplinkos kintamieji)

| Kintamasis | Numatyta | Paskirtis |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Reikalingas tik `--ai` / `--investigate` (MCP serveriui nereikalingas) |
| `DISCOVERY_DATA_DIR` | `~/.local/share/discovery-agent` | MCP serverio vietinė DB, ataskaitos, hipotezės |
| `DISCOVERY_MODEL` | `claude-opus-5-5` | Claude modelis |
| `DISCOVERY_EFFORT` | `high` | `low` / `medium` / `high` / `xhigh` / `max` |
| `DISCOVERY_LANGUAGE` | `lt` | `lt` arba `en` |
| `DISCOVERY_CACHE_DIR` | `~/.cache/discovery-agent` | API atsakymų kešas (SQLite) |
| `DISCOVERY_CACHE_TTL_HOURS` | `24` | Kešo galiojimas |
| `DISCOVERY_MAX_WORKERS` | `6` | Lygiagretus praturtinimas |
| `NCBI_API_KEY`, `DISCOVERY_CONTACT_EMAIL` | – | Didesnis PubMed užklausų limitas |

## Apribojimai

* Ištirtumas matuojamas pagal tai, kiek kartų pora paminėta (pavadinimuose ir santraukose, su sinonimais ir
  tėvine liga). Tai geras, bet netobulas rodiklis. Labai trumpi arba dviprasmiški genų simboliai (pvz., `MET`)
  ieškomi pagal pilną pavadinimą.
* Patentai imami iš Europe PMC / EPO aprėpties, kuri nėra pilna. Lens.org arba PatentsView prijungti galima
  kaip papildomą šaltinį (reikia API rakto).
* Open Targets duomenys atnaujinami kas ketvirtį. Literatūra, preprintai ir klinikiniai tyrimai tikrinami
  kiekvieno paleidimo metu.
* Hipotezės yra **tyrimų kryptys mokslininkams, ne išvados**. Jos turi būti patikrintos eksperimentiškai.
  Tai nėra medicininė rekomendacija.

## Kūrimas

```bash
pip install -e ".[dev]"
```

Neprisijungę testai (fiktyvūs šaltiniai ir imituotas Claude):

```bash
pytest
```

Testai su tikrais viešais API:

```bash
pytest -m live
```

```
src/discovery_agent/
  sources/        API klientai: opentargets, literature (Europe PMC, PubMed, bioRxiv), chemistry (ChEMBL,
                  UniProt, PDB), clinical (ClinicalTrials.gov)
  analysis/       evidence (A–D vertinimas), direction, scoring, critique (pasitikėjimas, alternatyvos)
  pipeline.py     kandidatai → praturtinimas → balai
  hypothesis.py   hipotezės pagal taisykles
  llm/            Claude: synthesizer, investigator (agentas su įrankiais), dossier
  monitor/        store (SQLite arba JSON atmintis), watcher (stebėsena, perspėjimai)
  managed.py      Claude Managed Agents: paleidimai, rezultatai, webhook'as
  mcp_server.py   vietinis MCP serveris (Claude Desktop / Claude Code / Codex, be API rakto)
  report/         Markdown ataskaitos
  agent.py, cli.py
```

---

## English summary

An AI agent that continuously mines **Open Targets** (20+ integrated evidence sources), **Europe PMC**,
**PubMed**, **bioRxiv/medRxiv**, **ChEMBL**, **UniProt**, **RCSB PDB**, **ClinicalTrials.gov** and **patents**.
It looks for target–disease pairs with strong causal evidence but little research attention, and writes
evidence-graded hypotheses. Each hypothesis includes:

* the hypothesis and the inferred direction of modulation (inhibit vs. activate, from LoF/GoF genetics);
* an A–D reliability grade for every evidence item;
* a GRADE-style confidence level;
* alternative explanations, each with a discriminating test;
* proposed experiments, safety considerations and the competitive landscape.

The deterministic pipeline works without any API key. With `--ai`, Claude (`claude-opus-5-5`, adaptive
thinking, schema-constrained output) writes the hypothesis, and every citation is checked against the
dossier. With `--investigate`, a tool-using Claude agent first queries the same databases for negative
results and mechanistic links. It can only record findings whose references were actually returned by its
tools. `discovery-agent watch` runs the agent on a schedule, diffs each run against the previous one, tracks
new papers, preprints and trials, and sends alerts. Use `--lang en` for English reports.

`discovery-agent mcp` runs a **local MCP server** (stdio) for Claude Desktop, Claude Code, Codex or any MCP
client. The client's own model does the reasoning, so it runs on your Claude or ChatGPT subscription and needs
no API key. The server provides the pipeline, the investigation tools and a local SQLite database of dossiers,
saved hypotheses (with checks of cited evidence ids, PMIDs, DOIs and NCT ids), watchlist runs and alerts.

`managed_agents/` deploys the same agent on **Claude Managed Agents**: a weekly scheduled deployment in which
a lead agent (`claude-opus-5-5`) runs this pipeline from the mounted repository, delegates each new
candidate to an investigator agent (`claude-sonnet-5-5`; dynamic workflows when there are many), verifies
the findings, and writes hypotheses and a digest. State persists in a memory store, a rubric-graded outcome
checks each run, and a $20 budget caps it. The resources are files synced with `ant apply`.
