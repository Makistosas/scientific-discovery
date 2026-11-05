import json

import pytest
from click.testing import CliRunner

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from discovery_agent.cli import main  # noqa: E402
from discovery_agent.scan import list_files, run_scan, write_results  # noqa: E402


def build_release(root):
    """A tiny Open Targets release with the four scan datasets."""
    for d in ("association_by_datatype_direct", "target", "disease", "clinical_target"):
        (root / d).mkdir(parents=True)
    rows = [  # disease, target, datatype, score
        ("EFO_A", "ENSG1", "genetic_association", 0.9), ("EFO_A", "ENSG1", "literature", 0.05),
        ("EFO_A", "ENSG2", "genetic_association", 0.8), ("EFO_A", "ENSG2", "literature", 0.9),   # well studied
        ("EFO_A", "ENSG3", "genetic_association", 0.7),                                          # in the clinic
        ("EFO_A", "ENSG4", "animal_model", 0.4),                                                 # weak
        ("EFO_A", "ENSG5", "genetic_association", 0.9),                                          # not protein coding
        ("EFO_TA", "ENSG1", "genetic_association", 0.9),                                         # therapeutic area
        ("EFO_B", "ENSG1", "somatic_mutation", 0.6), ("EFO_B", "ENSG1", "affected_pathway", 0.5),
    ]
    half = len(rows) // 2
    for i, chunk in enumerate((rows[:half], rows[half:])):  # two parts, one pair split across them
        d, t, dt, s = zip(*chunk)
        pq.write_table(pa.table({"diseaseId": list(d), "targetId": list(t), "aggregationType": ["datatypeId"] * len(d),
                                 "aggregationValue": list(dt), "associationScore": list(s),
                                 "evidenceCount": [1] * len(d)}), root / "association_by_datatype_direct" / f"p{i}.parquet")
    pq.write_table(pa.table({"id": ["ENSG1", "ENSG2", "ENSG3", "ENSG4", "ENSG5"],
                             "approvedSymbol": ["G1", "G2", "G3", "G4", "G5"],
                             "biotype": ["protein_coding"] * 4 + ["lncRNA"]}), root / "target" / "t.parquet")
    pq.write_table(pa.table({
        "id": ["EFO_A", "EFO_B", "EFO_TA", "EFO_SUB"], "name": ["disease a", "disease b", "area", "subtype a"],
        "therapeuticAreas": [["EFO_TA"], ["EFO_TA"], [], ["EFO_TA"]],
        "ancestors": [["EFO_TA"], ["EFO_TA"], [], ["EFO_A", "EFO_TA"]],
        "descendants": [["EFO_SUB"], [], ["EFO_A", "EFO_B", "EFO_SUB"], []],
        "ontology": [{"isTherapeuticArea": False}, {"isTherapeuticArea": False}, {"isTherapeuticArea": True},
                     {"isTherapeuticArea": False}]}), root / "disease" / "d.parquet")
    pq.write_table(pa.table({"targetId": ["ENSG3"], "maxClinicalStage": ["PHASE_3"],
                             "diseases": [[{"diseaseFromSource": "sub", "diseaseId": "EFO_SUB"}]]}),
                   root / "clinical_target" / "c.parquet")


def test_scan_ranks_and_filters(tmp_path):
    build_release(tmp_path)
    rows = run_scan(tmp_path, min_evidence=0.3, top=10)
    got = [(r.symbol, r.disease_id) for r in rows]
    assert got[0] == ("G1", "EFO_A")
    assert ("G2", "EFO_A") not in got          # literature attention 0.9 > 0.5
    assert ("G3", "EFO_A") not in got          # phase 3 for a subtype propagates to the parent
    assert ("G5", "EFO_A") not in got and all(d != "EFO_TA" for _, d in got)
    assert ("G4", "EFO_A") not in got          # evidence 0.2 < 0.3
    b = next(r for r in rows if r.disease_id == "EFO_B")
    assert b.evidence == pytest.approx(1 - (1 - 0.85 * 0.6) * (1 - 0.45 * 0.5), abs=1e-4)
    assert b.datatypes == {"somatic_mutation": 0.6, "affected_pathway": 0.5}
    assert rows[0].therapeutic_areas == ["area"]
    paths = write_results(rows, tmp_path / "out", "26.09")
    assert json.loads(paths["json"].read_text())["rows"][0]["symbol"] == "G1"
    assert "| 1 | G1 |" in paths["md"].read_text()


def test_list_files_parses_index():
    html = '<a href="?C=N;O=D">x</a><a href="part-0.zstd.parquet">p</a><a href="part-0.zstd.parquet">p</a><a href="_SUCCESS">s</a>'
    files = list_files(lambda url: html, "26.09", "target", "http")
    assert files == ["http://ftp.ebi.ac.uk/pub/databases/opentargets/platform/26.09/output/target/part-0.zstd.parquet"]


def test_cli_scan_run_and_mcp_results(tmp_path, settings):
    build_release(tmp_path / "ot" / "26.09")
    res = CliRunner().invoke(main, ["scan", "run", "--dir", str(tmp_path / "ot"), "--min-evidence", "0.3",
                                    "--out", str(tmp_path / "data" / "scan" / "r1")])
    assert res.exit_code == 0, res.output
    from discovery_agent.mcp_server import DiscoveryService

    import conftest
    svc = DiscoveryService(settings.with_overrides(data_dir=tmp_path / "data"), sources=conftest.make_sources())
    out = svc.scan_results(area="area", limit=5)
    assert out["release"] == "26.09" and out["rows"][0]["symbol"] == "G1"
    assert svc.scan_results(target="G1")["matching"] == 2
