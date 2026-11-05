"""Deterministic quality check of a hypothesis, without a language model.

Works on rule-based / Claude-written :class:`Hypothesis` objects and on free Markdown saved through MCP. Both
are reduced to the same features (statement, cited evidence ids, alternatives with tests, experiments with
expected outcomes, safety, uncertainties, why under-explored, direction and confidence claims) and checked
against the pair's dossier context: which evidence contradicts, how strong the causal grades are, which way the
genetics points and which on-target liabilities exist.

Score: 100 minus 15 per error and 5 per warning. "ready" means no errors and a score of at least 80.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from ..i18n import tr
from ..models import Aspect, Hypothesis, Opportunity, Reliability, Stance

Severity = Literal["error", "warning"]


class QualityCheck(BaseModel):
    id: str
    ok: bool
    severity: Severity
    message: str


class QualityReport(BaseModel):
    score: int
    verdict: Literal["ready", "revise"]
    checks: list[QualityCheck] = Field(default_factory=list)

    @property
    def problems(self) -> list[QualityCheck]:
        return [c for c in self.checks if not c.ok]

    def summary(self) -> str:
        bad = self.problems
        if not bad:
            return f"{self.score}/100, {self.verdict}"
        return f"{self.score}/100, {self.verdict}: " + "; ".join(f"[{c.severity}] {c.message}" for c in bad)


class Features(BaseModel):
    text: str
    statement: str = ""
    symbol: str = ""
    disease: str = ""
    cited_ids: list[str] = Field(default_factory=list)
    alternatives: int = 0
    alternatives_tested: int = 0
    experiments: int = 0
    experiments_with_outcomes: int = 0
    safety: str = ""
    uncertainties: str = ""
    novelty: str = ""
    modulation: str = "unknown"
    confidence: str = "low"


_EVIDENCE_ID = re.compile(r"\bE\d{1,3}\b")
_REF = re.compile(r"(PMID[:\s]*\d{5,9}|pubmed\.ncbi\.nlm\.nih\.gov/\d+|10\.\d{4,9}/\S+|NCT\d{8}|https?://\S+)", re.I)
_OVERCLAIM = re.compile(r"\b(prove[sn]?|proof that|definitive(ly)?|certainly|will cure|cures?|guarantee[sd]?|"
                        r"undoubtedly|įrod[oė]|įrodyta|neabejotinai|išgydys|garantuoja)\b", re.I)
_UNCERTAIN = re.compile(r"(uncertain|unknown|unclear|to be (established|determined)|direction|neaišk|nežinom|"
                        r"neapibrėžt|nenustatyt|kryptis|krypt)", re.I)
_TRUE = re.compile(r"(if true|if (the )?hypothesis (is )?(correct|true)|expected[^.\n]{0,40}(confirm|support)|"
                   r"jei (teising|pasitvirtin|hipotezė teisinga)|patvirtin)", re.I)
_FALSE = re.compile(r"(if false|if (the )?hypothesis (is )?(wrong|false|incorrect)|no (change|effect)|refut|"
                    r"jei (klaiding|nepasitvirtin|hipotezė klaidinga)|paneig|nesikeičia)", re.I)

_SECTION_KEYS = {
    "alternatives": ("alternativ", "alternatyv", "competing explanation", "kiti paaiškinimai"),
    "experiments": ("experiment", "eksperiment", "validation", "patikrinimo plan", "tests to run", "bandym"),
    "safety": ("safety", "saugum", "toxicit", "toksišk"),
    "uncertainties": ("uncertaint", "neapibrėžt", "limitation", "apribojim", "open question", "atviri klausim"),
    "novelty": ("novelty", "under-explored", "why little", "why under", "naujum", "spraga", "mažai ištirt",
                "gap", "competitive", "konkurenc"),
    "evidence": ("evidence", "įrodym"),
}


def _sections(md: str) -> dict[str, str]:
    """Markdown body grouped by the kind of its nearest heading."""
    out: dict[str, list[str]] = {}
    current = "intro"
    for line in md.splitlines():
        m = re.match(r"^\s*#{1,6}\s+(.*)$", line) or re.match(r"^\s*\*\*([^*]{3,80})\*\*:?\s*$", line)
        if m:
            head = m.group(1).lower()
            current = next((k for k, keys in _SECTION_KEYS.items() if any(x in head for x in keys)), "other")
            continue
        out.setdefault(current, []).append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def _items(block: str) -> list[str]:
    """Top-level list items or ### sub-sections of a section."""
    items, current = [], None
    for line in block.splitlines():
        if re.match(r"^(\s{0,1})([-*+]|\d+[.)])\s+\S", line) or re.match(r"^\s*#{3,6}\s+", line):
            if current is not None:
                items.append(current)
            current = line
        elif current is not None:
            current += "\n" + line
    if current is not None:
        items.append(current)
    return items


