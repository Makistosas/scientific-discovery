"""Is the target expressed where the disease happens?

Open Targets baseline expression combines GTEx bulk RNA (TPM per tissue), Tabula Sapiens single-cell RNA
(pseudobulk CPM per cell type and tissue) and PRIDE proteomics. Disease-relevant tissues come, in order of
preference, from the disease name, from the tissues of QTL colocalisations at the GWAS locus, and from the
disease's therapeutic areas. A target that is barely expressed there may still act through another tissue
(immune cells, liver-derived circulating proteins), so low expression is an *alternative explanation*,
never a veto.
"""

from __future__ import annotations

import re

from ..models import DiseaseInfo, ExpressionSummary, LocusSummary

GTEX_EXPRESSED_TPM = 5.0
SC_EXPRESSED_CPM = 10.0
# Single-cell and bulk data carry ambient / blood contamination (albumin "in lung"): a relevant value must also
# reach this fraction of the gene's own maximum in the same source.
MIN_FRACTION_OF_MAX = 0.02

# word pattern in the disease name -> tissue / cell keywords
_NAME_TISSUES: list[tuple[str, list[str]]] = [
    (r"pulmon|lung|bronch|asthma|respirat|alveol", ["lung", "bronch", "alveol"]),
    (r"hepat|liver|cholang|biliar|cirrho", ["liver", "hepatocyte", "bile duct"]),
    (r"renal|kidney|nephr|glomerul", ["kidney", "renal", "glomerul", "podocyte"]),
    (r"cardi|heart|coronary|myocard|atrial|ventric", ["heart", "cardiac", "cardiomyocyte", "coronary"]),
    (r"arter|aort|vascul|hypertens|athero", ["artery", "aorta", "endothelial", "smooth muscle"]),
    (r"colitis|crohn|bowel|colon|colorect|intestin|celiac|coeliac", ["colon", "intestin", "ileum", "enterocyte"]),
    (r"gastr|stomach|esophag|oesophag", ["stomach", "esophag", "gastric"]),
    (r"pancrea|diabet|insulin", ["pancrea", "islet", "beta cell", "liver", "adipose", "muscle"]),
    (r"skin|derm|psoria|eczema|melanom|acne|vitiligo|alopecia", ["skin", "keratinocyte", "melanocyte"]),
    (r"alzheimer|parkinson|dementia|neur|brain|cereb|schizo|epilep|sclerosis|ataxia|huntington|autism|bipolar|depress",
     ["brain", "cortex", "cerebell", "neuron", "astrocyte", "microglia", "oligodendrocyte", "spinal cord"]),
    (r"arthritis|joint|osteo|bone|cartilag|tendon", ["bone", "cartilage", "synovi", "chondrocyte", "osteoblast",
                                                     "fibroblast", "macrophage"]),
    (r"muscul|myopath|myositis|dystroph", ["muscle", "skeletal muscle", "myocyte"]),
    (r"thyroid", ["thyroid"]),
    (r"retin|macular|glaucoma|eye|ocular|cornea", ["eye", "retina", "cornea", "photoreceptor"]),
    (r"breast|mammary", ["breast", "mammary"]),
    (r"prostat", ["prostate"]),
    (r"ovar|uter|endometr", ["ovary", "uterus", "endometri"]),
    (r"lupus|immun|allerg|leuk|lymph|anemi|anaemi|blood|sickle|thalass", ["blood", "lymph node", "spleen",
                                                                       "bone marrow", "t cell", "b cell",
                                                                       "monocyte", "macrophage"]),
    (r"cholesterol|lipid|obes|metabol", ["liver", "adipose", "hepatocyte", "adipocyte"]),
]

_AREA_TISSUES: dict[str, list[str]] = {
    "respiratory or thoracic disease": ["lung"],
    "nervous system disease": ["brain", "neuron", "cortex"],
    "psychiatric disorder": ["brain", "neuron", "cortex"],
    "cardiovascular disease": ["heart", "artery", "aorta"],
    "digestive system disease": ["colon", "intestin", "stomach", "liver"],
    "gastrointestinal disease": ["colon", "intestin", "stomach"],
    "immune system disease": ["blood", "lymph node", "spleen", "t cell", "macrophage"],
    "integumentary system disease": ["skin"],
    "musculoskeletal or connective tissue disease": ["muscle", "bone", "fibroblast"],
    "endocrine system disease": ["pancrea", "thyroid", "adrenal", "pituitary"],
    "metabolic disease": ["liver", "adipose", "pancrea", "muscle"],
    "urinary system disease": ["kidney", "bladder"],
    "reproductive system or breast disease": ["breast", "uterus", "ovary", "testis", "prostate"],
    "visual system disease": ["eye", "retina"],
    "hematologic disease": ["blood", "bone marrow"],
}

