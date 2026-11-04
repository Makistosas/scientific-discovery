"""Parsing / request-building tests for every source client, with a scripted HTTP layer."""

import json

import pytest

from discovery_agent.config import Settings
from discovery_agent.http import HttpClient, HttpError, ResponseCache
from discovery_agent.sources.chemistry import ChemblClient, PdbClient, UniProtClient
from discovery_agent.sources.clinical import ClinicalTrialsClient, parse_study
from discovery_agent.sources.literature import (BioRxivClient, EuropePMCClient, PubMedClient, cooccurrence_query,
                                                gene_terms, parse_article)
from discovery_agent.sources.opentargets import (OpenTargetsClient, OpenTargetsError, parse_target, stage_rank,
                                                 usable_synonyms)


class ScriptedHttp:
    """Routes get/post calls to canned payloads by URL substring."""

    def __init__(self, routes, settings=None):
        self.routes = routes
        self.calls = []
        self.settings = settings or Settings()

    def _match(self, url, params=None, payload=None):
        self.calls.append((url, params, payload))
        for key, value in self.routes.items():
            if key in url:
                return value(url, params, payload) if callable(value) else value
        raise AssertionError(f"unexpected URL {url}")

    def get_json(self, url, params=None, *, ttl_hours=None, headers=None):
        return self._match(url, params=params)

    def post_json(self, url, payload, *, ttl_hours=None, headers=None):
        return self._match(url, payload=payload)

# --------------------------------------------------------------------------- HTTP client


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.requests = 0

    def request(self, method, url, **kwargs):
        self.requests += 1
        return self.responses.pop(0)


def test_http_retries_then_caches(tmp_path):
    session = FakeSession([FakeResponse(503, {}), FakeResponse(429, {}, {"Retry-After": "1"}),
                           FakeResponse(200, {"ok": 1})])
    sleeps = []
    http = HttpClient(Settings(cache_dir=tmp_path, http_retries=3), session_factory=lambda: session,
                      sleep=sleeps.append)
    assert http.get_json("https://example.org/x", {"a": 1}) == {"ok": 1}
    assert session.requests == 3 and len(sleeps) == 2
    # second call is served from cache
    assert http.get_json("https://example.org/x", {"a": 1}) == {"ok": 1}
    assert session.requests == 3


def test_http_client_error_raises(tmp_path):
    session = FakeSession([FakeResponse(404, {"error": "nope"})])
    http = HttpClient(Settings(cache_dir=tmp_path), session_factory=lambda: session, sleep=lambda s: None)
    with pytest.raises(HttpError) as err:
        http.get_json("https://example.org/missing")
    assert err.value.status == 404


def test_http_ttl_zero_bypasses_cache(tmp_path):
    session = FakeSession([FakeResponse(200, {"n": 1}), FakeResponse(200, {"n": 2})])
    http = HttpClient(Settings(cache_dir=tmp_path), session_factory=lambda: session, sleep=lambda s: None)
    assert http.get_json("https://example.org/y", ttl_hours=0) == {"n": 1}
    assert http.get_json("https://example.org/y", ttl_hours=0) == {"n": 2}


def test_response_cache_expiry(tmp_path):
    cache = ResponseCache(tmp_path / "c.sqlite")
    cache.set("k", {"v": 1})
    assert cache.get("k", 3600) == {"v": 1}
    cache._conn.execute("UPDATE cache SET created = created - 7200")
    assert cache.get("k", 3600) is None
    assert cache.get("k", -1) == {"v": 1}  # negative TTL = never expires
    cache.clear()
    assert cache.get("k", 3600) is None

# --------------------------------------------------------------------------- Open Targets

