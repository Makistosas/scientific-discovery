"""Agentic investigator: Claude explores the data sources with tools before the
hypothesis is written.

The agent gets the dossier and a toolbox over the same public APIs the
pipeline uses (Europe PMC search + abstracts, Open Targets evidence and target
profiles, ChEMBL, ClinicalTrials.gov). It records important findings with
``record_evidence``; each recorded finding must cite sources that a tool
actually returned in this session, otherwise it is rejected. Recorded findings
become new evidence items (type ``agent_finding``) in the dossier.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError

from ..config import Settings
from ..models import Aspect, Evidence, EvidenceType, Opportunity, Reference, Reliability, Stance
from ..sources import Sources
from ..sources.opentargets import EVIDENCE_GROUPS
from .client import (LLMError, LLMRefusal, check_stop, friendly_api_error, make_client, request_kwargs,
                     sanitize_for_echo, text_of)
from .dossier import dossier_json

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the investigation module of an automated scientific discovery agent. You are given \
a target–disease opportunity that looks under-explored, plus tools over public biomedical databases. \
Investigate it like a sceptical scientist before a hypothesis is written.

Priorities, in order:
1. Look for negative or failed results and for reasons why the pair received little attention \
(stopped trials, toxicity, contradictory genetics, technical obstacles).
2. Verify the therapeutic direction of modulation (loss- vs gain-of-function, protective variants, drug mechanisms).
3. Find mechanistic links between the target's biology and the disease (read abstracts, not just titles).
4. Check related targets in the same pathway that are already in the clinic, and tractability/safety signals.

Rules:
- Only rely on what the tools return. Record each important finding with record_evidence, citing references \
(URLs or IDs) exactly as the tools returned them; findings with unseen references are rejected.
- Grade reliability honestly: A (robust human genetics / approved drugs), B (curated or replicated but \
indirect), C (models, correlative, single study), D (text-mining, preprints, patents, abstracts only).
- Be efficient: about {max_tools} tool calls in total. Run independent searches in parallel.
- When done, reply with concise investigation notes (what you checked, what you found, what remains unknown)."""


class _RefIn(BaseModel):
    label: str
    url: str


class SearchLiteratureIn(BaseModel):
    query: str
    source: str = "articles"
    since: str = ""
    max_results: int = 8


class ReadAbstractsIn(BaseModel):
    ids: list[str] = Field(max_length=8)


class OTEvidenceIn(BaseModel):
    group: str


class GeneIn(BaseModel):
    gene: str


class TrialsIn(BaseModel):
    condition: str
    term: str


class RecordEvidenceIn(BaseModel):
    summary: str
    source: str
    aspect: Aspect
    stance: Stance
    reliability: Reliability
    reliability_rationale: str
    references: list[_RefIn] = Field(min_length=1)


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "name": name, "description": description, "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "properties": properties,
                         "required": required or list(properties)},
    }


TOOLS = [
    _tool("search_literature",
          "Search Europe PMC (covers PubMed/PMC articles, bioRxiv/medRxiv preprints and EPO patents). "
          "Supports Europe PMC syntax, e.g. TITLE:\"PARN\" AND ABSTRACT:\"fibrosis\". Returns hit count and records "
          "with ids, titles, years and URLs.",
          {"query": {"type": "string", "description": "Europe PMC query string."},
           "source": {"type": "string", "enum": ["articles", "preprints", "patents"]},
           "since": {"type": "string", "description": "Earliest first-publication date YYYY-MM-DD, or empty."},
           "max_results": {"type": "integer", "description": "1-15"}}),
    _tool("read_abstracts",
          "Fetch titles and abstracts for up to 8 records by PMID or Europe PMC id (e.g. PPR123456).",
          {"ids": {"type": "array", "items": {"type": "string"}}}),
    _tool("get_open_targets_evidence",
          "Open Targets evidence rows for the current target–disease pair, for one evidence group. Includes "
          "direction of effect (directionOnTarget LoF/GoF, directionOnTrait risk/protect), confidence, p-values, "
          "drugs and stop reasons.",
          {"group": {"type": "string", "enum": sorted(EVIDENCE_GROUPS)}}),
    _tool("get_target_profile",
          "Open Targets profile of any human gene: function, tractability, safety liabilities, genetic constraint, "
          "known drugs with indications. Use it for the current target or pathway neighbours.",
          {"gene": {"type": "string", "description": "HGNC symbol or Ensembl id."}}),
    _tool("get_chembl_profile",
          "ChEMBL bioactivity summary for a human gene: number of potent compounds, best compounds and known drug "
          "mechanisms (action types, max phase).",
          {"gene": {"type": "string"}}),
    _tool("search_clinical_trials",
          "Search ClinicalTrials.gov interventional studies by condition and free-text term (gene, drug or "
          "mechanism). Returns status, phases and why-stopped text.",
          {"condition": {"type": "string"}, "term": {"type": "string"}}),
    _tool("record_evidence",
          "Record one important finding as an evidence item for the hypothesis dossier. References must be URLs or "
          "ids exactly as returned by previous tool calls.",
          {"summary": {"type": "string"}, "source": {"type": "string", "description": "e.g. 'Europe PMC abstract'"},
           "aspect": {"type": "string", "enum": [a.value for a in Aspect]},
           "stance": {"type": "string", "enum": [s.value for s in Stance]},
           "reliability": {"type": "string", "enum": [r.value for r in Reliability]},
           "reliability_rationale": {"type": "string"},
           "references": {"type": "array", "items": {
               "type": "object", "additionalProperties": False, "required": ["label", "url"],
               "properties": {"label": {"type": "string"}, "url": {"type": "string"}}}}}),
]

