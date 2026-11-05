"""GWAS locus interpretation: which gene is causal, and which way does expression push disease risk?

For every GWAS credible set linking a target to a disease, Open Targets provides

* locus-to-gene (L2G) scores for *every* gene at the locus -> competing candidate genes;
* colocalisations with expression / protein / splicing QTLs, with ``betaRatioSignAverage``: the sign of
  (GWAS effect / QTL effect) for the shared variants. ``+1`` means the allele that raises disease risk also
  raises the gene's expression (so *inhibition* would be therapeutic); ``-1`` means it lowers expression
  (so *activation* / restoring the gene would be therapeutic). This is the logic of Mendelian randomisation.

Splicing QTLs support the gene assignment but are not used for direction (their sign is not interpretable).
"""

from __future__ import annotations

from ..models import LocusSummary, Modulation

DIRECTION_QTL_TYPES = {"eqtl": "eqtl_coloc", "sceqtl": "eqtl_coloc", "pqtl": "pqtl_coloc", "scpqtl": "pqtl_coloc"}
MIN_H4 = 0.8
MIN_CLPP = 0.01


def _strength(c: dict) -> float:
    if c.get("h4") is not None:
        return float(c["h4"])
    return min(1.0, float(c.get("clpp") or 0.0) * 5)


def _is_coloc(c: dict) -> bool:
    return (c.get("h4") or 0.0) >= MIN_H4 or (c.get("h4") is None and (c.get("clpp") or 0.0) >= MIN_CLPP)


def assess_loci(loci: list[dict], target_id: str, symbol: str) -> tuple[LocusSummary, dict[str, list[dict]]]:
    """Summarise the loci and return synthetic direction rows (keyed by pseudo-datasource) for
    :func:`discovery_agent.analysis.direction.infer_direction`."""
    def is_target(gene_id, sym) -> bool:
        return gene_id == target_id or (sym and sym == symbol)

    target_l2g: list[float] = []
    competitors: dict[str, tuple[float, float]] = {}
    votes: dict[tuple[str, str], dict] = {}
    splice: dict[str, float] = {}
    other: dict[str, float] = {}
    for locus in loci:
        own = max((x["score"] for x in locus["l2g"] if is_target(x.get("gene_id"), x.get("symbol"))), default=0.0)
        target_l2g.append(own)
        for x in locus["l2g"]:
            if is_target(x.get("gene_id"), x.get("symbol")) or not x.get("symbol"):
                continue
            if x["score"] >= max(0.2, 0.8 * own):
                prev = competitors.get(x["symbol"])
                if prev is None or x["score"] > prev[0]:
                    competitors[x["symbol"]] = (x["score"], own)
        for c in locus["coloc"]:
            if not _is_coloc(c):
                continue
            if is_target(c.get("gene_id"), c.get("symbol")):
                qtl = (c.get("type") or "").lower()
                if qtl in DIRECTION_QTL_TYPES and c.get("sign"):
                    key = (qtl, c.get("tissue") or "?")
                    if key not in votes or _strength(c) > votes[key]["strength"]:
                        votes[key] = {"type": qtl, "tissue": c.get("tissue") or "?", "strength": round(_strength(c), 3),
                                      "sign": 1 if c["sign"] > 0 else -1, "study_id": c.get("study_id")}
                elif qtl.endswith("sqtl"):
                    splice[c.get("tissue") or "?"] = max(splice.get(c.get("tissue") or "?", 0.0), _strength(c))
            elif c.get("symbol") and (c.get("h4") or 0.0) >= MIN_H4:
                other[c["symbol"]] = max(other.get(c["symbol"], 0.0), c["h4"])

    vote_list = sorted(votes.values(), key=lambda v: -v["strength"])
    signs = {v["sign"] for v in vote_list}
    has_target_coloc = bool(vote_list or splice)
    summary = LocusSummary(
        loci=len(loci),
        target_l2g=round(max(target_l2g), 3) if target_l2g else None,
        competitors=[f"{s} (L2G {sc:.2f} vs {own:.2f})" for s, (sc, own) in
                     sorted(competitors.items(), key=lambda kv: -kv[1][0])[:4]],
        coloc=vote_list[:8],
        splice_tissues=[t for t, _ in sorted(splice.items(), key=lambda kv: -kv[1])[:4]],
        other_coloc_genes=[f"{s} (H4 {h:.2f})" for s, h in sorted(other.items(), key=lambda kv: -kv[1])[:4]],
        opposite_signs=len(signs) > 1,
    )
    summary.conflict = bool(summary.competitors) or (bool(other) and not has_target_coloc)
    summary.supported = has_target_coloc and not summary.competitors

    rows: dict[str, list[dict]] = {}
    for v in vote_list:
        rows.setdefault(DIRECTION_QTL_TYPES[v["type"]], []).append({
            # higher expression goes with higher risk -> behaves like gain of function -> inhibit
            "directionOnTarget": "GoF" if v["sign"] > 0 else "LoF", "directionOnTrait": "risk",
            "score": v["strength"], "label": f"{v['type']} {v['tissue']}: risk allele {'↑' if v['sign'] > 0 else '↓'} expression"})
    return summary, rows


def coloc_modulation(v: dict) -> Modulation:
    return Modulation.INHIBITION if v["sign"] > 0 else Modulation.ACTIVATION
