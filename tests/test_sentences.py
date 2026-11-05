from discovery_agent.analysis.critique import alternative_explanations
from discovery_agent.analysis.sentences import classify, full_text_sentences, select_sentences
from discovery_agent.llm.investigator import ToolSession
from discovery_agent.pipeline import DiscoveryPipeline

from conftest import FakeOpenTargets


def test_classify_cues():
    assert classify("Knockdown of X attenuated fibrosis.")["cue"] == "inhibition_benefit"
    assert classify("X overexpression aggravated injury.")["cue"] == "activation_harm"
    assert classify("X was not associated with disease.")["negative"]
    assert classify("X is expressed in lung.") == {"negative": False, "cue": None}
    assert not classify("The drug was efficacious and not associated with safety concerns.")["negative"]


def test_select_dedupes_and_prefers_results_and_negatives():
    rows = FakeOpenTargets().literature_sentences("EFO_0000001", "ENSG00000000001")
    sel = select_sentences(rows)
    assert len(sel) == 3                                   # duplicate removed
    assert {sel[0]["pmid"], sel[1]["pmid"]} == {"44444444", "44444445"}  # one per paper first
    assert {sel[0]["cue"], sel[1]["cue"]} == {"inhibition_benefit", None} and sel[2]["section"] == "abstract"
    assert any(x["negative"] for x in sel[:2])


def test_full_text_sentences_ranks_results():
    xml = "<article><body><sec><title>Results</title><p>Loss of GENEA reduced fibrosis. Nothing else.</p></sec>" \
          "<sec><title>Introduction</title><p>GENEA is a kinase.</p></sec></body></article>"
    res = full_text_sentences(xml, ["GENEA"], ["fibrosis"])
    assert res["matches"] == 2 and res["sentences"][0]["section"] == "Results"
    assert res["sentences"][0]["cue"] == "inhibition_benefit"
    assert "error" in full_text_sentences("<not xml", ["GENEA"])


def test_pipeline_sentence_evidence_and_negative_alternative(sources, settings):
    opp = DiscoveryPipeline(sources, settings).analyze_pair("GENEA", "test fibrosis").opportunities[0]
    assert opp.text_sentences
    item = next(e for e in opp.evidence if e.source == "Europe PMC text mining (via Open Targets)")
    assert "negative result" in item.summary or "neigiamas rezultatas" in item.summary
    alt = next(a for a in alternative_explanations(opp, "en") if "44444445" in a.explanation)
    assert alt.plausibility == "moderate"   # one paper with a null result is not enough for "high"


def test_read_full_text_tool(sources, settings):
    opp = DiscoveryPipeline(sources, settings).analyze_pair("GENEA", "test fibrosis").opportunities[0]
    session = ToolSession(sources, opp)
    import json
    out, err = session.run("read_full_text", {"id": "44444444", "terms": []})
    data = json.loads(out)
    assert not err and data["pmcid"] == "PMC1000001" and data["sentences"][0]["section"] == "Results"
    assert any(s["negative"] for s in data["sentences"])
    out, err = session.run("read_full_text", {"id": "44444445", "terms": []})
    assert err and "open-access" in json.loads(out)["error"]