_ID_PATTERNS = [re.compile(p) for p in (r"\bPMC\d+\b", r"\bPPR\d+\b", r"\bNCT\d{8}\b", r"\bCHEMBL\d+\b",
                                         r"\b10\.\d{4,9}/[^\s\"'<>]+", r"\b\d{6,9}\b")]


def _ids_in(text: str) -> set[str]:
    out: set[str] = set()
    for p in _ID_PATTERNS:
        out.update(m.group(0).rstrip(".,;)") for m in p.finditer(text))
    return out


class InvestigationResult(BaseModel):
    notes: str = ""
    new_evidence: list[Evidence] = Field(default_factory=list)
    tool_calls: int = 0
    turns: int = 0
    stop_reason: str | None = None
    rejected_records: int = 0


class Investigator:
    def __init__(self, settings: Settings, sources: Sources, client: Any | None = None, *, max_turns: int = 12,
                 max_tool_calls: int = 20, use_fallbacks: bool = True, progress: Callable[[str], None] | None = None):
        self.settings = settings
        self.sources = sources
        self.client = client
        self.max_turns = max_turns
        self.max_tool_calls = max_tool_calls
        self.use_fallbacks = use_fallbacks
        self._progress = progress or (lambda m: log.info(m))

    # ------------------------------------------------------------------
    def investigate(self, opp: Opportunity) -> InvestigationResult:
        client = self.client or make_client(self.settings)
        session = _Session(self.sources, opp)
        system = SYSTEM_PROMPT.format(max_tools=self.max_tool_calls)
        messages: list[dict] = [{"role": "user", "content": (
            "Opportunity dossier (JSON):\n<dossier>\n" + dossier_json(opp) + "\n</dossier>\n\n"
            f"Current pair: target {opp.target.symbol} ({opp.target.ensembl_id}), disease {opp.disease.name} "
            f"({opp.disease.id}). Investigate it now.")}]
        kwargs = request_kwargs(self.settings, use_fallbacks=self.use_fallbacks)
        kwargs["output_config"] = {"effort": self.settings.effort}
        result = InvestigationResult()

        for turn in range(1, self.max_turns + 1):
            result.turns = turn
            try:
                response = client.beta.messages.create(
                    max_tokens=16000, system=system, tools=TOOLS, messages=messages,
                    cache_control={"type": "ephemeral"}, **kwargs)
            except Exception as exc:
                raise friendly_api_error(exc) from exc
            result.stop_reason = response.stop_reason
            content = sanitize_for_echo(list(response.content))

            if response.stop_reason == "refusal":
                try:
                    check_stop(response)
                except LLMRefusal as exc:
                    result.notes = f"[investigation stopped: {exc}]"
                break
            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": content})
                continue
            tool_uses = [b for b in content if getattr(b, "type", None) == "tool_use"]
            if response.stop_reason == "max_tokens" and tool_uses:
                # a tool input may be truncated: do not run it
                result.notes = text_of(response) or "[investigation truncated at max_tokens]"
                break
            if not tool_uses:
                result.notes = text_of(response)
                break

            messages.append({"role": "assistant", "content": content})
            budget_left = self.max_tool_calls - result.tool_calls
            with ThreadPoolExecutor(max_workers=min(4, len(tool_uses))) as pool:
                outputs = list(pool.map(lambda b: session.run(b.name, b.input) if budget_left > 0
                                        else (json.dumps({"error": "Tool budget exhausted. Write your notes now."}), True),
                                        tool_uses))
            result.tool_calls += len(tool_uses)
            for b in tool_uses:
                self._progress(f"    ↳ {b.name}({_short(b.input)})")
            tool_results = [{"type": "tool_result", "tool_use_id": b.id, "content": out, **({"is_error": True} if err else {})}
                            for b, (out, err) in zip(tool_uses, outputs)]
            if result.tool_calls >= self.max_tool_calls:
                tool_results.append({"type": "text", "text": "Tool budget reached. Reply with your investigation notes now."})
            messages.append({"role": "user", "content": tool_results})
        else:
            result.notes = result.notes or "[investigation stopped: turn limit]"

        result.new_evidence = session.recorded
        result.rejected_records = session.rejected
        return result

    def investigate_and_merge(self, opp: Opportunity) -> InvestigationResult:
        result = self.investigate(opp)
        start = len(opp.evidence)
        for i, e in enumerate(result.new_evidence, start + 1):
            e.id = f"E{i}"
            opp.evidence.append(e)
        return result


