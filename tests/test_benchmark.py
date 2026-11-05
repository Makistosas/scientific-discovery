import math

from discovery_agent.benchmark import NO_ATTENTION_WEIGHTS, precision_at, render_markdown, roc_auc, run_benchmark
from discovery_agent.models import Association, DiseaseInfo


def test_roc_auc_and_precision():
    assert roc_auc([0.9, 0.8, 0.1, 0.2], [True, True, False, False]) == 1.0
    assert roc_auc([0.1, 0.2, 0.9, 0.8], [True, True, False, False]) == 0.0
    assert roc_auc([0.5, 0.5, 0.5, 0.5], [True, False, True, False]) == 0.5  # ties
    assert math.isnan(roc_auc([0.1, 0.2], [True, True]))
    assert precision_at([0.9, 0.1, 0.8], [True, False, False], 2) == 0.5


class FakeOT:
    def __init__(self):
        self.calls = []

    def resolve_disease(self, name):
        if name == "missing":
            raise RuntimeError("not found")
        return DiseaseInfo(id=f"ID_{name}", name=name)

    def disease_associations(self, disease_id, size=200, datasources=None):
        self.calls.append(datasources)
        rows = []
        for i in range(40):
            rows.append(Association(target_id=f"T{i}", symbol=f"G{i}", disease_id=disease_id, disease_name="d",
                                    biotype="protein_coding", score=1 - i / 40,
                                    datatype_scores={"genetic_association": 1 - i / 40}))
        rows.append(Association(target_id="LIT", symbol="L", disease_id=disease_id, disease_name="d", score=0.1,
                                datatype_scores={"literature": 0.9}))  # literature-only: excluded from the pool
        return DiseaseInfo(id=disease_id, name=disease_id), rows

    def clinical_target_ids(self, disease_id, size=3000):
        return {"T0", "T1", "T3", "T30"} if disease_id != "ID_few" else {"T0"}


def test_run_benchmark_scores_without_clinical_evidence():
    ot = FakeOT()
    res = run_benchmark(ot, ["a", "few", "missing"], size=50)
    assert ot.calls[0] == NO_ATTENTION_WEIGHTS  # pool ranked without clinical/literature evidence
    assert [d.disease for d in res.diseases] == ["ID_a"] and set(res.skipped) == {"few", "missing"}
    d = res.diseases[0]
    assert d.targets == 40 and d.positives == 4
    assert d.metrics["default"]["auc"] > 0.8 and d.metrics["default"]["p@10"] == 0.3
    assert res.summary["default"]["lift@10"] == round(0.3 / (4 / 40), 2)
    text = render_markdown(res)
    assert "| default |" in text and "Skipped" in text
