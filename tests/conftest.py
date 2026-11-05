"""Offline fakes for every data source, so the whole pipeline can be tested without network."""

from __future__ import annotations

import pytest

from discovery_agent.config import Settings
from discovery_agent.models import Association, DiseaseInfo, DrugInfo, TargetInfo
from discovery_agent.sources import Sources
from discovery_agent.sources.chemistry import ChemblSummary, PdbSummary, UniProtEntry
from discovery_agent.sources.clinical import Trial, TrialSearch
from discovery_agent.sources.literature import Article, BioRxivClient, PubMedClient, SearchResult
from discovery_agent.sources.opentargets import OpenTargetsError

DISEASE = DiseaseInfo(id="EFO_0000001", name="test fibrosis", descendants=["EFO_0000002"],
                      synonyms=["fibrotic test disease"], parents=["parent fibrosis"],
                      therapeutic_areas=["respiratory or thoracic disease"])

TARGETS = {
    "ENSG00000000001": TargetInfo(ensembl_id="ENSG00000000001", symbol="GENEA", name="gene alpha kinase",
                                  biotype="protein_coding", uniprot_id="P00001", function="A kinase. Second sentence.",
                                  target_classes=["Enzyme", "Kinase"], tractability={"SM": ["High-Quality Pocket"]},
                                  n_associated_diseases=40, lof_oe_upper=0.9),
    "ENSG00000000002": TargetInfo(ensembl_id="ENSG00000000002", symbol="GENEB", name="gene beta receptor",
                                  biotype="protein_coding", uniprot_id="P00002",
                                  known_drugs=[DrugInfo(chembl_id="CHEMBL9", name="BETAMAB", max_stage="APPROVAL",
                                                        action_types=["ANTAGONIST"], disease_ids=["EFO_0000002"],
                                                        disease_names=["test fibrosis subtype"])]),
    "ENSG00000000003": TargetInfo(ensembl_id="ENSG00000000003", symbol="GENEC", name="gene gamma",
                                  biotype="protein_coding"),
    "ENSG00000000004": TargetInfo(ensembl_id="ENSG00000000004", symbol="GENED", name="gene delta protease",
                                  biotype="protein_coding", uniprot_id="P00004", safety_events=["cardiotoxicity"],
                                  is_essential=True, n_associated_diseases=1700),
}

ASSOCIATIONS = [
    Association(target_id="ENSG00000000001", symbol="GENEA", disease_id=DISEASE.id, disease_name=DISEASE.name,
                biotype="protein_coding", score=0.8,
                datatype_scores={"genetic_association": 0.9, "literature": 0.1},
                datasource_scores={"gene_burden": 0.9, "europepmc": 0.1}),
    Association(target_id="ENSG00000000002", symbol="GENEB", disease_id=DISEASE.id, disease_name=DISEASE.name,
                biotype="protein_coding", score=0.9,
                datatype_scores={"genetic_association": 0.8, "clinical": 0.9},
                datasource_scores={"gwas_credible_sets": 0.8, "clinical_precedence": 0.9}),
    Association(target_id="ENSG00000000003", symbol="GENEC", disease_id=DISEASE.id, disease_name=DISEASE.name,
                biotype="protein_coding", score=0.2, datatype_scores={"rna_expression": 0.3},
                datasource_scores={"expression_atlas": 0.3}),
    Association(target_id="ENSG00000000004", symbol="GENED", disease_id=DISEASE.id, disease_name=DISEASE.name,
                biotype="protein_coding", score=0.7,
                datatype_scores={"genetic_association": 0.7, "animal_model": 0.5, "rna_expression": 0.4,
                                 "literature": 0.6},
                datasource_scores={"gwas_credible_sets": 0.7, "impc": 0.5, "expression_atlas": 0.4,
                                   "europepmc": 0.6}),
]

