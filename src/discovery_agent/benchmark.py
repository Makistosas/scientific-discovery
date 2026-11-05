"""Benchmark: does the causal-evidence score rank clinically pursued targets higher?

For each disease we take the Open Targets associations **ranked with clinical and literature evidence
weighted 0** (otherwise the top-N pool mixes targets selected for genetics with targets selected for drugs,
which creates a spurious negative correlation — Berkson's paradox). Every target is scored with
:func:`evidence_strength` (genetics, somatic, models, pathways, expression — never clinical or literature,
so the test is not circular). Positives are targets with clinical-precedence evidence for the disease,
fetched by a separate query.

Metrics per weight preset: ROC AUC (0.5 = random), precision@10/@20 and lift over the base rate.
This is a *yardstick*, not a proof of clinical success: "reached the clinic" is a proxy label, and
well-studied diseases are over-represented in both the evidence and the label. Use it to compare
scoring changes against each other.
"""

from __future__ import annotations

from typing import Callable

from pydantic import BaseModel, Field

from .analysis.scoring import CAUSAL_WEIGHTS, evidence_strength
from .sources.versions import data_versions, versions_markdown

DEFAULT_DISEASES = [
    "idiopathic pulmonary fibrosis", "type 2 diabetes mellitus", "rheumatoid arthritis", "Crohn disease",
    "ulcerative colitis", "asthma", "psoriasis", "systemic lupus erythematosus",
    "EFO_0803536",  # multiple sclerosis root in Open Targets 26.x (labelled "… symptom measurement")
    "Alzheimer disease", "Parkinson disease", "amyotrophic lateral sclerosis", "schizophrenia",
    "coronary artery disease", "hypertension", "chronic obstructive pulmonary disease", "atopic eczema",
    "osteoarthritis", "breast carcinoma", "hypercholesterolemia",
]

PRESETS: dict[str, dict[str, float]] = {
    "default": dict(CAUSAL_WEIGHTS),
    "genetics_only": {"genetic_association": 1.0, "genetic_literature": 0.85},
    "equal": {k: 1.0 for k in CAUSAL_WEIGHTS},
    "no_expression": {k: v for k, v in CAUSAL_WEIGHTS.items() if k != "rna_expression"},
}


class DiseaseResult(BaseModel):
    disease: str
    disease_id: str
    targets: int
    positives: int
    metrics: dict[str, dict[str, float]] = Field(default_factory=dict)  # preset -> metric -> value


NO_ATTENTION_WEIGHTS = [{"id": "clinical_precedence", "weight": 0.0, "propagate": True},
                        {"id": "europepmc", "weight": 0.0, "propagate": True}]


class BenchmarkResult(BaseModel):
    size: int
    diseases: list[DiseaseResult] = Field(default_factory=list)
    skipped: dict[str, str] = Field(default_factory=dict)
    summary: dict[str, dict[str, float]] = Field(default_factory=dict)
    data_versions: dict = Field(default_factory=dict)


def roc_auc(scores: list[float], labels: list[bool]) -> float:
    """Mann–Whitney AUC with tie handling."""
    pairs = sorted(zip(scores, labels), key=lambda x: x[0])
    ranks: list[float] = [0.0] * len(pairs)
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2 + 1
        i = j + 1
    n_pos = sum(1 for _, y in pairs if y)
    n_neg = len(pairs) - n_pos
    if not n_pos or not n_neg:
        return float("nan")
    rank_sum = sum(r for r, (_, y) in zip(ranks, pairs) if y)
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def precision_at(scores: list[float], labels: list[bool], k: int) -> float:
    order = sorted(range(len(scores)), key=lambda i: -scores[i])[:k]
    return sum(labels[i] for i in order) / max(1, len(order))


