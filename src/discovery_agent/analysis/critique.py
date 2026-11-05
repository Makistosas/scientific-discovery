"""Self-critique: confidence grading and alternative explanations.

Confidence follows a GRADE-like logic: start from the quality of the best
causal evidence, then up/down-grade for convergence, direction consistency,
contradicting evidence and reliance on non-peer-reviewed sources.
Alternative explanations are generated from the *shape* of the evidence
(e.g. GWAS-only -> locus misassignment; expression-only -> reverse causation).
"""

from __future__ import annotations

from ..i18n import tr
from ..models import (AlternativeExplanation, Aspect, ConfidenceAssessment, ConfidenceLevel, EvidenceType,
                      Modulation, Opportunity, Reliability, Stance)

_LEVELS = [ConfidenceLevel.VERY_LOW, ConfidenceLevel.LOW, ConfidenceLevel.MODERATE, ConfidenceLevel.HIGH]
MENDELIAN_SOURCES = ("eva", "genomics_england", "clingen", "gene2phenotype", "orphanet", "uniprot_literature",
                     "uniprot_variants")


def assess_confidence(opp: Opportunity, lang: str) -> ConfidenceAssessment:
    causal = [e for e in opp.evidence if e.aspect is Aspect.CAUSALITY and e.stance is Stance.SUPPORTS]
    contradicting = [e for e in opp.evidence if e.stance is Stance.CONTRADICTS]
    upgrades: list[str] = []
    downgrades: list[str] = []

    genetics_a = any(e.type is EvidenceType.HUMAN_GENETICS and e.reliability is Reliability.A for e in causal)
    clinical_a = any(e.type is EvidenceType.CLINICAL and e.reliability is Reliability.A for e in causal)
    best = min((e.reliability.value for e in causal), default="D")
    if genetics_a:
        level, start = 3, tr(lang, "conf.start.genetics")
    elif clinical_a:
        level, start = 3, tr(lang, "conf.start.clinical")
    elif best == "B":
        level, start = 2, tr(lang, "conf.start.moderate")
    elif best == "C":
        level, start = 1, tr(lang, "conf.start.low")
    else:
        level, start = 0, tr(lang, "conf.start.very_low")

    types = {e.type for e in causal}
    datatypes = {dt for dt, s in opp.association.datatype_scores.items() if s >= 0.1 and dt != "literature"}
    n_types = max(len(types), len(datatypes))
    if n_types >= 3:
        level += 1
        upgrades.append(tr(lang, "conf.up.convergence", n=n_types))
    elif n_types <= 1:
        level -= 1
        downgrades.append(tr(lang, "conf.down.single"))

    d = opp.direction
    if d.modulation is not Modulation.UNKNOWN and d.confidence >= 0.6:
        upgrades.append(tr(lang, "conf.up.direction", c=d.confidence))
    else:
        level -= 1
        downgrades.append(tr(lang, "conf.down.direction"))

    if contradicting:
        level -= 1
        downgrades.append(tr(lang, "conf.down.contradicting", n=len(contradicting)))
    if causal and all(not e.peer_reviewed for e in causal):
        level -= 1
        downgrades.append(tr(lang, "conf.down.preprint"))
    if is_mendelian_only(opp):
        level -= 1
        downgrades.append(tr(lang, "conf.down.indirect"))

    level = max(0, min(3, level))
    # numeric score: mean reliability weight of supporting causal evidence, adjusted by grade level
    rel = sum(e.reliability.weight for e in causal) / len(causal) if causal else 0.1
    score = round(min(1.0, 0.6 * (level + 1) / 4 + 0.25 * rel + 0.15 * d.confidence), 3)
    lvl = _LEVELS[level]
    rationale = " ".join([start, *upgrades, *downgrades,
                          tr(lang, "conf.rationale", level=tr(lang, f"level.{lvl.value}"), score=score)])
    return ConfidenceAssessment(level=lvl, score=score, rationale=rationale, upgrades=upgrades, downgrades=downgrades)


def is_mendelian_only(opp: Opportunity) -> bool:
    """Evidence comes from rare Mendelian variants only (no GWAS / burden support)."""
    ds = opp.association.datasource_scores
    mendelian = max((ds.get(s, 0.0) for s in MENDELIAN_SOURCES), default=0.0)
    return mendelian > 0 and ds.get("gwas_credible_sets", 0.0) < 0.2 and ds.get("gene_burden", 0.0) < 0.2