EVIDENCE = {
    "ENSG00000000001": {"genetic": [
        {"datasourceId": "gene_burden", "score": 0.95, "directionOnTarget": "LoF", "directionOnTrait": "risk",
         "pValueMantissa": 1.0, "pValueExponent": -12, "literature": ["11111111"]},
        {"datasourceId": "eva", "score": 0.9, "directionOnTarget": "LoF", "directionOnTrait": "risk",
         "clinicalSignificances": ["pathogenic"], "confidence": "criteria provided, multiple submitters, no conflicts",
         "studyId": "RCV000001"},
    ], "literature": [{"datasourceId": "europepmc", "score": 0.1, "literature": ["22222222"]}]},
    "ENSG00000000004": {
        "genetic": [{"datasourceId": "gwas_credible_sets", "score": 0.6, "literature": ["33333333"]}],
        "animal": [{"datasourceId": "impc", "score": 0.5,
                    "diseaseModelAssociatedModelPhenotypes": [{"label": "lung fibrosis"}]}],
        "expression": [{"datasourceId": "expression_atlas", "score": 0.4, "log2FoldChangeValue": 2.1}],
    },
}


class FakeOpenTargets:
    def __init__(self):
        self.calls: list[str] = []

    def resolve_disease(self, text):
        self.calls.append("resolve_disease")
        if text in ("test fibrosis", DISEASE.id):
            return DISEASE
        raise OpenTargetsError("not found")

    def resolve_target(self, text):
        for t in TARGETS.values():
            if text in (t.symbol, t.ensembl_id):
                return t.ensembl_id
        raise OpenTargetsError("not found")

    def disease(self, disease_id):
        if disease_id == DISEASE.id:
            return DISEASE
        if disease_id == "MONDO_BROAD":
            return DiseaseInfo(id="MONDO_BROAD", name="hereditary disease", descendants=[f"X{i}" for i in range(500)])
        return DiseaseInfo(id=disease_id, name=f"disease {disease_id}")

    def disease_associations(self, disease_id, size=200):
        return DISEASE, [a.model_copy() for a in ASSOCIATIONS]

    def targets(self, ids, chunk=25):
        return {i: TARGETS[i].model_copy(deep=True) for i in ids if i in TARGETS}

    def target(self, ensembl_id):
        return TARGETS[ensembl_id].model_copy(deep=True)

    def target_associations(self, ensembl_id, size=200):
        t = TARGETS[ensembl_id]
        rows = [
            Association(target_id=ensembl_id, symbol=t.symbol, disease_id=DISEASE.id, disease_name=DISEASE.name,
                        score=0.8, datatype_scores={"genetic_association": 0.9},
                        datasource_scores={"gene_burden": 0.9}),
            Association(target_id=ensembl_id, symbol=t.symbol, disease_id="MONDO_BROAD",
                        disease_name="hereditary disease", score=0.9, datatype_scores={"genetic_association": 0.95},
                        datasource_scores={"eva": 0.95}),
            Association(target_id=ensembl_id, symbol=t.symbol, disease_id="EFO_0000099", disease_name="other disease",
                        score=0.5, datatype_scores={"genetic_association": 0.5},
                        datasource_scores={"gwas_credible_sets": 0.5}),
        ]
        return t.symbol, rows

    def baseline_expression(self, ensembl_id, size=2000):
        if ensembl_id == "ENSG00000000001":  # GENEA: made in the lung
            return [{"source": "gtex", "tissue": "lung", "cell": None, "value": 40.0, "unit": "TPM"},
                    {"source": "gtex", "tissue": "liver", "cell": None, "value": 3.0, "unit": "TPM"},
                    {"source": "tabula_sapiens", "tissue": "lung", "cell": "fibroblast", "value": 120.0, "unit": "CPM"}]
        if ensembl_id == "ENSG00000000002":  # GENEB: immune cells only
            return [{"source": "gtex", "tissue": "lung", "cell": None, "value": 0.5, "unit": "TPM"},
                    {"source": "gtex", "tissue": "spleen", "cell": None, "value": 60.0, "unit": "TPM"},
                    {"source": "tabula_sapiens", "tissue": "blood", "cell": "classical monocyte", "value": 300.0,
                     "unit": "CPM"}]
        return []

    def target_safety(self, ensembl_id, size=300, max_diseases=40):
        if ensembl_id != "ENSG00000000004":  # GENED (inhibition): LoF carriers have more infections
            return {"genetic_diseases": [], "genetic_rows": [], "mouse_lethal": [], "drugs": []}
        return {
            "genetic_diseases": [{"id": "EFO_INF", "name": "test infection", "genetic_score": 0.8, "areas": []},
                                 {"id": "EFO_GOUT", "name": "test gout", "genetic_score": 0.6, "areas": []},
                                 {"id": "EFO_MISC", "name": "test misc", "genetic_score": 0.5, "areas": []},
                                 {"id": DISEASE.id, "name": DISEASE.name, "genetic_score": 0.9, "areas": []}],
            "genetic_rows": [
                {"disease_id": "EFO_INF", "source": "gene_burden", "score": 0.9, "directionOnTarget": "LoF",
                 "directionOnTrait": "risk"},
                {"disease_id": "EFO_GOUT", "source": "gwas_credible_sets", "score": 0.7, "directionOnTarget": "GoF",
                 "directionOnTrait": "risk"},
                {"disease_id": "EFO_MISC", "source": "gwas_credible_sets", "score": 0.5, "directionOnTarget": None,
                 "directionOnTrait": None},
                {"disease_id": DISEASE.id, "source": "gene_burden", "score": 0.9, "directionOnTarget": "GoF",
                 "directionOnTrait": "risk"}],
            "mouse_lethal": ["preweaning lethality, complete penetrance"],
            "drugs": [{"id": "CHEMBL9", "name": "TESTMAB", "stage": "PHASE_2",
                       "warnings": [{"type": "Black Box Warning", "class": "hepatotoxicity", "description": ""}],
                       "adverse_events": [{"name": "liver injury", "llr": 40.0}]}]}

    def gwas_loci(self, disease_id, ensembl_id, size=8):
        if ensembl_id != "ENSG00000000004":
            return []
        return [{"locus_id": "LOC1", "study_id": "GCST000001", "trait": "test fibrosis", "n": 5000,
                 "literature": ["33333333"],
                 "l2g": [{"gene_id": "ENSG00000000004", "symbol": "GENED", "score": 0.8}],
                 "coloc": [{"type": "eqtl", "h4": 0.97, "clpp": None, "sign": 1, "study_id": "gtex_lung",
                            "gene_id": "ENSG00000000004", "symbol": "GENED", "tissue": "lung"}]}]

    def pair_evidence(self, disease_id, ensembl_id, groups=None):
        data = EVIDENCE.get(ensembl_id, {})
        if groups:
            return {g: list(data.get(g, [])) for g in groups}
        return {g: list(v) for g, v in data.items()}

    def query(self, query, variables=None, ttl_hours=None):
        rows = [a for a in ASSOCIATIONS if a.target_id in (variables or {}).get("t", [])]
        return {"disease": {"associatedTargets": {"rows": [
            {"score": a.score, "novelty": None,
             "datatypeScores": [{"id": k, "score": v} for k, v in a.datatype_scores.items()],
             "datasourceScores": [{"id": k, "score": v} for k, v in a.datasource_scores.items()]} for a in rows]}}}

    def search(self, text, entities=("target", "disease"), size=5):
        return [{"id": DISEASE.id, "name": DISEASE.name, "entity": "disease", "description": "fake"}]