OT_TARGET = {
    "id": "ENSG00000000001", "approvedSymbol": "GENEA", "approvedName": "gene alpha", "biotype": "protein_coding",
    "functionDescriptions": ["Does things."], "targetClass": [{"label": "Enzyme", "level": "l1"}],
    "subcellularLocations": [{"location": "Nucleus"}],
    "tractability": [{"label": "High-Quality Pocket", "modality": "SM", "value": True},
                     {"label": "Approved Drug", "modality": "SM", "value": False}],
    "safetyLiabilities": [{"event": "hepatotoxicity", "datasource": "ToxCast"}],
    "geneticConstraint": [{"constraintType": "lof", "oeUpper": 0.3}, {"constraintType": "mis", "oeUpper": 0.9}],
    "proteinIds": [{"id": "A0A1", "source": "uniprot_trembl"}, {"id": "P12345", "source": "uniprot_swissprot"}],
    "chemicalProbes": [{"id": "PROBE1", "isHighQuality": True}, {"id": "PROBE2", "isHighQuality": False}],
    "isEssential": False, "prioritisation": {"items": [{"key": "tissueSpecificity", "value": "-0.7"}]},
    "drugAndClinicalCandidates": {"count": 1, "rows": [{
        "maxClinicalStage": "PHASE_2",
        "drug": {"id": "CHEMBL1", "name": "DRUGX", "mechanismsOfAction": {"rows": [{"actionType": "INHIBITOR"}]}},
        "diseases": [{"disease": {"id": "EFO_1", "name": "d1"}}, {"disease": {"id": "EFO_1", "name": "d1"}}]}]},
    "associatedDiseases": {"count": 42},
}


def test_parse_target():
    t = parse_target(OT_TARGET)
    assert t.uniprot_id == "P12345"
    assert t.tractability == {"SM": ["High-Quality Pocket"]}
    assert t.lof_oe_upper == 0.3 and t.high_quality_probes == 1
    assert t.known_drugs[0].action_types == ["INHIBITOR"] and t.known_drugs[0].disease_ids == ["EFO_1"]
    assert t.prioritisation["tissueSpecificity"] == -0.7 and t.n_associated_diseases == 42


def test_usable_synonyms_filters_abbreviations():
    groups = [{"relation": "hasExactSynonym", "terms": ["IPF", "CFA", "cryptogenic fibrosing alveolitis",
                                                         "idiopathic pulmonary fibrosis", "fibrosis, idiopathic"]},
              {"relation": "hasRelatedSynonym", "terms": ["Hamman-rich disease"]}]
    assert usable_synonyms("idiopathic pulmonary fibrosis", groups) == ["cryptogenic fibrosing alveolitis"]


def test_stage_rank():
    assert stage_rank("APPROVAL") == 4 and stage_rank("phase_2") == 2 and stage_rank(None) == 0


def test_ot_resolve_disease_by_id_and_errors():
    disease = {"id": "EFO_0000768", "name": "idiopathic pulmonary fibrosis", "description": "x", "descendants": [],
               "isTherapeuticArea": False, "therapeuticAreas": [{"name": "respiratory"}],
               "synonyms": [], "parents": [{"id": "MONDO_1", "name": "pulmonary fibrosis", "isTherapeuticArea": False}]}
    http = ScriptedHttp({"opentargets": {"data": {"disease": disease}}})
    ot = OpenTargetsClient(http)
    d = ot.resolve_disease("EFO:0000768")
    assert d.id == "EFO_0000768" and d.parents == ["pulmonary fibrosis"]
    assert http.calls[0][2]["variables"] == {"id": "EFO_0000768"}
    bad = OpenTargetsClient(ScriptedHttp({"opentargets": {"errors": [{"message": "boom"}]}}))
    with pytest.raises(OpenTargetsError, match="boom"):
        bad.disease("EFO_1")


def test_ot_pair_evidence_uses_aliases():
    def respond(url, params, payload):
        assert "genetic: evidences" in payload["query"] and "clinical: evidences" in payload["query"]
        return {"data": {"disease": {"genetic": {"rows": [{"datasourceId": "eva"}]}, "clinical": None}}}

    ot = OpenTargetsClient(ScriptedHttp({"opentargets": respond}))
    out = ot.pair_evidence("EFO_1", "ENSG1", {"genetic": (["eva"], 5), "clinical": (["clinical_precedence"], 5)})
    assert out == {"genetic": [{"datasourceId": "eva"}], "clinical": []}

