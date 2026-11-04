"""Deterministic (rule-based) hypothesis builder.

Always available — no API key needed. The Claude-powered synthesizer
(:mod:`discovery_agent.llm.synthesizer`) starts from the same dossier and this
draft, and writes a richer, critically reasoned version.
"""

from __future__ import annotations

import hashlib
import re

from .analysis.critique import alternative_explanations, assess_confidence
from .i18n import tr
from .models import (Aspect, EvidenceAppraisal, EvidenceType, Experiment, Hypothesis, Modulation, Opportunity,
                     Reliability, Stance)


def clean_function_text(text: str | None, max_sentences: int = 2) -> str:
    """Strip UniProt evidence codes / PubMed tags and keep the first sentences."""
    if not text:
        return ""
    text = re.sub(r"\{ECO:[^}]*\}", "", text)
    text = re.sub(r"\s*\((?:PubMed:\d+(?:,\s*)?)+\)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    return " ".join(sentences[:max_sentences]).strip()


def hypothesis_id(opp: Opportunity) -> str:
    digest = hashlib.sha1(opp.key.encode()).hexdigest()[:8]
    return f"H-{opp.target.symbol}-{digest}"


def _mod_text(lang: str, mod: Modulation) -> str:
    return tr(lang, f"mod.{mod.value}")


def build_hypothesis(opp: Opportunity, lang: str = "lt") -> Hypothesis:
    t, d = opp.target, opp.disease
    mod = opp.direction.modulation
    name = t.name or t.symbol

    genetics = [e for e in opp.evidence if e.type is EvidenceType.HUMAN_GENETICS and e.stance is Stance.SUPPORTS]
    other_types = sorted({tr(lang, f"aspect.{e.aspect.value}") + f" ({e.source.split('·')[-1].strip()})"
                          for e in opp.evidence
                          if e.aspect is Aspect.CAUSALITY and e.type is not EvidenceType.HUMAN_GENETICS
                          and e.stance is Stance.SUPPORTS})
    parts = []
    function = t.function or next((e.summary for e in opp.evidence if e.source == "UniProt"), None)
    if function:
        parts.append(tr(lang, "hyp.mech.function", text=clean_function_text(function)))
    if genetics:
        sources = ", ".join(e.source.split("·")[-1].strip() for e in genetics[:4])
        key = "hyp.mech.genetics" if mod is not Modulation.UNKNOWN else "hyp.mech.genetics_nodir"
        parts.append(tr(lang, key, sources=sources, symbol=t.symbol, mod=_mod_text(lang, mod)))
    if other_types:
        parts.append(tr(lang, "hyp.mech.other", types="; ".join(other_types[:5])))
    a = opp.attention
    parts.append(tr(lang, "hyp.mech.gap", symbol=t.symbol, disease=d.name,
                    pubs=a.publications if a.publications is not None else "?",
                    trials=a.trials if a.trials is not None else "?",
                    patents=a.patents if a.patents is not None else "?"))

    supporting = [EvidenceAppraisal(evidence_id=e.id, interpretation=e.summary,
                                    reliability_comment=f"{e.reliability.value}: {e.reliability_rationale}")
                  for e in opp.evidence if e.stance is Stance.SUPPORTS and e.aspect in (Aspect.CAUSALITY, Aspect.DIRECTION)]
    contradicting = [EvidenceAppraisal(evidence_id=e.id, interpretation=e.summary,
                                       reliability_comment=f"{e.reliability.value}: {e.reliability_rationale}")
                     for e in opp.evidence if e.stance is Stance.CONTRADICTS]

    uncertainties = []
    if mod is Modulation.UNKNOWN or opp.direction.confidence < 0.5:
        uncertainties.append(tr(lang, "unc.direction"))
    uncertainties.append(tr(lang, "unc.tissue"))
    if mod is Modulation.ACTIVATION or "activation_hard" in opp.scores.notes:
        uncertainties.append(tr(lang, "unc.activation"))
    if not opp.tractability.potent_compounds and not t.high_quality_probes:
        uncertainties.append(tr(lang, "unc.no_chem"))
    if not any(e.reliability is Reliability.A for e in opp.evidence if e.aspect is Aspect.CAUSALITY):
        uncertainties.append(tr(lang, "unc.preliminary"))

    safety = []
    if t.safety_events:
        safety.append(tr(lang, "safety.events", events="; ".join(t.safety_events[:6])))
    if t.lof_oe_upper is not None and t.lof_oe_upper < 0.35:
        safety.append(tr(lang, "safety.constraint", v=t.lof_oe_upper))
    if t.is_essential:
        safety.append(tr(lang, "safety.essential"))
    if (t.n_associated_diseases or 0) > 1000:
        safety.append(tr(lang, "safety.pleiotropy", n=t.n_associated_diseases))
    if not safety:
        safety.append(tr(lang, "safety.none"))

    experiments = [
        Experiment(title=tr(lang, "exp.genetic.title"), design=tr(lang, f"exp.genetic.{mod.value}", symbol=t.symbol),
                   expected_if_true=tr(lang, "exp.genetic.true"), expected_if_false=tr(lang, "exp.genetic.false"),
                   priority="high"),
        Experiment(title=tr(lang, "exp.mr.title"), design=tr(lang, "exp.mr.design", symbol=t.symbol),
                   expected_if_true=tr(lang, "exp.mr.true"), expected_if_false=tr(lang, "exp.mr.false"),
                   priority="high" if opp.association.datasource_scores.get("gwas_credible_sets") else "medium"),
        Experiment(title=tr(lang, "exp.patient.title"), design=tr(lang, "exp.patient.design", symbol=t.symbol),
                   expected_if_true=tr(lang, "exp.patient.true"), expected_if_false=tr(lang, "exp.patient.false"),
                   priority="medium"),
    ]
    if opp.tractability.best_compounds and mod is not Modulation.ACTIVATION:
        cmpds = ", ".join(opp.tractability.best_compounds[:3])
        experiments.insert(1, Experiment(
            title=tr(lang, "exp.tool.title"), design=tr(lang, "exp.tool.design", cmpds=cmpds),
            expected_if_true=tr(lang, "exp.tool.true"), expected_if_false=tr(lang, "exp.tool.false"),
            priority="high"))
    elif opp.tractability.pdb_structures:
        pdb_refs = [r.label.split(":")[0].replace("PDB ", "") for e in opp.evidence if e.source == "RCSB PDB"
                    for r in e.references]
        experiments.append(Experiment(
            title=tr(lang, "exp.structure.title"), design=tr(lang, "exp.structure.design", pdb=", ".join(pdb_refs[:3]) or "PDB"),
            expected_if_true=tr(lang, "exp.structure.true"), expected_if_false=tr(lang, "exp.structure.false"),
            priority="medium"))

    disease_ids = {d.id, *d.descendants}
    in_disease = [f"{x.name} ({x.max_stage})" for x in t.known_drugs if disease_ids.intersection(x.disease_ids)]
    any_drugs = [f"{x.name} ({x.max_stage}; {', '.join(x.action_types) or '?'})" for x in t.known_drugs]
    if in_disease:
        landscape = tr(lang, "hyp.landscape.disease", symbol=t.symbol, drugs=", ".join(sorted(set(in_disease))[:6]))
    elif any_drugs:
        landscape = tr(lang, "hyp.landscape.none", symbol=t.symbol) + " " + \
            tr(lang, "hyp.landscape.drugs", symbol=t.symbol, drugs=", ".join(sorted(set(any_drugs))[:6]))
    else:
        landscape = tr(lang, "hyp.landscape.none", symbol=t.symbol)

    return Hypothesis(
        id=hypothesis_id(opp),
        title=tr(lang, "hyp.title", symbol=t.symbol, mod=_mod_text(lang, mod), disease=d.name),
        statement=tr(lang, f"hyp.statement.{mod.value}", symbol=t.symbol, name=name, disease=d.name),
        target_symbol=t.symbol, target_id=t.ensembl_id, disease_name=d.name, disease_id=d.id,
        modulation=mod, mechanism_rationale=" ".join(parts),
        supporting=supporting, contradicting=contradicting,
        confidence=assess_confidence(opp, lang),
        alternative_explanations=alternative_explanations(opp, lang),
        key_uncertainties=uncertainties, proposed_experiments=experiments, safety_considerations=safety,
        competitive_landscape=landscape,
        novelty_assessment=tr(lang, "hyp.novelty", attention=opp.scores.attention, gap=opp.scores.gap,
                              stage=opp.attention.max_clinical_stage or tr(lang, "stage.none")),
        generated_by="rules", language=lang,
    )
