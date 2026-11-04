"""Command-line interface: ``discovery-agent --help``."""

from __future__ import annotations

import logging
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
@click.pass_context
def watch(ctx, config, once, only, poll):
    """Continuously monitor diseases/targets from a YAML watchlist."""
    from .monitor import WatchConfig, Watcher

    cfg = WatchConfig.load(config)
    if only:
        cfg = cfg.model_copy(update={"watches": [w for w in cfg.watches if w.name in only]})
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
    """Show recent monitoring alerts."""
    from .monitor import Store

    if not db.exists():
        raise click.UsageError(f"No database at {db}")
    for row in Store(db).recent_alerts(watch_name, limit):
        click.echo(f"{row['created_at']}  [{row['watch']}] {row['kind']:16} {row['message']}")


@main.command("cache-clear")
@click.pass_context
def cache_clear(ctx):
    """Delete cached API responses."""
    HttpClient(ctx.obj["settings"]).cache.clear()
    click.echo("cache cleared")


if __name__ == "__main__":  # pragma: no cover
    main()