def evaluate(datatype_scores: list[dict[str, float]], labels: list[bool], weights: dict[str, float]) -> dict:
    scores = [evidence_strength(d, weights) for d in datatype_scores]
    base = sum(labels) / len(labels)
    p10, p20 = precision_at(scores, labels, 10), precision_at(scores, labels, 20)
    return {"auc": round(roc_auc(scores, labels), 4), "p@10": round(p10, 3), "p@20": round(p20, 3),
            "base_rate": round(base, 4), "lift@10": round(p10 / base, 2) if base else 0.0}


def run_benchmark(opentargets, diseases: list[str] | None = None, *, size: int = 500,
                  presets: dict[str, dict[str, float]] | None = None, min_positives: int = 3,
                  progress: Callable[[str], None] | None = None) -> BenchmarkResult:
    presets = presets or PRESETS
    progress = progress or (lambda m: None)
    http = getattr(opentargets, "http", None)
    result = BenchmarkResult(size=size, data_versions=data_versions(http) if http is not None else {})
    for name in diseases or DEFAULT_DISEASES:
        try:
            hint = opentargets.resolve_disease(name)
            disease, assocs = opentargets.disease_associations(hint.id, size, datasources=NO_ATTENTION_WEIGHTS)
            clinical = opentargets.clinical_target_ids(hint.id)
        except Exception as exc:  # one unreachable disease must not stop the benchmark
            result.skipped[name] = str(exc)[:200]
            continue
        assocs = [a for a in assocs if a.biotype in (None, "protein_coding")
                  and any(v > 0 for k, v in a.datatype_scores.items() if k not in ("clinical", "literature"))]
        labels = [a.target_id in clinical for a in assocs]
        if sum(labels) < min_positives or sum(labels) == len(labels):
            result.skipped[name] = f"only {sum(labels)} clinical targets among {len(labels)}"
            continue
        dts = [a.datatype_scores for a in assocs]
        dr = DiseaseResult(disease=disease.name, disease_id=disease.id, targets=len(assocs), positives=sum(labels),
                           metrics={p: evaluate(dts, labels, w) for p, w in presets.items()})
        result.diseases.append(dr)
        progress(f"  {disease.name}: AUC " + ", ".join(f"{p} {m['auc']:.2f}" for p, m in dr.metrics.items()))
    for p in presets:
        rows = [d.metrics[p] for d in result.diseases]
        if rows:
            result.summary[p] = {k: round(sum(r[k] for r in rows) / len(rows), 4)
                                 for k in ("auc", "p@10", "p@20", "lift@10")}
    return result


def render_markdown(res: BenchmarkResult) -> str:
    presets = list(res.summary) or list(PRESETS)
    lines = ["# Benchmark: causal-evidence score vs. clinical-stage targets",
             "",
             f"{len(res.diseases)} diseases; pool = top {res.size} associations ranked without clinical and literature "
             "evidence; positive = target with a clinical-stage or approved drug for the disease. AUC 0.5 = random.",
             "", "## Summary (mean over diseases)", "",
             "| Preset | AUC | P@10 | P@20 | Lift@10 |", "|---|---|---|---|---|"]
    for p, m in sorted(res.summary.items(), key=lambda kv: -kv[1]["auc"]):
        lines.append(f"| {p} | {m['auc']:.3f} | {m['p@10']:.2f} | {m['p@20']:.2f} | {m['lift@10']:.2f} |")
    lines += ["", "## Per disease (AUC)", "", "| Disease | Targets | Clinical | " + " | ".join(presets) + " |",
              "|---|---|---|" + "---|" * len(presets)]
    for d in res.diseases:
        lines.append(f"| {d.disease} | {d.targets} | {d.positives} | "
                     + " | ".join(f"{d.metrics[p]['auc']:.2f}" for p in presets) + " |")
    if res.skipped:
        lines += ["", "Skipped: " + "; ".join(f"{k} ({v})" for k, v in res.skipped.items())]
    if res.data_versions:
        lines += ["", "## Data versions", "", versions_markdown(res.data_versions)]
    return "\n".join(lines) + "\n"