# --------------------------------------------------------------------------- literature


def test_gene_terms_avoid_ambiguous_symbols():
    assert gene_terms("MET", "MET proto-oncogene, receptor tyrosine kinase") == [
        "MET proto-oncogene, receptor tyrosine kinase"]
    assert gene_terms("PARN", "poly(A)-specific ribonuclease")[0] == "PARN"
    assert gene_terms("IL", None) == ["IL"]  # nothing better available


def test_cooccurrence_query():
    q = cooccurrence_query(["PARN"], ["pulmonary fibrosis"])
    assert q == '(TITLE:"PARN" OR ABSTRACT:"PARN") AND (TITLE:"pulmonary fibrosis" OR ABSTRACT:"pulmonary fibrosis")'


def test_europepmc_search_and_parse():
    payload = {"hitCount": 7, "resultList": {"result": [
        {"id": "PPR9", "source": "PPR", "doi": "10.1101/x", "title": "Preprint ", "pubYear": "2026",
         "bookOrReportDetails": {"publisher": "bioRxiv"}, "firstPublicationDate": "2026-02-01"}]}}
    http = ScriptedHttp({"europepmc": payload})
    res = EuropePMCClient(http).preprints(["PARN"], ["fibrosis"], since="2025-01-01")
    assert res.hit_count == 7 and res.articles[0].is_preprint and res.articles[0].publisher == "bioRxiv"
    url, params, _ = http.calls[0]
    assert "SRC:PPR" in params["query"] and "FIRST_PDATE:[2025-01-01 TO" in params["query"]
    assert params["resultType"] == "core"
    art = parse_article({"id": "1", "source": "MED", "pmid": "1", "pubYear": "n/a"})
    assert art.year is None and art.url == "https://pubmed.ncbi.nlm.nih.gov/1/"


def test_pubmed_count_and_summaries():
    routes = {
        "esearch": {"esearchresult": {"count": "12", "idlist": ["1", "2"]}},
        "esummary": {"result": {"uids": ["1"], "1": {"title": "T", "pubdate": "2024 Jan", "source": "J",
                                                     "articleids": [{"idtype": "doi", "value": "10.1/x"}]}}},
    }
    pm = PubMedClient(ScriptedHttp(routes, Settings(ncbi_api_key="KEY")))
    assert pm.count('"A"[tiab]') == 12
    arts = pm.summaries(["1"])
    assert arts[0].year == 2024 and arts[0].doi == "10.1/x"
    assert pm.http.calls[0][1]["api_key"] == "KEY"
    assert PubMedClient.cooccurrence_term(["A"], ["b c"]) == '("A"[tiab]) AND ("b c"[tiab])'


def test_biorxiv_published_doi():
    http = ScriptedHttp({"biorxiv": {"collection": [{"version": "1", "published": "NA"},
                                                    {"version": "2", "published": "10.1038/s1"}]}})
    assert BioRxivClient(http).published_doi("10.1101/x") == "10.1038/s1"
    assert BioRxivClient.server_for("medRxiv") == "medrxiv"
    assert BioRxivClient.server_for(None, "10.1101/2020") == "biorxiv"

# --------------------------------------------------------------------------- chemistry / structure / trials


