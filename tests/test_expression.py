from discovery_agent.analysis.critique import alternative_explanations
from discovery_agent.analysis.expression import assess_expression, disease_tissues
from discovery_agent.hypothesis import build_hypothesis
from discovery_agent.models import DiseaseInfo, LocusSummary
from discovery_agent.pipeline import DiscoveryPipeline


def test_disease_tissues_by_name_qtl_and_area():
    assert "lung" in disease_tissues(DiseaseInfo(id="x", name="idiopathic pulmonary fibrosis"))[0]
    assert "brain" in disease_tissues(DiseaseInfo(id="x", name="Alzheimer disease"))[0]
    locus = LocusSummary(coloc=[{"type": "eqtl", "tissue": "Cells - Cultured fibroblasts", "strength": 0.9, "sign": 1}])
    words, origin = disease_tissues(DiseaseInfo(id="x", name="rare thing"), locus)
    assert origin == "qtl" and words == ["fibroblasts"]
    words, origin = disease_tissues(DiseaseInfo(id="x", name="rare thing", therapeutic_areas=["visual system disease"]))
    assert origin == "area" and "retina" in words
    assert disease_tissues(DiseaseInfo(id="x", name="rare thing")) == ([], "none")


def test_contamination_does_not_count_as_expression():
    d = DiseaseInfo(id="x", name="pulmonary disease")
    rows = [{"source": "gtex", "tissue": "lung", "value": 8.0}, {"source": "gtex", "tissue": "liver", "value": 30000.0}]
    assert assess_expression(rows, d).status == "low"   # 8 TPM is 0.03 % of the liver level: blood/ambient
    rows[1]["value"] = 50.0
    assert assess_expression(rows, d).status == "expressed"
    assert assess_expression([{"source": "gtex", "tissue": "skin", "value": 9.0}], d).status is None


def test_pipeline_expression_evidence_and_alternative(sources, settings):
    pipe = DiscoveryPipeline(sources, settings)
    a = pipe.analyze_pair("GENEA", "test fibrosis").opportunities[0]
    assert a.expression.status == "expressed" and a.expression.keyword_source == "area"
    item = next(e for e in a.evidence if e.source.startswith("Open Targets · baseline expression"))
    assert "lung · fibroblast 120 CPM" in item.summary
    hyp = build_hypothesis(a, "en")
    assert any("lung · fibroblast" in u for u in hyp.key_uncertainties)

    b = pipe.analyze_pair("GENEB", "test fibrosis").opportunities[0]
    assert b.expression.status == "low"
    alt = next(x for x in alternative_explanations(b, "en") if x.title == "Acts through another tissue")
    assert "classical monocyte" in alt.explanation