def features_from_markdown(md: str, *, symbol: str, disease: str, modulation: str = "unknown",
                           confidence: str = "low") -> Features:
    sec = _sections(md)
    alts = _items(sec.get("alternatives", ""))
    exps = _items(sec.get("experiments", ""))
    statement = next((ln.strip("> ").strip() for ln in md.splitlines()
                      if ln.strip() and not ln.lstrip().startswith("#")), "")
    return Features(
        text=md, statement=statement, symbol=symbol, disease=disease,
        cited_ids=sorted(set(_EVIDENCE_ID.findall(md)), key=lambda s: int(s[1:])),
        alternatives=len(alts),
        alternatives_tested=sum(1 for a in alts if re.search(r"(test|discriminat|distinguish|patikrin|atskir|"
                                                             r"eksperiment|experiment|how to)", a, re.I)),
        experiments=len(exps),
        experiments_with_outcomes=sum(1 for e in exps if _TRUE.search(e) and _FALSE.search(e)),
        safety=sec.get("safety", ""), uncertainties=sec.get("uncertainties", ""), novelty=sec.get("novelty", ""),
        modulation=modulation, confidence=confidence)


def features_from_hypothesis(h: Hypothesis) -> Features:
    text = " ".join([h.title, h.statement, h.mechanism_rationale, h.novelty_assessment, h.competitive_landscape,
                     *(a.interpretation for a in h.supporting + h.contradicting),
                     *(f"{a.title} {a.explanation} {a.how_to_test}" for a in h.alternative_explanations),
                     *(f"{e.title} {e.design} {e.expected_if_true} {e.expected_if_false}" for e in h.proposed_experiments),
                     *h.key_uncertainties, *h.safety_considerations])
    cited = {a.evidence_id for a in h.supporting + h.contradicting}
    for a in h.alternative_explanations:
        cited.update(a.evidence_ids)
    cited.update(_EVIDENCE_ID.findall(text))
    return Features(
        text=text, statement=h.statement, symbol=h.target_symbol, disease=h.disease_name,
        cited_ids=sorted(cited, key=lambda s: int(s[1:]) if s[1:].isdigit() else 0),
        alternatives=len(h.alternative_explanations),
        alternatives_tested=sum(1 for a in h.alternative_explanations if a.how_to_test.strip()),
        experiments=len(h.proposed_experiments),
        experiments_with_outcomes=sum(1 for e in h.proposed_experiments
                                      if e.expected_if_true.strip() and e.expected_if_false.strip()
                                      and e.expected_if_true.strip() != e.expected_if_false.strip()),
        safety=" ".join(h.safety_considerations), uncertainties=" ".join(h.key_uncertainties),
        novelty=h.novelty_assessment + " " + h.competitive_landscape,
        modulation=h.modulation.value, confidence=h.confidence.level.value)


def context_from_opportunity(opp: Opportunity) -> dict:
    """What the dossier says, kept with the dossier so saved Markdown can be checked later."""
    causal = [e for e in opp.evidence if e.aspect is Aspect.CAUSALITY and e.stance is Stance.SUPPORTS]
    gs = opp.genetic_safety
    return {
        "evidence_ids": [e.id for e in opp.evidence],
        "contradicting_ids": [e.id for e in opp.evidence if e.stance is Stance.CONTRADICTS],
        "causal_a": [e.id for e in causal if e.reliability is Reliability.A],
        "causal_ab": [e.id for e in causal if e.reliability in (Reliability.A, Reliability.B)],
        "direction": opp.direction.modulation.value, "direction_confidence": opp.direction.confidence,
        "liabilities": [r["disease"] for r in (gs.liabilities if gs else [])][:5],
        "has_references": any(e.references for e in opp.evidence),
    }


