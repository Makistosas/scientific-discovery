"""Compact, self-contained evidence dossier handed to Claude."""

from __future__ import annotations

import json

from ..models import Evidence, Hypothesis, Opportunity


def evidence_dict(e: Evidence) -> dict:
    return {
        "id": e.id, "source": e.source, "type": e.type.value, "aspect": e.aspect.value, "stance": e.stance.value,
        "reliability": e.reliability.value, "reliability_rationale": e.reliability_rationale,
        "summary": e.summary, "peer_reviewed": e.peer_reviewed, "year": e.year,
        "effect_direction": e.effect_direction.value if e.effect_direction else None,
        "references": [{"label": r.label, "url": r.url} for r in e.references],
    }


def build_dossier(opp: Opportunity, draft: Hypothesis | None = None, notes: str | None = None) -> dict:
    t, d = opp.target, opp.disease
    dossier = {
        "target": {
            "symbol": t.symbol, "name": t.name, "ensembl_id": t.ensembl_id, "uniprot_id": t.uniprot_id,
            "function": t.function, "target_classes": t.target_classes,
            "subcellular_locations": t.subcellular_locations, "tractability_buckets": t.tractability,
            "known_drugs": [{"name": x.name, "chembl_id": x.chembl_id, "max_stage": x.max_stage,
                             "action_types": x.action_types, "indications": x.disease_names[:8]}
                            for x in t.known_drugs[:15]],
            "safety_liabilities": t.safety_events, "lof_oe_upper_LOEUF": t.lof_oe_upper,
            "depmap_core_essential": t.is_essential, "n_associated_diseases": t.n_associated_diseases,
        },
        "disease": {"id": d.id, "name": d.name, "description": d.description,
                    "therapeutic_areas": d.therapeutic_areas},
        "open_targets_association": {
            "overall_score": opp.association.score, "datatype_scores": opp.association.datatype_scores,
            "datasource_scores": opp.association.datasource_scores,
        },
        "scores": opp.scores.model_dump(),
        "attention_metrics": opp.attention.model_dump(),
        "tractability_metrics": opp.tractability.model_dump(),
        "direction_of_effect": opp.direction.model_dump(mode="json"),
        "gwas_locus_neighbors": opp.locus_neighbors,
        "evidence": [evidence_dict(e) for e in opp.evidence],
        "pipeline_warnings": opp.warnings,
    }
    if draft is not None:
        dossier["rule_based_draft"] = {
            "statement": draft.statement, "modulation": draft.modulation.value,
            "confidence": draft.confidence.level.value, "confidence_rationale": draft.confidence.rationale,
            "alternative_explanations": [a.title for a in draft.alternative_explanations],
        }
    if notes:
        dossier["investigation_notes"] = notes
    return dossier


def dossier_json(opp: Opportunity, draft: Hypothesis | None = None, notes: str | None = None) -> str:
    return json.dumps(build_dossier(opp, draft, notes), ensure_ascii=False, indent=1, default=str)
