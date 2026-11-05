import conftest
import pytest
from click.testing import CliRunner

from discovery_agent.cli import main
from discovery_agent.mcp_server import DiscoveryService, ServiceError
from discovery_agent.monitor.review import compare

MD = "# GENEA\n\nGENEA inhibition for test fibrosis (E1, E2). " + "x" * 60


@pytest.fixture
def service(tmp_path, settings):
    return DiscoveryService(settings.with_overrides(data_dir=tmp_path / "data"), sources=conftest.make_sources())


def test_compare_reasons():
    old = {"publications": 10, "trials": 0, "trials_stopped": 0, "max_clinical_stage": None,
           "direction": "inhibition", "direction_confidence": 0.6, "liabilities": [], "negative_pmids": [],
           "opportunity": 0.5}
    new = {**old, "publications": 15, "trials": 2, "trials_stopped": 1, "max_clinical_stage": "PHASE_2",
           "direction": "activation", "liabilities": ["test infection"], "negative_pmids": ["123"],
           "opportunity": 0.2}
    codes = {r["code"] for r in compare(old, new, "en")}
    assert codes == {"papers", "trials", "stopped", "stage", "direction", "liability", "negative", "score"}
    assert compare(old, dict(old), "en") == []


def test_feedback_and_review_cycle(service):
    service.analyze_pair(target="GENEA", disease="test fibrosis")
    saved = service.save_hypothesis(target="GENEA", disease="test fibrosis", title="t", markdown=MD,
                                    verify_references=False)
    hid = saved["id"]
    assert service.get_hypothesis(hid)["snapshot"]["publications"] == 3

    # nothing changed: current; then pretend the saved snapshot had fewer papers and no trials
    res = service.review_hypotheses()
    assert res["reviewed"] == 1 and not res["items"][0]["stale"]
    last = service.store.last_review(hid)
    last["snapshot"]["publications"] = 0
    service.store.add_review(hid, False, [], {**last["snapshot"], "publications": 0, "trials": 0})
    res = service.review_hypotheses(hypothesis_id=hid)
    item = res["items"][0]
    assert item["stale"] and any("New publications" in r for r in item["reasons"])
    assert any(a["kind"] == "hypothesis_stale" for a in service.alerts())

    out = service.record_feedback(hid, "refuted", "knockdown had no effect")
    assert out["status"] == "refuted"
    assert service.review_hypotheses()["reviewed"] == 0          # refuted ones are skipped
    listed = service.list_hypotheses()["items"][0]
    assert listed["feedback"] == "refuted" and listed["stale"]
    row = service.find_opportunities(disease="test fibrosis", top_k=3, enrich_k=3)["opportunities"]
    assert next(o for o in row if o["target"] == "GENEA")["feedback"]["status"] == "refuted"
    with pytest.raises(ServiceError):
        service.record_feedback(hid, "maybe")


def test_cli_feedback(tmp_path, settings, monkeypatch, service):
    service.analyze_pair(target="GENEA", disease="test fibrosis")
    hid = service.save_hypothesis(target="GENEA", disease="test fibrosis", title="t", markdown=MD,
                                  verify_references=False)["id"]
    res = CliRunner().invoke(main, ["feedback", hid, "supported", "--note", "replicated",
                                    "--data-dir", str(service.data_dir)])
    assert res.exit_code == 0, res.output
    assert service.store.latest_feedback(hid)["status"] == "supported"