PUB_COUNTS = {"GENEA": 3, "GENEB": 50, "GENEC": 0, "GENED": 400}
GENE_COUNTS = {"GENEA": 120}


def _symbol_from(genes):
    for g in genes:
        if g.startswith("GENE"):
            return g
    return "?"


class FakeEuropePMC:
    def __init__(self):
        self.http = None
        self.queries: list[str] = []
        self.extra_articles: list[Article] = []

    def publications(self, genes, diseases, *, since=None, page_size=10, ttl_hours=None):
        sym = _symbol_from(genes)
        arts = [Article(id=f"{sym}-1", source="MED", pmid="44444444", title=f"{sym} in test fibrosis", year=2025)]
        arts += [a for a in self.extra_articles if sym in a.title]
        return SearchResult(query="pubs", hit_count=PUB_COUNTS.get(sym, 0), articles=arts)

    def preprints(self, genes, diseases, *, since=None, page_size=10, ttl_hours=None):
        sym = _symbol_from(genes)
        return SearchResult(query="ppr", hit_count=1, articles=[
            Article(id="PPR1", source="PPR", doi="10.1101/2026.01.01.000001", publisher="bioRxiv",
                    title=f"{sym} preprint", first_publication_date="2026-01-02", year=2026)])

    def patents(self, genes, diseases, *, page_size=10):
        return SearchResult(query="pat", hit_count=0, articles=[])

    def topic_count(self, terms, *, since=None, until=None):
        sym = _symbol_from(terms)
        if sym != "?":  # a gene alone
            return GENE_COUNTS.get(sym, 5000)
        return 1500 if since else 5000  # the disease field: flat over time

    def count(self, query, *, ttl_hours=None):
        self.queries.append(query)
        if "parent fibrosis" in query:
            return 5
        return 1

    def search(self, query, *, page_size=25, sort=None, result_type="lite", ttl_hours=None):
        self.queries.append(query)
        return SearchResult(query=query, hit_count=2, articles=[
            Article(id="55555555", source="MED", pmid="55555555", title="A relevant paper", year=2024,
                    abstract="Background text. GENEA loss raises test fibrosis risk in carriers. "
                             "Treatment did not improve lung function in the GENEA trial. Unrelated final sentence.")])


