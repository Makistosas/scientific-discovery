"""Whole-platform scan of Open Targets bulk data (Parquet), without per-disease API calls.

``download`` fetches the four datasets the scan needs for one release (≈ 0.9 GB for 26.09):

* ``association_by_datatype_direct`` – direct target–disease association score per datatype;
* ``target`` – symbols and biotypes;
* ``disease`` – names, therapeutic areas, ancestors/descendants;
* ``clinical_target`` – drug–target–indication rows with the maximum clinical stage.

``scan`` computes, for every direct target–disease pair, the same causal evidence strength as the pipeline
(noisy-OR over the causal datatypes, clinical and literature excluded) and an attention *proxy* (Open Targets
literature score and the highest clinical stage for the disease or its subtypes), keeps protein-coding targets
and specific diseases, and ranks by evidence × (1 − attention). The aggregation is vectorised with PyArrow
(``pip install -e ".[scan]"``). The top pairs can then be enriched with the full pipeline (papers, trials,
patents, direction, tractability, safety) – the scan only decides where to look.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .analysis.scoring import CAUSAL_WEIGHTS
from .sources.opentargets import CLINICAL_STAGE_RANK

FTP = "{scheme}://ftp.ebi.ac.uk/pub/databases/opentargets/platform/{release}/output/{dataset}/"
DATASETS = ("association_by_datatype_direct", "target", "disease", "clinical_target")
DISEASE_PREFIXES = ("EFO_", "MONDO_", "Orphanet_", "DOID_")
_STAGE = {**CLINICAL_STAGE_RANK, "WITHDRAWAL": 3.0, "PREAPPROVAL": 3.5}

Progress = Callable[[str], None]


def _require_pyarrow() -> None:
    import importlib.util

    if importlib.util.find_spec("pyarrow") is None:  # pragma: no cover - depends on the environment
        raise RuntimeError('The scan needs PyArrow: pip install -e ".[scan]"')


# --------------------------------------------------------------------------- download
def list_files(http_get_text: Callable[[str], str], release: str, dataset: str, scheme: str = "https") -> list[str]:
    url = FTP.format(scheme=scheme, release=release, dataset=dataset)
    html = http_get_text(url)
    return [url + name for name in dict.fromkeys(re.findall(r'href="([^"?/][^"]*\.parquet)"', html))]


def download(release: str, out_dir: Path, *, session=None, progress: Progress | None = None,
             datasets: tuple[str, ...] = DATASETS) -> Path:
    """Download the scan datasets for ``release`` into ``out_dir/<release>/<dataset>/`` (resumable: complete
    files are skipped). Tries HTTPS first and falls back to HTTP, which some networks allow when HTTPS to the
    EBI FTP server is blocked; Parquet files are checked to be readable either way."""
    import requests

    _require_pyarrow()
    import pyarrow.parquet as pq

    progress = progress or (lambda m: None)
    session = session or requests.Session()
    root = Path(out_dir).expanduser() / release
    scheme = None
    for dataset in datasets:
        files = []
        for sch in ([scheme] if scheme else ["https", "http"]):
            try:
                files = list_files(lambda u: _get(session, u).text, release, dataset, sch)
                scheme = sch
                break
            except Exception as exc:
                progress(f"  {sch} listing failed for {dataset}: {str(exc)[:120]}")
        if not files:
            raise RuntimeError(f"No Parquet files found for {dataset} in release {release}")
        target_dir = root / dataset
        target_dir.mkdir(parents=True, exist_ok=True)
        for i, url in enumerate(files, 1):
            path = target_dir / url.rsplit("/", 1)[1]
            if path.exists() and _readable(pq, path):
                continue
            progress(f"  {dataset} {i}/{len(files)}: {path.name}")
            tmp = path.with_suffix(path.suffix + ".part")
            with _get(session, url, stream=True) as r, open(tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
            if not _readable(pq, tmp):
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"Downloaded file is not valid Parquet: {url}")
            tmp.replace(path)
    (root / "manifest.json").write_text(json.dumps(
        {"release": release, "datasets": list(datasets), "scheme": scheme,
         "downloaded": datetime.now(timezone.utc).isoformat(timespec="seconds")}, indent=1), encoding="utf-8")
    return root


def _get(session, url: str, stream: bool = False):
    r = session.get(url, timeout=300, stream=stream)
    r.raise_for_status()
    return r


def _readable(pq, path: Path) -> bool:
    try:
        pq.ParquetFile(str(path)).metadata
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- scan
@dataclass
class ScanRow:
    target_id: str
    symbol: str
    disease_id: str
    disease: str
    therapeutic_areas: list[str]
    evidence: float
    literature: float
    max_stage: str | None
    attention: float
    score: float
    datatypes: dict[str, float]


def _diseases(root: Path):
    import pyarrow.parquet as pq

    t = pq.read_table(root / "disease", columns=["id", "name", "therapeuticAreas", "ancestors", "descendants",
                                                 "ontology"])
    names, areas, ancestors, ok = {}, {}, {}, set()
    for r in t.to_pylist():
        names[r["id"]] = r["name"]
        areas[r["id"]] = r["therapeuticAreas"] or []
        ancestors[r["id"]] = r["ancestors"] or []
        is_ta = bool((r.get("ontology") or {}).get("isTherapeuticArea"))
        if r["id"].startswith(DISEASE_PREFIXES) and not is_ta and len(r["descendants"] or []) <= 150:
            ok.add(r["id"])
    return names, areas, ancestors, ok


def _targets(root: Path):
    import pyarrow.parquet as pq

    t = pq.read_table(root / "target", columns=["id", "approvedSymbol", "biotype"])
    return {i: s for i, s, b in zip(t["id"].to_pylist(), t["approvedSymbol"].to_pylist(), t["biotype"].to_pylist())
            if b == "protein_coding"}


def _clinical(root: Path, ancestors: dict[str, list[str]]) -> dict[tuple[str, str], str]:
    """(target, disease) -> highest stage, propagated from each indication to its ancestors."""
    import pyarrow.parquet as pq

    best: dict[tuple[str, str], str] = {}
    for r in pq.read_table(root / "clinical_target", columns=["targetId", "diseases", "maxClinicalStage"]).to_pylist():
        stage = r["maxClinicalStage"]
        for d in r["diseases"] or []:
            if not d or not d.get("diseaseId"):
                continue
            for did in [d["diseaseId"], *ancestors.get(d["diseaseId"], [])]:
                key = (r["targetId"], did)
                if _STAGE.get(stage or "", 0) > _STAGE.get(best.get(key) or "", 0):
                    best[key] = stage
    return best


def _aggregate(root: Path, weights: dict[str, float], progress: Progress):
    """Per pair: log Π(1 − w·s) over causal datatypes, literature score, and the causal scores themselves."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    keep = list(weights) + ["literature"]
    w_keys = pa.array(list(weights))
    w_vals = pa.array([float(v) for v in weights.values()])
    parts = []
    files = sorted((root / "association_by_datatype_direct").glob("*.parquet"))
    for i, f in enumerate(files, 1):
        progress(f"  aggregating {i}/{len(files)}")
        t = pq.read_table(f, columns=["diseaseId", "targetId", "aggregationValue", "associationScore"])
        t = t.filter(pc.is_in(t["aggregationValue"], value_set=pa.array(keep)))
        idx = pc.index_in(t["aggregationValue"], value_set=w_keys)
        w = pc.fill_null(pc.take(w_vals, idx), 0.0)
        s = pc.min_element_wise(pc.max_element_wise(t["associationScore"], 0.0), 1.0)
        log1m = pc.ln(pc.max_element_wise(pc.subtract(1.0, pc.multiply(w, s)), 1e-12))
        lit = pc.if_else(pc.equal(t["aggregationValue"], "literature"), s, 0.0)
        parts.append(pa.table({"diseaseId": t["diseaseId"], "targetId": t["targetId"], "dt": t["aggregationValue"],
                               "s": s, "log1m": log1m, "lit": lit}))
    full = pa.concat_tables(parts)
    agg = full.group_by(["diseaseId", "targetId"]).aggregate([("log1m", "sum"), ("lit", "max")])
    return full, agg


