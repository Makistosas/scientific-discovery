# Claude Managed Agents versija

Ši versija veikia **Anthropic debesyje**: nereikia savo serverio, cron'o ar Anthropic API rakto
sandbox'e. Anthropic pati kartą per savaitę paleidžia agentą izoliuotame konteineryje. Agentas:

1. prisijungia šią repozitoriją (`main` šaką) ir paleidžia deterministinį atradimų konvejerį
   (`python -m discovery_agent watch ...`) visoms ligoms iš [`watchlist.yaml`](watchlist.yaml);
2. palygina rezultatus su praėjusia savaite. Būsena laikoma Managed Agents **atminties saugykloje**
   (`/mnt/memory/discovery-memory/`), todėl išlieka tarp paleidimų;
3. naujas ar pasikeitusias galimybes išdalija **tyrėjams** (atskiras pigesnis agentas, `claude-sonnet-5-5`):
   kiekvienas tyrėjas vienu metu tiria vieną kandidatą ir ieško neigiamų rezultatų, sustabdytų tyrimų,
   poveikio krypties ir mechanizmo;
4. **vadovas** (`claude-opus-5-5`) patikrina tyrėjų nuorodas, parašo hipotezes su A–D patikimumo
   įvertinimais, alternatyviais paaiškinimais ir eksperimentais bei bendrą santrauką `DIGEST.md`;
5. atskiras vertintojas pagal kriterijų sąrašą (rubric) patikrina rezultatą. Jei kriterijai neįvykdyti,
   agentas pataiso (iki 2 iteracijų).

```
managed_agents/
  agents/discovery-lead.md            vadovas (Opus 5.5): sistema + komanda + darbų eigos
  agents/evidence-investigator.md     tyrėjas (Sonnet 5.5): vienas kandidatas → findings JSON
  environments/discovery-cloud.yaml   debesies konteineris; tinklas tik į biomedicinos API ir PyPI
  memory_stores/discovery-memory.yaml atmintis tarp paleidimų
  deployments/weekly-discovery.md     tvarkaraštis (pirmadieniais 06:40 Vilniaus laiku), biudžetas, kriterijai
  watchlist.yaml                      ką stebėti (skaitoma paleidimo metu iš main šakos)
  claude-lock.json                    atsiras po `ant apply` – commit'inkite jį
```

## Diegimas

1. **Sulieti PR į `main`.** Agentas kiekvieną kartą prisijungia `main` šaką, todėl kodas ir `watchlist.yaml`
   turi būti ten.
2. **Įdiegti `ant` CLI** (≥ 1.40.0, nes agento faile kiti agentai nurodomi kaip failų keliai) ir prisijungti.
   Diegimo instrukcijos: <https://github.com/anthropics/anthropic-cli/releases>.

   ```bash
   ant auth login
   ```

3. **Peržiūrėti planą ir sukurti resursus** (iš `managed_agents/` katalogo, kad `claude-lock.json` atsirastų
   čia):

   ```bash
   cd managed_agents
   ```

   ```bash
   ant apply --dry-run -v agents/discovery-lead.md agents/evidence-investigator.md environments/discovery-cloud.yaml memory_stores/discovery-memory.yaml deployments/weekly-discovery.md
   ```

   ```bash
   ant apply agents/discovery-lead.md agents/evidence-investigator.md environments/discovery-cloud.yaml memory_stores/discovery-memory.yaml deployments/weekly-discovery.md
   ```

4. **Commit'inti `managed_agents/claude-lock.json`.** Jame saugomi resursų ID; be jo kitas `ant apply` sukurtų
   dublikatus.
5. **Bandomasis paleidimas** iš repozitorijos šaknies. Paleidžia agentą iš karto, seka sesiją ir atsisiunčia
   rezultatus į `reports/managed/<session>/`. Reikia `pip install -e ".[ai]"` ir prisijungimo
   (`ANTHROPIC_API_KEY` arba `ant auth login`):

   ```bash
   discovery-agent managed run
   ```

## Kasdienis naudojimas

Naujausio paleidimo rezultatai (DIGEST.md, hipotezės, konvejerio ataskaitos):

```bash
discovery-agent managed latest
```