def test_chembl_summary_counts_distinct_potent_molecules():
    routes = {
        "target.json": {"targets": [{"target_chembl_id": "CHEMBL_T", "target_type": "PROTEIN FAMILY", "organism": "Homo sapiens"},
                                    {"target_chembl_id": "CHEMBL_S", "target_type": "SINGLE PROTEIN", "organism": "Homo sapiens",
                                     "pref_name": "Kinase"}]},
        "activity.json": {"page_meta": {"total_count": 4}, "activities": [
            {"molecule_chembl_id": "M1", "pchembl_value": "8.5"}, {"molecule_chembl_id": "M1", "pchembl_value": "7.0"},
            {"molecule_chembl_id": "M2", "pchembl_value": "6.2"}, {"molecule_chembl_id": "M3", "pchembl_value": "5.0"}]},
        "mechanism.json": {"mechanisms": [{"molecule_chembl_id": "M1", "action_type": "INHIBITOR", "max_phase": 4}]},
    }
    s = ChemblClient(ScriptedHttp(routes)).summary("P1")
    assert s.target_chembl_id == "CHEMBL_S"
    assert (s.potent_compounds, s.very_potent_compounds, s.best_pchembl) == (2, 1, 8.5)
    assert s.mechanisms[0]["action_type"] == "INHIBITOR"


def test_chembl_no_target():
    assert ChemblClient(ScriptedHttp({"target.json": {"targets": []}})).summary("P1").target_chembl_id is None


def test_uniprot_entry():
    payload = {"primaryAccession": "P1", "annotationScore": 5,
               "proteinDescription": {"recommendedName": {"fullName": {"value": "Kinase 1"}, "ecNumbers": [{"value": "2.7"}]}},
               "comments": [{"commentType": "FUNCTION", "texts": [{"value": "Phosphorylates."}]},
                            {"commentType": "SUBCELLULAR LOCATION", "subcellularLocations": [{"location": {"value": "Membrane"}}]},
                            {"commentType": "DISEASE", "disease": {"diseaseId": "Fibrosis 1"}}],
               "uniProtKBCrossReferences": [{"database": "PDB", "id": "1ABC"}, {"database": "GO", "id": "GO:1"}],
               "sequence": {"length": 300}}
    e = UniProtClient(ScriptedHttp({"uniprot": payload})).entry("P1")
    assert (e.protein_name, e.function, e.is_enzyme, e.pdb_ids, e.diseases) == (
        "Kinase 1", "Phosphorylates.", True, ["1ABC"], ["Fibrosis 1"])


def test_pdb_summary_and_empty():
    def search(url, params, payload):
        nodes = payload["query"].get("nodes")
        return {"total_count": 1} if nodes else {"total_count": 3, "result_set": [{"identifier": "1ABC"}]}

    routes = {"rcsbsearch": search, "graphql": {"data": {"entries": [
        {"rcsb_id": "1ABC", "struct": {"title": "T"}, "exptl": [{"method": "X-RAY"}],
         "rcsb_entry_info": {"resolution_combined": [1.9], "nonpolymer_entity_count": 1}}]}}}
    s = PdbClient(ScriptedHttp(routes)).summary("P1")
    assert (s.total, s.with_ligand, s.best_resolution) == (3, 1, 1.9)

    def no_content(url, params, payload):
        raise HttpError("Invalid JSON", 204, url)

    assert PdbClient(ScriptedHttp({"rcsbsearch": no_content})).summary("P1").total == 0


def test_clinical_trials_parse():
    study = {"protocolSection": {
        "identificationModule": {"nctId": "NCT01", "briefTitle": "T"},
        "statusModule": {"overallStatus": "TERMINATED", "whyStopped": "futility", "startDateStruct": {"date": "2020"}},
        "designModule": {"phases": ["PHASE2"]},
        "armsInterventionsModule": {"interventions": [{"name": "Drug A"}]},
        "conditionsModule": {"conditions": ["Fibrosis"]}}}
    t = parse_study(study)
    assert t.stopped and t.url.endswith("NCT01") and t.interventions == ["Drug A"]
    http = ScriptedHttp({"clinicaltrials": {"totalCount": 1, "studies": [study]}})
    res = ClinicalTrialsClient(http).search(condition="Fibrosis", term="GENEA")
    assert res.total == 1 and len(res.stopped) == 1
    params = http.calls[0][1]
    assert params["query.cond"] == "Fibrosis" and "INTERVENTIONAL" in params["filter.advanced"]
