import pytest

from discovery_agent.analysis import evidence as ev
from discovery_agent.analysis.critique import alternative_explanations, assess_confidence
from discovery_agent.analysis.direction import infer_direction
from discovery_agent.analysis.scoring import (ScoringWeights, attention_score, clinical_stage_for_disease, combine,
                                              evidence_strength, safety_risk, saturate, tractability_score)
from discovery_agent.models import (Association, AttentionMetrics, ConfidenceLevel, DirectionAssessment, DiseaseInfo,
                                    DrugInfo, Evidence, EvidenceType, Aspect, Modulation, Opportunity, Reliability,
                                    Stance, TargetInfo, TractabilityMetrics)

# --------------------------------------------------------------------------- direction


def test_direction_lof_risk_means_activation():
    d = infer_direction({"gene_burden": [{"directionOnTarget": "LoF", "directionOnTrait": "risk", "score": 1}]})
    assert d.modulation is Modulation.ACTIVATION and d.confidence > 0.4


def test_direction_gof_risk_and_lof_protect_mean_inhibition():
    d = infer_direction({
        "gwas_credible_sets": [{"directionOnTarget": "GoF", "directionOnTrait": "risk", "score": 0.9}],
        "clinical_precedence": [{"directionOnTarget": "LoF", "directionOnTrait": "protect", "score": 1}],
    })
    assert d.modulation is Modulation.INHIBITION
    assert d.votes_activation == 0


def test_direction_conflict_is_unknown():
    d = infer_direction({
        "gene_burden": [{"directionOnTarget": "LoF", "directionOnTrait": "risk", "score": 1}],
        "clinical_precedence": [{"directionOnTarget": "LoF", "directionOnTrait": "protect", "score": 1}],
    })
    assert d.modulation is Modulation.UNKNOWN


def test_direction_per_source_cap_prevents_clinvar_flood():
    many = [{"directionOnTarget": "LoF", "directionOnTrait": "risk", "score": 0.9}] * 60
    d = infer_direction({"eva": many,
                         "gene_burden": [{"directionOnTarget": "LoF", "directionOnTrait": "protect", "score": 1}]})
    assert d.votes_activation <= 2.0 * 0.6 + 1e-9


def test_no_direction_rows():
    d = infer_direction({"gwas_credible_sets": [{"score": 0.9}]})
    assert d.modulation is Modulation.UNKNOWN and d.confidence == 0

# --------------------------------------------------------------------------- scoring


def test_evidence_strength_noisy_or_rewards_convergence():
    single = evidence_strength({"genetic_association": 0.6})
    both = evidence_strength({"genetic_association": 0.6, "animal_model": 0.6})
    assert 0 < single < both < 1
    assert evidence_strength({"literature": 1.0, "clinical": 1.0}) == 0  # attention-type datatypes excluded


def test_saturate():
    assert saturate(0, 300) == 0 and saturate(None, 300) == 0
    assert saturate(300, 300) == pytest.approx(1.0)
    assert saturate(10, 300) < saturate(100, 300) < 1


def test_attention_clinical_stage_and_parent():
    w = ScoringWeights()
    assert attention_score(AttentionMetrics(max_clinical_stage="APPROVAL"), w, enriched=False) == 1.0
    low = attention_score(AttentionMetrics(publications=2), w, enriched=True)
    inherited = attention_score(AttentionMetrics(publications=2, parent_publications=5000), w, enriched=True)
    assert low < 0.25 and inherited > 0.6


def test_clinical_stage_matches_descendants():
    t = TargetInfo(ensembl_id="E", symbol="X", known_drugs=[
        DrugInfo(chembl_id="C1", name="D1", max_stage="PHASE_2", disease_ids=["SUB"]),
        DrugInfo(chembl_id="C2", name="D2", max_stage="APPROVAL", disease_ids=["OTHER"])])
    d = DiseaseInfo(id="D", name="d", descendants=["SUB"])
    assert clinical_stage_for_disease(t, d) == ("PHASE_2", 2.0)


def test_tractability_activation_penalty_for_enzymes():
    t = TargetInfo(ensembl_id="E", symbol="X", target_classes=["Enzyme"], tractability={"SM": ["Druggable Family"]})
    tm = TractabilityMetrics()
    inh, _ = tractability_score(t, tm, Modulation.INHIBITION)
    act, notes = tractability_score(t, tm, Modulation.ACTIVATION)
    assert act < inh and "activation_hard" in notes
    receptor = t.model_copy(update={"target_classes": ["GPCR"]})
    assert tractability_score(receptor, tm, Modulation.ACTIVATION)[0] == inh


