# Vietinio MCP serverio diegimas

Ši instrukcija paaiškina, kaip savo kompiuteryje įdiegti mokslinių atradimų agentą kaip **MCP serverį** ir
prijungti jį prie **Claude Desktop**, **Claude Code** arba **Codex**. Mąsto jūsų programos modelis, todėl
naudojamas prenumeratos limitas (Claude Pro/Max arba ChatGPT), o API raktas nereikalingas.

> Norite, kad viską įdiegtų ir patikrintų **Codex** agentas? Atidarykite repozitoriją Codex ir parašykite:
> *„Įdiek šio projekto MCP serverį į Claude pagal docs/codex-install-claude-mcp.md ir viską patikrink.“*

## Ko reikia

* **Python 3.10** ar naujesnio. Patikrinimas: macOS/Linux `python3 --version`, Windows `py -3 --version`.
  Atsisiuntimas – <https://www.python.org/downloads/>.
* **git** (macOS jį pasiūlo įdiegti pirmą kartą paleidus `git`, Windows – <https://git-scm.com/>).
* **Claude Desktop** (<https://claude.ai/download>), **Claude Code** arba **Codex CLI**, prisijungę su jūsų
  paskyra.
* Interneto prieiga prie viešų duomenų bazių: Open Targets, EBI (Europe PMC, ChEMBL), NCBI, UniProt, RCSB PDB,
  ClinicalTrials.gov, bioRxiv.

## 1. Atsisiųsti ir įdiegti

**macOS / Linux** (terminale):

```bash
git clone https://github.com/Makistosas/scientific-discovery.git
```

```bash
cd scientific-discovery
```

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -e ".[mcp]"
```

**Windows** (PowerShell):

```powershell
git clone https://github.com/Makistosas/scientific-discovery.git
```

```powershell
cd scientific-discovery
```

```powershell
py -3 -m venv .venv
```

```powershell
.venv\Scripts\pip install -e ".[mcp]"
```

> **Windows ir WSL:** jei Claude Desktop įdiegtas Windows, diekite su Windows Python (PowerShell), ne WSL viduje.
> Windows programa negali tiesiogiai paleisti Linux aplinkoje įdiegto Python.

Toliau komandos parašytos macOS/Linux forma. Windows vietoje `.venv/bin/discovery-agent` rašykite
`.venv\Scripts\discovery-agent`.

## 2. Patikrinti įdiegimą

```bash
.venv/bin/discovery-agent mcp --self-test --online
```

Komanda paleidžia serverį taip pat, kaip jį paleis Claude, ir patikrina:

* ar serveris startuoja;
* ar yra visi 15 įrankių ir 3 šablonai;
* ar veikia vietinė duomenų bazė;
* ar pasiekiami Open Targets, Europe PMC, ClinicalTrials.gov ir ChEMBL.

Turi būti `9/9 checks passed`.

## 3. Prijungti prie programos

### Claude Desktop

1. Įrašykite serverį į Claude Desktop konfigūraciją:

   ```bash
   .venv/bin/discovery-agent mcp --install claude-desktop
   ```

   Komanda:
   * pasidaro atsarginę failo kopiją (`claude_desktop_config.json.bak-…`);
   * kitų MCP serverių ir nustatymų neliečia;
   * į konfigūraciją įrašo tikslų `.venv` Python kelią.

   Numatytoji failo vieta:
   * macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   * Windows: `%APPDATA%\Claude\claude_desktop_config.json`

   Jei Claude Desktop **Settings → Developer → Edit Config** atidaro kitą failą, nurodykite jį:
   `--config-path "<tas failas>"`. Duomenis norite laikyti kitur? Pridėkite `--data-dir ~/Documents/discovery`.
2. Patikrinkite būtent tai, ką paleis Claude Desktop:

   ```bash
   .venv/bin/discovery-agent mcp --self-test --config-path "$HOME/Library/Application Support/Claude/claude_desktop_config.json"
   ```

   (Windows: `--config-path "$env:APPDATA\Claude\claude_desktop_config.json"`.)
3. **Visiškai uždarykite** Claude Desktop ir atidarykite iš naujo:
   * macOS: meniu Claude → Quit arba `Cmd+Q`;
   * Windows: dešiniuoju pelės mygtuku spustelėkite Claude piktogramą sistemos dėkle → Quit.

   Vien lango uždarymas neperkrauna serverių.
4. Naujame pokalbyje įrankių meniu (prie žinutės laukelio) turi matytis `scientific-discovery`. Parašykite
   *„Parodyk discovery_status“* ir leiskite naudoti įrankį. Atsakyme turi būti duomenų katalogo kelias.

### Claude Code

Pridėkite serverį visiems projektams. Komandą paleiskite iš repozitorijos katalogo, kad `$(pwd)` taptų
absoliučiu keliu:

```bash
claude mcp add --scope user scientific-discovery -- "$(pwd)/.venv/bin/discovery-agent" mcp
```

Windows (PowerShell): `claude mcp add --scope user scientific-discovery -- "$PWD\.venv\Scripts\discovery-agent.exe" mcp`.

Patikrinkite (turi būti „Connected“):

```bash
claude mcp get scientific-discovery
```

Repozitorijoje esantis `.mcp.json` veikia tik tada, kai prieš paleidžiant `claude` aktyvuojate `.venv`. Tada
Claude Code paprašys jį patvirtinti.

### Codex CLI (ChatGPT prenumerata)

```bash
.venv/bin/discovery-agent mcp --print-config codex
```

Atspausdintą bloką pridėkite į `~/.codex/config.toml` ir perkraukite Codex.

## 4. Naudojimas

* Paprastas prašymas: *„Rask mažai ištirtas terapines galimybes idiopatinei plaučių fibrozei ir parašyk 3
  hipotezes lietuviškai.“* Modelis pats:
  * suranda galimybes ir išanalizuoja geriausias;
  * patikrina literatūrą ir klinikinius tyrimus;
  * įrašo hipotezes į duomenų bazę. Įrašant patikrinama, ar cituojami įrodymų ID yra rinkinyje ir ar PMID,
    DOI ir NCT ID tikrai egzistuoja.
* Paruošti šablonai:
  * `investigate_disease` – visas ciklas ligai;
  * `write_hypothesis` – viena taikinio–ligos pora;
  * `weekly_review` – stebėsenos peržiūra.

  Claude Code juos rodo kaip `/mcp__scientific-discovery__investigate_disease`, Claude Desktop – meniu „+“.
* Pirmas tyrimas ligai užtrunka iki minutės. Vėliau duomenys imami iš kešo ir viskas veikia per kelias
  sekundes.
* Kiekvienas įrankio kvietimas naudoja jūsų prenumeratos limitą. Ilgas tyrimas su daug kandidatų jį sunaudoja
  greičiau.

## Kur saugomi duomenys

Numatytasis katalogas – `~/.local/share/discovery-agent/` (Windows:
`C:\Users\<vardas>\.local\share\discovery-agent\`). Jį keičia `--data-dir` arba `DISCOVERY_DATA_DIR`.

| Kelias | Turinys |
|---|---|
| `discovery.sqlite` | duomenų bazė: įrodymų rinkiniai, hipotezės, stebėsena, perspėjimai |
| `hypotheses/*.md` | išsaugotos hipotezės |
| `reports/` | galimybių ataskaitos ir porų analizės |
| `watchlist.yaml` | stebimos ligos ar taikiniai (redaguokite laisvai) |
| `watch/` | stebėsenos ataskaitos ir santraukos |

API atsakymų kešas laikomas `~/.cache/discovery-agent/`.

## Atnaujinimas

```bash
git pull
```

```bash
.venv/bin/pip install -e ".[mcp]"
```

```bash
.venv/bin/discovery-agent mcp --self-test
```

Tada perkraukite Claude Desktop arba Claude Code.

## Reguliarus paleidimas be API

* **Stebėsena be modelio** (nemokama). Pakartoja paiešką ir praneša apie naujus straipsnius ar tyrimus:
  `discovery-agent watch <watchlist.yaml> --once`. Ją galima paleisti per cron (macOS/Linux) arba Task
  Scheduler (Windows).
* **AI peržiūra su prenumerata**. Claude Code gali veikti neinteraktyviai su jūsų prisijungimu:

  ```bash
  claude -p "Atlik savaitinę peržiūrą su scientific-discovery įrankiais: discovery_run_watchlist, ištirk naujas galimybes ir išsaugok hipotezes." --allowedTools "mcp__scientific-discovery"
  ```

## Jei kas nors neveikia

| Požymis | Ką daryti |
|---|---|
| Claude Desktop nemato serverio | Paleiskite `--self-test --config-path <failas>`. Patikrinkite, ar JSON be klaidų. Programą perkraukite **visiškai** |
| Claude Desktop žurnalai | macOS `~/Library/Logs/Claude/` (`mcp-server-scientific-discovery.log`, `mcp.log`); Windows `%APPDATA%\Claude\logs\` |
| `No module named 'mcp'` | `.venv/bin/pip install -e ".[mcp]"` |
| `--install` sako „not valid JSON“ | Konfigūracijos faile yra klaida. Ištaisykite ją arba perkelkite failą (komanda tokio failo neliečia) |
| Pirmas tyrimas nutrūksta dėl laiko | Pakartokite (antrą kartą duomenys jau keše) arba prašykite mažiau kandidatų. Claude Code: `MCP_TOOL_TIMEOUT` (ms) |
| `online:` patikrinimai nepraeina | Tinklas ar įmonės užkarda blokuoja viešų duomenų bazių adresus |
| Interaktyvus patikrinimas | `npx @modelcontextprotocol/inspector .venv/bin/python -m discovery_agent mcp` (reikia Node.js) |
