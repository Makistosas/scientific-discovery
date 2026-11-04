from discovery_agent.agent import DiscoveryAgent, load_run
from discovery_agent.hypothesis import build_hypothesis, clean_function_text
from discovery_agent.i18n import MESSAGES, tr
from discovery_agent.pipeline import DiscoveryPipeline
from discovery_agent.report import ranking_table, render_hypothesis


def test_every_message_has_both_languages():
    missing = [k for k, v in MESSAGES.items() if not v.get("lt") or not v.get("en")]
    assert missing == []
    assert tr("lt", "level.high") == "Aukštas" and tr("xx", "level.high") == "High"
    assert tr("en", "no.such.key") == "no.such.key"


def test_clean_function_text():
    raw = "Kinase that does X (PubMed:123, PubMed:456). It also does Y. Third. {ECO:0000269|PubMed:1}"
    assert clean_function_text(raw) == "Kinase that does X. It also does Y."


def test_rule_based_hypothesis_lt_and_en(sources, settings, settings_lt):
    for st, word in ((settings, "Restoring or enhancing"), (settings_lt, "funkcijos atkūrimas")):
        result = DiscoveryPipeline(sources, st).analyze_pair("GENEA", "test fibrosis")
        opp = result.opportunities[0]
        h = build_hypothesis(opp, st.language)
        assert word in h.statement
        assert h.supporting and all(a.evidence_id in opp.evidence_by_id() for a in h.supporting)
        assert h.proposed_experiments and h.alternative_explanations
        assert h.generated_by == "rules"
        text = render_hypothesis(h, opp, st.language)
        assert "E1" in text and opp.target.symbol in text


def test_tool_compound_experiment_for_inhibition(sources, settings):
    opp = DiscoveryPipeline(sources, settings).analyze_pair("GENED", "test fibrosis").opportunities[0]
    opp.tractability.best_compounds = ["CHEMBL5 (TOOL, pChEMBL 8.4)"]
    h = build_hypothesis(opp, "en")
    assert any("CHEMBL5" in x.design for x in h.proposed_experiments)


def test_agent_writes_outputs_and_roundtrips(tmp_path, sources, settings):
    agent = DiscoveryAgent(settings, sources=sources)
    run = agent.run_disease("test fibrosis", top_k=3, enrich_k=3)
    paths = agent.write_outputs(run, tmp_path / "out")
    report = paths["report"].read_text(encoding="utf-8")
    assert "GENEA" in report and "Opportunity ranking" in report
    assert len(list((tmp_path / "out" / "hypotheses").glob("*.md"))) == len(run.result.opportunities)
    again = load_run(paths["json"])
    assert again.result.opportunities[0].key == run.result.opportunities[0].key
    assert set(again.hypotheses) == set(run.hypotheses)
    table = ranking_table(run.result.opportunities, "en")
    assert table.count("\n") == len(run.result.opportunities) + 1