class FakePubMed:
    cooccurrence_term = staticmethod(PubMedClient.cooccurrence_term)

    def count(self, term):
        return 2


class FakeBioRxiv:
    server_for = staticmethod(BioRxivClient.server_for)

    def published_doi(self, doi, server="biorxiv"):
        return "10.1000/journal.1"


class FakeTrials:
    def __init__(self):
        self.extra: list[Trial] = []

    def search(self, *, condition=None, term=None, intervention=None, page_size=20, interventional_only=True):
        trials = list(self.extra)
        if term and "GENED" in term:
            trials.append(Trial(nct_id="NCT00000001", title="GENED inhibitor study", status="TERMINATED",
                                why_stopped="Lack of efficacy at interim analysis", phases=["PHASE2"]))
        return TrialSearch(total=len(trials), trials=trials)


class FakeUniProt:
    def entry(self, accession):
        return UniProtEntry(accession=accession, protein_name=f"Protein {accession}",
                            function="Catalyses something important. {ECO:0000269|PubMed:1}",
                            subcellular_locations=["Cytoplasm"], is_enzyme=True)


class FakeChembl:
    def summary(self, accession):
        if accession == "P00001":
            return ChemblSummary(target_chembl_id="CHEMBL1001", potent_compounds=25, very_potent_compounds=3,
                                 best_pchembl=8.4,
                                 best_compounds=[{"molecule": "CHEMBL5", "name": "TOOLCPD", "pchembl": 8.4}],
                                 mechanisms=[])
        return ChemblSummary()


class FakePdb:
    def summary(self, accession):
        if accession == "P00001":
            return PdbSummary(total=4, with_ligand=2, best_resolution=1.8,
                              entries=[{"id": "1ABC", "title": "GENEA with ligand", "resolution": 1.8}])
        return PdbSummary()


def make_sources() -> Sources:
    return Sources(opentargets=FakeOpenTargets(), europepmc=FakeEuropePMC(), pubmed=FakePubMed(),
                   biorxiv=FakeBioRxiv(), chembl=FakeChembl(), uniprot=FakeUniProt(), pdb=FakePdb(),
                   trials=FakeTrials())


@pytest.fixture
def sources() -> Sources:
    return make_sources()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(cache_dir=tmp_path / "cache", language="en", max_workers=2)


@pytest.fixture
def settings_lt(tmp_path) -> Settings:
    return Settings(cache_dir=tmp_path / "cache", language="lt", max_workers=2)
