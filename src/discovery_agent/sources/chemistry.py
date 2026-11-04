"""Chemistry & structure sources: ChEMBL bioactivity, UniProt annotation, RCSB PDB."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..http import HttpClient, HttpError

CHEMBL_API = "https://www.ebi.ac.uk/chembl/api/data"
UNIPROT_API = "https://rest.uniprot.org/uniprotkb"
RCSB_SEARCH = "https://search.rcsb.org/rcsbsearch/v2/query"
RCSB_GRAPHQL = "https://data.rcsb.org/graphql"


class ChemblSummary(BaseModel):
    target_chembl_id: str | None = None
    target_name: str | None = None
    activities_with_pchembl: int = 0
    potent_compounds: int = 0          # distinct molecules with pChEMBL >= 6 (among the top 1000 activities)
    very_potent_compounds: int = 0     # pChEMBL >= 8
    best_pchembl: float | None = None
    best_compounds: list[dict] = Field(default_factory=list)
    mechanisms: list[dict] = Field(default_factory=list)

    @property
    def url(self) -> str | None:
        if not self.target_chembl_id:
            return None
        return f"https://www.ebi.ac.uk/chembl/explore/target/{self.target_chembl_id}"


class ChemblClient:
    def __init__(self, http: HttpClient):
        self.http = http

    def target_for_uniprot(self, accession: str) -> dict | None:
        data = self.http.get_json(f"{CHEMBL_API}/target.json", {
            "target_components__accession": accession, "limit": 20})
        targets = data.get("targets") or []
        single = [t for t in targets if t.get("target_type") == "SINGLE PROTEIN"]
        human = [t for t in single if t.get("organism") == "Homo sapiens"]
        pick = (human or single or targets or [None])[0]
        return pick

    def summary(self, accession: str) -> ChemblSummary:
        target = self.target_for_uniprot(accession)
        if not target:
            return ChemblSummary()
        tid = target["target_chembl_id"]
        acts = self.http.get_json(f"{CHEMBL_API}/activity.json", {
            "target_chembl_id": tid, "pchembl_value__isnull": "false", "order_by": "-pchembl_value",
            "limit": 1000, "only": "molecule_chembl_id,molecule_pref_name,pchembl_value,standard_type",
        })
        rows = acts.get("activities") or []
        best: dict[str, dict] = {}
        for r in rows:
            try:
                p = float(r.get("pchembl_value"))
            except (TypeError, ValueError):
                continue
            mol = r.get("molecule_chembl_id")
            if mol and (mol not in best or p > best[mol]["pchembl"]):
                best[mol] = {"molecule": mol, "name": r.get("molecule_pref_name"), "pchembl": p,
                             "type": r.get("standard_type")}
        ranked = sorted(best.values(), key=lambda x: -x["pchembl"])
        mech = self.http.get_json(f"{CHEMBL_API}/mechanism.json", {"target_chembl_id": tid, "limit": 50})
        mechanisms = [
            {"molecule": m.get("molecule_chembl_id"), "action_type": m.get("action_type"),
             "mechanism": m.get("mechanism_of_action"), "max_phase": m.get("max_phase")}
            for m in mech.get("mechanisms") or []
        ]
        return ChemblSummary(
            target_chembl_id=tid, target_name=target.get("pref_name"),
            activities_with_pchembl=int((acts.get("page_meta") or {}).get("total_count") or len(rows)),
            potent_compounds=sum(1 for x in ranked if x["pchembl"] >= 6),
            very_potent_compounds=sum(1 for x in ranked if x["pchembl"] >= 8),
            best_pchembl=ranked[0]["pchembl"] if ranked else None,
            best_compounds=ranked[:5], mechanisms=mechanisms,
        )


class UniProtEntry(BaseModel):
    accession: str
    protein_name: str | None = None
    function: str | None = None
    subcellular_locations: list[str] = Field(default_factory=list)
    diseases: list[str] = Field(default_factory=list)
    tissue_specificity: str | None = None
    pdb_ids: list[str] = Field(default_factory=list)
    is_enzyme: bool = False
    annotation_score: float | None = None
    length: int | None = None

    @property
    def url(self) -> str:
        return f"https://www.uniprot.org/uniprotkb/{self.accession}/entry"


def _comment_text(comment: dict) -> str:
    return " ".join(t.get("value", "") for t in comment.get("texts") or []).strip()


class UniProtClient:
    FIELDS = ("accession,protein_name,cc_function,cc_subcellular_location,cc_disease,"
              "cc_tissue_specificity,xref_pdb,length,annotation_score,cc_catalytic_activity")

    def __init__(self, http: HttpClient):
        self.http = http

    def entry(self, accession: str) -> UniProtEntry:
        d = self.http.get_json(f"{UNIPROT_API}/{accession}.json", {"fields": self.FIELDS})
        function = tissue = None
        locations: list[str] = []
        diseases: list[str] = []
        enzyme = False
        for c in d.get("comments") or []:
            ct = c.get("commentType")
            if ct == "FUNCTION" and not function:
                function = _comment_text(c)
            elif ct == "TISSUE SPECIFICITY" and not tissue:
                tissue = _comment_text(c)
            elif ct == "SUBCELLULAR LOCATION":
                for loc in c.get("subcellularLocations") or []:
                    v = (loc.get("location") or {}).get("value")
                    if v:
                        locations.append(v)
            elif ct == "DISEASE":
                dis = c.get("disease") or {}
                if dis.get("diseaseId"):
                    diseases.append(dis["diseaseId"])
            elif ct == "CATALYTIC ACTIVITY":
                enzyme = True
        desc = d.get("proteinDescription") or {}
        name = ((desc.get("recommendedName") or {}).get("fullName") or {}).get("value")
        if desc.get("recommendedName", {}).get("ecNumbers"):
            enzyme = True
        pdb = [x["id"] for x in d.get("uniProtKBCrossReferences") or [] if x.get("database") == "PDB"]
        return UniProtEntry(
            accession=d.get("primaryAccession", accession), protein_name=name, function=function,
            subcellular_locations=sorted(set(locations)), diseases=diseases, tissue_specificity=tissue,
            pdb_ids=pdb, is_enzyme=enzyme, annotation_score=d.get("annotationScore"),
            length=(d.get("sequence") or {}).get("length"),
        )


class PdbSummary(BaseModel):
    total: int = 0
    with_ligand: int = 0
    best_resolution: float | None = None
    entries: list[dict] = Field(default_factory=list)


class PdbClient:
    def __init__(self, http: HttpClient):
        self.http = http

    @staticmethod
    def _accession_node(accession: str) -> dict:
        return {"type": "terminal", "service": "text", "parameters": {
            "attribute": "rcsb_polymer_entity_container_identifiers.reference_sequence_identifiers.database_accession",
            "operator": "exact_match", "value": accession}}

    def _search(self, node: dict, rows: int) -> dict:
        query = {"query": node, "return_type": "entry", "request_options": {
            "paginate": {"start": 0, "rows": rows}, "results_content_type": ["experimental"],
            "sort": [{"sort_by": "rcsb_entry_info.resolution_combined", "direction": "asc"}]}}
        try:
            return self.http.post_json(RCSB_SEARCH, query) or {}
        except HttpError as exc:
            if exc.status == 204:
                return {}
            raise

    def summary(self, accession: str, rows: int = 5) -> PdbSummary:
        base = self._accession_node(accession)
        res = self._search(base, rows)
        total = int(res.get("total_count") or 0)
        if not total:
            return PdbSummary()
        lig_node = {"type": "group", "logical_operator": "and", "nodes": [base, {
            "type": "terminal", "service": "text", "parameters": {
                "attribute": "rcsb_entry_info.nonpolymer_entity_count", "operator": "greater", "value": 0}}]}
        with_ligand = int((self._search(lig_node, 1) or {}).get("total_count") or 0)
        ids = [r["identifier"] for r in res.get("result_set") or []]
        entries: list[dict] = []
        best = None
        if ids:
            gq = """query($ids: [String!]!) { entries(entry_ids: $ids) { rcsb_id struct { title }
                     exptl { method } rcsb_entry_info { resolution_combined nonpolymer_entity_count } } }"""
            data = self.http.post_json(RCSB_GRAPHQL, {"query": gq, "variables": {"ids": ids}})
            for e in (data.get("data") or {}).get("entries") or []:
                res_list = (e.get("rcsb_entry_info") or {}).get("resolution_combined") or []
                resolution = res_list[0] if res_list else None
                if resolution is not None and (best is None or resolution < best):
                    best = resolution
                entries.append({"id": e["rcsb_id"], "title": (e.get("struct") or {}).get("title"),
                                "method": ((e.get("exptl") or [{}])[0]).get("method"), "resolution": resolution,
                                "ligands": (e.get("rcsb_entry_info") or {}).get("nonpolymer_entity_count")})
        return PdbSummary(total=total, with_ligand=with_ligand, best_resolution=best, entries=entries)
