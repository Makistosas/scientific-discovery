"""Infer the therapeutic direction of modulation (inhibit vs. activate).

Human genetics gives a "natural experiment": if loss-of-function (LoF) variants
increase disease risk, the therapeutic move is to *activate*; if LoF variants
protect (or gain-of-function variants increase risk), the move is to *inhibit*.
Open Targets annotates this as ``directionOnTarget`` (LoF/GoF) and
``directionOnTrait`` (risk/protect). Clinical precedence evidence uses the same
encoding (an approved inhibitor = "LoF protects").
"""

from __future__ import annotations

from collections import defaultdict

from ..models import DirectionAssessment, Modulation

# How much one datasource row is trusted as a direction signal.
DIRECTION_WEIGHT = {
    "gene_burden": 1.0,
    "clinical_precedence": 1.0,
    "gwas_credible_sets": 0.8,
    "eva": 0.6,
    "genomics_england": 0.5,
    "clingen": 0.5,
    "gene2phenotype": 0.5,
    "orphanet": 0.4,
    "uniprot_variants": 0.5,
    "uniprot_literature": 0.4,
    "cancer_gene_census": 0.6,
    "intogen": 0.5,
    "eva_somatic": 0.4,
    "impc": 0.3,
    "expression_atlas": 0.15,
    "pqtl_coloc": 0.9,   # GWAS–protein QTL colocalisation (Mendelian-randomisation-like)
    "eqtl_coloc": 0.6,   # GWAS–expression QTL colocalisation
}
MAX_PER_SOURCE = 2.0  # cap so that 60 ClinVar records do not outvote one burden test

_MAP = {
    ("LOF", "RISK"): Modulation.ACTIVATION,
    ("GOF", "PROTECT"): Modulation.ACTIVATION,
    ("GOF", "RISK"): Modulation.INHIBITION,
    ("LOF", "PROTECT"): Modulation.INHIBITION,
}


def row_direction(row: dict) -> Modulation | None:
    target = (row.get("directionOnTarget") or "").upper()
    trait = (row.get("directionOnTrait") or "").upper()
    return _MAP.get((target, trait))


def expression_direction(row: dict) -> Modulation | None:
    """Correlative only: up-regulated in disease -> weak vote for inhibition."""
    fc = row.get("log2FoldChangeValue")
    if fc is None:
        return None
    if fc > 0:
        return Modulation.INHIBITION
    if fc < 0:
        return Modulation.ACTIVATION
    return None


def infer_direction(rows_by_source: dict[str, list[dict]]) -> DirectionAssessment:
    votes: dict[Modulation, float] = defaultdict(float)
    tallies: dict[str, dict[str, int]] = {}
    for source, rows in rows_by_source.items():
        weight = DIRECTION_WEIGHT.get(source, 0.3)
        per_source: dict[Modulation, float] = defaultdict(float)
        for row in rows:
            mod = expression_direction(row) if source == "expression_atlas" else row_direction(row)
            if mod is None:
                continue
            score = float(row.get("score") or 0.5)
            per_source[mod] += weight * max(score, 0.2)
            key = row.get("label") or f"{row.get('directionOnTarget') or ('↑' if mod is Modulation.INHIBITION else '↓')}→" \
                  f"{row.get('directionOnTrait') or 'expr'}"
            tallies.setdefault(source, {}).setdefault(key, 0)
            tallies[source][key] += 1
        for mod, v in per_source.items():
            votes[mod] += min(v, MAX_PER_SOURCE * weight)

    inh, act = votes[Modulation.INHIBITION], votes[Modulation.ACTIVATION]
    total = inh + act
    rationale = [
        f"{src}: " + ", ".join(f"{k} ×{n}" for k, n in sorted(t.items(), key=lambda kv: -kv[1]))
        for src, t in tallies.items()
    ]
    if total < 0.3:
        return DirectionAssessment(modulation=Modulation.UNKNOWN, confidence=0.0,
                                   votes_inhibition=inh, votes_activation=act, rationale=rationale)
    winner = Modulation.INHIBITION if inh >= act else Modulation.ACTIVATION
    agreement = abs(inh - act) / total
    support = min(1.0, total / 2.0)
    confidence = round(agreement * support, 3)
    if max(inh, act) / total < 0.6:
        winner = Modulation.UNKNOWN
    return DirectionAssessment(modulation=winner, confidence=confidence, votes_inhibition=round(inh, 3),
                               votes_activation=round(act, 3), rationale=rationale)


def direction_pairs_text(rows: list[dict]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.get("directionOnTarget") and r.get("directionOnTrait"):
            counts[f"{r['directionOnTarget']}→{r['directionOnTrait']}"] += 1
    return ", ".join(f"{k} ×{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
