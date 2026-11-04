"""Claude-powered hypothesis writer.

Takes the evidence dossier (and the rule-based draft) and asks Claude to write
a critically reasoned hypothesis as schema-constrained JSON. Every citation is
then checked against the dossier: unknown evidence IDs are removed and
reported, so the final document only cites evidence that actually exists.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..config import Settings
from ..hypothesis import hypothesis_id
from ..i18n import normalize_lang
from ..models import (AlternativeExplanation, ConfidenceAssessment, ConfidenceLevel, EvidenceAppraisal, Experiment,
                      Hypothesis, Modulation, Opportunity)
from .client import LLMError, check_stop, friendly_api_error, make_client, request_kwargs, text_of
from .dossier import dossier_json

LANGUAGE_NAMES = {"lt": "Lithuanian", "en": "English"}

SYSTEM_PROMPT = """You are a rigorous translational scientist and drug-discovery strategist working inside an \
automated scientific discovery system. The system has mined Open Targets, Europe PMC, PubMed, bioRxiv/medRxiv, \
ChEMBL, UniProt, RCSB PDB, ClinicalTrials.gov and patent records and flagged a target–disease pair that has \
causal evidence but little research attention. Your job is to turn the evidence dossier into a research \
hypothesis that a principal investigator could act on — or explain why it should not be pursued.

