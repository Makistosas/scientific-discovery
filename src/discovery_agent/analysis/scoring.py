"""Scoring: evidence strength, research attention/gap, tractability, safety risk
and the combined *opportunity* score.

The opportunity score is deliberately multiplicative so that a candidate must be
good on every axis: strong causal evidence **and** little prior attention **and**
not obviously undruggable or unsafe.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..models import (AttentionMetrics, GeneticSafety, DiseaseInfo, DirectionAssessment, Modulation, ScoreBreakdown,
                      TargetInfo, TractabilityMetrics)
from ..sources.opentargets import stage_rank
from .phewas import genetic_safety_risk

# Causal / mechanistic datatypes and how much each counts towards evidence strength.
# "literature" and "clinical" are excluded: they measure attention, not novelty-compatible causality.
CAUSAL_WEIGHTS = {
    "genetic_association": 1.0,
    "genetic_literature": 0.85,
    "somatic_mutation": 0.85,
    "animal_model": 0.5,
    "affected_pathway": 0.45,
    "rna_expression": 0.25,
}

SM_BUCKETS = {
    "Approved Drug": 1.0, "Advanced Clinical": 0.95, "Phase 1 Clinical": 0.9, "Structure with Ligand": 0.75,
    "High-Quality Ligand": 0.75, "High-Quality Pocket": 0.65, "Med-Quality Pocket": 0.5, "Druggable Family": 0.5,
}
AB_BUCKETS = {
    "Approved Drug": 1.0, "Advanced Clinical": 0.95, "Phase 1 Clinical": 0.9, "UniProt loc high conf": 0.6,
    "GO CC high conf": 0.55, "UniProt loc med conf": 0.45, "UniProt SigP or TMHMM": 0.4,
    "Human Protein Atlas loc": 0.4, "GO CC med conf": 0.35,
}
PR_BUCKETS = {"Literature": 0.5, "Small Molecule Binder": 0.45, "UniProt Ubiquitination": 0.35,
              "Database Ubiquitination": 0.3, "Half-life Data": 0.2}
OC_BUCKETS = {"Approved Drug": 1.0, "Advanced Clinical": 0.95, "Phase 1 Clinical": 0.9}
MODALITY_BUCKETS = {"SM": SM_BUCKETS, "AB": AB_BUCKETS, "PR": PR_BUCKETS, "OC": OC_BUCKETS}

# Target classes where pharmacological activation (agonism/opening/potentiation) is routine.
ACTIVATABLE_CLASSES = ("receptor", "gpcr", "ion channel", "nuclear receptor", "transporter", "kinase")



@dataclass
class ScoringWeights:
    publications_saturation: int = 300
    trials_saturation: int = 30
    patents_saturation: int = 50
    preprints_saturation: int = 20
    lit_weight: float = 0.9
    trials_weight: float = 0.5
    patents_weight: float = 0.3
    preprints_weight: float = 0.25
    parent_weight: float = 0.7         # attention inherited from the parent disease (subtype artefacts)
    relative_saturation_bp: float = 100  # share of the disease literature (basis points) that counts as saturated
    rising_weight: float = 0.15        # extra attention when the pair grows ≥ 2× faster than its field
    max_descendants: int = 150         # target mode: skip broad disease categories
    tractability_floor: float = 0.55   # opportunity multiplier for an undruggable target
    safety_penalty: float = 0.35       # max opportunity reduction for maximal safety risk
    direction_floor: float = 0.85      # multiplier when direction is unknown
    min_evidence: float = 0.25
    causal_weights: dict[str, float] = field(default_factory=lambda: dict(CAUSAL_WEIGHTS))

    @classmethod
    def from_dict(cls, data: dict | None) -> "ScoringWeights":
        if not data:
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def saturate(n: int | float | None, saturation: float) -> float:
    if not n or n <= 0:
        return 0.0
    return min(1.0, math.log1p(n) / math.log1p(saturation))


def evidence_strength(datatype_scores: dict[str, float], weights: dict[str, float] | None = None) -> float:
    """Noisy-OR over independent causal evidence lines: rewards convergence."""
    weights = weights or CAUSAL_WEIGHTS
    prod = 1.0
    for dt, w in weights.items():
        s = max(0.0, min(1.0, datatype_scores.get(dt, 0.0)))
        prod *= 1.0 - w * s
    return round(1.0 - prod, 4)


def clinical_stage_for_disease(target: TargetInfo, disease: DiseaseInfo) -> tuple[str | None, float]:
    ids = {disease.id, *disease.descendants}
    best_stage, best_rank = None, 0.0
    for drug in target.known_drugs:
        if ids.intersection(drug.disease_ids):
            r = stage_rank(drug.max_stage)
            if r > best_rank:
                best_stage, best_rank = drug.max_stage, r
    return best_stage, best_rank


def attention_score(att: AttentionMetrics, w: ScoringWeights, *, enriched: bool) -> float:
    a_clin = stage_rank(att.max_clinical_stage) / 4.0
    if enriched and att.publications is not None:
        a_lit = saturate(att.publications, w.publications_saturation)
        if att.relative_share is not None:
            # geometric mean of absolute and field-relative attention: 30 papers mean more in a small field
            a_rel = saturate(att.relative_share * 10_000, w.relative_saturation_bp)
            a_lit = math.sqrt(a_lit * a_rel)
        if att.parent_publications:
            a_lit = max(a_lit, w.parent_weight * saturate(att.parent_publications, w.publications_saturation))
    else:
        a_lit = max(0.0, min(1.0, att.ot_literature_score))
    if enriched:
        # noisy-OR: any one channel (papers, trials, patents, preprints) can show the pair is well trodden
        soft = 1.0 - ((1.0 - w.lit_weight * a_lit)
                      * (1.0 - w.trials_weight * saturate(att.trials, w.trials_saturation))
                      * (1.0 - w.patents_weight * saturate(att.patents, w.patents_saturation))
                      * (1.0 - w.preprints_weight * saturate(att.preprints, w.preprints_saturation)))
        if att.trend == "rising":  # others are publishing now: the gap is closing
            soft = 1.0 - (1.0 - soft) * (1.0 - w.rising_weight)
    else:
        soft = a_lit
    return round(1.0 - (1.0 - a_clin) * (1.0 - soft), 4)


def tractability_score(target: TargetInfo, tract: TractabilityMetrics, modulation: Modulation) -> tuple[float, list[str]]:
    notes: list[str] = []
    t_ot = 0.0
    for modality, labels in target.tractability.items():
        table = MODALITY_BUCKETS.get(modality, {})
        for label in labels:
            t_ot = max(t_ot, table.get(label, 0.0))
    t_chem = saturate(tract.potent_compounds, 200) if tract.potent_compounds else 0.0
    if tract.best_pchembl and tract.best_pchembl >= 8:
        t_chem = min(1.0, t_chem + 0.15)
    t_pdb = 0.0
    if tract.pdb_structures:
        t_pdb = 0.5 + 0.5 * (1.0 if tract.pdb_with_ligand else 0.0)
    probe = 1.0 if (tract.high_quality_probes or target.high_quality_probes) else 0.0
    t = 1.0 - (1.0 - t_ot) * (1.0 - 0.6 * t_chem) * (1.0 - 0.3 * t_pdb) * (1.0 - 0.5 * probe)

    if modulation is Modulation.ACTIVATION:
        classes = " ".join(target.target_classes).lower()
        mechanisms = " ".join(tract.mechanisms).lower()
        if not any(c in classes for c in ACTIVATABLE_CLASSES) and not any(
                k in mechanisms for k in ("agonist", "activator", "opener", "positive")):
            t *= 0.6
            notes.append("activation_hard")
    return round(min(1.0, t), 4), notes


def safety_risk(target: TargetInfo, modulation: Modulation) -> tuple[float, list[str]]:
    risk, notes = 0.0, []
    if target.safety_events:
        risk += min(0.5, 0.1 * len(target.safety_events))
        notes.append("safety_events")
    if target.is_essential and modulation is not Modulation.ACTIVATION:
        risk += 0.3
        notes.append("essential")
    if target.lof_oe_upper is not None and target.lof_oe_upper < 0.35 and modulation is not Modulation.ACTIVATION:
        risk += 0.25
        notes.append("lof_intolerant")
    if (target.n_associated_diseases or 0) > 1500:
        risk += 0.1
        notes.append("pleiotropic")
    spec = target.prioritisation.get("tissueSpecificity")
    if spec is not None and spec < -0.5:
        risk += 0.05
        notes.append("broad_expression")
    return round(min(1.0, risk), 4), notes


def combine(evidence: float, attention: float, tractability: float, safety: float,
            direction: DirectionAssessment, w: ScoringWeights) -> float:
    gap = 1.0 - attention
    t_factor = w.tractability_floor + (1.0 - w.tractability_floor) * tractability
    s_factor = 1.0 - w.safety_penalty * safety
    d_factor = w.direction_floor + (1.0 - w.direction_floor) * direction.confidence
    return round(evidence * gap * t_factor * s_factor * d_factor, 4)


def score_opportunity(target: TargetInfo, disease: DiseaseInfo, datatype_scores: dict[str, float],
                      attention: AttentionMetrics, tract: TractabilityMetrics, direction: DirectionAssessment,
                      w: ScoringWeights, *, enriched: bool,
                      genetic_safety: GeneticSafety | None = None) -> ScoreBreakdown:
    e = evidence_strength(datatype_scores, w.causal_weights)
    a = attention_score(attention, w, enriched=enriched)
    t, t_notes = tractability_score(target, tract, direction.modulation)
    s, s_notes = safety_risk(target, direction.modulation)
    g, g_notes = genetic_safety_risk(genetic_safety)
    if g:
        s = round(min(1.0, s + g), 4)
    s_notes = s_notes + g_notes
    if enriched and attention.trend == "rising":
        s_notes = s_notes + ["attention_rising"]
    return ScoreBreakdown(
        evidence=e, attention=a, gap=round(1.0 - a, 4), tractability=t, safety_risk=s,
        direction_confidence=direction.confidence, opportunity=combine(e, a, t, s, direction, w),
        notes=t_notes + s_notes,
    )
