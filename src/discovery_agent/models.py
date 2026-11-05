"""Domain model shared by the pipeline, the AI layer, the monitor and the reports."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EvidenceType(str, Enum):
    HUMAN_GENETICS = "human_genetics"
    SOMATIC = "somatic"
    CLINICAL = "clinical"
    ANIMAL_MODEL = "animal_model"
    FUNCTIONAL = "functional"
    EXPRESSION = "expression"
    LITERATURE = "literature"
    PREPRINT = "preprint"
    CHEMISTRY = "chemistry"
    STRUCTURE = "structure"
    PROTEIN = "protein"
    TRIAL_REGISTRY = "trial_registry"
    PATENT = "patent"
    SAFETY = "safety"
    AGENT_FINDING = "agent_finding"


class Aspect(str, Enum):
    """Which question a piece of evidence speaks to."""

    CAUSALITY = "causality"        # is the target causally involved in the disease?
    DIRECTION = "direction"        # which way should we modulate it?
    TRACTABILITY = "tractability"  # can we drug it?
    SAFETY = "safety"              # what could go wrong?
    ATTENTION = "attention"        # how much has already been done?
    MECHANISM = "mechanism"        # what does the protein do?


class Reliability(str, Enum):
    """GRADE-inspired reliability grade of one evidence item."""

    A = "A"  # high: human genetics with robust statistics, approved drugs, curated definitive
    B = "B"  # moderate: curated/replicated but indirect
    C = "C"  # low: model organisms, correlative, single study
    D = "D"  # very low: text-mining, preprints, patents

    @property
    def weight(self) -> float:
        return {"A": 1.0, "B": 0.7, "C": 0.4, "D": 0.15}[self.value]


class Stance(str, Enum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    NEUTRAL = "neutral"


class Modulation(str, Enum):
    INHIBITION = "inhibition"
    ACTIVATION = "activation"
    UNKNOWN = "unknown"


class ConfidenceLevel(str, Enum):
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    VERY_LOW = "very_low"


class Reference(BaseModel):
    label: str
    url: str | None = None
    pmid: str | None = None
    doi: str | None = None


class Evidence(BaseModel):
    id: str = ""  # short stable id within a dossier, e.g. "E3"; assigned by the pipeline
    source: str
    type: EvidenceType
    aspect: Aspect
    summary: str
    stance: Stance = Stance.SUPPORTS
    reliability: Reliability
    reliability_rationale: str
    strength: float = Field(0.0, ge=0.0, le=1.0)
    effect_direction: Modulation | None = None
    peer_reviewed: bool = True
    year: int | None = None
    references: list[Reference] = Field(default_factory=list)


class DrugInfo(BaseModel):
    chembl_id: str
    name: str
    max_stage: str | None = None
    action_types: list[str] = Field(default_factory=list)
    disease_ids: list[str] = Field(default_factory=list)
    disease_names: list[str] = Field(default_factory=list)


class TargetInfo(BaseModel):
    ensembl_id: str
    symbol: str
    name: str = ""
    biotype: str | None = None
    uniprot_id: str | None = None
    function: str | None = None
    target_classes: list[str] = Field(default_factory=list)
    subcellular_locations: list[str] = Field(default_factory=list)
    tractability: dict[str, list[str]] = Field(default_factory=dict)  # modality -> positive buckets
    safety_events: list[str] = Field(default_factory=list)
    lof_oe_upper: float | None = None  # LOEUF; < 0.35 = strongly LoF-intolerant
    is_essential: bool | None = None
    chemical_probes: list[str] = Field(default_factory=list)
    high_quality_probes: int = 0
    n_associated_diseases: int | None = None
    known_drugs: list[DrugInfo] = Field(default_factory=list)
    prioritisation: dict[str, float] = Field(default_factory=dict)
    chromosome: str | None = None
    start: int | None = None
    end: int | None = None


class DiseaseInfo(BaseModel):
    id: str
    name: str
    description: str | None = None
    therapeutic_areas: list[str] = Field(default_factory=list)
    descendants: list[str] = Field(default_factory=list)
    synonyms: list[str] = Field(default_factory=list)       # exact synonyms usable as search terms
    parents: list[str] = Field(default_factory=list)        # direct parent disease names (non-therapeutic-area)
    is_therapeutic_area: bool = False


class Association(BaseModel):
    target_id: str
    symbol: str
    target_name: str = ""
    biotype: str | None = None
    disease_id: str
    disease_name: str
    score: float
    novelty: float | None = None
    datatype_scores: dict[str, float] = Field(default_factory=dict)
    datasource_scores: dict[str, float] = Field(default_factory=dict)


class DirectionAssessment(BaseModel):
    modulation: Modulation = Modulation.UNKNOWN
    confidence: float = 0.0  # 0..1 agreement between independent direction signals
    votes_inhibition: float = 0.0
    votes_activation: float = 0.0
    rationale: list[str] = Field(default_factory=list)


class AttentionMetrics(BaseModel):
    """How much research/commercial attention the pair already received."""

    publications: int | None = None          # Europe PMC title/abstract co-mentions
    publications_recent: int | None = None   # same, last 3 years
    pubmed: int | None = None                # PubMed [tiab] co-mentions
    preprints: int | None = None
    preprints_recent: int | None = None
    trials: int | None = None
    trials_stopped: int | None = None
    patents: int | None = None
    max_clinical_stage: str | None = None    # for this disease (or its subtypes)
    ot_literature_score: float = 0.0
    parent_publications: int | None = None   # co-mentions with the closest parent disease
    parent_disease: str | None = None
    publications_prior: int | None = None    # co-mentions 3–6 years ago (trend baseline)
    disease_publications: int | None = None  # all papers on the disease (title/abstract)
    gene_publications: int | None = None     # all papers on the gene (title/abstract)
    relative_share: float | None = None      # co-mentions / disease papers
    trend: str | None = None                 # rising | stable | declining | sparse (relative to the field)
    trend_ratio: float | None = None         # pair growth ÷ disease-field growth, last 3 y vs the 3 y before


class TractabilityMetrics(BaseModel):
    ot_buckets: dict[str, list[str]] = Field(default_factory=dict)
    chembl_target_id: str | None = None
    potent_compounds: int | None = None  # distinct molecules with pChEMBL >= 6
    best_pchembl: float | None = None
    best_compounds: list[str] = Field(default_factory=list)
    mechanisms: list[str] = Field(default_factory=list)
    pdb_structures: int | None = None
    pdb_with_ligand: int | None = None
    best_resolution: float | None = None
    high_quality_probes: int = 0


class ScoreBreakdown(BaseModel):
    evidence: float = 0.0       # strength of causal evidence (0..1)
    attention: float = 0.0      # how studied the pair already is (0..1)
    gap: float = 0.0            # 1 - attention
    tractability: float = 0.0
    safety_risk: float = 0.0
    direction_confidence: float = 0.0
    opportunity: float = 0.0
    notes: list[str] = Field(default_factory=list)


class ExpressionSummary(BaseModel):
    """Baseline expression of the target in disease-relevant tissues (Open Targets: GTEx, Tabula Sapiens, PRIDE)."""

    keywords: list[str] = Field(default_factory=list)   # disease-relevant tissue / cell keywords
    keyword_source: str = "none"                        # name | qtl | area | none
    status: str | None = None                           # expressed | low | absent | None (unknown)
    relevant: list[dict] = Field(default_factory=list)  # best rows in relevant tissues {source,tissue,cell,value,unit}
    relevant_gtex_tpm: float | None = None
    top_tissue: str | None = None                       # GTEx tissue with the highest TPM
    top_cells: list[dict] = Field(default_factory=list)       # highest single-cell expression
    breadth: float | None = None                        # share of GTEx tissues with TPM >= 5


class LocusSummary(BaseModel):
    """GWAS loci of the pair: competing genes (L2G) and QTL colocalisation (effect direction)."""

    loci: int = 0
    target_l2g: float | None = None
    competitors: list[str] = Field(default_factory=list)
    coloc: list[dict] = Field(default_factory=list)  # type, tissue, strength (H4), sign (+1 risk allele raises expression)
    splice_tissues: list[str] = Field(default_factory=list)
    other_coloc_genes: list[str] = Field(default_factory=list)
    opposite_signs: bool = False
    conflict: bool = False
    supported: bool = False


class Opportunity(BaseModel):
    target: TargetInfo
    disease: DiseaseInfo
    association: Association
    scores: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    attention: AttentionMetrics = Field(default_factory=AttentionMetrics)
    tractability: TractabilityMetrics = Field(default_factory=TractabilityMetrics)
    direction: DirectionAssessment = Field(default_factory=DirectionAssessment)
    evidence: list[Evidence] = Field(default_factory=list)
    locus_neighbors: list[str] = Field(default_factory=list)  # other GWAS-linked genes within ±500 kb
    locus: LocusSummary | None = None
    expression: ExpressionSummary | None = None
    enriched: bool = False
    warnings: list[str] = Field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.target.ensembl_id}|{self.disease.id}"

    def evidence_by_id(self) -> dict[str, Evidence]:
        return {e.id: e for e in self.evidence}


class AlternativeExplanation(BaseModel):
    title: str
    explanation: str
    plausibility: Literal["high", "moderate", "low"] = "moderate"
    how_to_test: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class Experiment(BaseModel):
    title: str
    design: str
    expected_if_true: str = ""
    expected_if_false: str = ""
    priority: Literal["high", "medium", "low"] = "medium"


class ConfidenceAssessment(BaseModel):
    level: ConfidenceLevel
    score: float = Field(ge=0.0, le=1.0)
    rationale: str = ""
    upgrades: list[str] = Field(default_factory=list)
    downgrades: list[str] = Field(default_factory=list)


class EvidenceAppraisal(BaseModel):
    evidence_id: str
    interpretation: str
    reliability_comment: str = ""


class Hypothesis(BaseModel):
    id: str
    title: str
    statement: str
    target_symbol: str
    target_id: str
    disease_name: str
    disease_id: str
    modulation: Modulation
    mechanism_rationale: str
    supporting: list[EvidenceAppraisal] = Field(default_factory=list)
    contradicting: list[EvidenceAppraisal] = Field(default_factory=list)
    confidence: ConfidenceAssessment
    alternative_explanations: list[AlternativeExplanation] = Field(default_factory=list)
    key_uncertainties: list[str] = Field(default_factory=list)
    proposed_experiments: list[Experiment] = Field(default_factory=list)
    safety_considerations: list[str] = Field(default_factory=list)
    competitive_landscape: str = ""
    novelty_assessment: str = ""
    generated_by: str = "rules"
    language: str = "lt"
    ungrounded_citations: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
