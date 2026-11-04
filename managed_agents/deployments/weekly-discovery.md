---
# yaml-language-server: $schema=https://platform.claude.com/schemas/ant/beta/deployment.json
name: weekly-scientific-discovery
description: >-
  Every Monday morning (Vilnius time): run the discovery pipeline for the watchlist, investigate new or
  changed opportunities and write evidence-graded hypotheses plus a digest.
agent: ../agents/discovery-lead.md
environment_id: ../environments/discovery-cloud.yaml
resources:
  - type: github_repository            # public repository: no token needed
    url: https://github.com/Makistosas/scientific-discovery
    mount_path: /workspace/scientific-discovery
    checkout: {type: branch, name: main}
  - path: ../memory_stores/discovery-memory.yaml
    access: read_write
schedule:
  type: cron
  expression: "40 6 * * 1"
  timezone: Europe/Vilnius
budget:                                 # hard cap per run (cents, USD): $20.00 — includes workflow runs
  type: limit
  max_list_cost: {amount: "2000", currency: USD}
initial_events:
  - type: user.define_outcome
    description: |
      Run this week's scientific discovery cycle for every watch in
      /workspace/scientific-discovery/managed_agents/watchlist.yaml, following your system prompt: run the
      pipeline, compare with the previous run, investigate up to three new or changed top opportunities per
      watch, write evidence-graded hypotheses and DIGEST.md to /mnt/session/outputs/, and update your notes in
      the memory store.
    max_iterations: 2
    rubric:
      type: text
      content: |
        # Starter rubric — tune it to your needs
        1. `/mnt/session/outputs/DIGEST.md` exists and covers every watch in the watchlist: run date, what changed
           since the previous run (or that this is the first run), the candidates investigated with a one-line
           conclusion and confidence each, and any failures.
        2. For every watch that did not fail, the pipeline outputs exist:
           `/mnt/session/outputs/pipeline/<watch>/latest/report.md` and `latest/result.json`.
        3. For every watch, up to three candidates were investigated (new or changed top opportunities first),
           each with a hypothesis file at `/mnt/session/outputs/<watch>/hypotheses/<SYMBOL>.md`.
        4. Every hypothesis states the direction of modulation (inhibition, activation, or explicitly unknown)
           and has these sections: mechanistic rationale; supporting and contradicting evidence; overall
           confidence with reasons; why the pair is under-explored; at least two alternative explanations, each
           with a discriminating test; at least two experiments, each with the expected result if true and if
           false; safety considerations; competitive landscape.
        5. Every evidence item cited in a hypothesis has a reliability grade A–D with a one-line justification
           and a reference (PMID, DOI, NCT id or database URL) that appears in the pipeline outputs or in an
           investigator's findings file. No reference is invented.
        6. Each hypothesis says what was checked to explain the low attention (including stopped or terminated
           trials) and whether it looks like a genuine gap, a hard target, or a possible unpublished failure.
        7. Reports are written in the watchlist's language, keep gene symbols and ids unchanged, and end with the
           statement that this is a research hypothesis, not medical advice.
        8. `notes/<watch>.md` in the memory store was updated with the date, candidates investigated and
           conclusions, and contains no credentials or secrets.
---