def _short(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False)
    return text if len(text) < 90 else text[:87] + "…"


class _Session:
    """Executes tools for one investigation and remembers which sources were seen."""

    def __init__(self, sources: Sources, opp: Opportunity):
        self.s = sources
        self.opp = opp
        self.seen_ids: set[str] = set()
        self.seen_urls: set[str] = set()
        self.recorded: list[Evidence] = []
        self.rejected = 0
        # sources already in the dossier count as seen
        for e in opp.evidence:
            for r in e.references:
                self._remember_ref(r.label, r.url)

    def _remember_ref(self, label: str | None, url: str | None) -> None:
        if url:
            self.seen_urls.add(url.rstrip("/"))
            self.seen_ids.update(_ids_in(url))
        if label:
            self.seen_ids.update(_ids_in(label))

    def _remember(self, payload: Any) -> None:
        text = json.dumps(payload, ensure_ascii=False, default=str)
        self.seen_ids.update(_ids_in(text))
        self.seen_urls.update(u.rstrip("/") for u in re.findall(r"https?://[^\s\"']+", text))

    def run(self, name: str, raw: dict) -> tuple[str, bool]:
        handlers = {
            "search_literature": (SearchLiteratureIn, self.search_literature),
            "read_abstracts": (ReadAbstractsIn, self.read_abstracts),
            "get_open_targets_evidence": (OTEvidenceIn, self.ot_evidence),
            "get_target_profile": (GeneIn, self.target_profile),
            "get_chembl_profile": (GeneIn, self.chembl_profile),
            "search_clinical_trials": (TrialsIn, self.trials),
            "record_evidence": (RecordEvidenceIn, self.record_evidence),
        }
        if name not in handlers:
            return json.dumps({"error": f"unknown tool {name}"}), True
        model, fn = handlers[name]
        try:
            args = model.model_validate(raw)
        except ValidationError as exc:
            return json.dumps({"error": "invalid input", "details": exc.errors(include_url=False)}, default=str), True
        try:
            payload = fn(args)
        except Exception as exc:  # surface tool failures to the model instead of crashing
            log.warning("tool %s failed: %s", name, exc)
            return json.dumps({"error": str(exc)[:500]}), True
        if name != "record_evidence":
            self._remember(payload)
        return json.dumps(payload, ensure_ascii=False, default=str), bool(payload.get("error")) if isinstance(payload, dict) else False

    # -- tools ------------------------------------------------------------
    def search_literature(self, a: SearchLiteratureIn) -> dict:
        src = {"articles": " AND (SRC:MED OR SRC:PMC)", "preprints": " AND SRC:PPR", "patents": " AND SRC:PAT"}
        q = f"({a.query}){src.get(a.source, '')}"
        if a.since and re.match(r"^\d{4}-\d{2}-\d{2}$", a.since):
            q += f" AND FIRST_PDATE:[{a.since} TO 3000-12-31]"
        res = self.s.europepmc.search(q, page_size=max(1, min(a.max_results, 15)),
                                      result_type="core" if a.source == "preprints" else "lite")
        return {"query": q, "hit_count": res.hit_count, "records": [
            {"id": x.id, "source": x.source, "pmid": x.pmid, "doi": x.doi, "title": x.title, "year": x.year,
             "venue": x.journal or x.publisher, "url": x.url} for x in res.articles]}

    def read_abstracts(self, a: ReadAbstractsIn) -> dict:
        clean = [i.strip() for i in a.ids if re.match(r"^(PPR|PMC)?\d+$", i.strip())]
        if not clean:
            return {"error": "Provide PMIDs or Europe PMC ids such as PPR123456."}
        q = " OR ".join(f"EXT_ID:{i}" for i in clean)
        data = self.s.europepmc.http.get_json(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            {"query": q, "format": "json", "resultType": "core", "pageSize": len(clean)})
        out = []
        for r in (data.get("resultList") or {}).get("result", []):
            pmid = r.get("pmid")
            out.append({"id": r.get("id"), "pmid": pmid, "doi": r.get("doi"), "title": r.get("title"),
                        "year": r.get("pubYear"), "abstract": r.get("abstractText", "")[:4000],
                        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else
                        f"https://europepmc.org/article/{r.get('source')}/{r.get('id')}"})
        return {"records": out}

    def ot_evidence(self, a: OTEvidenceIn) -> dict:
        if a.group not in EVIDENCE_GROUPS:
            return {"error": f"group must be one of {sorted(EVIDENCE_GROUPS)}"}
        rows = self.s.opentargets.pair_evidence(self.opp.disease.id, self.opp.target.ensembl_id,
                                                {a.group: EVIDENCE_GROUPS[a.group]})[a.group]
        keep = ("datasourceId", "score", "directionOnTarget", "directionOnTrait", "confidence", "clinicalSignificances",
                "clinicalStage", "clinicalReportId", "trialWhyStopped", "trialStopReasonCategories", "literature",
                "studyId", "pValueMantissa", "pValueExponent", "oddsRatio", "beta", "variantRsId", "cohortShortName",
                "contrast", "log2FoldChangeValue")
        slim = []
        for r in rows[:20]:
            item = {k: r[k] for k in keep if r.get(k) not in (None, [], "")}
            if r.get("drug"):
                item["drug"] = r["drug"].get("name")
            if r.get("disease"):
                item["disease"] = r["disease"].get("name")
            slim.append(item)
        return {"group": a.group, "n_rows": len(rows), "rows": slim}

    def target_profile(self, a: GeneIn) -> dict:
        tid = self.s.opentargets.resolve_target(a.gene)
        t = self.s.opentargets.target(tid)
        return {"symbol": t.symbol, "ensembl_id": t.ensembl_id, "name": t.name, "function": t.function,
                "uniprot_id": t.uniprot_id, "classes": t.target_classes, "tractability": t.tractability,
                "safety_liabilities": t.safety_events, "LOEUF": t.lof_oe_upper, "core_essential": t.is_essential,
                "known_drugs": [{"name": d.name, "stage": d.max_stage, "action": d.action_types,
                                 "indications": d.disease_names[:6]} for d in t.known_drugs[:12]],
                "url": f"https://platform.opentargets.org/target/{t.ensembl_id}"}

    def chembl_profile(self, a: GeneIn) -> dict:
        tid = self.s.opentargets.resolve_target(a.gene)
        t = self.s.opentargets.target(tid)
        if not t.uniprot_id:
            return {"error": f"No UniProt accession for {a.gene}"}
        c = self.s.chembl.summary(t.uniprot_id)
        return {"gene": t.symbol, "target_chembl_id": c.target_chembl_id, "url": c.url,
                "potent_compounds": c.potent_compounds, "very_potent_compounds": c.very_potent_compounds,
                "best_pchembl": c.best_pchembl, "best_compounds": c.best_compounds, "mechanisms": c.mechanisms[:15]}

    def trials(self, a: TrialsIn) -> dict:
        res = self.s.trials.search(condition=a.condition, term=a.term, page_size=15)
        return {"total": res.total, "trials": [
            {"nct_id": x.nct_id, "title": x.title, "status": x.status, "phases": x.phases,
             "why_stopped": x.why_stopped, "interventions": x.interventions[:5], "sponsor": x.sponsor, "url": x.url}
            for x in res.trials]}

    def record_evidence(self, a: RecordEvidenceIn) -> dict:
        valid, invalid = [], []
        for ref in a.references:
            ids = _ids_in(ref.url) | _ids_in(ref.label)
            if ref.url.rstrip("/") in self.seen_urls or ids & self.seen_ids:
                valid.append(Reference(label=ref.label, url=ref.url))
            else:
                invalid.append(ref.url or ref.label)
        if not valid:
            self.rejected += 1
            return {"error": "Rejected: none of the references were returned by a tool in this session. "
                             f"Unseen: {invalid}. Cite URLs/ids exactly as the tools returned them."}
        evidence = Evidence(
            source=f"AI investigator · {a.source}", type=EvidenceType.AGENT_FINDING, aspect=a.aspect,
            summary=a.summary, stance=a.stance, reliability=a.reliability,
            reliability_rationale=a.reliability_rationale, peer_reviewed=a.reliability is not Reliability.D,
            references=valid,
        )
        self.recorded.append(evidence)
        return {"recorded": True, "index": len(self.recorded), "dropped_references": invalid}


__all__ = ["Investigator", "InvestigationResult", "TOOLS", "LLMError"]
