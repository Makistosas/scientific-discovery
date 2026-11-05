"""Open Targets Platform GraphQL client.

Open Targets integrates 20+ evidence sources (GWAS, ClinVar, gene burden,
ClinGen, Gene2Phenotype, Orphanet, Cancer Gene Census, IntOGen, ChEMBL clinical
precedence, IMPC mouse models, Reactome, CRISPR screens, Expression Atlas,
Europe PMC text-mining, …) into target–disease association scores and exposes
them through one GraphQL API. It is the backbone of candidate generation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from ..http import HttpClient, HttpError
from ..models import Association, DiseaseInfo, DrugInfo, EvidenceType, TargetInfo

OT_GRAPHQL = "https://api.platform.opentargets.org/api/v4/graphql"
OT_WEB = "https://platform.opentargets.org"


@dataclass(frozen=True)
class DatasourceInfo:
    datatype: str
    evidence_type: EvidenceType
    label: str


DATASOURCES: dict[str, DatasourceInfo] = {
    "gwas_credible_sets": DatasourceInfo("genetic_association", EvidenceType.HUMAN_GENETICS, "GWAS fine-mapped credible sets (locus-to-gene)"),
    "gene_burden": DatasourceInfo("genetic_association", EvidenceType.HUMAN_GENETICS, "Rare-variant gene burden analyses"),
    "eva": DatasourceInfo("genetic_association", EvidenceType.HUMAN_GENETICS, "ClinVar germline variants (EVA)"),
    "genomics_england": DatasourceInfo("genetic_association", EvidenceType.HUMAN_GENETICS, "Genomics England PanelApp"),
    "uniprot_variants": DatasourceInfo("genetic_association", EvidenceType.HUMAN_GENETICS, "UniProt curated disease variants"),
    "clingen": DatasourceInfo("genetic_literature", EvidenceType.HUMAN_GENETICS, "ClinGen gene–disease validity"),
    "gene2phenotype": DatasourceInfo("genetic_literature", EvidenceType.HUMAN_GENETICS, "Gene2Phenotype"),
    "orphanet": DatasourceInfo("genetic_literature", EvidenceType.HUMAN_GENETICS, "Orphanet"),
    "uniprot_literature": DatasourceInfo("genetic_literature", EvidenceType.HUMAN_GENETICS, "UniProt literature curation"),
    "cancer_gene_census": DatasourceInfo("somatic_mutation", EvidenceType.SOMATIC, "COSMIC Cancer Gene Census"),
    "intogen": DatasourceInfo("somatic_mutation", EvidenceType.SOMATIC, "IntOGen cancer drivers"),
    "eva_somatic": DatasourceInfo("somatic_mutation", EvidenceType.SOMATIC, "ClinVar somatic variants"),
    "cancer_biomarkers": DatasourceInfo("somatic_mutation", EvidenceType.SOMATIC, "Cancer Genome Interpreter biomarkers"),
    "clinical_precedence": DatasourceInfo("clinical", EvidenceType.CLINICAL, "Clinical precedence (ChEMBL drugs & trials)"),
    "impc": DatasourceInfo("animal_model", EvidenceType.ANIMAL_MODEL, "IMPC mouse knockout phenotypes"),
    "reactome": DatasourceInfo("affected_pathway", EvidenceType.FUNCTIONAL, "Reactome pathways"),
    "crispr": DatasourceInfo("affected_pathway", EvidenceType.FUNCTIONAL, "Project Score CRISPR dependencies"),
    "crispr_screen": DatasourceInfo("affected_pathway", EvidenceType.FUNCTIONAL, "CRISPR screens (BioGRID ORCS)"),
    "expression_atlas": DatasourceInfo("rna_expression", EvidenceType.EXPRESSION, "Expression Atlas differential expression"),
    "europepmc": DatasourceInfo("literature", EvidenceType.LITERATURE, "Europe PMC text-mining co-mentions"),
}

EVIDENCE_GROUPS: dict[str, tuple[list[str], int]] = {
    "genetic": (["gwas_credible_sets", "gene_burden", "eva", "genomics_england", "uniprot_variants",
                 "clingen", "gene2phenotype", "orphanet", "uniprot_literature"], 60),
    "somatic": (["cancer_gene_census", "intogen", "eva_somatic", "cancer_biomarkers"], 25),
    "clinical": (["clinical_precedence"], 40),
    "animal": (["impc"], 10),
    "pathway": (["reactome", "crispr", "crispr_screen"], 15),
    "expression": (["expression_atlas"], 10),
    "literature": (["europepmc"], 10),
}

CLINICAL_STAGE_RANK = {
    "APPROVAL": 4.0, "PHASE_4": 4.0, "PHASE_3": 3.0, "PHASE_2_3": 2.5, "PHASE_2": 2.0,
    "PHASE_1_2": 1.5, "PHASE_1": 1.0, "EARLY_PHASE_1": 0.5, "PRECLINICAL": 0.25, "UNKNOWN": 0.5,
}

_DISEASE_ID = re.compile(r"^(EFO|MONDO|Orphanet|HP|DOID|OTAR|GO|NCIT|MP)_\w+$", re.I)
_ENSEMBL_ID = re.compile(r"^ENSG\d{11}$", re.I)

_EVIDENCE_FIELDS = """
  id datasourceId datatypeId score directionOnTrait directionOnTarget targetModulation
  confidence clinicalSignificances clinicalStage clinicalReportId trialWhyStopped trialStopReasonCategories
  literature publicationYear studyId studySampleSize oddsRatio beta pValueMantissa pValueExponent
  variantRsId cohortShortName diseaseFromSource targetFromSource contrast log2FoldChangeValue
  biologicalModelAllelicComposition
  disease { id name }
  drug { id name mechanismsOfAction { rows { actionType } } }
  diseaseModelAssociatedModelPhenotypes { label }
