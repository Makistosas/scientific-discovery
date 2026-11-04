"""High-level orchestration: discovery pipeline -> hypotheses (rules and/or Claude) -> reports."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field

from .analysis.scoring import ScoringWeights
from .config import Settings
from .hypothesis import build_hypothesis
from .i18n import normalize_lang, tr
from .models import Hypothesis, Opportunity
from .pipeline import DiscoveryPipeline, DiscoveryResult
from .report import render_hypothesis, render_report
from .sources import Sources

log = logging.getLogger(__name__)


class AgentRun(BaseModel):
    result: DiscoveryResult
    hypotheses: dict[str, Hypothesis] = Field(default_factory=dict)
    investigation_notes: dict[str, str] = Field(default_factory=dict)
    ai_errors: dict[str, str] = Field(default_factory=dict)
    title: str = ""

    def report_markdown(self) -> str:
        lang = normalize_lang(self.result.params.get("language") or "lt")
        return render_report(self.title, self.result.opportunities, self.hypotheses, lang,
                             candidates_considered=self.result.candidates_considered)


class DiscoveryAgent:
    def __init__(self, settings: Settings | None = None, *, sources: Sources | None = None,
                 weights: ScoringWeights | None = None, progress: Callable[[str], None] | None = None,
                 use_ai: bool = False, investigate: bool = False, ai_top: int = 3, llm_client=None):
        self.settings = settings or Settings.from_env()
        self.sources = sources or Sources.create(self.settings)
        self.progress = progress or (lambda m: log.info(m))
        self.pipeline = DiscoveryPipeline(self.sources, self.settings, weights, self.progress)
        self.lang = normalize_lang(self.settings.language)
        self.use_ai = use_ai or investigate
        self.investigate = investigate
        self.ai_top = ai_top
        self.llm_client = llm_client

    # ------------------------------------------------------------------
    def run_disease(self, query: str, **kwargs) -> AgentRun:
        result = self.pipeline.discover_disease(query, **kwargs)
        name = result.disease.name if result.disease else query
        return self._finish(result, tr(self.lang, "rep.title.discovery", query=name))

    def run_target(self, query: str, **kwargs) -> AgentRun:
        result = self.pipeline.discover_target(query, **kwargs)
        name = result.target.symbol if result.target else query
        return self._finish(result, tr(self.lang, "rep.title.discovery", query=name))

    def run_pair(self, target: str, disease: str) -> AgentRun:
        result = self.pipeline.analyze_pair(target, disease)
        opp = result.opportunities[0]
        return self._finish(result, tr(self.lang, "rep.title.discovery",
                                       query=f"{opp.target.symbol} – {opp.disease.name}"), ai_top=1)

    # ------------------------------------------------------------------
    def _finish(self, result: DiscoveryResult, title: str, ai_top: int | None = None) -> AgentRun:
        run = AgentRun(result=result, title=title)
        for opp in result.opportunities:
            run.hypotheses[opp.key] = build_hypothesis(opp, self.lang)
        if self.use_ai:
            top = ai_top if ai_top is not None else self.ai_top
            self.apply_ai(run, run.result.opportunities[:top])
        return run

    def apply_ai(self, run: AgentRun, opportunities: list[Opportunity]) -> None:
        """Investigate (optional) and write Claude hypotheses for the given opportunities, in place."""
        from .llm import HypothesisSynthesizer, Investigator, LLMError

        synth = HypothesisSynthesizer(self.settings, self.llm_client)
        investigator = Investigator(self.settings, self.sources, self.llm_client, progress=self.progress)
        for opp in opportunities:
            draft = run.hypotheses.get(opp.key) or build_hypothesis(opp, self.lang)
            notes = None
            try:
                if self.investigate:
                    self.progress(f"AI investigator: {opp.target.symbol} – {opp.disease.name}")
                    inv = investigator.investigate_and_merge(opp)
                    notes = inv.notes
                    run.investigation_notes[opp.key] = notes
                    self.progress(f"  ✓ {inv.tool_calls} tool calls, {len(inv.new_evidence)} new evidence items")
                self.progress(f"AI synthesizer ({self.settings.model}): {opp.target.symbol} – {opp.disease.name}")
                run.hypotheses[opp.key] = synth.synthesize(opp, draft, notes)
            except LLMError as exc:
                log.warning("AI step failed for %s: %s", opp.key, exc)
                run.ai_errors[opp.key] = str(exc)
                draft_warning = f"AI: {exc}"
                if draft_warning not in opp.warnings:
                    opp.warnings.append(draft_warning)

    # ------------------------------------------------------------------
    def write_outputs(self, run: AgentRun, out_dir: Path) -> dict[str, Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = {"report": out_dir / "report.md", "json": out_dir / "result.json"}
        paths["report"].write_text(run.report_markdown(), encoding="utf-8")
        paths["json"].write_text(run.model_dump_json(indent=1), encoding="utf-8")
        hyp_dir = out_dir / "hypotheses"
        hyp_dir.mkdir(exist_ok=True)
        for opp in run.result.opportunities:
            h = run.hypotheses.get(opp.key)
            if not h:
                continue
            fname = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{opp.target.symbol}__{opp.disease.id}") + ".md"
            (hyp_dir / fname).write_text(render_hypothesis(h, opp, self.lang, level=1), encoding="utf-8")
        return paths


def load_run(path: Path) -> AgentRun:
    return AgentRun.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
