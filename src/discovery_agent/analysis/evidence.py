"""Turn raw source records into graded :class:`~discovery_agent.models.Evidence` items.

Each item carries a reliability grade (A–D) with an explicit rationale, the
question it answers (causality, direction, tractability, safety, attention,
mechanism), its stance (supports / contradicts / neutral) and references.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from ..i18n import tr
from ..models import (Aspect, AttentionMetrics, Evidence, EvidenceType, LocusSummary, Modulation, Reference, Reliability, Stance,
                      TargetInfo)
from ..sources.chemistry import ChemblSummary, PdbSummary, UniProtEntry
from ..sources.clinical import TrialSearch
from ..sources.literature import Article, SearchResult
from ..sources.opentargets import DATASOURCES, evidence_url, stage_rank
from .direction import direction_pairs_text, infer_direction

CLINVAR_STARS = {
    "practice guideline": 4,
    "reviewed by expert panel": 3,
    "criteria provided, multiple submitters, no conflicts": 2,
    "criteria provided, single submitter": 1,
    "criteria provided, conflicting classifications": 1,
    "criteria provided, conflicting interpretations": 1,
    "no assertion criteria provided": 0,
}
_CURATED_STRONG = {"definitive", "strong"}
_CURATED_MODERATE = {"moderate"}
_CURATED_REFUTED = {"disputed", "refuted", "no known disease relationship"}
_NEGATIVE_STOP = ("negative", "efficacy", "safety", "futility", "toxicity", "adverse")


def _pmid_refs(rows: Iterable[dict], limit: int = 5) -> list[Reference]:
    seen: list[str] = []
    for r in rows:
        for pmid in r.get("literature") or []:
            if pmid and pmid not in seen and str(pmid).isdigit():
                seen.append(pmid)
            if len(seen) >= limit:
                break
        if len(seen) >= limit:
            break
    return [Reference(label=f"PMID:{p}", pmid=p, url=f"https://pubmed.ncbi.nlm.nih.gov/{p}/") for p in seen]


def _best_p(rows: Iterable[dict]) -> float | None:
    best = None
    for r in rows:
        m, e = r.get("pValueMantissa"), r.get("pValueExponent")
        if m is None or e is None:
            continue
        p = float(m) * 10 ** int(e)
        best = p if best is None or p < best else best
    return best


def _majority(rows: list[dict]) -> Modulation | None:
    d = infer_direction({"x": rows})
    return d.modulation if d.modulation is not Modulation.UNKNOWN else None


def _grade(source: str, rows: list[dict], lang: str) -> tuple[Reliability, str, Stance]:
    max_score = max((float(r.get("score") or 0) for r in rows), default=0.0)
    confidences = [str(r.get("confidence") or "").lower() for r in rows if r.get("confidence")]
    if source == "gwas_credible_sets":
        return (Reliability.A if max_score >= 0.5 else Reliability.B), tr(lang, "rel.gwas", s=max_score), Stance.SUPPORTS
    if source == "gene_burden":
        p = _best_p(rows)
        grade = Reliability.A if p is not None and p <= 2.5e-6 else Reliability.B
        return grade, tr(lang, "rel.burden", p=f"{p:.1e}" if p is not None else "?"), Stance.SUPPORTS
    if source == "eva":
        pathogenic = [r for r in rows if any("pathogenic" in s.lower() and "likely" not in s.lower()
                                             for s in r.get("clinicalSignificances") or [])]
        stars = max((CLINVAR_STARS.get(str(r.get("confidence") or "").lower(), 0) for r in pathogenic), default=0)
        if (pathogenic and stars >= 2) or len(pathogenic) >= 3:
            grade = Reliability.A
        elif pathogenic:
            grade = Reliability.B
        else:
            grade = Reliability.C
        return grade, tr(lang, "rel.clinvar", n=len(pathogenic), stars=stars), Stance.SUPPORTS
    if source == "genomics_england":
        best = "green" if "green" in confidences else ("amber" if "amber" in confidences else (confidences[0] if confidences else "?"))
        grade = {"green": Reliability.A, "amber": Reliability.B}.get(best, Reliability.C)
        return grade, tr(lang, "rel.panelapp", c=best), Stance.SUPPORTS
    if source in ("clingen", "gene2phenotype"):
        if any(c in _CURATED_STRONG for c in confidences):
            grade, stance = Reliability.A, Stance.SUPPORTS
        elif any(c in _CURATED_MODERATE for c in confidences):
            grade, stance = Reliability.B, Stance.SUPPORTS
        elif confidences and all(c in _CURATED_REFUTED for c in confidences):
            grade, stance = Reliability.B, Stance.CONTRADICTS
        else:
            grade, stance = Reliability.C, Stance.SUPPORTS
        label = ", ".join(sorted(set(confidences))) or "?"
        return grade, tr(lang, "rel.curated", c=label), stance
    if source in ("orphanet", "uniprot_literature", "uniprot_variants"):
        return Reliability.B, tr(lang, "rel.curated_plain"), Stance.SUPPORTS
    if source == "cancer_gene_census":
        return Reliability.A, tr(lang, "rel.somatic"), Stance.SUPPORTS
    if source in ("intogen", "eva_somatic", "cancer_biomarkers"):
        return Reliability.B, tr(lang, "rel.somatic"), Stance.SUPPORTS
    if source == "clinical_precedence":
        best = max(rows, key=lambda r: stage_rank(r.get("clinicalStage")), default={})
        rank = stage_rank(best.get("clinicalStage"))
        grade = Reliability.A if rank >= 3 else (Reliability.B if rank >= 2 else Reliability.C)
        return grade, tr(lang, "rel.clinical", stage=best.get("clinicalStage") or "?"), Stance.SUPPORTS
    if source == "impc":
        return Reliability.C, tr(lang, "rel.animal"), Stance.SUPPORTS
    if source == "crispr":
        return Reliability.B, tr(lang, "rel.crispr"), Stance.SUPPORTS
    if source in ("reactome", "crispr_screen"):
        return Reliability.C, tr(lang, "rel.pathway"), Stance.SUPPORTS
    if source == "expression_atlas":
        return Reliability.C, tr(lang, "rel.expression"), Stance.SUPPORTS
    return Reliability.D, tr(lang, "rel.textmining"), Stance.SUPPORTS


def _aspect_for(source: str) -> Aspect:
    info = DATASOURCES.get(source)
    if info and info.evidence_type in (EvidenceType.HUMAN_GENETICS, EvidenceType.SOMATIC, EvidenceType.CLINICAL):
        return Aspect.CAUSALITY
    if source == "europepmc":
        return Aspect.ATTENTION
    return Aspect.CAUSALITY if source in ("impc", "crispr") else Aspect.MECHANISM


def ot_evidence(groups: dict[str, list[dict]], target: TargetInfo, disease_id: str, lang: str) -> list[Evidence]:
    """One graded Evidence item per Open Targets datasource with records for the pair."""
    by_source: dict[str, list[dict]] = defaultdict(list)
    for rows in groups.values():
        for r in rows:
            by_source[r.get("datasourceId") or "unknown"].append(r)

    items: list[Evidence] = []
    for source, rows in by_source.items():
        if source == "clinical_precedence":
            items.extend(_clinical_items(rows, target, disease_id, lang))
            continue
        info = DATASOURCES.get(source)
        label = info.label if info else source
        etype = info.evidence_type if info else EvidenceType.LITERATURE
        reliability, rationale, stance = _grade(source, rows, lang)
        max_score = max((float(r.get("score") or 0) for r in rows), default=0.0)
        summary = tr(lang, "ev.ot.rows", label=label, n=len(rows), score=max_score)
        pairs = direction_pairs_text(rows)
        if pairs:
            summary += tr(lang, "ev.ot.direction", pairs=pairs)
        if source == "gene_burden":
            p = _best_p(rows)
            if p is not None:
                summary += tr(lang, "ev.ot.pvalue", p=p)
        if source == "impc":
            phen = sorted({p["label"] for r in rows for p in r.get("diseaseModelAssociatedModelPhenotypes") or []
                           if p.get("label")})
            if phen:
                summary += tr(lang, "ev.ot.phenotypes", ph=", ".join(phen[:6]))
        if source == "expression_atlas":
            up = sum(1 for r in rows if (r.get("log2FoldChangeValue") or 0) > 0)
            down = sum(1 for r in rows if (r.get("log2FoldChangeValue") or 0) < 0)
            summary += tr(lang, "ev.ot.expression", up=up, down=down)
        refs = _pmid_refs(rows)
        refs.insert(0, Reference(label="Open Targets", url=evidence_url(target.ensembl_id, disease_id)))
        if source == "eva":
            for r in rows[:3]:
                if r.get("studyId"):
                    refs.append(Reference(label=r["studyId"],
                                          url=f"https://www.ncbi.nlm.nih.gov/clinvar/{r['studyId']}/"))
        years = [int(r["publicationYear"]) for r in rows if r.get("publicationYear")]
        items.append(Evidence(
            source=f"Open Targets · {label}", type=etype, aspect=_aspect_for(source), summary=summary,
            stance=Stance.NEUTRAL if source == "europepmc" else stance,
            reliability=reliability, reliability_rationale=rationale, strength=min(1.0, max_score),
            effect_direction=_majority(rows) if source != "europepmc" else None,
            year=max(years) if years else None, references=refs,
        ))
    return items


def _clinical_items(rows: list[dict], target: TargetInfo, disease_id: str, lang: str) -> list[Evidence]:
    items = []
    reliability, rationale, _ = _grade("clinical_precedence", rows, lang)
    drugs = sorted({(r.get("drug") or {}).get("name") for r in rows if (r.get("drug") or {}).get("name")})
    summary = tr(lang, "ev.ot.rows", label=DATASOURCES["clinical_precedence"].label, n=len(rows),
                 score=max((float(r.get("score") or 0) for r in rows), default=0.0))
    if drugs:
        summary += tr(lang, "ev.ot.drugs", drugs=", ".join(drugs[:8]))
    refs = [Reference(label="Open Targets", url=evidence_url(target.ensembl_id, disease_id))]
    for r in rows[:5]:
        rid = str(r.get("clinicalReportId") or "")
        if rid.lower().startswith("nct"):
            refs.append(Reference(label=rid.upper(), url=f"https://clinicaltrials.gov/study/{rid.upper()}"))
    items.append(Evidence(
        source="Open Targets · " + DATASOURCES["clinical_precedence"].label, type=EvidenceType.CLINICAL,
        aspect=Aspect.CAUSALITY, summary=summary, stance=Stance.SUPPORTS, reliability=reliability,
        reliability_rationale=rationale, strength=max((float(r.get("score") or 0) for r in rows), default=0.0),
        effect_direction=_majority(rows), references=refs,
    ))
    stopped = [r for r in rows if r.get("trialWhyStopped") or r.get("trialStopReasonCategories")]
    negative = [r for r in stopped if any(k in " ".join([str(r.get("trialWhyStopped") or "")] +
                                                        list(r.get("trialStopReasonCategories") or [])).lower()
                                          for k in _NEGATIVE_STOP)]
    if negative:
        listing = "; ".join(
            f"{(r.get('drug') or {}).get('name', '?')} ({', '.join(r.get('trialStopReasonCategories') or []) or r.get('trialWhyStopped')})"
            for r in negative[:4])
        items.append(Evidence(
            source="Open Targets · clinical stop reasons", type=EvidenceType.CLINICAL, aspect=Aspect.CAUSALITY,
            summary=tr(lang, "ev.stopped", symbol=target.symbol, items=listing), stance=Stance.CONTRADICTS,
            reliability=Reliability.B, reliability_rationale=tr(lang, "rel.trial_stop"), strength=0.6,
            references=[Reference(label=str(r.get("clinicalReportId")).upper(),
                                  url=f"https://clinicaltrials.gov/study/{str(r.get('clinicalReportId')).upper()}")
                        for r in negative[:4] if str(r.get("clinicalReportId") or "").lower().startswith("nct")],
        ))
    return items


def _article_refs(articles: list[Article], limit: int = 3) -> list[Reference]:
    return [Reference(label=(a.title[:90] + ("…" if len(a.title) > 90 else "")) or a.id, url=a.url,
                      pmid=a.pmid, doi=a.doi) for a in articles[:limit]]


def literature_evidence(symbol: str, pubs: SearchResult, recent: int, pubmed: int | None, lang: str, *,
                        parent: str | None = None, parent_count: int | None = None,
                        attention: AttentionMetrics | None = None) -> Evidence:
    summary = tr(lang, "ev.lit", n=pubs.hit_count, symbol=symbol, recent=recent,
                 pubmed=pubmed if pubmed is not None else "?")
    if parent and parent_count is not None:
        summary += tr(lang, "ev.lit.parent", parent=parent, n=parent_count)
    a = attention
    if a is not None and a.relative_share is not None:
        summary += tr(lang, "ev.lit.relative", share=a.relative_share * 100, disease=a.disease_publications,
                      gene=a.gene_publications if a.gene_publications is not None else "?", symbol=symbol)
    if a is not None and a.trend:
        summary += tr(lang, f"ev.lit.trend.{a.trend}", prior=a.publications_prior, recent=recent,
                      ratio=a.trend_ratio)
    return Evidence(
        source="Europe PMC / PubMed", type=EvidenceType.LITERATURE, aspect=Aspect.ATTENTION,
        summary=summary,
        stance=Stance.NEUTRAL, reliability=Reliability.D, reliability_rationale=tr(lang, "rel.textmining"),
        strength=0.0, references=_article_refs(pubs.articles),
        year=max((a.year for a in pubs.articles if a.year), default=None),
    )


def preprint_evidence(preprints: SearchResult, recent: int, published: int, lang: str) -> Evidence:
    rationale = tr(lang, "rel.preprint_pub") if published else tr(lang, "rel.preprint")
    return Evidence(
        source="bioRxiv / medRxiv (via Europe PMC)", type=EvidenceType.PREPRINT, aspect=Aspect.ATTENTION,
        summary=tr(lang, "ev.preprints", n=preprints.hit_count, recent=recent, published=published),
        stance=Stance.NEUTRAL, reliability=Reliability.C if published else Reliability.D,
        reliability_rationale=rationale, peer_reviewed=False, references=_article_refs(preprints.articles),
        year=max((a.year for a in preprints.articles if a.year), default=None),
    )


def patent_evidence(symbol: str, patents: SearchResult, lang: str) -> Evidence:
    return Evidence(
        source="Patents (Europe PMC / EPO)", type=EvidenceType.PATENT, aspect=Aspect.ATTENTION,
        summary=tr(lang, "ev.patents", n=patents.hit_count, symbol=symbol), stance=Stance.NEUTRAL,
        reliability=Reliability.D, reliability_rationale=tr(lang, "rel.patent"), peer_reviewed=False,
        references=_article_refs(patents.articles),
    )


def trial_evidence(symbol: str, trials: TrialSearch, lang: str) -> list[Evidence]:
    items = [Evidence(
        source="ClinicalTrials.gov", type=EvidenceType.TRIAL_REGISTRY, aspect=Aspect.ATTENTION,
        summary=tr(lang, "ev.trials", n=trials.total, symbol=symbol, stopped=len(trials.stopped)),
        stance=Stance.NEUTRAL, reliability=Reliability.C, reliability_rationale=tr(lang, "rel.registry"),
        references=[Reference(label=f"{t.nct_id}: {t.title[:70]}", url=t.url) for t in trials.trials[:3]],
    )]
    for t in trials.stopped:
        why = (t.why_stopped or "").strip()
        negative = any(k in why.lower() for k in _NEGATIVE_STOP)
        if not negative:
            continue
        items.append(Evidence(
            source="ClinicalTrials.gov", type=EvidenceType.TRIAL_REGISTRY, aspect=Aspect.CAUSALITY,
            summary=tr(lang, "ev.trial_stopped", nct=t.nct_id, status=t.status, why=why or "?"),
            stance=Stance.CONTRADICTS, reliability=Reliability.B, reliability_rationale=tr(lang, "rel.trial_stop"),
            strength=0.5, references=[Reference(label=t.nct_id, url=t.url)],
        ))
    return items


def chembl_evidence(chembl: ChemblSummary, lang: str) -> Evidence:
    if not chembl.target_chembl_id or not chembl.potent_compounds:
        return Evidence(
            source="ChEMBL", type=EvidenceType.CHEMISTRY, aspect=Aspect.TRACTABILITY,
            summary=tr(lang, "ev.chembl.none"), stance=Stance.NEUTRAL, reliability=Reliability.B,
            reliability_rationale=tr(lang, "rel.chembl"), strength=0.0,
            references=[Reference(label=chembl.target_chembl_id, url=chembl.url)] if chembl.url else [],
        )
    summary = tr(lang, "ev.chembl", potent=chembl.potent_compounds, very=chembl.very_potent_compounds,
                 best=f"{chembl.best_pchembl:.1f}" if chembl.best_pchembl else "?")
    mech = sorted({f"{m['action_type']}" for m in chembl.mechanisms if m.get("action_type")})
    if mech:
        summary += tr(lang, "ev.chembl.mech", mech=", ".join(mech))
    approved = any((m.get("max_phase") or 0) >= 4 for m in chembl.mechanisms)
    refs = [Reference(label=chembl.target_chembl_id, url=chembl.url)]
    refs += [Reference(label=f"{c['molecule']} ({c.get('name') or '-'}, pChEMBL {c['pchembl']:.1f})",
                       url=f"https://www.ebi.ac.uk/chembl/explore/compound/{c['molecule']}")
             for c in chembl.best_compounds[:3]]
    return Evidence(
        source="ChEMBL", type=EvidenceType.CHEMISTRY, aspect=Aspect.TRACTABILITY, summary=summary,
        stance=Stance.SUPPORTS, reliability=Reliability.A if approved else Reliability.B,
        reliability_rationale=tr(lang, "rel.chembl"),
        strength=min(1.0, chembl.potent_compounds / 100), references=refs,
    )


def pdb_evidence(pdb: PdbSummary, accession: str, lang: str) -> Evidence:
    if not pdb.total:
        return Evidence(
            source="RCSB PDB", type=EvidenceType.STRUCTURE, aspect=Aspect.TRACTABILITY,
            summary=tr(lang, "ev.pdb.none"), stance=Stance.NEUTRAL, reliability=Reliability.A,
            reliability_rationale=tr(lang, "rel.structure"),
            references=[Reference(label=f"AlphaFold {accession}", url=f"https://alphafold.ebi.ac.uk/entry/{accession}")],
        )
    return Evidence(
        source="RCSB PDB", type=EvidenceType.STRUCTURE, aspect=Aspect.TRACTABILITY,
        summary=tr(lang, "ev.pdb", n=pdb.total, lig=pdb.with_ligand,
                   res=f"{pdb.best_resolution:.2f}" if pdb.best_resolution else "?"),
        stance=Stance.SUPPORTS, reliability=Reliability.A, reliability_rationale=tr(lang, "rel.structure"),
        strength=min(1.0, 0.3 + 0.7 * (pdb.with_ligand > 0)),
        references=[Reference(label=f"PDB {e['id']}: {(e.get('title') or '')[:60]}",
                              url=f"https://www.rcsb.org/structure/{e['id']}") for e in pdb.entries[:3]],
    )


def uniprot_evidence(entry: UniProtEntry, lang: str) -> Evidence:
    func = (entry.function or "").strip()
    summary = tr(lang, "ev.uniprot", name=entry.protein_name or entry.accession,
                 loc=", ".join(entry.subcellular_locations[:4]) or "?", func=func)
    if entry.diseases:
        summary += tr(lang, "ev.uniprot.disease", d="; ".join(entry.diseases[:4]))
    return Evidence(
        source="UniProt", type=EvidenceType.PROTEIN, aspect=Aspect.MECHANISM, summary=summary,
        stance=Stance.NEUTRAL, reliability=Reliability.B, reliability_rationale=tr(lang, "rel.uniprot"),
        references=[Reference(label=f"UniProt {entry.accession}", url=entry.url)],
    )


def safety_evidence(target: TargetInfo, lang: str) -> list[Evidence]:
    items = []
    base_ref = [Reference(label="Open Targets", url=f"https://platform.opentargets.org/target/{target.ensembl_id}")]
    if target.safety_events:
        items.append(Evidence(
            source="Open Targets · safety", type=EvidenceType.SAFETY, aspect=Aspect.SAFETY,
            summary=tr(lang, "ev.safety", events="; ".join(target.safety_events[:8])), stance=Stance.NEUTRAL,
            reliability=Reliability.B, reliability_rationale=tr(lang, "rel.safety"), references=base_ref))
    if target.lof_oe_upper is not None and target.lof_oe_upper < 0.35:
        items.append(Evidence(
            source="gnomAD constraint (via Open Targets)", type=EvidenceType.SAFETY, aspect=Aspect.SAFETY,
            summary=tr(lang, "ev.constraint", v=target.lof_oe_upper), stance=Stance.NEUTRAL,
            reliability=Reliability.B, reliability_rationale=tr(lang, "rel.safety"), references=base_ref))
    if target.is_essential:
        items.append(Evidence(
            source="DepMap (via Open Targets)", type=EvidenceType.SAFETY, aspect=Aspect.SAFETY,
            summary=tr(lang, "ev.essential"), stance=Stance.NEUTRAL, reliability=Reliability.B,
            reliability_rationale=tr(lang, "rel.safety"), references=base_ref))
    if target.high_quality_probes:
        items.append(Evidence(
            source="Open Targets · chemical probes", type=EvidenceType.CHEMISTRY, aspect=Aspect.TRACTABILITY,
            summary=tr(lang, "ev.probes", n=target.high_quality_probes, ids=", ".join(target.chemical_probes[:4])),
            stance=Stance.SUPPORTS, reliability=Reliability.B, reliability_rationale=tr(lang, "rel.chembl"),
            references=base_ref))
    return items


def locus_evidence(summary: LocusSummary, loci: list[dict], target: TargetInfo, disease_id: str,
                   lang: str) -> Evidence:
    """GWAS locus assignment (L2G, competing genes) and QTL colocalisation direction as one evidence item."""
    parts = [tr(lang, "ev.locus", n=summary.loci, symbol=target.symbol,
                l2g=f"{summary.target_l2g:.2f}" if summary.target_l2g is not None else "?")]
    if summary.competitors:
        parts.append(tr(lang, "ev.locus.competitors", genes=", ".join(summary.competitors)))
    if summary.coloc:
        parts.append(tr(lang, "ev.locus.coloc", items="; ".join(
            f"{v['type']} {v['tissue']} H4 {v['strength']:.2f} "
            f"({tr(lang, 'ev.locus.up' if v['sign'] > 0 else 'ev.locus.down')})" for v in summary.coloc[:4])))
    elif summary.splice_tissues:
        parts.append(tr(lang, "ev.locus.splice", tissues=", ".join(summary.splice_tissues)))
    else:
        parts.append(tr(lang, "ev.locus.no_coloc"))
    if summary.other_coloc_genes:
        parts.append(tr(lang, "ev.locus.other", genes=", ".join(summary.other_coloc_genes)))
    votes = [v for v in summary.coloc if v["type"] in ("pqtl", "scpqtl", "eqtl", "sceqtl")]
    strong = any(v["strength"] >= 0.9 for v in votes)
    reliability = Reliability.A if strong and not summary.opposite_signs and not summary.conflict else Reliability.B
    direction = None
    if votes and not summary.opposite_signs:
        direction = Modulation.INHIBITION if votes[0]["sign"] > 0 else Modulation.ACTIVATION
    refs = [Reference(label=f"Open Targets credible set {l['study_id'] or ''}".strip(),
                      url=f"https://platform.opentargets.org/credible-set/{l['locus_id']}")
            for l in loci[:3] if l.get("locus_id")]
    refs += _pmid_refs([{"literature": l.get("literature")} for l in loci], limit=3)
    return Evidence(
        source="Open Targets · GWAS locus-to-gene & QTL colocalisation", type=EvidenceType.HUMAN_GENETICS,
        aspect=Aspect.DIRECTION, summary=" ".join(parts),
        stance=Stance.SUPPORTS if summary.supported else Stance.NEUTRAL, reliability=reliability,
        reliability_rationale=tr(lang, "rel.coloc"), strength=summary.target_l2g or 0.0,
        effect_direction=direction, references=refs,
    )


def assign_ids(items: list[Evidence]) -> list[Evidence]:
    order = {Aspect.CAUSALITY: 0, Aspect.DIRECTION: 1, Aspect.MECHANISM: 2, Aspect.TRACTABILITY: 3,
             Aspect.SAFETY: 4, Aspect.ATTENTION: 5}
    rel = {"A": 0, "B": 1, "C": 2, "D": 3}
    items = sorted(items, key=lambda e: (order.get(e.aspect, 9), rel[e.reliability.value], -e.strength))
    for i, e in enumerate(items, 1):
        e.id = f"E{i}"
    return items
