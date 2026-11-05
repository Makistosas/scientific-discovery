from discovery_agent.analysis.critique import alternative_explanations
from discovery_agent.analysis.direction import infer_direction
from discovery_agent.analysis.locus import assess_loci
from discovery_agent.models import Association, DiseaseInfo, Modulation, Opportunity, TargetInfo


def locus(l2g, coloc, locus_id="L1"):
    return {"locus_id": locus_id, "study_id": "GCST1", "trait": "IPF", "n": 1000, "literature": ["123456"],
            "l2g": [{"gene_id": g, "symbol": g, "score": s} for g, s in l2g],
            "coloc": [{"type": t, "h4": h, "clpp": None, "sign": sign, "study_id": "q", "gene_id": g, "symbol": g,
                       "tissue": tissue} for t, g, tissue, h, sign in coloc]}


def test_risk_allele_lowering_expression_means_activation():
    loci = [locus([("DPP9", 0.93)], [("eqtl", "DPP9", "fibroblast", 0.997, -1), ("sqtl", "DPP9", "lung", 0.999, -1)])]
    summary, rows = assess_loci(loci, "DPP9", "DPP9")
    assert summary.supported and not summary.conflict and summary.splice_tissues == ["lung"]
    assert [v["type"] for v in summary.coloc] == ["eqtl"]  # splicing QTLs never vote on direction
    d = infer_direction(rows)
    assert d.modulation is Modulation.ACTIVATION and 0.25 < d.confidence < 0.5  # one tissue: moderate
    assert "fibroblast" in d.rationale[0]


def test_risk_allele_raising_protein_means_inhibition():
    _, rows = assess_loci([locus([("X", 0.9)], [("pqtl", "X", "plasma", 0.95, 1)])], "X", "X")
    assert infer_direction(rows).modulation is Modulation.INHIBITION


def test_competing_gene_and_other_colocalisation_flag_conflict():
    loci = [locus([("MUC5AC", 0.12), ("MUC5B", 0.57)], [("eqtl", "MOB2", "lung", 0.99, 1)])]
    summary, rows = assess_loci(loci, "MUC5AC", "MUC5AC")
    assert summary.conflict and summary.competitors == ["MUC5B (L2G 0.57 vs 0.12)"]
    assert summary.other_coloc_genes == ["MOB2 (H4 0.99)"] and rows == {}
    opp = Opportunity(target=TargetInfo(ensembl_id="MUC5AC", symbol="MUC5AC"), disease=DiseaseInfo(id="D", name="d"),
                      association=Association(target_id="MUC5AC", symbol="MUC5AC", disease_id="D", disease_name="d",
                                              score=0.5, datasource_scores={"gwas_credible_sets": 0.83}),
                      locus=summary)
    alt = alternative_explanations(opp, "en")[0]
    assert alt.plausibility == "high" and "MUC5B" in alt.explanation and "MOB2" in alt.explanation


def test_opposite_signs_across_tissues():
    loci = [locus([("A", 0.8)], [("eqtl", "A", "lung", 0.99, -1), ("eqtl", "A", "thyroid", 0.99, 1)])]
    summary, rows = assess_loci(loci, "A", "A")
    assert summary.opposite_signs
    assert infer_direction(rows).modulation is Modulation.UNKNOWN
    opp = Opportunity(target=TargetInfo(ensembl_id="A", symbol="A"), disease=DiseaseInfo(id="D", name="d"),
                      association=Association(target_id="A", symbol="A", disease_id="D", disease_name="d", score=0.5,
                                              datasource_scores={"gwas_credible_sets": 0.8}), locus=summary)
    titles = [a.title for a in alternative_explanations(opp, "en")]
    assert "Opposite effects in different tissues" in titles


def test_weak_colocalisation_is_ignored():
    summary, rows = assess_loci([locus([("A", 0.9)], [("eqtl", "A", "lung", 0.3, 1)])], "A", "A")
    assert not summary.coloc and rows == {} and not summary.supported
