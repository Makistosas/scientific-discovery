import conftest
from discovery_agent.analysis.quality import check_hypothesis, check_markdown, context_from_opportunity
from discovery_agent.hypothesis import build_hypothesis
from discovery_agent.mcp_server import DiscoveryService
from discovery_agent.pipeline import DiscoveryPipeline

GOOD = """# GENEA inhibition for test fibrosis

GENEA inhibition is proposed to slow test fibrosis (E1, E2), PMID:44444444.

## Alternative explanations
1. **Linkage to a neighbouring gene.** Test: fine-mapping and colocalisation to tell the genes apart.
2. **Reverse causation.** Test: Mendelian randomisation distinguishes cause from consequence.

## Experiments
1. CRISPRi of GENEA in fibroblasts. If true: less collagen. If false: no change in collagen.
2. Tool compound in the mouse model. If true: dose-dependent improvement. If false: no effect at full target occupancy.

## Safety
Kinase inhibition may affect wound healing; monitor liver enzymes.

## Uncertainties
The direction of effect rests on one source and the relevant cell type is unknown.

## Why under-explored
Few papers and no trials: likely a genuine gap rather than a failure.
"""


def ctx(**kw):
    base = {"evidence_ids": ["E1", "E2", "E3"], "contradicting_ids": [], "causal_a": ["E1"], "causal_ab": ["E1"],
            "direction": "inhibition", "direction_confidence": 0.7, "liabilities": [], "has_references": True}
    return {**base, **kw}


def ids(report):
    return {c.id for c in report.problems}


def test_good_markdown_is_ready():
    rep = check_markdown(GOOD, symbol="GENEA", disease="test fibrosis", modulation="inhibition",
                         confidence="moderate", context=ctx())
    assert rep.verdict == "ready" and rep.score == 100, rep.summary()


def test_missing_sections_and_overclaims():
    bad = "# GENEA\n\nThis proves that GENEA inhibition will cure test fibrosis (E1, E9).\n"
    rep = check_markdown(bad, symbol="GENEA", disease="test fibrosis", modulation="inhibition",
                         confidence="high", context=ctx(causal_a=[], contradicting_ids=["E3"]))
    assert rep.verdict == "revise"
    assert {"evidence.known", "evidence.contradicting", "alternatives.count", "experiments.count",
            "confidence.high", "language", "safety", "novelty"} <= ids(rep)


def test_direction_conflict_and_genetic_liabilities():
    rep = check_markdown(GOOD.replace("inhibition", "activation"), symbol="GENEA", disease="test fibrosis",
                         modulation="activation", confidence="low", context=ctx(liabilities=["test infection"]))
    assert {"direction.conflict", "safety.genetic"} <= ids(rep)


def test_rule_based_hypotheses_pass(sources, settings):
    opp = DiscoveryPipeline(sources, settings).analyze_pair("GENEA", "test fibrosis").opportunities[0]
    h = build_hypothesis(opp, "en")
    assert check_hypothesis(h, opp).verdict == "ready"
    assert context_from_opportunity(opp)["evidence_ids"]


def test_mcp_check_and_save_report_quality(tmp_path, settings):
    svc = DiscoveryService(settings.with_overrides(data_dir=tmp_path / "data"), sources=conftest.make_sources())
    svc.analyze_pair(target="GENEA", disease="test fibrosis")
    res = svc.check_hypothesis(target="GENEA", disease="test fibrosis", markdown="# GENEA\n\nGENEA (E1, E2) " + "x" * 60)
    assert res["dossier_found"] and res["verdict"] == "revise"
    assert any(p["id"] == "alternatives.count" for p in res["problems"])
    saved = svc.save_hypothesis(target="GENEA", disease="test fibrosis", title="GENEA inhibition", markdown=GOOD,
                                modulation="inhibition", confidence="low", verify_references=False)
    assert saved["quality"]["verdict"] in ("ready", "revise") and "score" in saved["quality"]
    assert svc.get_hypothesis(saved["id"])["quality"]["score"] == saved["quality"]["score"]
