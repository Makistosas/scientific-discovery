"""The discovery pipeline: candidate generation -> enrichment -> scoring -> ranking.

Stage 1 (cheap, one or two Open Targets calls): pull up to a few hundred
target–disease associations with per-datatype scores and target annotation,
compute evidence strength and an attention *proxy*, keep the promising ones.

Stage 2 (enrichment, per candidate, in parallel): Open Targets evidence rows
(direction of effect, reliability), Europe PMC + PubMed co-mention counts,
bioRxiv/medRxiv preprints, patents, ClinicalTrials.gov, ChEMBL bioactivity,
UniProt function, RCSB PDB structures. Then re-score and rank.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Callable, Literal

from pydantic import BaseModel, Field

from .analysis import evidence as ev
from .analysis.direction import infer_direction
from .analysis.scoring import ScoringWeights, clinical_stage_for_disease, score_opportunity
from .config import Settings
from .i18n import normalize_lang
from .models import (Association, AttentionMetrics, DiseaseInfo, DirectionAssessment, Opportunity, TargetInfo,
                     TractabilityMetrics, utcnow)
from .sources import Sources
from .sources.literature import cooccurrence_query, date_clause, disease_terms, gene_terms
from .sources.opentargets import stage_rank

log = logging.getLogger(__name__)
Progress = Callable[[str], None]


class DiscoveryResult(BaseModel):
    mode: Literal["disease", "target", "pair"]
    query: str
    disease: DiseaseInfo | None = None
    target: TargetInfo | None = None
    opportunities: list[Opportunity] = Field(default_factory=list)
    candidates_considered: int = 0
    params: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class DiscoveryPipeline:
    def __init__(self, sources: Sources, settings: Settings | None = None, weights: ScoringWeights | None = None,
                 progress: Progress | None = None):
        self.sources = sources
        self.settings = settings or sources.opentargets.http.settings
        self.weights = weights or ScoringWeights()
        self.lang = normalize_lang(self.settings.language)
        self._progress = progress or (lambda msg: log.info(msg))

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------
    def discover_disease(self, query: str, *, top_k: int = 10, candidates: int = 200, enrich_k: int = 20,
                         include_known: bool = False, protein_coding_only: bool = True) -> DiscoveryResult:
        ot = self.sources.opentargets
        hint = ot.resolve_disease(query)
        self._progress(f"Open Targets: {hint.name} ({hint.id}) — loading {candidates} associations")
        disease, assocs = ot.disease_associations(hint.id, candidates)
        if protein_coding_only:
            assocs = [a for a in assocs if a.biotype in (None, "protein_coding")]
        targets = ot.targets([a.target_id for a in assocs])
        opps = [self._initial(targets[a.target_id], disease, a) for a in assocs if a.target_id in targets]
        selected = self._select(opps, include_known=include_known, enrich_k=enrich_k)
        annotate_locus_neighbors(selected, opps)
        self._enrich_many(selected)
        ranked = sorted(selected, key=lambda o: o.scores.opportunity, reverse=True)
        return DiscoveryResult(mode="disease", query=query, disease=disease, opportunities=ranked[:top_k],
                               candidates_considered=len(opps),
                               params=self._params(top_k, candidates, enrich_k, include_known))

    def discover_target(self, query: str, *, top_k: int = 10, candidates: int = 200, enrich_k: int = 20,
                        include_known: bool = False) -> DiscoveryResult:
        ot = self.sources.opentargets
        tid = ot.resolve_target(query)
        target = ot.target(tid)
        self._progress(f"Open Targets: {target.symbol} ({tid}) — loading {candidates} disease associations")
        _, assocs = ot.target_associations(tid, candidates)
        opps = [self._initial(target, DiseaseInfo(id=a.disease_id, name=a.disease_name), a) for a in assocs]
        selected = self._select(opps, include_known=include_known, enrich_k=enrich_k * 2)
        # resolve subtype hierarchy so clinical stage matching is correct, then reselect
        for o in selected:
            info = self._safe(lambda o=o: ot.disease(o.disease.id), o, "Open Targets disease")
            if info:
                o.disease = info
                o.attention.max_clinical_stage = clinical_stage_for_disease(target, info)[0]
                self._rescore(o, enriched=False)
        # broad categories ("hereditary disease", therapeutic areas) are not actionable indications
        selected = [o for o in selected if not o.disease.is_therapeutic_area
                    and len(o.disease.descendants) <= self.weights.max_descendants]
        selected = self._select(selected, include_known=include_known, enrich_k=enrich_k)
        self._enrich_many(selected)
        ranked = sorted(selected, key=lambda o: o.scores.opportunity, reverse=True)
        return DiscoveryResult(mode="target", query=query, target=target, opportunities=ranked[:top_k],
                               candidates_considered=len(opps),
                               params=self._params(top_k, candidates, enrich_k, include_known))

    def analyze_pair(self, target_query: str, disease_query: str) -> DiscoveryResult:
        ot = self.sources.opentargets
        disease = ot.resolve_disease(disease_query)
        tid = ot.resolve_target(target_query)
        target = ot.target(tid)
        assoc = self.pair_association(disease, target)
        opp = self._initial(target, disease, assoc)
        if assoc.score == 0:
            opp.warnings.append("No Open Targets association between this target and disease.")
        elif assoc.datasource_scores.get("gwas_credible_sets"):
            def neighbours():
                _, rows = ot.disease_associations(disease.id, 200)
                gwas = [r for r in rows if r.datasource_scores.get("gwas_credible_sets") and r.target_id != tid]
                info = ot.targets([r.target_id for r in gwas])
                annotate_locus_neighbors([opp], [Opportunity(target=info[r.target_id], disease=disease, association=r)
                                                 for r in gwas if r.target_id in info])
            self._safe(neighbours, opp, "Open Targets locus neighbours")
        self.enrich(opp)
        return DiscoveryResult(mode="pair", query=f"{target_query} | {disease_query}", disease=disease,
                               target=target, opportunities=[opp], candidates_considered=1)

    def pair_association(self, disease: DiseaseInfo, target: TargetInfo) -> Association:
        q = """query($d: String!, $t: [String!]) { disease(efoId: $d) {
            associatedTargets(Bs: $t, page: {index: 0, size: 1}) {
              rows { score novelty datatypeScores { id score } datasourceScores { id score } } } } }"""
        data = self.sources.opentargets.query(q, {"d": disease.id, "t": [target.ensembl_id]})
        rows = ((data.get("disease") or {}).get("associatedTargets") or {}).get("rows") or []
        r = rows[0] if rows else {}
        return Association(
            target_id=target.ensembl_id, symbol=target.symbol, target_name=target.name, biotype=target.biotype,
            disease_id=disease.id, disease_name=disease.name, score=r.get("score", 0.0), novelty=r.get("novelty"),
            datatype_scores={x["id"]: x["score"] for x in r.get("datatypeScores") or []},
            datasource_scores={x["id"]: x["score"] for x in r.get("datasourceScores") or []},
        )

    # ------------------------------------------------------------------
    # Stage 1
    # ------------------------------------------------------------------
    def _initial(self, target: TargetInfo, disease: DiseaseInfo, assoc: Association) -> Opportunity:
        stage, _ = clinical_stage_for_disease(target, disease)
        opp = Opportunity(
            target=target, disease=disease, association=assoc,
            attention=AttentionMetrics(max_clinical_stage=stage,
                                       ot_literature_score=assoc.datatype_scores.get("literature", 0.0)),
            tractability=TractabilityMetrics(ot_buckets=target.tractability,
                                             high_quality_probes=target.high_quality_probes),
        )
        self._rescore(opp, enriched=False)
        return opp

    def _select(self, opps: list[Opportunity], *, include_known: bool, enrich_k: int) -> list[Opportunity]:
        keep = [o for o in opps if o.scores.evidence >= self.weights.min_evidence]
        if not include_known:
            keep = [o for o in keep if stage_rank(o.attention.max_clinical_stage) < 3.0]
        keep.sort(key=lambda o: o.scores.opportunity, reverse=True)
        return keep[:enrich_k]

    def _rescore(self, opp: Opportunity, *, enriched: bool) -> None:
        opp.scores = score_opportunity(opp.target, opp.disease, opp.association.datatype_scores, opp.attention,
                                       opp.tractability, opp.direction, self.weights, enriched=enriched)

    # ------------------------------------------------------------------
    # Stage 2
    # ------------------------------------------------------------------
    def _enrich_many(self, opps: list[Opportunity]) -> None:
        if not opps:
            return
        self._progress(f"Enriching {len(opps)} candidates (literature, preprints, patents, trials, ChEMBL, UniProt, PDB)")
        workers = max(1, min(self.settings.max_workers, len(opps)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(self.enrich, opps))

    def _safe(self, fn, opp: Opportunity, name: str):
        try:
            return fn()
        except Exception as exc:  # one failing source must not sink the whole run
            log.warning("%s failed for %s: %s", name, opp.key, exc)
            opp.warnings.append(f"{name}: {exc}")
            return None

    def enrich(self, opp: Opportunity) -> Opportunity:
        s, lang, t, d = self.sources, self.lang, opp.target, opp.disease
        items = []

        groups = self._safe(lambda: s.opentargets.pair_evidence(d.id, t.ensembl_id), opp, "Open Targets evidence")
        if groups:
            items += ev.ot_evidence(groups, t, d.id, lang)
            by_source: dict[str, list[dict]] = defaultdict(list)
            for rows in groups.values():
                for r in rows:
                    by_source[r.get("datasourceId") or "?"].append(r)
            opp.direction = infer_direction(by_source)

        genes = gene_terms(t.symbol, t.name)
        diseases = disease_terms(d.name, d.synonyms)
        today = date.today()
        since3 = date(today.year - 3, today.month, 1).isoformat()
        since2 = date(today.year - 2, today.month, 1).isoformat()
        europepmc = s.europepmc

        pubs = self._safe(lambda: europepmc.publications(genes, diseases, page_size=5), opp, "Europe PMC")
        recent = self._safe(lambda: europepmc.count(
            f"{cooccurrence_query(genes, diseases)} AND (SRC:MED OR SRC:PMC){date_clause(since3)}"), opp, "Europe PMC")
        pubmed = self._safe(lambda: s.pubmed.count(s.pubmed.cooccurrence_term(genes, diseases)), opp, "PubMed")
        # attention inherited from parent diseases guards against subtype artefacts
        # (e.g. "young-onset Parkinson disease" vs. the heavily studied "Parkinson disease")
        for parent in d.parents[:2]:
            n = self._safe(lambda parent=parent: europepmc.count(
                f"{cooccurrence_query(genes, [parent])} AND (SRC:MED OR SRC:PMC)"), opp, "Europe PMC")
            if n is not None and n > (opp.attention.parent_publications or -1):
                opp.attention.parent_publications, opp.attention.parent_disease = n, parent
        if pubs is not None:
            items.append(ev.literature_evidence(t.symbol, pubs, recent or 0, pubmed, lang,
                                                parent=opp.attention.parent_disease,
                                                parent_count=opp.attention.parent_publications))
            opp.attention.publications = pubs.hit_count
            opp.attention.publications_recent = recent
        opp.attention.pubmed = pubmed

        pre = self._safe(lambda: europepmc.preprints(genes, diseases, page_size=5), opp, "bioRxiv/medRxiv")
        if pre is not None:
            pre_recent = sum(1 for a in pre.articles if (a.first_publication_date or "") >= since2)
            published = 0
            for art in pre.articles[:3]:
                server = s.biorxiv.server_for(art.publisher, art.doi)
                if server and art.doi:
                    if self._safe(lambda art=art, server=server: s.biorxiv.published_doi(art.doi, server), opp, "bioRxiv"):
                        published += 1
            if pre.hit_count:
                items.append(ev.preprint_evidence(pre, pre_recent, published, lang))
            opp.attention.preprints = pre.hit_count
            opp.attention.preprints_recent = pre_recent

        patents = self._safe(lambda: europepmc.patents(genes, diseases, page_size=3), opp, "Patents")
        if patents is not None:
            if patents.hit_count:
                items.append(ev.patent_evidence(t.symbol, patents, lang))
            opp.attention.patents = patents.hit_count

        term = " OR ".join(f'"{g}"' for g in genes)
        trials = self._safe(lambda: s.trials.search(condition=d.name, term=term, page_size=20), opp,
                            "ClinicalTrials.gov")
        if trials is not None:
            items += ev.trial_evidence(t.symbol, trials, lang)
            opp.attention.trials = trials.total
            opp.attention.trials_stopped = len(trials.stopped)

        if t.uniprot_id:
            acc = t.uniprot_id
            entry = self._safe(lambda: s.uniprot.entry(acc), opp, "UniProt")
            if entry:
                items.append(ev.uniprot_evidence(entry, lang))
                if not t.function and entry.function:
                    t.function = entry.function
            chembl = self._safe(lambda: s.chembl.summary(acc), opp, "ChEMBL")
            if chembl is not None:
                items.append(ev.chembl_evidence(chembl, lang))
                opp.tractability.chembl_target_id = chembl.target_chembl_id
                opp.tractability.potent_compounds = chembl.potent_compounds
                opp.tractability.best_pchembl = chembl.best_pchembl
                opp.tractability.best_compounds = [
                    f"{c['molecule']} ({c.get('name') or '-'}, pChEMBL {c['pchembl']:.1f})" for c in chembl.best_compounds[:5]]
                opp.tractability.mechanisms = sorted({m["action_type"] for m in chembl.mechanisms if m.get("action_type")})
            pdb = self._safe(lambda: s.pdb.summary(acc), opp, "RCSB PDB")
            if pdb is not None:
                items.append(ev.pdb_evidence(pdb, acc, lang))
                opp.tractability.pdb_structures = pdb.total
                opp.tractability.pdb_with_ligand = pdb.with_ligand
                opp.tractability.best_resolution = pdb.best_resolution
        else:
            opp.warnings.append("No UniProt accession: ChEMBL/PDB/UniProt enrichment skipped.")

        items += ev.safety_evidence(t, lang)
        opp.evidence = ev.assign_ids(items)
        opp.enriched = True
        self._rescore(opp, enriched=True)
        self._progress(f"  ✓ {t.symbol} – {d.name}: opportunity {opp.scores.opportunity:.3f}")
        return opp

    def _params(self, top_k: int, candidates: int, enrich_k: int, include_known: bool) -> dict:
        return {"top_k": top_k, "candidates": candidates, "enrich_k": enrich_k, "include_known": include_known,
                "min_evidence": self.weights.min_evidence, "language": self.lang}


def annotate_locus_neighbors(selected: list[Opportunity], pool: list[Opportunity], window: int = 500_000,
                             min_gwas: float = 0.1) -> None:
    """Record other GWAS-associated genes within ±window bp: candidates for the true causal gene at a locus."""
    located = [o for o in pool if o.target.chromosome and o.target.start is not None
               and o.association.datasource_scores.get("gwas_credible_sets", 0.0) >= min_gwas]
    for opp in selected:
        t = opp.target
        if not (t.chromosome and t.start is not None and opp.association.datasource_scores.get("gwas_credible_sets")):
            continue
        hits = []
        for other in located:
            o = other.target
            if o.ensembl_id == t.ensembl_id or o.chromosome != t.chromosome:
                continue
            gap = max(0, max(o.start, t.start) - min(o.end or o.start, t.end or t.start))
            if gap <= window:
                hits.append((other.association.datasource_scores["gwas_credible_sets"], gap, o.symbol))
        opp.locus_neighbors = [f"{sym} (GWAS {score:.2f}, {gap // 1000} kb)"
                               for score, gap, sym in sorted(hits, reverse=True)[:4]]


__all__ = ["DiscoveryPipeline", "DiscoveryResult", "DirectionAssessment", "annotate_locus_neighbors"]
