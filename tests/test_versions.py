from discovery_agent.config import Settings
from discovery_agent.http import HttpClient
from discovery_agent.monitor.review import compare
from discovery_agent.agent import DiscoveryAgent
from discovery_agent.sources.versions import data_versions, sources_data_versions, versions_markdown


class Resp:
    def __init__(self, body=None, headers=None):
        self.status_code, self._body, self.headers = 200, body, headers or {}
        self.text = ""

    def json(self):
        return self._body


class Session:
    headers: dict = {}

    def request(self, method, url, **kw):
        if "graphql" in url:
            return Resp({"data": {"meta": {"dataVersion": {"year": "26", "month": "09", "iteration": None},
                                           "apiVersion": {"x": "26", "y": "9", "z": "0"}}}})
        if "chembl" in url:
            return Resp({"chembl_db_version": "ChEMBL_37", "chembl_release_date": "2026-05-01"})
        if "uniprot" in url:
            return Resp({}, {"X-UniProt-Release": "2026_04", "X-UniProt-Release-Date": "04-November-2026"})
        if "clinicaltrials" in url:
            raise ConnectionError("offline")
        raise AssertionError(url)


def test_data_versions_parse_and_tolerate_failures(tmp_path):
    http = HttpClient(Settings(cache_dir=tmp_path, http_retries=0), session_factory=Session, sleep=lambda s: None)
    v = data_versions(http)
    assert v["open_targets"] == "26.09 (API 26.9.0)"
    assert v["chembl"] == "ChEMBL_37 (2026-05-01)" and v["uniprot"].startswith("2026_04")
    assert v["clinicaltrials_gov"].startswith("unknown")
    assert "- Open Targets: 26.09" in versions_markdown(v)


def test_fake_sources_get_tool_and_date_only(sources):
    v = sources_data_versions(sources)
    assert set(v) == {"tool", "queried"}


def test_report_lists_versions_and_release_change_is_stale(sources, settings, tmp_path):
    agent = DiscoveryAgent(settings, sources=sources)
    run = agent.run_pair("GENEA", "test fibrosis")
    md = run.report_markdown()
    assert ("Data versions" in md or "Duomenų versijos" in md) and run.data_versions["tool"].startswith("discovery-agent")
    old = {"open_targets_release": "26.06 (API 26.6.0)"}
    assert [r["code"] for r in compare(old, {"open_targets_release": "26.09 (API 26.9.0)"}, "en")] == ["release"]