Pristabdyti arba vėl įjungti tvarkaraštį:

```bash
discovery-agent managed pause
```

```bash
discovery-agent managed unpause
```

* **Ką stebėti** keičiama `watchlist.yaml` faile `main` šakoje. Iš naujo diegti nereikia, kitas paleidimas
  pasiims pakeitimą.
* **Agento elgsena** (sistemos instrukcijos, modeliai, įrankiai, tvarkaraštis, biudžetas, kriterijai) keičiama
  failuose po `agents/` ir `deployments/`. Po pakeitimo paleiskite tą pačią `ant apply` komandą: agentas gaus
  naują versiją.
* Sesiją galima stebėti gyvai Console: `https://platform.claude.com/workspaces/default/sessions/<session>`.

## Leidimai ir webhook'as

Visiems įrankiams nustatyta `auto` politika: Anthropic serveris pats įvertina kiekvieną kvietimą. Saugius
vykdo, pavojingus atmeta, o kai negali nuspręsti, kvietimą **sustabdo** ir laukia patvirtinimo. Suplanuoto
paleidimo niekas nestebi, todėl tokį kvietimą reikia atsakyti, kitaip sesija lauks neribotai. Būdai:

* **Webhook'as (rekomenduojama).** Paleiskite `discovery-agent managed serve-webhook --port 8080` už HTTPS
  (443 prievadas, viešas adresas). Console → Manage → Webhooks užregistruokite URL su įvykiais
  `session.status_idled` ir `deployment_run.failed`, o gautą `whsec_…` paslaptį nustatykite kaip
  `ANTHROPIC_WEBHOOK_SIGNING_KEY`. Webhook'as atsako į sustabdytus kvietimus ir atsisiunčia rezultatus, kai
  paleidimas baigiasi.
* **Rankiniu būdu** – `discovery-agent managed answer <session_id>` arba Console sesijos lange.

Pagal nutylėjimą sustabdyti kvietimai **atmetami**, o agentui paaiškinama, kad reikia naudoti
`python -m discovery_agent` komandas. Jei norite kai kurias komandas leisti automatiškai, nustatykite
`DISCOVERY_AUTO_ALLOW`, pvz. `python -m discovery_agent,pip install`. Leidžiamos tik paprastos komandos su
šiais prefiksais, be `;`, `|`, `&`, `$` ir peradresavimų.

## Kaina ir dinaminės darbų eigos (workflows)

* Kiekvienam paleidimui taikomas **griežtas $20 biudžetas** (`budget` faile `deployments/weekly-discovery.md`,
  suma nurodoma centais: `"2000"`). Pasiekus ribą, sesija sustoja.
* Vadovui įjungtos **dinaminės darbų eigos**. Kai tirti reikia daugiau nei šešių kandidatų, vadovas paleidžia
  darbų eigą: daug tyrėjų dirba lygiagrečiai, etapais, fone. Mažesnius kiekius jis išdalija tiesiogiai
  tyrėjams, o vieną kandidatą tiria pats. Kiekvienas eigoje dirbantis agentas naudoja žetonus, todėl ir
  reikalingas biudžetas. Išjungti: `agents/discovery-lead.md` faile nustatykite
  `workflows: {type: disabled}` ir paleiskite `ant apply`.
* Kainą mažina: mažiau ligų `watchlist.yaml` faile, mažesnis `max_iterations` (vertintojo ciklai), tyrėjo
  `effort: low`.

## Tinklas ir saugumas

* Konteinerio tinklas `limited`: pasiekiami tik Open Targets, Europe PMC/ChEMBL (`www.ebi.ac.uk`), PubMed,
  bioRxiv, UniProt, RCSB PDB, ClinicalTrials.gov ir PyPI. `web_search`/`web_fetch` išjungti. Agentas duomenis
  gauna per projekto įrankius (`python -m discovery_agent tool ...`).
* Repozitorija vieša, todėl jai prijungti prieigos rakto nereikia ir konfigūracijoje nėra jokių paslapčių.
  Jei repozitorija taps privati, deployment'ui reikės `authorization_token` (GitHub PAT su Contents: Read).
* Atmintyje niekada nesaugomi raktai ar slaptažodžiai.