def run_scan(root: Path, *, min_evidence: float = 0.4, top: int = 200, max_attention: float = 0.5,
             weights: dict[str, float] | None = None, progress: Progress | None = None) -> list[ScanRow]:
    _require_pyarrow()
    import pyarrow.compute as pc

    progress = progress or (lambda m: None)
    root = Path(root).expanduser()
    weights = weights or dict(CAUSAL_WEIGHTS)
    progress("reading diseases, targets, clinical precedence")
    names, areas, ancestors, ok_diseases = _diseases(root)
    targets = _targets(root)
    clinical = _clinical(root, ancestors)
    full, agg = _aggregate(root, weights, progress)

    evidence = pc.subtract(1.0, pc.exp(agg["log1m_sum"]))
    mask = pc.greater_equal(evidence, min_evidence)
    agg = agg.filter(mask)
    evidence = evidence.filter(mask)
    rows: list[ScanRow] = []
    for d, t, e, lit in zip(agg["diseaseId"].to_pylist(), agg["targetId"].to_pylist(), evidence.to_pylist(),
                            agg["lit_max"].to_pylist()):
        if d not in ok_diseases or t not in targets:
            continue
        stage = clinical.get((t, d))
        a_clin = _STAGE.get(stage or "", 0.0) / 4.0
        attention = 1.0 - (1.0 - a_clin) * (1.0 - (lit or 0.0))
        if attention > max_attention:
            continue
        rows.append(ScanRow(target_id=t, symbol=targets[t], disease_id=d, disease=names.get(d, d),
                            therapeutic_areas=[names.get(a, a) for a in areas.get(d, [])],
                            evidence=round(e, 4), literature=round(lit or 0.0, 4), max_stage=stage,
                            attention=round(attention, 4), score=round(e * (1.0 - attention), 4), datatypes={}))
    rows.sort(key=lambda r: -r.score)
    rows = rows[:top]
    # datatype breakdown for the kept pairs only
    want = {(r.disease_id, r.target_id): r for r in rows}
    for d, t, dt, s in zip(full["diseaseId"].to_pylist(), full["targetId"].to_pylist(), full["dt"].to_pylist(),
                           full["s"].to_pylist()):
        r = want.get((d, t))
        if r is not None and dt in weights:
            r.datatypes[dt] = round(s, 3)
    progress(f"{len(rows)} pairs kept")
    return rows