def alternative_explanations(opp: Opportunity, lang: str) -> list[AlternativeExplanation]:
    ds = opp.association.datasource_scores
    dt = opp.association.datatype_scores
    ids = lambda pred: [e.id for e in opp.evidence if pred(e)]  # noqa: E731
    out: list[AlternativeExplanation] = []

    gwas = ds.get("gwas_credible_sets", 0.0)
    if gwas > 0:
        text = tr(lang, "alt.locus.text", s=gwas)
        locus = opp.locus
        if locus and locus.conflict:
            details = "; ".join(locus.competitors + locus.other_coloc_genes)
            text += " " + tr(lang, "alt.locus.conflict", details=details)
        elif locus and locus.supported:
            text += " " + tr(lang, "alt.locus.supported", symbol=opp.target.symbol)
        if opp.locus_neighbors:
            text += " " + tr(lang, "alt.locus.neighbors", genes=", ".join(opp.locus_neighbors))
        if locus and locus.conflict:
            plaus = "high"
        elif locus and locus.supported and (locus.target_l2g or 0) >= 0.5:
            plaus = "low"
        else:
            plaus = "high" if gwas < 0.5 or opp.locus_neighbors else ("moderate" if gwas < 0.8 else "low")
        out.append(AlternativeExplanation(
            title=tr(lang, "alt.locus.title"), explanation=text, plausibility=plaus,
            how_to_test=tr(lang, "alt.locus.test"), evidence_ids=ids(lambda e: "GWAS" in e.source)))

    if is_mendelian_only(opp):
        out.append(AlternativeExplanation(
            title=tr(lang, "alt.mendelian.title"), explanation=tr(lang, "alt.mendelian.text"),
            plausibility="high", how_to_test=tr(lang, "alt.mendelian.test"),
            evidence_ids=ids(lambda e: e.type is EvidenceType.HUMAN_GENETICS)))

    if dt.get("somatic_mutation", 0.0) > 0.1:
        out.append(AlternativeExplanation(
            title=tr(lang, "alt.passenger.title"), explanation=tr(lang, "alt.passenger.text"),
            plausibility="moderate", how_to_test=tr(lang, "alt.passenger.test"),
            evidence_ids=ids(lambda e: e.type is EvidenceType.SOMATIC)))

    causal_non_expr = sum(v for k, v in dt.items() if k not in ("rna_expression", "literature"))
    if dt.get("rna_expression", 0.0) > 0.1:
        out.append(AlternativeExplanation(
            title=tr(lang, "alt.reverse.title"), explanation=tr(lang, "alt.reverse.text"),
            plausibility="high" if causal_non_expr < 0.2 else "moderate", how_to_test=tr(lang, "alt.reverse.test"),
            evidence_ids=ids(lambda e: e.type is EvidenceType.EXPRESSION)))

    if dt.get("animal_model", 0.0) > 0.1 and dt.get("genetic_association", 0.0) < 0.2:
        out.append(AlternativeExplanation(
            title=tr(lang, "alt.species.title"), explanation=tr(lang, "alt.species.text"),
            plausibility="moderate", how_to_test=tr(lang, "alt.species.test"),
            evidence_ids=ids(lambda e: e.type is EvidenceType.ANIMAL_MODEL)))

    n_dis = opp.target.n_associated_diseases or 0
    if n_dis > 300:
        out.append(AlternativeExplanation(
            title=tr(lang, "alt.pleiotropy.title"),
            explanation=tr(lang, "alt.pleiotropy.text", symbol=opp.target.symbol, n=n_dis),
            plausibility="moderate" if n_dis > 1000 else "low", how_to_test=tr(lang, "alt.pleiotropy.test")))

    stopped = [e for e in opp.evidence if e.stance is Stance.CONTRADICTS]
    if opp.scores.gap > 0.5 or stopped:
        text = tr(lang, "alt.negative.text") + (" " + tr(lang, "alt.negative.text_stopped") if stopped else "")
        att = opp.attention
        if att.gene_publications is not None and att.gene_publications < 300:
            text += " " + tr(lang, "alt.negative.dark_gene", symbol=opp.target.symbol, n=att.gene_publications)
        elif att.gene_publications is not None and att.gene_publications >= 3000:
            text += " " + tr(lang, "alt.negative.known_gene", symbol=opp.target.symbol, n=att.gene_publications)
        if att.trend == "declining":
            text += " " + tr(lang, "alt.negative.declining")
        out.append(AlternativeExplanation(
            title=tr(lang, "alt.negative.title"), explanation=text,
            plausibility="high" if stopped else "moderate", how_to_test=tr(lang, "alt.negative.test"),
            evidence_ids=[e.id for e in stopped]))

    if dt.get("literature", 0.0) > 0 and causal_non_expr < 0.15:
        out.append(AlternativeExplanation(
            title=tr(lang, "alt.textmining.title"), explanation=tr(lang, "alt.textmining.text"),
            plausibility="high", how_to_test=tr(lang, "alt.textmining.test"),
            evidence_ids=ids(lambda e: e.type is EvidenceType.LITERATURE)))

    if opp.locus and opp.locus.opposite_signs:
        items = "; ".join(f"{v['type']} {v['tissue']} {'+' if v['sign'] > 0 else '−'}" for v in opp.locus.coloc[:6])
        out.append(AlternativeExplanation(
            title=tr(lang, "alt.tissue.title"), explanation=tr(lang, "alt.tissue.text", items=items),
            plausibility="high", how_to_test=tr(lang, "alt.tissue.test")))
    if opp.direction.modulation is Modulation.ACTIVATION:
        out.append(AlternativeExplanation(
            title=tr(lang, "alt.irreversible.title"), explanation=tr(lang, "alt.irreversible.text"),
            plausibility="moderate", how_to_test=tr(lang, "alt.irreversible.test")))
    if opp.direction.modulation is Modulation.UNKNOWN or opp.direction.confidence < 0.5:
        out.append(AlternativeExplanation(
            title=tr(lang, "alt.direction.title"), explanation=tr(lang, "alt.direction.text"),
            plausibility="moderate", how_to_test=tr(lang, "alt.direction.test")))
    return out