"""

_DISEASE_FIELDS = """
  id name description descendants isTherapeuticArea
  therapeuticAreas { name }
  synonyms { relation terms }
  parents { id name isTherapeuticArea }
"""

_TARGET_FIELDS = """
  id approvedSymbol approvedName biotype functionDescriptions
  genomicLocation { chromosome start end }
  targetClass { label level }
  subcellularLocations { location }
  tractability { label modality value }
  safetyLiabilities { event datasource }
  geneticConstraint { constraintType oeUpper }
  proteinIds { id source }
  chemicalProbes { id isHighQuality mechanismOfAction }
  isEssential
  prioritisation { items { key value } }
  drugAndClinicalCandidates {
    count
    rows { maxClinicalStage drug { id name mechanismsOfAction { rows { actionType } } } diseases { disease { id name } } }
  }
  associatedDiseases(page: {index: 0, size: 1}) { count }
"""


def stage_rank(stage: str | None) -> float:
    return CLINICAL_STAGE_RANK.get((stage or "").upper(), 0.0)


def target_url(ensembl_id: str) -> str:
    return f"{OT_WEB}/target/{ensembl_id}"


def evidence_url(ensembl_id: str, disease_id: str) -> str:
    return f"{OT_WEB}/evidence/{ensembl_id}/{disease_id}"


class OpenTargetsError(RuntimeError):
    pass


class OpenTargetsClient:
    def __init__(self, http: HttpClient, url: str = OT_GRAPHQL):
        self.http = http
        self.url = url

    # -- low level ----------------------------------------------------------
    def query(self, query: str, variables: dict | None = None, *, ttl_hours: float | None = None) -> dict:
        try:
            data = self.http.post_json(self.url, {"query": query, "variables": variables or {}}, ttl_hours=ttl_hours)
        except HttpError as exc:
            raise OpenTargetsError(str(exc)) from exc
        if data.get("errors"):
            raise OpenTargetsError("; ".join(e.get("message", "?") for e in data["errors"]))
        return data.get("data") or {}

    # -- entity resolution --------------------------------------------------
    def search(self, text: str, entities: Iterable[str] = ("target", "disease"), size: int = 5) -> list[dict]:
        q = """query($q: String!, $e: [String!], $size: Int!) {
          search(queryString: $q, entityNames: $e, page: {index: 0, size: $size}) {
            hits { id name entity description }
          }
        }"""
        data = self.query(q, {"q": text, "e": list(entities), "size": size})
        return data.get("search", {}).get("hits", [])

    def resolve_disease(self, text: str) -> DiseaseInfo:
        text = text.strip()
        disease_id = text.replace(":", "_") if _DISEASE_ID.match(text.replace(":", "_")) else None
        if disease_id is None:
            hits = self.search(text, ["disease"], size=10)
            if not hits:
                raise OpenTargetsError(f"Disease not found in Open Targets: {text!r}")
            exact = [h for h in hits if h["name"].lower() == text.lower()]  # else trust the search ranking
            disease_id = (exact or hits)[0]["id"]
        info = self.disease(disease_id)
        if info is None:
            raise OpenTargetsError(f"Unknown disease id: {disease_id}")
        return info

    def resolve_target(self, text: str) -> str:
        text = text.strip()
        if _ENSEMBL_ID.match(text):
            return text.upper()
        q = """query($q: [String!]!) { mapIds(queryTerms: $q, entityNames: ["target"]) {
            mappings { term hits { id name entity } } } }"""
        data = self.query(q, {"q": [text]})
        for mapping in data.get("mapIds", {}).get("mappings", []):
            for hit in mapping.get("hits") or []:
                if hit.get("entity") == "target":
                    return hit["id"]
        hits = self.search(text, ["target"], size=1)
        if not hits:
            raise OpenTargetsError(f"Target not found in Open Targets: {text!r}")
        return hits[0]["id"]

    def disease(self, disease_id: str) -> DiseaseInfo | None:
        q = "query($id: String!) { disease(efoId: $id) { " + _DISEASE_FIELDS + " } }"
        d = self.query(q, {"id": disease_id}).get("disease")
        return parse_disease(d) if d else None

    # -- associations -------------------------------------------------------
    def disease_associations(self, disease_id: str, size: int = 200,
                             datasources: list[dict] | None = None) -> tuple[DiseaseInfo, list[Association]]:
        """Associated targets ranked by score; `datasources` overrides per-source weights
        (e.g. weight 0 for clinical evidence to rank by causal evidence only)."""
        q = """query($id: String!, $size: Int!, $ds: [DatasourceSettingsInput!]) { disease(efoId: $id) {
            """ + _DISEASE_FIELDS + """
            associatedTargets(datasources: $ds, page: {index: 0, size: $size}) {
              count
              rows { score novelty datatypeScores { id score } datasourceScores { id score }
                     target { id approvedSymbol approvedName biotype } }
            } } }"""
        d = self.query(q, {"id": disease_id, "size": size, "ds": datasources}).get("disease")
        if not d:
            raise OpenTargetsError(f"Unknown disease id: {disease_id}")
        disease = parse_disease(d)
        rows = []
        for r in d["associatedTargets"]["rows"]:
            t = r["target"]
            rows.append(Association(
                target_id=t["id"], symbol=t["approvedSymbol"], target_name=t.get("approvedName") or "",
                biotype=t.get("biotype"), disease_id=disease.id, disease_name=disease.name,
                score=r["score"], novelty=r.get("novelty"),
                datatype_scores={x["id"]: x["score"] for x in r.get("datatypeScores") or []},
                datasource_scores={x["id"]: x["score"] for x in r.get("datasourceScores") or []},
            ))
        return disease, rows

    def clinical_target_ids(self, disease_id: str, size: int = 3000) -> set[str]:
        """Targets with clinical-precedence evidence (a clinical-stage or approved drug) for the disease."""
        q = """query($id: String!, $size: Int!) { disease(efoId: $id) {
            associatedTargets(datasources: [{id: "clinical_precedence", weight: 1, propagate: true, required: true}],
                              page: {index: 0, size: $size}) { rows { target { id } } } } }"""
        d = self.query(q, {"id": disease_id, "size": size}).get("disease") or {}
        return {r["target"]["id"] for r in (d.get("associatedTargets") or {}).get("rows", [])}

    def target_associations(self, ensembl_id: str, size: int = 200) -> tuple[str, list[Association]]:
        q = """query($id: String!, $size: Int!) { target(ensemblId: $id) {
            id approvedSymbol approvedName biotype
            associatedDiseases(page: {index: 0, size: $size}) {
              rows { score novelty datatypeScores { id score } datasourceScores { id score }
                     disease { id name } }
            } } }"""
        t = self.query(q, {"id": ensembl_id, "size": size}).get("target")
        if not t:
            raise OpenTargetsError(f"Unknown target id: {ensembl_id}")
        rows = []
        for r in t["associatedDiseases"]["rows"]:
            rows.append(Association(
                target_id=t["id"], symbol=t["approvedSymbol"], target_name=t.get("approvedName") or "",
                biotype=t.get("biotype"), disease_id=r["disease"]["id"], disease_name=r["disease"]["name"],
                score=r["score"], novelty=r.get("novelty"),
                datatype_scores={x["id"]: x["score"] for x in r.get("datatypeScores") or []},
                datasource_scores={x["id"]: x["score"] for x in r.get("datasourceScores") or []},
            ))
        return t["approvedSymbol"], rows

    # -- target annotation --------------------------------------------------
    def targets(self, ensembl_ids: list[str], chunk: int = 25) -> dict[str, TargetInfo]:
        q = "query($ids: [String!]!) { targets(ensemblIds: $ids) {" + _TARGET_FIELDS + "} }"
        out: dict[str, TargetInfo] = {}
        for i in range(0, len(ensembl_ids), chunk):
            batch = ensembl_ids[i:i + chunk]
            for t in self.query(q, {"ids": batch}).get("targets") or []:
                info = parse_target(t)
                out[info.ensembl_id] = info
        return out

    def target(self, ensembl_id: str) -> TargetInfo:
        found = self.targets([ensembl_id])
        if ensembl_id not in found:
            raise OpenTargetsError(f"Unknown target id: {ensembl_id}")
        return found[ensembl_id]

    # -- evidence -----------------------------------------------------------
    def gwas_loci(self, disease_id: str, ensembl_id: str, size: int = 8) -> list[dict]:
        """GWAS credible sets linking the pair, with locus-to-gene scores of every gene at the locus and
        colocalisations with expression/protein/splicing QTLs (including the effect-sign ratio)."""
        d = self.query(_LOCI_QUERY, {"d": disease_id, "t": [ensembl_id], "n": size}).get("disease") or {}
        out = []
        for row in (d.get("evidences") or {}).get("rows", []):
            cs = row.get("credibleSet") or {}
            if not cs:
                continue
            l2g = [{"gene_id": (x.get("target") or {}).get("id"), "symbol": (x.get("target") or {}).get("approvedSymbol"),
                    "score": x.get("score") or 0.0} for x in (cs.get("l2GPredictions") or {}).get("rows", [])]
            coloc = []
            for c in (cs.get("colocalisation") or {}).get("rows", []):
                st = ((c.get("otherStudyLocus") or {}).get("study") or {})
                coloc.append({"type": c.get("rightStudyType") or st.get("studyType"), "h4": c.get("h4"), "clpp": c.get("clpp"),
                              "sign": c.get("betaRatioSignAverage"), "study_id": st.get("id"),
                              "gene_id": (st.get("target") or {}).get("id"),
                              "symbol": (st.get("target") or {}).get("approvedSymbol"),
                              "tissue": (st.get("biosample") or {}).get("biosampleName")})
            study = cs.get("study") or {}
            out.append({"locus_id": cs.get("studyLocusId"), "study_id": study.get("id"),
                        "trait": study.get("traitFromSource"), "n": study.get("nSamples"),
                        "literature": row.get("literature") or [], "l2g": l2g, "coloc": coloc})
        return out

    def baseline_expression(self, ensembl_id: str, size: int = 2000) -> list[dict]:
        """Baseline RNA/protein expression rows: GTEx tissues, Tabula Sapiens cell types, PRIDE proteomics."""
        t = self.query(_EXPRESSION_QUERY, {"t": ensembl_id, "n": size}).get("target") or {}
        out = []
        for r in ((t.get("baselineExpression") or {}).get("rows") or []):
            out.append({"source": r.get("datasourceId"), "unit": r.get("unit"), "value": r.get("median"),
                        "specificity": r.get("specificity_score"),
                        "tissue": (r.get("tissueBiosample") or {}).get("biosampleName"),
                        "cell": (r.get("celltypeBiosample") or {}).get("biosampleName")})
        return out

    def target_safety(self, ensembl_id: str, size: int = 300, max_diseases: int = 40) -> dict:
        """Phenome-wide genetics (other diseases with LoF/GoF direction), mouse knockout phenotypes and the
        warnings / FAERS adverse events of drugs acting on the target."""
        t = self.query(_SAFETY_QUERY, {"t": ensembl_id, "n": size}).get("target") or {}
        genetic = []
        for r in (t.get("associatedDiseases") or {}).get("rows") or []:
            ds = {x["id"]: x["score"] for x in r.get("datasourceScores") or []}
            g = max(ds.get(k, 0.0) for k in _GENETIC_SOURCES)
            if g > 0:
                d = r.get("disease") or {}
                genetic.append({"id": d.get("id"), "name": d.get("name"), "genetic_score": g,
                                "areas": [a["name"] for a in d.get("therapeuticAreas") or []]})
        genetic.sort(key=lambda x: -x["genetic_score"])
        genetic = genetic[:max_diseases]
        rows = []
        if genetic:
            e = self.query(_SAFETY_EVIDENCE_QUERY, {"t": ensembl_id, "e": [g["id"] for g in genetic]}).get("target") or {}
            rows = [{"disease_id": (r.get("disease") or {}).get("id"), "source": r.get("datasourceId"),
                     "score": r.get("score"), "directionOnTarget": r.get("directionOnTarget"),
                     "directionOnTrait": r.get("directionOnTrait")}
                    for r in (e.get("evidences") or {}).get("rows") or []]
        lethal = sorted({m.get("modelPhenotypeLabel") for m in t.get("mousePhenotypes") or []
                         if "lethal" in (m.get("modelPhenotypeLabel") or "").lower()})
        drugs = []
        for r in (t.get("drugAndClinicalCandidates") or {}).get("rows") or []:
            d = r.get("drug") or {}
            if not d:
                continue
            ae = d.get("adverseEvents") or {}
            crit = ae.get("criticalValue") or 0.0
            drugs.append({"id": d.get("id"), "name": d.get("name"), "stage": r.get("maxClinicalStage"),
                          "warnings": [{"type": w.get("warningType"), "class": w.get("toxicityClass"),
                                        "description": w.get("description")} for w in d.get("drugWarnings") or []],
                          "adverse_events": [{"name": x.get("name"), "llr": x.get("logLR")}
                                             for x in ae.get("rows") or [] if (x.get("logLR") or 0) > crit]})
        return {"genetic_diseases": genetic, "genetic_rows": rows, "mouse_lethal": lethal, "drugs": drugs}

    def pair_evidence(self, disease_id: str, ensembl_id: str,
                      groups: dict[str, tuple[list[str], int]] | None = None) -> dict[str, list[dict]]:
        """Evidence rows for one target–disease pair, grouped (one aliased GraphQL call)."""
        groups = groups or EVIDENCE_GROUPS
        parts = []
        for alias, (sources, size) in groups.items():
            ds = ", ".join(f'"{s}"' for s in sources)
            parts.append(
                f"{alias}: evidences(ensemblIds: [$t], enableIndirect: true, datasourceIds: [{ds}], size: {size})"
                f" {{ count rows {{ {_EVIDENCE_FIELDS} }} }}"
            )
        q = "query($d: String!, $t: String!) { disease(efoId: $d) { " + " ".join(parts) + " } }"
        d = self.query(q, {"d": disease_id, "t": ensembl_id}).get("disease") or {}
        return {alias: (d.get(alias) or {}).get("rows", []) for alias in groups}


_EXPRESSION_QUERY = """query($t: String!, $n: Int!) { target(ensemblId: $t) {
  baselineExpression(page: {index: 0, size: $n}) { rows {
    datasourceId unit median specificity_score
    tissueBiosample { biosampleName } celltypeBiosample { biosampleName } } } } }"""


_GENETIC_SOURCES = ("gwas_credible_sets", "gene_burden", "eva", "genomics_england", "gene2phenotype", "orphanet",
                    "uniprot_variants", "clingen")

_SAFETY_QUERY = """query($t: String!, $n: Int!) { target(ensemblId: $t) {
  associatedDiseases(enableIndirect: false, includeMeasurements: false, page: {index: 0, size: $n}) {
    rows { disease { id name therapeuticAreas { name } } datasourceScores { id score } } }
  mousePhenotypes { modelPhenotypeLabel }
  drugAndClinicalCandidates { rows { maxClinicalStage drug { id name
    drugWarnings { warningType toxicityClass description }
    adverseEvents(page: {index: 0, size: 8}) { criticalValue rows { name logLR } } } } } } }"""

_SAFETY_EVIDENCE_QUERY = """query($t: String!, $e: [String!]!) { target(ensemblId: $t) {
  evidences(efoIds: $e, datasourceIds: ["gene_burden", "gwas_credible_sets", "eva", "genomics_england",
                                        "gene2phenotype", "orphanet", "uniprot_variants", "clingen"], size: 500) {
    rows { datasourceId disease { id } score directionOnTarget directionOnTrait } } } }"""


def usable_synonyms(name: str, groups: list[dict] | None, limit: int = 3) -> list[str]:
    """Exact synonyms that make sensible literature search terms (no short abbreviations)."""
    out: list[str] = []
    for g in groups or []:
        if g.get("relation") != "hasExactSynonym":
            continue
        for term in g.get("terms") or []:
            t = term.strip()
            if len(t) < 6 or t.isupper() or t.lower() == name.lower() or "," in t:
                continue
            if t.lower() not in {o.lower() for o in out}:
                out.append(t)
    out.sort(key=lambda s: (" " not in s, len(s)))
    return out[:limit]


def parse_disease(d: dict[str, Any]) -> DiseaseInfo:
    return DiseaseInfo(
        id=d["id"], name=d["name"], description=d.get("description"),
        therapeutic_areas=[t["name"] for t in d.get("therapeuticAreas") or []],
        descendants=list(d.get("descendants") or []),
        synonyms=usable_synonyms(d["name"], d.get("synonyms")),
        parents=[p["name"] for p in d.get("parents") or [] if not p.get("isTherapeuticArea")][:3],
        is_therapeutic_area=bool(d.get("isTherapeuticArea")),
    )


_LOCI_QUERY = """query($d: String!, $t: [String!]!, $n: Int!) { disease(efoId: $d) {
  evidences(ensemblIds: $t, enableIndirect: true, datasourceIds: ["gwas_credible_sets"], size: $n) {
    rows { score literature
      credibleSet { studyLocusId beta pValueMantissa pValueExponent
        study { id nSamples traitFromSource }
        l2GPredictions(page: {index: 0, size: 5}) { rows { score target { id approvedSymbol } } }
        colocalisation(studyTypes: [eqtl, pqtl, sceqtl, scpqtl, sqtl], page: {index: 0, size: 50}) { rows {
          rightStudyType h4 clpp betaRatioSignAverage
          otherStudyLocus { study { id studyType target { id approvedSymbol } biosample { biosampleName } } } } }
      } } } } }"""


def _protein_id(t: dict[str, Any]) -> str | None:
    ids = t.get("proteinIds") or []
    for p in ids:
        if p.get("source") == "uniprot_swissprot":
            return p["id"]
    for p in ids:
        if str(p.get("source", "")).startswith("uniprot"):
            return p["id"]
    return None


def parse_target(t: dict[str, Any]) -> TargetInfo:
    tract: dict[str, list[str]] = {}
    for b in t.get("tractability") or []:
        if b.get("value"):
            tract.setdefault(b["modality"], []).append(b["label"])
    lof = next((c for c in t.get("geneticConstraint") or [] if c.get("constraintType") == "lof"), None)
    probes = t.get("chemicalProbes") or []
    prio = {}
    for item in (t.get("prioritisation") or {}).get("items") or []:
        try:
            prio[item["key"]] = float(item["value"])
        except (TypeError, ValueError):
            continue
    drugs = []
    for row in (t.get("drugAndClinicalCandidates") or {}).get("rows") or []:
        drug = row.get("drug") or {}
        if not drug:
            continue
        actions = sorted({m.get("actionType") for m in ((drug.get("mechanismsOfAction") or {}).get("rows") or [])
                          if m.get("actionType")})
        diseases = list({x["disease"]["id"]: x["disease"] for x in row.get("diseases") or []
                         if x.get("disease")}.values())
        drugs.append(DrugInfo(
            chembl_id=drug["id"], name=drug.get("name") or drug["id"], max_stage=row.get("maxClinicalStage"),
            action_types=actions, disease_ids=[x["id"] for x in diseases], disease_names=[x["name"] for x in diseases],
        ))
    functions = t.get("functionDescriptions") or []
    loc = t.get("genomicLocation") or {}
    return TargetInfo(
        ensembl_id=t["id"], symbol=t["approvedSymbol"], name=t.get("approvedName") or "", biotype=t.get("biotype"),
        uniprot_id=_protein_id(t), function=functions[0] if functions else None,
        target_classes=sorted({c["label"] for c in t.get("targetClass") or []}),
        subcellular_locations=sorted({s["location"] for s in t.get("subcellularLocations") or []})[:12],
        tractability=tract,
        safety_events=sorted({s.get("event") or s.get("datasource") for s in t.get("safetyLiabilities") or []
                              if s.get("event") or s.get("datasource")}),
        lof_oe_upper=lof.get("oeUpper") if lof else None,
        is_essential=t.get("isEssential"),
        chemical_probes=[p["id"] for p in probes],
        high_quality_probes=sum(1 for p in probes if p.get("isHighQuality")),
        n_associated_diseases=(t.get("associatedDiseases") or {}).get("count"),
        known_drugs=drugs,
        prioritisation=prio,
        chromosome=loc.get("chromosome"), start=loc.get("start"), end=loc.get("end"),
    )
