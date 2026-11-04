# AGENTS.md

Guidance for coding agents (Codex, Claude Code, …) working in this repository.

## What this is

An AI scientific discovery agent. It mines public biomedical databases for target–disease opportunities with
strong causal evidence but little research attention, and writes evidence-graded hypotheses. The databases are
Open Targets, Europe PMC, PubMed, bioRxiv/medRxiv, ChEMBL, UniProt, RCSB PDB, ClinicalTrials.gov and patents.

It can be used in four ways:

* `discovery-agent` CLI, deterministic, no API key needed;
* `--ai` / `--investigate`, using the Claude API;
* `managed_agents/` resource files for a Claude Managed Agents deployment;
* a **local MCP server** (`discovery-agent mcp`), which runs on a Claude/ChatGPT subscription.

**Installing the MCP server into Claude on this machine?** Follow
[`docs/codex-install-claude-mcp.md`](docs/codex-install-claude-mcp.md) step by step.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Checks to run before finishing a change

```bash
.venv/bin/python -m pyflakes src tests     # lint (pip install pyflakes if missing)
.venv/bin/python -m pytest -q              # offline suite: fake sources, fake Claude client
.venv/bin/python -m pytest -q -m live      # optional: real public APIs (network)
```

## Layout

* `src/discovery_agent/sources/` – API clients (Open Targets GraphQL, Europe PMC, PubMed, bioRxiv, ChEMBL, UniProt,
  PDB, ClinicalTrials.gov).
* `src/discovery_agent/analysis/` – evidence grading (A–D), direction of modulation, scoring, critique.
* `src/discovery_agent/pipeline.py` – candidate generation → enrichment → ranking.
* `src/discovery_agent/hypothesis.py` – rule-based hypotheses.
* `src/discovery_agent/llm/` – Claude synthesizer and tool-using investigator (`ToolSession` holds the vetted
  tools).
* `src/discovery_agent/mcp_server.py` – local MCP server, `--install claude-desktop`, `--self-test`.
* `src/discovery_agent/monitor/` – watchlist runs, SQLite/JSON state, alerts.
* `src/discovery_agent/managed.py` and `managed_agents/` – Claude Managed Agents deployment.
* `tests/conftest.py` – offline fakes for every source. Extend them instead of calling real APIs in unit tests.

## Conventions

* User-facing text goes through `discovery_agent.i18n.tr` with both `lt` and `en` entries; a test enforces
  this. Lithuanian is the default language.
* Never invent references. Evidence keeps its source references, and the AI layers check citations.
* No secrets in the repository. The deployment files must stay secret-free (a test checks this).
* Keep network politeness: HTTP goes through `discovery_agent.http.HttpClient` (cache, per-host rate limits,
  retries).
