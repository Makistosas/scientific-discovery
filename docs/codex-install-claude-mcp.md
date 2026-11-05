# Runbook for a coding agent (Codex): install the `scientific-discovery` MCP server into Claude and verify it

> Lietuviškai: tai žingsnis po žingsnio instrukcija agentui (pvz., OpenAI Codex su „computer use“).
> Ji nurodo įdiegti šio projekto MCP serverį, prijungti jį prie Claude Desktop ir Claude Code, ir patikrinti
> visą grandinę. Paleiskite Codex šioje repozitorijoje ir parašykite: *„Įdiek šio projekto MCP serverį į Claude
> pagal docs/codex-install-claude-mcp.md ir viską patikrink.“*

## Goal

On **this computer**:

1. install the project into its own virtual environment;
2. connect the local MCP server to **Claude Desktop**, and to **Claude Code** if it is installed;
3. prove that it works:
   * offline tests;
   * a stdio self-test of the exact command Claude will run;
   * a real tool call inside Claude Desktop, using computer use.

The server needs **no API key**: Claude's own model does the reasoning on the user's subscription.

## Rules

* Do not ask for, create or store API keys or passwords. None are needed.
* Install only into the project's `.venv`. No `sudo`, no global `pip install`, no changes to system Python.
* Change only the following outside the repository:
  * the `scientific-discovery` entry in Claude Desktop's config, written through the project's own
    `--install` command, which backs up the file and keeps every other server and setting;
  * the `scientific-discovery` server in Claude Code's user config;
  * the data directory the server creates.
* Never delete or rewrite other MCP servers, and never hand-edit Claude's config if `--install` reports invalid
  JSON. Stop and show the user the error instead.
* Do not commit, push, or modify tracked files in the repository.
* Before you quit Claude Desktop, tell the user you are about to restart it.
* Do not run long research tasks in Claude, such as `investigate_disease`: they use the user's subscription
  limits. The verification below uses two quick tool calls only.
* If a step fails, follow the troubleshooting table, retry once, then stop and report what failed with the exact
  output.

## Step 0 — Detect the platform

* Determine the OS: macOS, Windows or Linux.
* **Windows:** run every command in **PowerShell on Windows**, not inside WSL. Claude Desktop is a Windows app
  and cannot start a Python that lives inside WSL.
* Check the prerequisites and record their versions:
  * Python ≥ 3.10: `python3 --version` (macOS/Linux) or `py -3 --version` (Windows);
  * `git --version`.
* If Python ≥ 3.10 is missing, stop and ask the user to install it from <https://www.python.org/downloads/>
  (macOS: Homebrew `brew install python` is fine too).
