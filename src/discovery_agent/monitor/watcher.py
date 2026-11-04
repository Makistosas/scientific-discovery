"""Continuous monitoring: re-run watched diseases/targets on a schedule, diff
against the previous run, watch the literature, preprints and trial registries
for new items about the top opportunities, and raise alerts.

Use ``discovery-agent watch config/watchlist.yaml`` for a long-running loop, or
``--once`` from cron / systemd timers / CI schedules.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests
import yaml
from pydantic import BaseModel, Field, model_validator

from ..agent import AgentRun, DiscoveryAgent
from ..analysis.scoring import ScoringWeights
from ..config import Settings
from ..i18n import normalize_lang, tr
from ..models import Opportunity
from ..report import render_digest
from ..sources import Sources
from ..sources.literature import disease_terms, gene_terms
from .store import Store

log = logging.getLogger(__name__)


class WatchItem(BaseModel):
    name: str
    disease: str | None = None
    target: str | None = None
    top_k: int = 10
    candidates: int = 200
    enrich_k: int = 20
    include_known: bool = False
    ai: bool = False
    investigate: bool = False
    ai_top: int = 3
    interval_hours: float | None = None
    score_change_threshold: float = 0.05
    surveillance: bool = True

    @model_validator(mode="after")
    def _one_query(self) -> "WatchItem":
        if bool(self.disease) == bool(self.target):
            raise ValueError(f"watch {self.name!r}: set exactly one of 'disease' or 'target'")
        if not re.match(r"^[A-Za-z0-9_.-]+$", self.name):
            raise ValueError(f"watch name {self.name!r} may only contain letters, digits, '_', '-', '.'")
        return self


class Notify(BaseModel):
    webhook_url: str | None = None


class WatchConfig(BaseModel):
    output_dir: Path = Path("reports/watch")
    database: Path = Path("data/discovery.sqlite")
    language: str = "lt"
    interval_hours: float = 24.0
    cache_ttl_hours: float = 12.0
    notify: Notify = Field(default_factory=Notify)
    scoring: dict[str, Any] = Field(default_factory=dict)
    watches: list[WatchItem]

    @classmethod
    def load(cls, path: Path | str) -> "WatchConfig":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)


class WatchRunSummary(BaseModel):
    watch: str
    run_id: int
    alerts: list[dict] = Field(default_factory=list)
    digest_path: Path | None = None
    report_path: Path | None = None
    first_run: bool = False
    error: str | None = None


class Watcher:
    def __init__(self, config: WatchConfig, settings: Settings | None = None, *, sources: Sources | None = None,
                 store: Store | None = None, progress: Callable[[str], None] | None = None, llm_client=None,
                 agent_factory: Callable[[WatchItem], DiscoveryAgent] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        base = settings or Settings.from_env()
        self.settings = base.with_overrides(language=config.language, cache_ttl_hours=config.cache_ttl_hours)
        self.config = config
        self.lang = normalize_lang(config.language)
        self.sources = sources or Sources.create(self.settings)
        self.store = store or Store(config.database)
        self.progress = progress or (lambda m: log.info(m))
        self.llm_client = llm_client
        self.weights = ScoringWeights.from_dict(config.scoring)
        self._agent_factory = agent_factory
        self._sleep = sleep

    # ------------------------------------------------------------------
    def agent_for(self, watch: WatchItem) -> DiscoveryAgent:
        if self._agent_factory:
            return self._agent_factory(watch)
        # AI is applied selectively by the watcher (only to new/changed opportunities), not by the agent.
        return DiscoveryAgent(self.settings, sources=self.sources, weights=self.weights, progress=self.progress,
                              use_ai=False, llm_client=self.llm_client)

    def run_watch(self, watch: WatchItem) -> WatchRunSummary:
        params = watch.model_dump()
        previous = self.store.last_run(watch.name)
        run_id = self.store.start_run(watch.name, params)
        summary = WatchRunSummary(watch=watch.name, run_id=run_id, first_run=previous is None)
        self.progress(f"[watch:{watch.name}] run #{run_id} started")
        try:
            agent = self.agent_for(watch)
            kwargs = dict(top_k=watch.top_k, candidates=watch.candidates, enrich_k=watch.enrich_k,
                          include_known=watch.include_known)
            run = agent.run_disease(watch.disease, **kwargs) if watch.disease else agent.run_target(watch.target, **kwargs)
            opps = run.result.opportunities
            self.store.save_opportunities(run_id, watch.name, [
                {"key": o.key, "rank": i, "symbol": o.target.symbol, "disease": o.disease.name,
                 "opportunity": o.scores.opportunity, "evidence": o.scores.evidence, "gap": o.scores.gap,
                 "tractability": o.scores.tractability, "safety": o.scores.safety_risk,
                 "payload": {"direction": o.direction.modulation.value, "attention": o.attention.model_dump()}}
                for i, o in enumerate(opps, 1)])

            alerts: list[dict] = []
            changed_keys: list[str] = []
            if previous is None:
                alerts.append({"kind": "baseline", "message": tr(self.lang, "dig.first_run", n=len(opps))})
                changed_keys = [o.key for o in opps]
            else:
                diff_alerts, changed_keys = self.diff(self.store.opportunities(previous["id"]), opps, watch)
                alerts += diff_alerts
            if watch.surveillance:
                since = _parse_ts(previous["started_at"]) if previous else None
                alerts += self.surveil(watch, opps, since)

            if watch.ai or watch.investigate:
                self.apply_ai(agent, run, watch, changed_keys)
            for o in opps:
                h = run.hypotheses.get(o.key)
                if h and o.key in changed_keys:
                    self.store.save_hypothesis(watch.name, o.key, h.model_dump(mode="json"))

            summary.report_path, summary.digest_path = self.write(watch, agent, run, alerts, run_id)
            self.store.add_alerts(watch.name, alerts)
            summary.alerts = alerts
            self.notify(watch, alerts, summary.digest_path)
            self.store.finish_run(run_id, "ok")
            self.progress(f"[watch:{watch.name}] done: {len(opps)} opportunities, {len(alerts)} alerts")
        except Exception as exc:
            log.exception("watch %s failed", watch.name)
            self.store.finish_run(run_id, "error")
            summary.error = str(exc)
        return summary

    # ------------------------------------------------------------------
    def diff(self, previous: dict, opps: list[Opportunity], watch: WatchItem) -> tuple[list[dict], list[str]]:
        alerts, changed = [], []
        current_keys = {o.key for o in opps}
        for rank, o in enumerate(opps, 1):
            old = previous.get(o.key)
            if old is None:
                changed.append(o.key)
                alerts.append({"kind": "new_opportunity", "payload": {"key": o.key, "rank": rank},
                               "message": tr(self.lang, "dig.new_opp", k=watch.top_k, symbol=o.target.symbol,
                                             disease=o.disease.name, score=o.scores.opportunity, rank=rank)})
            elif abs(o.scores.opportunity - old["opportunity"]) >= watch.score_change_threshold:
                changed.append(o.key)
                alerts.append({"kind": "score_change", "payload": {"key": o.key},
                               "message": tr(self.lang, "dig.score_change", symbol=o.target.symbol,
                                             disease=o.disease.name, old=old["opportunity"],
                                             new=o.scores.opportunity)})
        for key, old in previous.items():
            if key not in current_keys:
                alerts.append({"kind": "dropped", "payload": {"key": key},
                               "message": tr(self.lang, "dig.dropped", k=watch.top_k, symbol=old["symbol"],
                                             disease=old["disease"])})
        return alerts, changed

    def surveil(self, watch: WatchItem, opps: list[Opportunity], since: datetime | None) -> list[dict]:
        """New publications, preprints and trials about the top opportunities since the last run."""
        alerts: list[dict] = []
        since_date = (since - timedelta(days=1)).date().isoformat() if since else None
        for o in opps:
            genes = gene_terms(o.target.symbol, o.target.name)
            diseases = disease_terms(o.disease.name)
            found: list[tuple[str, str, str]] = []  # (item key, kind, title)
            try:
                for art in self.sources.europepmc.publications(genes, diseases, since=since_date, page_size=10,
                                                               ttl_hours=0).articles:
                    found.append((f"pub:{art.pmid or art.doi or art.id}", "publication", art.title))
                for art in self.sources.europepmc.preprints(genes, diseases, since=since_date, page_size=10,
                                                            ttl_hours=0).articles:
                    found.append((f"ppr:{art.doi or art.id}", "preprint", art.title))
                term = " OR ".join(f'"{g}"' for g in genes)
                for trial in self.sources.trials.search(condition=o.disease.name, term=term, page_size=50).trials:
                    found.append((f"nct:{trial.nct_id}", "trial", trial.title))
            except Exception as exc:
                log.warning("surveillance failed for %s: %s", o.key, exc)
                continue
            new = set(self.store.mark_seen(watch.name, [(f"{o.key}|{k}", kind) for k, kind, _ in found]))
            if since is None:
                continue  # first run: baseline only
            for k, kind, title in found:
                if f"{o.key}|{k}" in new:
                    msg_key = {"publication": "dig.new_paper", "preprint": "dig.new_preprint",
                               "trial": "dig.new_trial"}[kind]
                    alerts.append({"kind": f"new_{kind}", "payload": {"key": o.key, "item": k},
                                   "message": tr(self.lang, msg_key, symbol=o.target.symbol, disease=o.disease.name,
                                                 title=title)})
        return alerts

    def apply_ai(self, agent: DiscoveryAgent, run: AgentRun, watch: WatchItem, keys: list[str]) -> None:
        targets = [o for o in run.result.opportunities if o.key in set(keys)][:watch.ai_top]
        if not targets:
            return
        ai_agent = DiscoveryAgent(self.settings, sources=self.sources, weights=self.weights, progress=self.progress,
                                  use_ai=True, investigate=watch.investigate, llm_client=self.llm_client)
        ai_agent.apply_ai(run, targets)

    def write(self, watch: WatchItem, agent: DiscoveryAgent, run: AgentRun,
              alerts: list[dict], run_id: int) -> tuple[Path, Path]:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-run{run_id}"
        base = self.config.output_dir / watch.name
        paths = agent.write_outputs(run, base / "latest")
        agent.write_outputs(run, base / "runs" / stamp)
        digest = base / f"digest-{stamp}.md"
        digest.write_text(render_digest(watch.name, [a["message"] for a in alerts], self.lang), encoding="utf-8")
        return paths["report"], digest

    def notify(self, watch: WatchItem, alerts: list[dict], digest_path: Path | None) -> None:
        url = self.config.notify.webhook_url
        interesting = [a for a in alerts if a["kind"] != "baseline"]
        if not url or not interesting:
            return
        text = f"*{tr(self.lang, 'dig.title', name=watch.name)}*\n" + "\n".join(f"• {a['message']}" for a in interesting[:20])
        if digest_path:
            text += f"\n({digest_path})"
        try:
            requests.post(url, json={"text": text}, timeout=20).raise_for_status()
        except requests.RequestException as exc:
            log.warning("webhook notification failed: %s", exc)

    # ------------------------------------------------------------------
    def run_all(self, only: list[str] | None = None) -> list[WatchRunSummary]:
        return [self.run_watch(w) for w in self.config.watches if not only or w.name in only]

    def due(self, watch: WatchItem, now: datetime | None = None) -> bool:
        last = self.store.last_run(watch.name)
        if not last:
            return True
        interval = watch.interval_hours or self.config.interval_hours
        now = now or datetime.now(timezone.utc)
        return now - _parse_ts(last["started_at"]) >= timedelta(hours=interval)

    def run_forever(self, poll_seconds: float = 300.0, max_cycles: int | None = None) -> None:
        cycles = 0
        self.progress(f"Monitoring {len(self.config.watches)} watch(es); Ctrl+C to stop")
        while max_cycles is None or cycles < max_cycles:
            for w in self.config.watches:
                if self.due(w):
                    self.run_watch(w)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            self._sleep(poll_seconds)


def _parse_ts(value: str) -> datetime:
    ts = datetime.fromisoformat(value)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