How to work:
- Ground every factual claim in the dossier and cite evidence items by their IDs (E1, E2, …). Never invent \
papers, numbers, trial IDs or evidence IDs. General textbook biology is allowed but must be phrased as \
background, not as evidence.
- Keep four questions separate: (1) is the target causally involved in the disease, (2) which direction of \
modulation would be therapeutic, (3) can it be drugged, (4) what could go wrong (safety).
- Judge each cited item's reliability critically: study design, directness (human vs model, Mendelian vs \
sporadic), statistical strength, replication, peer review. The dossier's A–D grades are a starting point; \
disagree when warranted and say why.
- Low research attention is ambiguous: it may be a genuine gap, a technically hard target, or unpublished \
failure. Address this explicitly in the novelty assessment.
- Generate the strongest competing explanations (e.g. a different causal gene at a GWAS locus, reverse \
causation, pleiotropy, Mendelian-subtype effects) and give a concrete, discriminating test for each.
- Propose experiments with predictions that would come out differently if the hypothesis were false.
- Calibrate confidence (high / moderate / low / very_low) and a 0–1 score; be conservative.
- If the direction of modulation is not supported, set modulation to "unknown" rather than guessing.
Write all prose in {language}. Keep gene symbols, identifiers and evidence IDs unchanged."""

_APPRAISAL = {
    "type": "object", "additionalProperties": False,
    "required": ["evidence_id", "interpretation", "reliability_comment"],
    "properties": {"evidence_id": {"type": "string"}, "interpretation": {"type": "string"},
                   "reliability_comment": {"type": "string"}},
}

HYPOTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "statement", "modulation", "mechanism_rationale", "supporting", "contradicting",
                 "confidence", "alternative_explanations", "key_uncertainties", "proposed_experiments",
                 "safety_considerations", "competitive_landscape", "novelty_assessment"],
    "properties": {
        "title": {"type": "string"},
        "statement": {"type": "string"},
        "modulation": {"type": "string", "enum": ["inhibition", "activation", "unknown"]},
        "mechanism_rationale": {"type": "string"},
        "supporting": {"type": "array", "items": _APPRAISAL},
        "contradicting": {"type": "array", "items": _APPRAISAL},
        "confidence": {
            "type": "object", "additionalProperties": False, "required": ["level", "score", "rationale"],
            "properties": {"level": {"type": "string", "enum": ["high", "moderate", "low", "very_low"]},
                           "score": {"type": "number"}, "rationale": {"type": "string"}},
        },
        "alternative_explanations": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["title", "explanation", "plausibility", "how_to_test", "evidence_ids"],
            "properties": {"title": {"type": "string"}, "explanation": {"type": "string"},
                           "plausibility": {"type": "string", "enum": ["high", "moderate", "low"]},
                           "how_to_test": {"type": "string"},
                           "evidence_ids": {"type": "array", "items": {"type": "string"}}},
        }},
        "key_uncertainties": {"type": "array", "items": {"type": "string"}},
        "proposed_experiments": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["title", "design", "expected_if_true", "expected_if_false", "priority"],
            "properties": {"title": {"type": "string"}, "design": {"type": "string"},
                           "expected_if_true": {"type": "string"}, "expected_if_false": {"type": "string"},
                           "priority": {"type": "string", "enum": ["high", "medium", "low"]}},
        }},
        "safety_considerations": {"type": "array", "items": {"type": "string"}},
        "competitive_landscape": {"type": "string"},
        "novelty_assessment": {"type": "string"},
    },
}

_EVIDENCE_ID = re.compile(r"\bE\d{1,3}\b")


class HypothesisSynthesizer:
    def __init__(self, settings: Settings, client: Any | None = None, *, max_tokens: int = 32000,
                 use_fallbacks: bool = True):
        self.settings = settings
        self.client = client
        self.max_tokens = max_tokens
        self.use_fallbacks = use_fallbacks

    def _client(self):
        if self.client is None:
            self.client = make_client(self.settings)
        return self.client

    def synthesize(self, opp: Opportunity, draft: Hypothesis | None = None, notes: str | None = None) -> Hypothesis:
        lang = normalize_lang(self.settings.language)
        system = SYSTEM_PROMPT.format(language=LANGUAGE_NAMES.get(lang, "English"))
        user = (
            "Evidence dossier (JSON):\n<dossier>\n" + dossier_json(opp, draft, notes) + "\n</dossier>\n\n"
            "Write the hypothesis for this target–disease pair following the output schema. "
            "Cite only evidence IDs that appear in the dossier."
        )
        kwargs = request_kwargs(self.settings, use_fallbacks=self.use_fallbacks)
        kwargs["output_config"] = {"effort": self.settings.effort,
                                   "format": {"type": "json_schema", "schema": HYPOTHESIS_SCHEMA}}
        try:
            with self._client().beta.messages.stream(
                max_tokens=self.max_tokens, system=system,
                messages=[{"role": "user", "content": user}], **kwargs,
            ) as stream:
                message = stream.get_final_message()
        except LLMError:
            raise
        except Exception as exc:
            raise friendly_api_error(exc) from exc
        check_stop(message)
        raw = text_of(message)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Model returned invalid JSON: {raw[:200]}") from exc
        return to_hypothesis(data, opp, lang, generated_by=f"claude:{getattr(message, 'model', self.settings.model)}")


def to_hypothesis(data: dict, opp: Opportunity, lang: str, *, generated_by: str) -> Hypothesis:
    known = set(opp.evidence_by_id())
    ungrounded: set[str] = set()

    def appraisals(items) -> list[EvidenceAppraisal]:
        out = []
        for a in items or []:
            eid = str(a.get("evidence_id", "")).strip()
            if eid not in known:
                ungrounded.add(eid or "?")
                continue
            out.append(EvidenceAppraisal(evidence_id=eid, interpretation=a.get("interpretation", ""),
                                         reliability_comment=a.get("reliability_comment", "")))
        return out

    alternatives = []
    for a in data.get("alternative_explanations") or []:
        ids = [i for i in a.get("evidence_ids") or [] if i in known]
        ungrounded.update(i for i in a.get("evidence_ids") or [] if i not in known)
        alternatives.append(AlternativeExplanation(
            title=a.get("title", ""), explanation=a.get("explanation", ""),
            plausibility=a.get("plausibility", "moderate") if a.get("plausibility") in ("high", "moderate", "low") else "moderate",
            how_to_test=a.get("how_to_test", ""), evidence_ids=ids))

    prose = " ".join(str(data.get(k, "")) for k in ("statement", "mechanism_rationale", "competitive_landscape",
                                                     "novelty_assessment"))
    prose += " " + str((data.get("confidence") or {}).get("rationale", ""))
    ungrounded.update(i for i in _EVIDENCE_ID.findall(prose) if i not in known)

    conf = data.get("confidence") or {}
    level = conf.get("level") if conf.get("level") in {c.value for c in ConfidenceLevel} else "low"
    try:
        score = float(conf.get("score", 0.3))
    except (TypeError, ValueError):
        score = 0.3
    mod = data.get("modulation") if data.get("modulation") in {m.value for m in Modulation} else "unknown"

    experiments = []
    for x in data.get("proposed_experiments") or []:
        prio = x.get("priority") if x.get("priority") in ("high", "medium", "low") else "medium"
        experiments.append(Experiment(title=x.get("title", ""), design=x.get("design", ""),
                                      expected_if_true=x.get("expected_if_true", ""),
                                      expected_if_false=x.get("expected_if_false", ""), priority=prio))

    return Hypothesis(
        id=hypothesis_id(opp), title=data.get("title", ""), statement=data.get("statement", ""),
        target_symbol=opp.target.symbol, target_id=opp.target.ensembl_id,
        disease_name=opp.disease.name, disease_id=opp.disease.id, modulation=Modulation(mod),
        mechanism_rationale=data.get("mechanism_rationale", ""),
        supporting=appraisals(data.get("supporting")), contradicting=appraisals(data.get("contradicting")),
        confidence=ConfidenceAssessment(level=ConfidenceLevel(level), score=max(0.0, min(1.0, score)),
                                        rationale=conf.get("rationale", "")),
        alternative_explanations=alternatives,
        key_uncertainties=[str(u) for u in data.get("key_uncertainties") or []],
        proposed_experiments=experiments,
        safety_considerations=[str(s) for s in data.get("safety_considerations") or []],
        competitive_landscape=data.get("competitive_landscape", ""),
        novelty_assessment=data.get("novelty_assessment", ""),
        generated_by=generated_by, language=lang, ungrounded_citations=sorted(ungrounded),
    )