def write_results(rows: list[ScanRow], out: Path, release: str) -> dict[str, Path]:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    paths = {"csv": out / "scan.csv", "md": out / "scan.md", "json": out / "scan.json"}
    with open(paths["csv"], "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "symbol", "target_id", "disease", "disease_id", "score", "evidence", "attention",
                    "literature", "max_stage", "therapeutic_areas", "datatypes"])
        for i, r in enumerate(rows, 1):
            w.writerow([i, r.symbol, r.target_id, r.disease, r.disease_id, r.score, r.evidence, r.attention,
                        r.literature, r.max_stage or "", "; ".join(r.therapeutic_areas),
                        "; ".join(f"{k}={v}" for k, v in sorted(r.datatypes.items(), key=lambda kv: -kv[1]))])
    paths["json"].write_text(json.dumps({"release": release, "generated": datetime.now(timezone.utc).isoformat(
        timespec="seconds"), "rows": [r.__dict__ for r in rows]}, indent=1), encoding="utf-8")
    lines = [f"# Open Targets {release} – whole-platform scan", "",
             "Ranked by causal evidence × (1 − attention proxy). Attention proxy = Open Targets literature score "
             "and highest clinical stage for the disease or its subtypes. Enrich the top pairs with "
             "`discovery-agent hypothesis -t SYMBOL -d DISEASE_ID` before trusting them.", "",
             "| # | Target | Disease | Score | Evidence | Attention | Stage | Main evidence |", "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        main = ", ".join(f"{k} {v:.2f}" for k, v in sorted(r.datatypes.items(), key=lambda kv: -kv[1])[:3])
        lines.append(f"| {i} | {r.symbol} | {r.disease} (`{r.disease_id}`) | {r.score:.3f} | {r.evidence:.2f} | "
                     f"{r.attention:.2f} | {r.max_stage or '–'} | {main} |")
    paths["md"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return paths

