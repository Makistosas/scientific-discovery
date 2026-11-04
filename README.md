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
| `ANTHROPIC_API_KEY` | – | Reikalingas tik `--ai` / `--investigate` |
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
  monitor/        store (SQLite atmintis), watcher (stebėsena, perspėjimai)
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