* Check whether Claude Desktop is installed:
  * macOS: `/Applications/Claude.app` exists;
  * Windows: "Claude" in the Start menu, or `%LOCALAPPDATA%\AnthropicClaude`.

  If it is not installed, tell the user (download: <https://claude.ai/download>) and continue with the other
  steps.
* Check whether Claude Code is installed: `claude --version`.

Below, `BIN` means `.venv/bin` on macOS/Linux and `.venv\Scripts` on Windows. Use paths that match the OS.

## Step 1 — Get the code

* If the current directory is already this repository (it contains `pyproject.toml` with
  `name = "scientific-discovery-agent"`), run `git pull`.
* Otherwise run `git clone https://github.com/Makistosas/scientific-discovery.git` into the user's home
  directory, then `cd scientific-discovery`.
* Record the absolute path of the repository as `REPO`.

## Step 2 — Virtual environment and install

```bash
python3 -m venv .venv          # Windows: py -3 -m venv .venv
BIN/pip install --upgrade pip
BIN/pip install -e ".[dev]"    # includes the MCP SDK and the test dependencies
```

**Expected:** the installs finish without errors, and `BIN/discovery-agent --version` prints a version.

## Step 3 — Offline tests

```bash
BIN/python -m pytest -q
```

**Expected:** every test passes (more than 100 at the time of writing; some are deselected live tests, which is
fine). If tests fail, stop and report the failure output; do not continue to the client setup.

## Step 4 — Self-test of this installation

```bash
BIN/discovery-agent mcp --self-test --online
```

**Expected:** `9/9 checks passed`:

* the server starts over stdio;
* 21 tools and 3 prompts are present;
* the local database works;
* Open Targets, Europe PMC, ClinicalTrials.gov and ChEMBL are reachable through the server.

If only `online:` checks fail, the network blocks those hosts. Report it, then continue: the client setup can
still be done.

## Step 5 — Connect Claude Desktop

1. Write the config entry:

   ```bash
   BIN/discovery-agent mcp --install claude-desktop
   ```

   It prints the config path (`CONFIG`), the backup path, the command it wrote (the absolute path of
   `BIN`'s Python) and any other servers it kept.
2. Confirm that `CONFIG` is the file Claude really uses. In Claude Desktop open **Settings → Developer →
   Edit Config** (use computer use). If it opens or reveals a different file, run the install again with
   `--config-path "<that file>"` and use that path as `CONFIG` from now on.
3. Self-test exactly what Claude Desktop will launch:

   ```bash
   BIN/discovery-agent mcp --self-test --config-path "CONFIG"
   ```

   **Expected:** `5/5 checks passed`. The `discovery_status` line shows the data directory.
4. Tell the user, then **quit Claude Desktop completely**. Closing the window is not enough.
   * macOS: menu Claude → Quit, or `osascript -e 'quit app "Claude"'`.
   * Windows: right-click the Claude icon in the system tray → Quit.

   Start it again:
   * macOS: `open -a Claude`;
   * Windows: Start menu → Claude.

## Step 6 — Verify inside Claude Desktop (computer use)

1. Open a **new chat**.
2. Open the tools / connectors menu next to the message box and confirm that **scientific-discovery** is listed
   and enabled, with its tools (for example `discovery_status`). Take a screenshot.
3. Send: `Call the discovery_status tool and show me the data_dir.` When Claude asks for permission to use the
   tool, choose **Allow once**. Let the user decide later whether to allow it always.
   **Expected:** the reply contains a path ending in `discovery-agent` and the watchlist path.
4. Send: `Use discovery_search_entities to find the Open Targets id of idiopathic pulmonary fibrosis.`
   **Expected:** `EFO_0000768`.
5. If the server is missing or shows an error, read the logs:
   * macOS: `~/Library/Logs/Claude/mcp-server-scientific-discovery.log` and `mcp.log`;
   * Windows: `%APPDATA%\Claude\logs\`.

   Fix the cause using the troubleshooting table, re-run Step 5.3, restart Claude Desktop, and repeat this step
   once.

## Step 7 — Connect Claude Code (only if `claude` is installed)

```bash
claude mcp get scientific-discovery
```

* If a user-scope entry already exists, remove it first: `claude mcp remove scientific-discovery -s user`.
* Then add it with the **absolute** path. On macOS/Linux:

  ```bash
  claude mcp add --scope user scientific-discovery -- "REPO/.venv/bin/discovery-agent" mcp
  ```

  On Windows:

  ```powershell
  claude mcp add --scope user scientific-discovery -- "REPO\.venv\Scripts\discovery-agent.exe" mcp
  ```

**Expected:** `claude mcp get scientific-discovery` shows the server as connected. Optional quick check (it uses
the user's subscription, so keep it to this one call):

```bash
claude -p "Call the discovery_status tool and print only the data_dir value." --allowedTools "mcp__scientific-discovery"
```

## Step 8 — Optional: Codex itself

Do this only if the user asks to use the server from Codex as well:

1. Run `BIN/discovery-agent mcp --print-config codex`.
2. Append the printed block to `~/.codex/config.toml`, keeping all existing content. Make a backup copy first.

## Step 9 — Report

Finish with a short report for the user:

| Check | Result |
|---|---|
| Python / git versions | … |
| Offline tests (`pytest -q`) | N passed |
| Self-test (`--online`) | 9/9 or which failed |
| Claude Desktop config | `CONFIG`, backup path, other servers kept |
| Self-test of Claude Desktop config | 5/5 |
| Claude Desktop UI: server listed, `discovery_status`, `EFO_0000768` | PASS/FAIL + screenshot |
| Claude Code (`claude mcp get`) | connected / not installed |

Also list:
* the repository path, the data directory and the log locations;
* anything that needs the user's action, for example installing Claude Desktop, allowing the tool permanently,
  or a network block.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No module named 'mcp'` | `BIN/pip install -e ".[dev]"` (or `".[mcp]"`) |
| `--install`: "not valid JSON" | Stop. Show the user the file and the error; do not overwrite it |
| `--self-test --config-path`: "was not found" | The command path in the config is wrong. Re-run `--install` from the correct `REPO` |
| Self-test passes but Claude Desktop does not show the server | The app was not fully restarted, or it uses another config file (Step 5.2) |
| Windows: server fails to start from Claude Desktop | The venv was created in WSL. Recreate it with `py -3 -m venv .venv` in PowerShell and re-run Steps 2–6 |
| Only `online:` checks fail | Network or firewall blocks `api.platform.opentargets.org`, `www.ebi.ac.uk`, `clinicaltrials.gov` or `rest.uniprot.org`. Report it |
| Tool call times out on a first big search | Expected only for large searches on a cold cache. The verification calls above are small |
