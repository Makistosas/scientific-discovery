"""Command-line interface: ``discovery-agent --help``."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from . import __version__
from .agent import DiscoveryAgent
from .analysis.scoring import ScoringWeights
from .config import Settings
from .http import HttpClient
from .report import ranking_table, render_hypothesis
from .sources import Sources


def _progress(msg: str) -> None:
    click.echo(msg, err=True)


def _default_out(prefix: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in prefix)[:60]
    return Path("reports") / f"{stamp}_{safe}"


@click.group()
@click.version_option(__version__, prog_name="discovery-agent")
@click.option("--lang", type=click.Choice(["lt", "en"]), default=None, help="Report language (default: lt).")
@click.option("--model", default=None, help="Claude model for --ai (default: claude-opus-5-5).")
@click.option("--effort", type=click.Choice(["low", "medium", "high", "xhigh", "max"]), default=None,
              help="Claude effort level for --ai (default: high).")
@click.option("--cache-dir", type=click.Path(path_type=Path), default=None, help="HTTP cache directory.")
@click.option("--no-cache", is_flag=True, help="Disable the HTTP response cache.")
@click.option("-v", "--verbose", count=True, help="More logging (-v info, -vv debug).")
@click.pass_context
def main(ctx: click.Context, lang, model, effort, cache_dir, no_cache, verbose) -> None:
    """AI mokslinių atradimų agentas / AI scientific discovery agent.

    Mines Open Targets, Europe PMC, PubMed, bioRxiv/medRxiv, ChEMBL, UniProt, PDB,
    ClinicalTrials.gov and patents for under-explored target–disease opportunities,
    and writes evidence-graded hypotheses.
    """
    logging.basicConfig(level=logging.WARNING - 10 * min(verbose, 2),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env().with_overrides(language=lang, model=model, effort=effort, cache_dir=cache_dir)
    if no_cache:
        settings = settings.with_overrides(cache_ttl_hours=0.0)
    ctx.obj = {"settings": settings}


def _agent(ctx, *, ai: bool, investigate: bool, ai_top: int, min_evidence: float | None) -> DiscoveryAgent:
    settings: Settings = ctx.obj["settings"]
    http = HttpClient(settings, cache=settings.cache_ttl_hours > 0)
    weights = ScoringWeights(min_evidence=min_evidence) if min_evidence is not None else ScoringWeights()
    return DiscoveryAgent(settings, sources=Sources.create(settings, http), weights=weights, progress=_progress,
                          use_ai=ai, investigate=investigate, ai_top=ai_top)


def _ai_options(f):
    f = click.option("--ai", is_flag=True, help="Write hypotheses with Claude (needs ANTHROPIC_API_KEY).")(f)
    f = click.option("--investigate", is_flag=True,
                     help="Let the Claude investigator agent query the databases first (implies --ai).")(f)
    f = click.option("--ai-top", type=int, default=3, show_default=True, help="How many top opportunities get AI.")(f)
    return f


@main.command()
@click.option("--disease", "-d", help="Disease name or ontology id (EFO_/MONDO_/Orphanet_…).")
@click.option("--target", "-t", help="Gene symbol or Ensembl id (find new indications for a target).")
@click.option("--top", type=int, default=10, show_default=True, help="Opportunities to report.")
@click.option("--candidates", type=int, default=200, show_default=True, help="Open Targets associations to scan.")
@click.option("--enrich", type=int, default=20, show_default=True, help="Candidates to enrich with all sources.")
@click.option("--include-known", is_flag=True, help="Keep targets already in phase 3+/approved for the disease.")
@click.option("--min-evidence", type=float, default=None, help="Minimum causal evidence strength (0-1).")
@click.option("--out", type=click.Path(path_type=Path), default=None, help="Output directory.")
@_ai_options
@click.pass_context
def discover(ctx, disease, target, top, candidates, enrich, include_known, min_evidence, out, ai, investigate,
             ai_top):
    """Find under-explored opportunities for a disease (or new indications for a target)."""
    if bool(disease) == bool(target):
        raise click.UsageError("Give exactly one of --disease or --target.")
    agent = _agent(ctx, ai=ai, investigate=investigate, ai_top=ai_top, min_evidence=min_evidence)
    kwargs = dict(top_k=top, candidates=candidates, enrich_k=max(enrich, top), include_known=include_known)
    run = agent.run_disease(disease, **kwargs) if disease else agent.run_target(target, **kwargs)
    out = out or _default_out(disease or target)
    paths = agent.write_outputs(run, out)
    click.echo(ranking_table(run.result.opportunities, agent.lang))
    for key, err in run.ai_errors.items():
        click.echo(f"AI error for {key}: {err}", err=True)
    click.echo(f"\nReport: {paths['report']}\nJSON:   {paths['json']}", err=True)


@main.command()
@click.option("--target", "-t", required=True, help="Gene symbol or Ensembl id.")
@click.option("--disease", "-d", required=True, help="Disease name or ontology id.")
@click.option("--out", type=click.Path(path_type=Path), default=None, help="Output directory.")
@_ai_options
@click.pass_context
def hypothesis(ctx, target, disease, out, ai, investigate, ai_top):
    """Build a full, evidence-graded hypothesis for one target–disease pair."""
    agent = _agent(ctx, ai=ai, investigate=investigate, ai_top=1, min_evidence=None)
    run = agent.run_pair(target, disease)
    opp = run.result.opportunities[0]
    out = out or _default_out(f"{target}_{disease}")
    paths = agent.write_outputs(run, out)
    click.echo(render_hypothesis(run.hypotheses[opp.key], opp, agent.lang, level=1))
    for key, err in run.ai_errors.items():
        click.echo(f"AI error for {key}: {err}", err=True)
    click.echo(f"\nReport: {paths['report']}", err=True)


@main.command()
@click.argument("config", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--once", is_flag=True, help="Run every watch once and exit (for cron/CI).")
@click.option("--only", multiple=True, help="Only run these watch names.")
@click.option("--poll", type=float, default=300.0, show_default=True, help="Seconds between schedule checks.")
@click.option("--database", type=click.Path(path_type=Path), default=None,
              help="Override the state store: *.sqlite file, or a directory for JSON state (e.g. a memory mount).")
@click.option("--output-dir", type=click.Path(path_type=Path), default=None, help="Override output_dir.")
@click.pass_context
def watch(ctx, config, once, only, poll, database, output_dir):
    """Continuously monitor diseases/targets from a YAML watchlist."""
    from .monitor import WatchConfig, Watcher

    cfg = WatchConfig.load(config)
    if only:
        cfg = cfg.model_copy(update={"watches": [w for w in cfg.watches if w.name in only]})
    overrides = {k: v for k, v in (("database", database), ("output_dir", output_dir)) if v is not None}
    if overrides:
        cfg = cfg.model_copy(update=overrides)
    watcher = Watcher(cfg, ctx.obj["settings"], progress=_progress)
    if once:
        failed = False
        for s in watcher.run_all():
            status = "ERROR: " + s.error if s.error else f"{len(s.alerts)} alerts → {s.digest_path}"
            click.echo(f"{s.watch}: {status}")
            failed |= bool(s.error)
        sys.exit(1 if failed else 0)
    try:
        watcher.run_forever(poll_seconds=poll)
    except KeyboardInterrupt:
        click.echo("stopped", err=True)


@main.command()
@click.argument("text")
@click.option("--entity", type=click.Choice(["disease", "target", "drug"]), multiple=True)
@click.pass_context
def search(ctx, text, entity):
    """Look up Open Targets ids for a disease, target or drug name."""
    sources = Sources.create(ctx.obj["settings"])
    for hit in sources.opentargets.search(text, entity or ("target", "disease"), size=10):
        desc = (hit.get("description") or "")[:90]
        click.echo(f"{hit['entity']:8} {hit['id']:18} {hit['name']}  {desc}")


@main.command()
@click.option("--db", type=click.Path(path_type=Path), default=Path("data/discovery.sqlite"), show_default=True)
@click.option("--watch", "watch_name", default=None)
@click.option("--limit", type=int, default=30, show_default=True)
def alerts(db, watch_name, limit):
    """Show recent monitoring alerts (SQLite file or JSON state directory)."""
    from .monitor import open_store

    if not db.exists():
        raise click.UsageError(f"No database at {db}")
    for row in open_store(db).recent_alerts(watch_name, limit):
        click.echo(f"{row['created_at']}  [{row['watch']}] {row['kind']:16} {row['message']}")


@main.command()
@click.argument("name", required=False)
@click.argument("input_json", required=False, default="{}")
@click.option("--target", "-t", default=None, help="Target for pair-specific tools (get_open_targets_evidence).")
@click.option("--disease", "-d", default=None, help="Disease for pair-specific tools.")
@click.option("--list", "list_tools", is_flag=True, help="List tools and their input schemas.")
@click.pass_context
def tool(ctx, name, input_json, target, disease, list_tools):
    """Run one investigation tool and print JSON (used by the Claude Managed Agent from bash).

    Example: discovery-agent tool search_literature '{"query": "PARN AND fibrosis", "source": "articles",
    "since": "", "max_results": 5}'
    """
    import json as _json

    from .llm.investigator import TOOLS, ToolSession
    from .models import Association, Opportunity

    if list_tools or not name:
        for t in TOOLS:
            if t["name"] != "record_evidence":
                click.echo(_json.dumps({"name": t["name"], "description": t["description"],
                                        "input_schema": t["input_schema"]}, ensure_ascii=False))
        return
    if name not in ToolSession.TOOL_NAMES or name == "record_evidence":
        raise click.UsageError(f"Unknown tool {name!r}; see --list")
    try:
        args = _json.loads(input_json)
    except ValueError as exc:
        raise click.UsageError(f"INPUT_JSON is not valid JSON: {exc}") from exc
    sources = Sources.create(ctx.obj["settings"])
    opp = None
    if target and disease:
        ot = sources.opentargets
        d = ot.resolve_disease(disease)
        t = ot.target(ot.resolve_target(target))
        opp = Opportunity(target=t, disease=d, association=Association(
            target_id=t.ensembl_id, symbol=t.symbol, disease_id=d.id, disease_name=d.name, score=0.0))
    out, is_error = ToolSession(sources, opp).run(name, args)
    click.echo(out)
    if is_error:
        sys.exit(2)


@main.group()
@click.option("--lock", type=click.Path(path_type=Path), default=Path("managed_agents/claude-lock.json"),
              show_default=True, help="claude-lock.json written by `ant apply`.")
@click.option("--deployment-id", default=None, help="Use this deployment instead of the one in the lock file.")
@click.pass_context
def managed(ctx, lock, deployment_id):
    """Claude Managed Agents deployment: trigger runs, fetch results, answer paused calls."""
    ctx.obj["lock"] = lock
    ctx.obj["deployment_id"] = deployment_id


def _managed_ctx(ctx):
    from . import managed as m

    try:
        client = m.make_client()
        dep = ctx.obj["deployment_id"] or m.deployment_id(ctx.obj["lock"])
    except m.ManagedError as exc:
        raise click.ClickException(str(exc)) from exc
    return m, client, dep


@managed.command("run")
@click.option("--wait/--no-wait", default=True, show_default=True, help="Follow the session until it is done.")
@click.option("--out", type=click.Path(path_type=Path), default=Path("reports/managed"), show_default=True)
@click.pass_context
def managed_run(ctx, wait, out):
    """Trigger the deployment now (smoke test) and download its outputs."""
    m, client, dep = _managed_ctx(ctx)
    try:
        result = m.run_now(client, dep, wait=wait, dest=out, progress=_progress,
                           on_text=lambda t: click.echo(t, err=True))
    except m.ManagedError as exc:
        raise click.ClickException(str(exc)) from exc
    for path in result.get("files", []):
        click.echo(str(path))


@managed.command("latest")
@click.option("--download/--no-download", default=True, show_default=True)
@click.option("--out", type=click.Path(path_type=Path), default=Path("reports/managed"), show_default=True)
@click.pass_context
def managed_latest(ctx, download, out):
    """Show the newest deployment run and download what it produced."""
    m, client, dep = _managed_ctx(ctx)
    try:
        run = m.latest_run(client, dep)
    except m.ManagedError as exc:
        raise click.ClickException(str(exc)) from exc
    if run.error:
        raise click.ClickException(f"{run.created_at}: run failed: {run.error.type}: {run.error.message}")
    click.echo(f"{run.created_at}  {run.session_id}  {m.console_url(run.session_id)}")
    if download:
        for path in m.download_outputs(client, run.session_id, out / run.session_id):
            click.echo(str(path))


@managed.command("answer")
@click.argument("session_id")
@click.pass_context
def managed_answer(ctx, session_id):
    """Answer tool calls that paused for approval (deny unless DISCOVERY_AUTO_ALLOW allows them)."""
    from . import managed as m

    for event_id, result in m.answer_pending(m.make_client(), session_id):
        click.echo(f"{event_id}: {result}")


@managed.command("pause")
@click.pass_context
def managed_pause(ctx):
    """Pause the weekly schedule (manual runs still work)."""
    m, client, dep = _managed_ctx(ctx)
    d = client.beta.deployments.pause(dep)
    click.echo(f"{dep}: {d.status}")


@managed.command("unpause")
@click.pass_context
def managed_unpause(ctx):
    """Resume the weekly schedule from its next occurrence (missed runs are not backfilled)."""
    m, client, dep = _managed_ctx(ctx)
    d = client.beta.deployments.unpause(dep)
    upcoming = ", ".join(str(t) for t in (d.schedule.upcoming_runs_at or [])[:3]) if d.schedule else ""
    click.echo(f"{dep}: {d.status}  next: {upcoming}")


@managed.command("serve-webhook")
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", type=int, default=8080, show_default=True)
@click.option("--out", type=click.Path(path_type=Path), default=Path("reports/managed"), show_default=True)
def managed_serve_webhook(host, port, out):
    """Webhook endpoint (behind HTTPS): answers paused calls, downloads finished runs' outputs."""
    from . import managed as m

    if not os.environ.get("ANTHROPIC_WEBHOOK_SIGNING_KEY"):
        raise click.ClickException("Set ANTHROPIC_WEBHOOK_SIGNING_KEY to the whsec_ secret from Console -> Webhooks")
    server = m.make_webhook_server(m.make_client(), host=host, port=port, dest=out)
    click.echo(f"Listening on {host}:{port}", err=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("stopped", err=True)


@main.command("cache-clear")
@click.pass_context
def cache_clear(ctx):
    """Delete cached API responses."""
    HttpClient(ctx.obj["settings"]).cache.clear()
    click.echo("cache cleared")


if __name__ == "__main__":  # pragma: no cover
    main()