# QTL colocalisation biosamples are often cell lines or generic cell types; keep the informative words
_COLOC_STOP = {"cells", "cell", "cultured", "transformed", "tissue", "-", "of", "the", "and", "primary", "naive",
               "stimulated", "unstimulated", "type"}


def disease_tissues(disease: DiseaseInfo, locus: LocusSummary | None = None) -> tuple[list[str], str]:
    """Keywords for disease-relevant tissues/cell types and where they came from (name, qtl, area)."""
    name = " ".join([disease.name, *disease.synonyms[:2]]).lower()
    for pattern, tissues in _NAME_TISSUES:
        if re.search(pattern, name):
            return tissues, "name"
    if locus and locus.coloc:
        words: list[str] = []
        for v in locus.coloc:
            for w in re.split(r"[\s,_/()]+", (v.get("tissue") or "").lower()):
                if len(w) > 3 and w not in _COLOC_STOP and w not in words:
                    words.append(w)
        if words:
            return words[:6], "qtl"
    out: list[str] = []
    for area in disease.therapeutic_areas:
        for t in _AREA_TISSUES.get(area.lower(), []):
            if t not in out:
                out.append(t)
    return out, "area" if out else "none"


def _matches(row: dict, keywords: list[str]) -> bool:
    text = f"{row.get('tissue') or ''} {row.get('cell') or ''}".lower()
    return any(k in text for k in keywords)


def _expressed(row: dict, source_max: dict[str, float] | None = None) -> bool:
    v = row.get("value") or 0.0
    if source_max and v < MIN_FRACTION_OF_MAX * source_max.get(row["source"], 0.0):
        return False
    if row["source"] == "gtex":
        return v >= GTEX_EXPRESSED_TPM
    if row["source"] == "tabula_sapiens":
        return v >= SC_EXPRESSED_CPM
    return v > 0


def assess_expression(rows: list[dict], disease: DiseaseInfo, locus: LocusSummary | None = None) -> ExpressionSummary:
    keywords, origin = disease_tissues(disease, locus)
    summary = ExpressionSummary(keywords=keywords, keyword_source=origin)
    if not rows:
        return summary
    gtex = [r for r in rows if r["source"] == "gtex"]
    sc = [r for r in rows if r["source"] == "tabula_sapiens" and r.get("cell")]
    if gtex:
        summary.breadth = round(sum(_expressed(r) for r in gtex) / len(gtex), 3)
        top = max(gtex, key=lambda r: r.get("value") or 0.0)
        summary.top_tissue = top.get("tissue")
    seen: set[str] = set()
    for r in sorted(sc, key=lambda r: (-(r.get("value") or 0.0), r.get("tissue") is None)):
        if r["cell"].lower() not in seen and len(summary.top_cells) < 3:
            seen.add(r["cell"].lower())
            summary.top_cells.append(_fmt(r))
    source_max: dict[str, float] = {}
    for r in rows:
        source_max[r["source"]] = max(source_max.get(r["source"], 0.0), r.get("value") or 0.0)
    if not keywords:
        return summary
    relevant = [r for r in rows if _matches(r, keywords)]
    summary.relevant = [_fmt(r) for r in sorted(relevant, key=lambda r: -(r.get("value") or 0.0))[:4]]
    summary.relevant_gtex_tpm = max((r.get("value") or 0.0 for r in relevant if r["source"] == "gtex"), default=None)
    if not relevant:
        summary.status = None  # the atlas has no matching tissue: say nothing rather than guess
    elif any(_expressed(r, source_max) for r in relevant):
        summary.status = "expressed"
    elif any(_expressed(r) for r in relevant) or any((r.get("value") or 0.0) > 0 for r in relevant):
        summary.status = "low"
    else:
        summary.status = "absent"
    return summary


def _fmt(r: dict) -> dict:
    return {"source": r["source"], "tissue": r.get("tissue"), "cell": r.get("cell"),
            "value": round(r.get("value") or 0.0, 1), "unit": r.get("unit")}


def describe(row: dict) -> str:
    where = " · ".join(x for x in (row.get("tissue"), row.get("cell")) if x)
    unit = {"gtex": "TPM", "tabula_sapiens": "CPM", "PRIDE": "ppb"}.get(row["source"], "")
    return f"{where} {row['value']:g} {unit}".strip()