def check(f: Features, context: dict | None = None, lang: str = "en") -> QualityReport:
    ctx = context or {}
    checks: list[QualityCheck] = []

    def add(cid: str, ok: bool, severity: Severity, **kw) -> None:
        checks.append(QualityCheck(id=cid, ok=bool(ok), severity=severity, message=tr(lang, f"q.{cid}", **kw)))

    low = f.text.lower()
    add("statement", bool(f.statement) and f.symbol.lower() in low, "error", symbol=f.symbol)
    add("evidence.cited", len(f.cited_ids) >= 2, "error")
    known = set(ctx.get("evidence_ids") or [])
    unknown = [i for i in f.cited_ids if known and i not in known]
    add("evidence.known", not unknown, "error", ids=", ".join(unknown))
    missing = [i for i in ctx.get("contradicting_ids") or [] if i not in f.cited_ids]
    add("evidence.contradicting", not missing, "warning", ids=", ".join(missing))
    add("references", bool(_REF.search(f.text)) or bool(f.cited_ids and ctx.get("has_references")), "warning")
    add("alternatives.count", f.alternatives >= 2, "error")
    add("alternatives.tests", f.alternatives and f.alternatives_tested >= min(2, f.alternatives), "warning")
    add("experiments.count", f.experiments >= 2, "error")
    add("experiments.outcomes", f.experiments and f.experiments_with_outcomes >= min(2, f.experiments), "warning")
    add("safety", len(f.safety.strip()) > 20, "warning")
    liab = [x for x in ctx.get("liabilities") or [] if x.lower() not in low]
    add("safety.genetic", not liab, "warning", items="; ".join(liab[:3]))
    add("uncertainties", len(f.uncertainties.strip()) > 20, "warning")
    add("novelty", len(f.novelty.strip()) > 20, "warning")

    dossier_dir, dconf = ctx.get("direction"), ctx.get("direction_confidence") or 0.0
    if f.modulation in ("inhibition", "activation") and dossier_dir and dossier_dir != "unknown" \
            and dossier_dir != f.modulation and dconf >= 0.5:
        add("direction.conflict", False, "error", mod=f.modulation, dossier=dossier_dir, conf=dconf)
    if f.modulation in ("inhibition", "activation") and (dossier_dir in (None, "unknown") or dconf < 0.3):
        add("direction.uncertainty", bool(_UNCERTAIN.search(f.uncertainties + " " + f.text)), "warning")
    if f.confidence == "high":
        add("confidence.high", bool(ctx.get("causal_a")) if ctx else True, "error")
    elif f.confidence == "moderate":
        add("confidence.moderate", bool(ctx.get("causal_ab")) if ctx else True, "warning")
    over = sorted({m.group(0).lower() for m in _OVERCLAIM.finditer(f.text)})
    add("language", not over, "warning", words=", ".join(over))

    errors = sum(1 for c in checks if not c.ok and c.severity == "error")
    warnings = sum(1 for c in checks if not c.ok and c.severity == "warning")
    score = max(0, 100 - 15 * errors - 5 * warnings)
    return QualityReport(score=score, verdict="ready" if not errors and score >= 80 else "revise", checks=checks)


def check_hypothesis(h: Hypothesis, opp: Opportunity | None = None, lang: str | None = None) -> QualityReport:
    return check(features_from_hypothesis(h), context_from_opportunity(opp) if opp else None, lang or h.language)


def check_markdown(md: str, *, symbol: str, disease: str, modulation: str = "unknown", confidence: str = "low",
                   context: dict | None = None, lang: str = "en") -> QualityReport:
    return check(features_from_markdown(md, symbol=symbol, disease=disease, modulation=modulation,
                                        confidence=confidence), context, lang)
