from discovery_agent.analysis.phewas import assess_genetic_safety, genetic_safety_risk
from discovery_agent.hypothesis import build_hypothesis
from discovery_agent.models import DiseaseInfo, Modulation
from discovery_agent.pipeline import DiscoveryPipeline

from conftest import FakeOpenTargets


def _data():
    return FakeOpenTargets().target_safety("ENSG00000000004")


def test_inhibition_mimics_lof():
    d = DiseaseInfo(id="EFO_0000001", name="test fibrosis")
    gs = assess_genetic_safety(_data(), d, Modulation.INHIBITION)
    assert gs.mimics == "LoF"
    assert [r["disease"] for r in gs.liabilities] == ["test infection"]   # LoF -> risk
    assert [r["disease"] for r in gs.benefits] == ["test gout"]           # GoF -> risk, so LoF protects
    assert gs.other_genetic == ["test misc"]                              # no direction; own disease excluded
    assert gs.drug_warnings[0]["class"] == "hepatotoxicity"
    risk, notes = genetic_safety_risk(gs)
    assert 0.2 < risk <= 0.4 and {"genetic_liability", "mouse_lethal", "drug_warning"} <= set(notes)


def test_activation_flips_and_unknown_direction_has_no_verdict():
    d = DiseaseInfo(id="EFO_0000001", name="test fibrosis")
    act = assess_genetic_safety(_data(), d, Modulation.ACTIVATION)
    assert [r["disease"] for r in act.liabilities] == ["test gout"]
    assert [r["disease"] for r in act.benefits] == ["test infection"]
    unk = assess_genetic_safety(_data(), d, Modulation.UNKNOWN)
    assert not unk.liabilities and not unk.benefits and "test infection" in unk.other_genetic


def test_pipeline_genetic_safety_lowers_opportunity(sources, settings):
    opp = DiscoveryPipeline(sources, settings).analyze_pair("GENED", "test fibrosis").opportunities[0]
    assert opp.direction.modulation is Modulation.INHIBITION
    assert opp.genetic_safety and opp.genetic_safety.liabilities
    assert "genetic_liability" in opp.scores.notes and opp.scores.safety_risk >= 0.25
    srcs = {e.source for e in opp.evidence}
    assert "Open Targets · PheWAS (genetic direction)" in srcs and "ChEMBL warnings & FAERS (via Open Targets)" in srcs
    hyp = build_hypothesis(opp, "en")
    assert any("test infection" in s for s in hyp.safety_considerations)