def test_safety_risk_depends_on_direction():
    t = TargetInfo(ensembl_id="E", symbol="X", is_essential=True, lof_oe_upper=0.1)
    inh, notes = safety_risk(t, Modulation.INHIBITION)
    act, _ = safety_risk(t, Modulation.ACTIVATION)
    assert inh == pytest.approx(0.55) and act == 0 and "essential" in notes


def test_combine_is_bounded_and_monotonic():
    w = ScoringWeights()
    d = DirectionAssessment(confidence=1.0)
    hi = combine(1.0, 0.0, 1.0, 0.0, d, w)
    assert hi == pytest.approx(1.0)
    assert combine(1.0, 0.5, 1.0, 0.0, d, w) < hi
    assert combine(0.5, 0.0, 0.0, 1.0, DirectionAssessment(), w) >= 0


def test_weights_from_dict_ignores_unknown_keys():
    w = ScoringWeights.from_dict({"min_evidence": 0.4, "bogus": 1})
    assert w.min_evidence == 0.4

# --------------------------------------------------------------------------- evidence grading


def _target():
    return TargetInfo(ensembl_id="ENSG1", symbol="X")


def test_clinvar_grading_by_review_status():
    rows = [{"datasourceId": "eva", "score": 0.9, "clinicalSignificances": ["pathogenic"],
             "confidence": "reviewed by expert panel"}]
    item = ev.ot_evidence({"genetic": rows}, _target(), "D", "en")[0]
    assert item.reliability is Reliability.A and "3★" in item.reliability_rationale
    rows[0]["confidence"] = "criteria provided, single submitter"
    assert ev.ot_evidence({"genetic": rows}, _target(), "D", "en")[0].reliability is Reliability.B
    rows[0]["clinicalSignificances"] = ["likely pathogenic"]
    assert ev.ot_evidence({"genetic": rows}, _target(), "D", "en")[0].reliability is Reliability.C


def test_refuted_curation_contradicts():
    rows = [{"datasourceId": "clingen", "score": 0.1, "confidence": "Refuted"}]
    item = ev.ot_evidence({"genetic": rows}, _target(), "D", "en")[0]
    assert item.stance is Stance.CONTRADICTS


def test_gene_burden_pvalue_threshold():
    strong = [{"datasourceId": "gene_burden", "score": 1, "pValueMantissa": 3.0, "pValueExponent": -9}]
    weak = [{"datasourceId": "gene_burden", "score": 1, "pValueMantissa": 3.0, "pValueExponent": -4}]
    assert ev.ot_evidence({"g": strong}, _target(), "D", "en")[0].reliability is Reliability.A
    assert ev.ot_evidence({"g": weak}, _target(), "D", "en")[0].reliability is Reliability.B


def test_clinical_stop_reasons_create_contradicting_item():
    rows = [{"datasourceId": "clinical_precedence", "score": 0.5, "clinicalStage": "PHASE_2",
             "clinicalReportId": "nct01234567", "trialStopReasonCategories": ["Negative"],
             "drug": {"name": "FAILMAB"}}]
    items = ev.ot_evidence({"clinical": rows}, _target(), "D", "en")
    assert len(items) == 2
    contra = [i for i in items if i.stance is Stance.CONTRADICTS][0]
    assert "FAILMAB" in contra.summary and contra.references[0].label == "NCT01234567"


def test_aggregates_one_item_per_datasource_with_refs():
    rows = [{"datasourceId": "eva", "score": 0.9, "literature": ["123456"], "clinicalSignificances": ["pathogenic"]}] * 5
    items = ev.ot_evidence({"genetic": rows}, _target(), "D", "en")
    assert len(items) == 1
    assert any(r.pmid == "123456" for r in items[0].references)


def test_assign_ids_orders_by_aspect_then_reliability():
    def e(aspect, rel):
        return Evidence(source="s", type=EvidenceType.LITERATURE, aspect=aspect, summary="x", reliability=rel,
                        reliability_rationale="r")
    items = ev.assign_ids([e(Aspect.ATTENTION, Reliability.D), e(Aspect.CAUSALITY, Reliability.B),
                           e(Aspect.CAUSALITY, Reliability.A)])
    assert [(i.id, i.aspect, i.reliability) for i in items][0] == ("E1", Aspect.CAUSALITY, Reliability.A)
    assert items[-1].aspect is Aspect.ATTENTION

# --------------------------------------------------------------------------- critique


def _opp(datatypes, sources_, evidence, direction=None, gap=0.8, n_dis=10):
    opp = Opportunity(
        target=TargetInfo(ensembl_id="E", symbol="X", n_associated_diseases=n_dis),
        disease=DiseaseInfo(id="D", name="d"),
        association=Association(target_id="E", symbol="X", disease_id="D", disease_name="d", score=0.5,
                                datatype_scores=datatypes, datasource_scores=sources_),
        evidence=evidence, direction=direction or DirectionAssessment(),
    )
    opp.scores.gap = gap
    return opp


def _item(etype, rel, stance=Stance.SUPPORTS, aspect=Aspect.CAUSALITY, i="E1"):
    return Evidence(id=i, source=etype.value, type=etype, aspect=aspect, summary="s", reliability=rel,
                    reliability_rationale="r", stance=stance)


def test_confidence_high_for_convergent_human_genetics():
    opp = _opp({"genetic_association": 0.9, "animal_model": 0.5, "affected_pathway": 0.4},
               {"gene_burden": 0.9, "gwas_credible_sets": 0.8, "impc": 0.5},
               [_item(EvidenceType.HUMAN_GENETICS, Reliability.A), _item(EvidenceType.ANIMAL_MODEL, Reliability.C, i="E2"),
                _item(EvidenceType.FUNCTIONAL, Reliability.C, i="E3")],
               DirectionAssessment(modulation=Modulation.INHIBITION, confidence=0.9))
    c = assess_confidence(opp, "en")
    assert c.level is ConfidenceLevel.HIGH and c.score > 0.8


def test_confidence_very_low_for_literature_only():
    opp = _opp({"literature": 0.5}, {"europepmc": 0.5}, [])
    c = assess_confidence(opp, "en")
    assert c.level is ConfidenceLevel.VERY_LOW and c.downgrades


def test_mendelian_only_is_downgraded_and_explained():
    opp = _opp({"genetic_association": 0.9, "genetic_literature": 0.9}, {"eva": 0.9, "clingen": 0.9},
               [_item(EvidenceType.HUMAN_GENETICS, Reliability.A)],
               DirectionAssessment(modulation=Modulation.ACTIVATION, confidence=0.9))
    c = assess_confidence(opp, "en")
    assert any("Mendelian" in d for d in c.downgrades)
    titles = [a.title for a in alternative_explanations(opp, "en")]
    assert any("Mendelian" in t for t in titles)
    assert any("Restoring function" in t for t in titles)


def test_alternatives_follow_evidence_shape():
    opp = _opp({"genetic_association": 0.5, "rna_expression": 0.5, "literature": 0.3},
               {"gwas_credible_sets": 0.4, "expression_atlas": 0.5},
               [_item(EvidenceType.HUMAN_GENETICS, Reliability.B),
                _item(EvidenceType.CLINICAL, Reliability.B, stance=Stance.CONTRADICTS, i="E2")],
               n_dis=1200)
    alts = {a.title: a for a in alternative_explanations(opp, "en")}
    assert alts["A different causal gene at the same locus"].plausibility == "high"
    assert "Reverse causation" in alts
    assert "Pleiotropy / mediating trait" in alts
    neg = alts["Low attention may hide unpublished negative results"]
    assert neg.plausibility == "high" and neg.evidence_ids == ["E2"]
    assert "Wrong direction of modulation" in alts


def test_locus_neighbors_raise_locus_alternative():
    from discovery_agent.pipeline import annotate_locus_neighbors

    def gw(symbol, start, score):
        return _opp({"genetic_association": score}, {"gwas_credible_sets": score}, []).model_copy(update={
            "target": TargetInfo(ensembl_id=symbol, symbol=symbol, chromosome="11", start=start, end=start + 40_000)})

    muc5ac, muc5b, far = gw("MUC5AC", 1_157_953, 0.83), gw("MUC5B", 1_223_066, 0.68), gw("FAR", 9_000_000, 0.9)
    annotate_locus_neighbors([muc5ac], [muc5ac, muc5b, far])
    assert muc5ac.locus_neighbors == ["MUC5B (GWAS 0.68, 25 kb)"]
    alt = alternative_explanations(muc5ac, "en")[0]
    assert alt.plausibility == "high" and "MUC5B" in alt.explanation
