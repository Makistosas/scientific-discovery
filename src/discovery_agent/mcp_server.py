"""Local MCP server: use the discovery agent from Claude Desktop, Claude Code, Codex or any MCP client.

The *client's* model (for example Claude in Claude Desktop / Claude Code on your Pro/Max plan, or
Codex on a ChatGPT plan) does the reasoning, so no API key is needed. This server provides the data
and the memory:

* the deterministic discovery pipeline (Open Targets, Europe PMC, PubMed, bioRxiv, ChEMBL, UniProt,
  PDB, ClinicalTrials.gov, patents) with A–D evidence grading;
* investigation tools for literature, evidence, targets, compounds and trials;
* a local SQLite database (``~/.local/share/discovery-agent/discovery.sqlite`` by default) holding
  analysed dossiers, saved hypotheses, watchlist runs and alerts, plus Markdown reports on disk.

Run with ``discovery-agent mcp`` (stdio transport). ``discovery-agent mcp --print-config claude-desktop``
prints a ready-to-paste client configuration.
"""

# No `from __future__ import annotations` here: MCPServer reads the tool signatures (including the
# locally imported `Context`) at decoration time.
import hashlib
import itertools
import json
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from pydantic import Field

from . import __version__
from .agent import DiscoveryAgent
from .config import Settings
from .hypothesis import build_hypothesis
from .i18n import normalize_lang
from .llm.dossier import build_dossier
from .llm.investigator import TOOLS, ToolSession
from .models import Association, Opportunity
from .monitor.store import Store
from .report import ranking_table, render_hypothesis
from .report.markdown import evidence_basis
from .sources import Sources
from .sources.opentargets import OpenTargetsError

Progress = Callable[[str], None]
Format = Literal["markdown", "json"]

WATCHLIST_TEMPLATE = """\
# Watchlist for `discovery_run_watchlist` (edit freely).
language: lt            # lt | en
cache_ttl_hours: 12
watches:
  - name: ipf
    disease: idiopathic pulmonary fibrosis
    top_k: 8
    enrich_k: 12
"""

_PMID = re.compile(r"(?:PMID[:\s]*|pubmed\.ncbi\.nlm\.nih\.gov/)(\d{5,9})", re.I)
_NCT = re.compile(r"\bNCT\d{8}\b")
_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>()\[\]]+[^\s\"'<>()\[\].,;:])")
_EVIDENCE_ID = re.compile(r"\bE\d{1,3}\b")


class ServiceError(RuntimeError):
    """An error the calling model should see and can act on."""


def _slug(text: str, n: int = 60) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_")[:n] or "item"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class DiscoveryService:
    """Everything the MCP tools do, independent of MCP (unit-testable)."""

    def __init__(self, settings: Settings | None = None, *, sources: Sources | None = None,
                 store: Store | None = None):
        self.settings = settings or Settings.from_env()
        self.data_dir = Path(self.settings.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sources = sources or Sources.create(self.settings)
        self.db_path = self.data_dir / "discovery.sqlite"
        self.store = store or Store(self.db_path)

    # -- helpers ------------------------------------------------------------
    def _settings(self, language: str | None) -> Settings:
        return self.settings.with_overrides(language=normalize_lang(language) if language else None)

    def _agent(self, language: str | None, progress: Progress | None) -> DiscoveryAgent:
        return DiscoveryAgent(self._settings(language), sources=self.sources, progress=progress)

    def _remember(self, opp: Opportunity, lang: str) -> dict:
        draft = build_hypothesis(opp, lang)
        refs = sorted({r.url or r.label for e in opp.evidence for r in e.references if (r.url or r.label)})
        payload = {"key": opp.key, "symbol": opp.target.symbol, "target_id": opp.target.ensembl_id,
                   "disease": opp.disease.name, "disease_id": opp.disease.id,
                   "evidence_ids": [e.id for e in opp.evidence], "references": refs,
                   "dossier": build_dossier(opp, draft), "draft_markdown": render_hypothesis(draft, opp, lang)}
        self.store.save_dossier(opp.key, opp.target.symbol, opp.disease.name, payload)
        return payload

    def _resolve_key(self, target: str, disease: str) -> tuple[str, str, str]:
        ot = self.sources.opentargets
        try:
            d = ot.resolve_disease(disease)
            t = ot.target(ot.resolve_target(target))
        except OpenTargetsError as exc:
            raise ServiceError(f"{exc}. Use discovery_search_entities to find the right name or id.") from exc
        return f"{t.ensembl_id}|{d.id}", t.symbol, d.id

    # -- discovery ----------------------------------------------------------
    def find_opportunities(self, *, disease: str | None = None, target: str | None = None, top_k: int = 8,
                           enrich_k: int = 12, include_known: bool = False, language: str | None = None,
                           progress: Progress | None = None) -> dict:
        if bool(disease) == bool(target):
            raise ServiceError("Give exactly one of `disease` or `target`.")
        agent = self._agent(language, progress)
        kwargs = dict(top_k=top_k, enrich_k=max(enrich_k, top_k), include_known=include_known)
        try:
            run = agent.run_disease(disease, **kwargs) if disease else agent.run_target(target, **kwargs)
        except OpenTargetsError as exc:
            raise ServiceError(f"{exc}. Use discovery_search_entities to find the right name or id.") from exc
        out_dir = self.data_dir / "reports" / f"{_now()}_{_slug(disease or target)}"
        paths = agent.write_outputs(run, out_dir)
        for opp in run.result.opportunities:
            self._remember(opp, agent.lang)
        return {"query": disease or target, "mode": run.result.mode, "language": agent.lang,
                "candidates_considered": run.result.candidates_considered,
                "report_path": str(paths["report"]), "json_path": str(paths["json"]),
                "opportunities": [self._opp_row(i, o, agent.lang) for i, o in enumerate(run.result.opportunities, 1)],
                "_ranking_markdown": ranking_table(run.result.opportunities, agent.lang)}

    @staticmethod
    def _opp_row(rank: int, o: Opportunity, lang: str) -> dict:
        s, a = o.scores, o.attention
        return {"rank": rank, "target": o.target.symbol, "target_id": o.target.ensembl_id,
                "disease": o.disease.name, "disease_id": o.disease.id, "opportunity": s.opportunity,
                "evidence": s.evidence, "gap": s.gap, "tractability": s.tractability, "safety_risk": s.safety_risk,
                "direction": o.direction.modulation.value, "direction_confidence": o.direction.confidence,
                "basis": evidence_basis(o, lang), "publications": a.publications, "trials": a.trials,
                "max_clinical_stage": a.max_clinical_stage, "locus_neighbors": o.locus_neighbors,
                "warnings": o.warnings}

    def analyze_pair(self, target: str, disease: str, *, language: str | None = None,
                     progress: Progress | None = None) -> dict:
        agent = self._agent(language, progress)
        try:
            run = agent.run_pair(target, disease)
        except OpenTargetsError as exc:
            raise ServiceError(f"{exc}. Use discovery_search_entities to find the right name or id.") from exc
        opp = run.result.opportunities[0]
        payload = self._remember(opp, agent.lang)
        path = self.data_dir / "reports" / "pairs" / f"{_slug(opp.target.symbol)}__{_slug(opp.disease.id)}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload["draft_markdown"], encoding="utf-8")
        return {**payload, "report_path": str(path)}

    def search_entities(self, text: str, entity: str | None = None, limit: int = 10) -> list[dict]:
        entities = [entity] if entity else ["target", "disease", "drug"]
        return self.sources.opentargets.search(text, entities, size=limit)

    # -- investigation tools --------------------------------------------------
    def run_tool(self, name: str, args: dict, *, target: str | None = None, disease: str | None = None) -> dict:
        opp = None
        if target and disease:
            ot = self.sources.opentargets
            try:
                d = ot.resolve_disease(disease)
                t = ot.target(ot.resolve_target(target))
            except OpenTargetsError as exc:
                raise ServiceError(str(exc)) from exc
            opp = Opportunity(target=t, disease=d, association=Association(
                target_id=t.ensembl_id, symbol=t.symbol, disease_id=d.id, disease_name=d.name, score=0.0))
        out, is_error = ToolSession(self.sources, opp).run(name, args)
        data = json.loads(out)
        if is_error:
            raise ServiceError(data.get("error") if isinstance(data, dict) else out)
        return data

    # -- hypotheses -----------------------------------------------------------
    def verify_references(self, text: str, known: set[str] | None = None, *, online: bool = True,
                          limit: int = 25) -> dict:
        """Check that PMIDs, NCT ids and DOIs cited in `text` exist (dossier first, then online)."""
        known = known or set()
        known_text = " ".join(known)
        refs = ([("pmid", m) for m in dict.fromkeys(_PMID.findall(text))]
                + [("nct", m) for m in dict.fromkeys(_NCT.findall(text))]
                + [("doi", m) for m in dict.fromkeys(_DOI.findall(text))])
        verified, not_found, unchecked = [], [], []
        for kind, ref in refs[:limit]:
            if ref in known_text:
                verified.append({"type": kind, "id": ref, "how": "dossier"})
                continue
            if not online:
                unchecked.append({"type": kind, "id": ref})
                continue
            try:
                ok = self._exists(kind, ref)
            except Exception as exc:  # network trouble: report, don't fail the save
                unchecked.append({"type": kind, "id": ref, "error": str(exc)[:120]})
                continue
            (verified if ok else not_found).append({"type": kind, "id": ref, **({"how": "online"} if ok else {})})
        unchecked += [{"type": k, "id": r, "error": "over limit"} for k, r in refs[limit:]]
        return {"verified": verified, "not_found": not_found, "unchecked": unchecked}

    def _exists(self, kind: str, ref: str) -> bool:
        epmc = self.sources.europepmc
        if kind == "pmid":
            return epmc.count(f"EXT_ID:{ref} AND SRC:MED") > 0
        if kind == "doi":
            return epmc.count(f'DOI:"{ref}"') > 0
        res = self.sources.trials.search(term=ref, page_size=1, interventional_only=False)
        return any(t.nct_id == ref for t in res.trials)

    def save_hypothesis(self, *, target: str, disease: str, title: str, markdown: str,
                        modulation: str = "unknown", confidence: str = "low", generated_by: str = "mcp-client",
                        verify_references: bool = True) -> dict:
        key, symbol, disease_id = self._resolve_key(target, disease)
        dossier = self.store.get_dossier(key)
        known_ids = set(dossier["evidence_ids"]) if dossier else set()
        cited = sorted(set(_EVIDENCE_ID.findall(markdown)), key=lambda s: int(s[1:]))
        unknown_ids = [i for i in cited if i not in known_ids]
        verification = self.verify_references(markdown, set(dossier["references"]) if dossier else set(),
                                              online=verify_references)
        created = _now()
        hid = f"HM-{_slug(symbol, 20)}-{hashlib.sha1(f'{key}|{created}'.encode()).hexdigest()[:8]}"
        path = self.data_dir / "hypotheses" / f"{created[:8]}_{_slug(symbol)}__{_slug(disease_id)}_{hid[-8:]}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (f"<!-- id: {hid} | target: {symbol} | disease: {disease_id} | modulation: {modulation} | "
                  f"confidence: {confidence} | generated_by: {generated_by} | saved: {created} -->\n\n")
        path.write_text(header + markdown.strip() + "\n", encoding="utf-8")
        record = {"id": hid, "title": title, "target": symbol, "disease": dossier["disease"] if dossier else disease,
                  "disease_id": disease_id, "modulation": modulation, "confidence": {"level": confidence},
                  "generated_by": generated_by, "markdown": markdown, "path": str(path),
                  "dossier_found": bool(dossier), "cited_evidence_ids": cited, "unknown_evidence_ids": unknown_ids,
                  "verification": verification}
        self.store.save_hypothesis("mcp", key, record)
        warnings = []
        if not dossier:
            warnings.append("No dossier for this pair: run discovery_analyze_pair first so evidence ids can be checked.")
        if unknown_ids:
            warnings.append(f"Evidence ids not in the dossier: {', '.join(unknown_ids)}")
        if verification["not_found"]:
            warnings.append("References not found: " + ", ".join(r["id"] for r in verification["not_found"]))
        return {"id": hid, "path": str(path), "warnings": warnings, "verification": verification,
                "cited_evidence_ids": cited}

    def list_hypotheses(self, query: str | None = None, limit: int = 20, offset: int = 0) -> dict:
        total, rows = self.store.list_hypotheses(query, limit, offset)
        items = [{"id": r["id"], "created_at": r["created_at"], "title": r["payload"].get("title"),
                  "target": r["payload"].get("target") or r["payload"].get("target_symbol"),
                  "disease": r["payload"].get("disease") or r["payload"].get("disease_name"),
                  "confidence": r["confidence"], "generated_by": r["generated_by"], "source": r["watch"],
                  "path": r["payload"].get("path")} for r in rows]
        return {"total": total, "count": len(items), "offset": offset, "items": items,
                "has_more": total > offset + len(items),
                "next_offset": offset + len(items) if total > offset + len(items) else None}

    def get_hypothesis(self, hypothesis_id: str) -> dict:
        row = self.store.get_hypothesis(hypothesis_id)
        if not row:
            raise ServiceError(f"No hypothesis {hypothesis_id!r}. Use discovery_list_hypotheses to find ids.")
        return {"id": row["id"], "created_at": row["created_at"], "source": row["watch"], **row["payload"]}

    # -- monitoring -----------------------------------------------------------
    def watchlist_path(self, config_path: str | None = None) -> Path:
        import os

        path = Path(config_path or os.environ.get("DISCOVERY_WATCHLIST") or self.data_dir / "watchlist.yaml")
        path = path.expanduser()
        if not path.exists():
            if config_path:
                raise ServiceError(f"Watchlist not found: {path}")
            path.write_text(WATCHLIST_TEMPLATE, encoding="utf-8")
        return path

    def run_watchlist(self, *, config_path: str | None = None, only: list[str] | None = None,
                      progress: Progress | None = None) -> dict:
        from .monitor import WatchConfig, Watcher

        path = self.watchlist_path(config_path)
        cfg = WatchConfig.load(path).model_copy(update={"database": self.db_path,
                                                        "output_dir": self.data_dir / "watch"})
        watcher = Watcher(cfg, self.settings, sources=self.sources, store=self.store, progress=progress)
        summaries = watcher.run_all(only)
        return {"watchlist": str(path), "runs": [
            {"watch": s.watch, "run_id": s.run_id, "first_run": s.first_run, "error": s.error,
             "alerts": [{"kind": a["kind"], "message": a["message"]} for a in s.alerts],
             "report_path": str(s.report_path) if s.report_path else None,
             "digest_path": str(s.digest_path) if s.digest_path else None} for s in summaries]}

    def alerts(self, watch: str | None = None, limit: int = 30) -> list[dict]:
        return [{"created_at": r["created_at"], "watch": r["watch"], "kind": r["kind"], "message": r["message"]}
                for r in self.store.recent_alerts(watch, limit)]

    def status(self) -> dict:
        from .monitor import WatchConfig

        path = self.watchlist_path()
        watches = []
        for w in WatchConfig.load(path).watches:
            last = self.store.last_run(w.name)
            top = []
            if last:
                top = [{"rank": r["rank"], "target": r["symbol"], "opportunity": r["opportunity"]}
                       for r in self.store.opportunities(last["id"]).values()][:5]
            watches.append({"name": w.name, "query": w.disease or w.target,
                            "last_run": last["started_at"] if last else None, "top": top})
        total, _ = self.store.list_hypotheses(limit=1)
        return {"version": __version__, "data_dir": str(self.data_dir), "database": str(self.db_path),
                "watchlist": str(path), "watches": watches, "saved_hypotheses": total}


# =============================================================================== MCP layer

INSTRUCTIONS = """\
Scientific discovery tools: find target–disease opportunities with strong causal evidence but little
research attention, investigate them, and keep hypotheses in a local database.

Typical workflow: discovery_find_opportunities (rank candidates) -> discovery_analyze_pair (full graded
dossier with evidence ids E1, E2, … and a rule-based draft) -> investigate with discovery_search_literature,
discovery_read_abstracts, discovery_get_open_targets_evidence, discovery_get_target_profile,
discovery_get_chembl_profile, discovery_search_clinical_trials -> write the hypothesis yourself ->
discovery_save_hypothesis. For recurring monitoring use discovery_run_watchlist and discovery_list_alerts.

Rules for hypotheses: cite evidence ids from the dossier and references (PMID, DOI, NCT id, database URL)
exactly as tools returned them; never invent references (saving checks them). Keep causality, direction of
modulation, tractability and safety separate; grade reliability A–D; give at least two alternative
explanations with discriminating tests and at least two experiments with expected results if true and if
false; say why the pair is under-explored (genuine gap, hard target, or possible unpublished failure).
These are research hypotheses for scientists, not medical advice."""

_READ_ONLY_REMOTE = {"read_only_hint": True, "destructive_hint": False, "idempotent_hint": True,
                     "open_world_hint": True}


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1, default=str)


def _md_find(res: dict) -> str:
    lines = [f"## {res['query']} — {len(res['opportunities'])} opportunities "
             f"(from {res['candidates_considered']} Open Targets candidates)", "", res["_ranking_markdown"], ""]
    for o in res["opportunities"]:
        extra = []
        if o["locus_neighbors"]:
            extra.append("GWAS locus neighbours: " + ", ".join(o["locus_neighbors"]))
        if o["max_clinical_stage"]:
            extra.append(f"clinical stage for this disease: {o['max_clinical_stage']}")
        if o["warnings"]:
            extra.append("warnings: " + "; ".join(o["warnings"][:3]))
        if extra:
            lines.append(f"- **{o['target']}**: " + " · ".join(extra))
    lines += ["", f"Full report: `{res['report_path']}`",
              "Next: discovery_analyze_pair(target, disease) for the graded evidence dossier of a candidate."]
    return "\n".join(lines)


def _md_list(res: dict) -> str:
    if not res["items"]:
        return "No saved hypotheses yet."
    lines = [f"{res['count']} of {res['total']} hypotheses (offset {res['offset']}):", ""]
    for h in res["items"]:
        lines.append(f"- `{h['id']}` {h['created_at']} — **{h['title'] or h['target']}** "
                     f"({h['target']} – {h['disease']}; confidence {h['confidence']}; by {h['generated_by']})")
    if res["has_more"]:
        lines.append(f"\nMore: offset={res['next_offset']}")
    return "\n".join(lines)


def build_server(service: DiscoveryService | None = None):
    """Create the MCP server. The service is created lazily so `--help`/config printing stays fast."""
    import anyio
    from mcp.server.mcpserver import Context, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    holder: dict[str, DiscoveryService] = {}
    lock = threading.Lock()

    def svc() -> DiscoveryService:
        with lock:  # tools run on worker threads; create the service once
            if "s" not in holder:
                holder["s"] = service or DiscoveryService()
            return holder["s"]

    server = MCPServer("scientific_discovery", title="Scientific discovery agent", version=__version__,
                       instructions=INSTRUCTIONS)

    async def blocking(ctx: Context | None, fn: Callable[[Progress], Any]) -> Any:
        """Run blocking pipeline code in a worker thread and relay its progress messages."""
        counter = itertools.count(1)

        def progress(message: str) -> None:
            if ctx is None:
                return
            try:
                anyio.from_thread.run(ctx.report_progress, float(next(counter)), None, message)
            except Exception:  # client without a progress token, closed session, ...
                pass

        try:
            return await anyio.to_thread.run_sync(fn, progress)
        except ServiceError as exc:
            raise ToolError(str(exc)) from exc

    def call(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except ServiceError as exc:
            raise ToolError(str(exc)) from exc

    ro = ToolAnnotations(**_READ_ONLY_REMOTE)
    writes = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                             open_world_hint=True)

    @server.tool(name="discovery_find_opportunities", annotations=writes, structured_output=False)
    async def find_opportunities(
        ctx: Context,
        disease: Annotated[str | None, Field(description="Disease name or ontology id, e.g. 'idiopathic pulmonary fibrosis' or 'EFO_0000768'. Give this OR target.")] = None,
        target: Annotated[str | None, Field(description="Gene symbol or Ensembl id to find new indications for, e.g. 'LRRK2'. Give this OR disease.")] = None,
        top_k: Annotated[int, Field(ge=1, le=30, description="Opportunities to return")] = 8,
        enrich_k: Annotated[int, Field(ge=1, le=40, description="Candidates enriched with all sources (more = slower)")] = 12,
        include_known: Annotated[bool, Field(description="Keep targets already in phase 3+/approved for the disease")] = False,
        language: Annotated[Literal["lt", "en"] | None, Field(description="Report language")] = None,
        response_format: Annotated[Format, Field(description="markdown (readable) or json")] = "markdown",
    ) -> str:
        """Rank under-explored target–disease opportunities: strong causal evidence (human genetics, somatic,
        models, pathways) but little research attention (papers, trials, patents, clinical stage).

        Scans Open Targets associations, enriches the best candidates with Europe PMC, PubMed, bioRxiv,
        ChEMBL, UniProt, PDB and ClinicalTrials.gov, and scores opportunity = evidence × gap × tractability ×
        safety × direction. Takes ~10–60 s (cached afterwards). Saves a Markdown/JSON report and each
        candidate's dossier in the local database. Next step: discovery_analyze_pair for one candidate.
        """
        res = await blocking(ctx, lambda p: svc().find_opportunities(
            disease=disease, target=target, top_k=top_k, enrich_k=enrich_k, include_known=include_known,
            language=language, progress=p))
        if response_format == "json":
            res = {k: v for k, v in res.items() if not k.startswith("_")}
            return _dump(res)
        return _md_find(res)

    @server.tool(name="discovery_analyze_pair", annotations=writes, structured_output=False)
    async def analyze_pair(
        ctx: Context,
        target: Annotated[str, Field(description="Gene symbol or Ensembl id, e.g. 'DPP9'")],
        disease: Annotated[str, Field(description="Disease name or id, e.g. 'idiopathic pulmonary fibrosis'")],
        language: Annotated[Literal["lt", "en"] | None, Field(description="Report language")] = None,
        response_format: Annotated[Format, Field(description="markdown (graded dossier + draft) or json (full dossier)")] = "markdown",
    ) -> str:
        """Build the full evidence dossier for one target–disease pair: scores, direction of modulation
        (from LoF/GoF × risk/protect genetics and drug mechanisms), every evidence item with an id (E1, E2, …),
        A–D reliability grade, stance and references, plus a rule-based draft hypothesis with confidence,
        alternative explanations and experiments. Stored locally so discovery_save_hypothesis can check
        which evidence ids you cite. Use it before writing a hypothesis.
        """
        res = await blocking(ctx, lambda p: svc().analyze_pair(target, disease, language=language, progress=p))
        if response_format == "json":
            return _dump({"key": res["key"], "report_path": res["report_path"], "dossier": res["dossier"]})
        return (f"Dossier for **{res['symbol']} – {res['disease']}** (`{res['key']}`), saved to "
                f"`{res['report_path']}`. Cite evidence ids below; save your hypothesis with "
                f"discovery_save_hypothesis.\n\n" + res["draft_markdown"])

    @server.tool(name="discovery_search_entities", annotations=ro, structured_output=False)
    def search_entities(
        text: Annotated[str, Field(min_length=2, description="Free text, e.g. 'pulmonary fibrosis' or 'LRRK2'")],
        entity: Annotated[Literal["target", "disease", "drug"] | None, Field(description="Restrict to one entity type")] = None,
        limit: Annotated[int, Field(ge=1, le=25)] = 10,
    ) -> str:
        """Look up Open Targets ids for diseases, targets (genes) or drugs. Use it when a name is not found."""
        hits = call(lambda: svc().search_entities(text, entity, limit))
        if not hits:
            return f"No Open Targets matches for {text!r}."
        return "\n".join(f"- {h['entity']}: **{h['name']}** (`{h['id']}`) — {(h.get('description') or '')[:120]}"
                         for h in hits)

    # -- investigation tools reuse the vetted ToolSession implementations ----------
    tool_descriptions = {t["name"]: t["description"] for t in TOOLS}

    @server.tool(name="discovery_search_literature", annotations=ro, structured_output=False,
                 description=tool_descriptions["search_literature"])
    def search_literature(
        query: Annotated[str, Field(min_length=2, description='Europe PMC query, e.g. TITLE:"PARN" AND ABSTRACT:"fibrosis"')],
        source: Annotated[Literal["articles", "preprints", "patents"], Field(description="Which collection")] = "articles",
        since: Annotated[str, Field(description="Earliest first-publication date YYYY-MM-DD, or empty")] = "",
        max_results: Annotated[int, Field(ge=1, le=15)] = 8,
    ) -> str:
        return _dump(call(lambda: svc().run_tool("search_literature", {
            "query": query, "source": source, "since": since, "max_results": max_results})))

    @server.tool(name="discovery_read_abstracts", annotations=ro, structured_output=False,
                 description=tool_descriptions["read_abstracts"])
    def read_abstracts(
        ids: Annotated[list[str], Field(min_length=1, max_length=8, description="PMIDs or Europe PMC ids, e.g. ['36653562', 'PPR1260607']")],
    ) -> str:
        return _dump(call(lambda: svc().run_tool("read_abstracts", {"ids": ids})))

    @server.tool(name="discovery_get_open_targets_evidence", annotations=ro, structured_output=False)
    def ot_evidence(
        target: Annotated[str, Field(description="Gene symbol or Ensembl id")],
        disease: Annotated[str, Field(description="Disease name or id")],
        group: Annotated[Literal["genetic", "somatic", "clinical", "animal", "pathway", "expression", "literature"],
                         Field(description="Evidence group")] = "genetic",
    ) -> str:
        """Open Targets evidence rows for a target–disease pair and one evidence group: direction of effect
        (directionOnTarget LoF/GoF, directionOnTrait risk/protect), confidence, p-values, drugs, stop reasons."""
        return _dump(call(lambda: svc().run_tool("get_open_targets_evidence", {"group": group},
                                                 target=target, disease=disease)))

    @server.tool(name="discovery_get_target_profile", annotations=ro, structured_output=False,
                 description=tool_descriptions["get_target_profile"])
    def target_profile(gene: Annotated[str, Field(description="HGNC symbol or Ensembl id")]) -> str:
        return _dump(call(lambda: svc().run_tool("get_target_profile", {"gene": gene})))

    @server.tool(name="discovery_get_chembl_profile", annotations=ro, structured_output=False,
                 description=tool_descriptions["get_chembl_profile"])
    def chembl_profile(gene: Annotated[str, Field(description="HGNC symbol or Ensembl id")]) -> str:
        return _dump(call(lambda: svc().run_tool("get_chembl_profile", {"gene": gene})))

    @server.tool(name="discovery_search_clinical_trials", annotations=ro, structured_output=False,
                 description=tool_descriptions["search_clinical_trials"])
    def clinical_trials(
        condition: Annotated[str, Field(description="Condition, e.g. 'idiopathic pulmonary fibrosis'")],
        term: Annotated[str, Field(description="Gene, drug or mechanism, e.g. 'telomerase'")],
    ) -> str:
        return _dump(call(lambda: svc().run_tool("search_clinical_trials", {"condition": condition, "term": term})))

    # -- local database ------------------------------------------------------
    @server.tool(name="discovery_save_hypothesis", annotations=writes, structured_output=False)
    def save_hypothesis(
        target: Annotated[str, Field(description="Gene symbol or Ensembl id")],
        disease: Annotated[str, Field(description="Disease name or id")],
        title: Annotated[str, Field(min_length=3, max_length=300)],
        markdown: Annotated[str, Field(min_length=50, description="The full hypothesis in Markdown, citing evidence ids (E1…) and references")],
        modulation: Annotated[Literal["inhibition", "activation", "unknown"], Field()] = "unknown",
        confidence: Annotated[Literal["high", "moderate", "low", "very_low"], Field()] = "low",
        generated_by: Annotated[str, Field(max_length=80, description="Who wrote it, e.g. 'claude-desktop'")] = "mcp-client",
        verify_references: Annotated[bool, Field(description="Check PMIDs/DOIs/NCT ids online (Europe PMC, ClinicalTrials.gov)")] = True,
    ) -> str:
        """Save a hypothesis you wrote to the local database and as a Markdown file. Checks that cited evidence
        ids exist in the pair's dossier (run discovery_analyze_pair first) and that cited PMIDs, DOIs and NCT ids
        exist. Returns the id, the file path and warnings — fix any reported problem and save again."""
        return _dump(call(lambda: svc().save_hypothesis(
            target=target, disease=disease, title=title, markdown=markdown, modulation=modulation,
            confidence=confidence, generated_by=generated_by, verify_references=verify_references)))

    @server.tool(name="discovery_list_hypotheses", annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=False)
    def list_hypotheses(
        query: Annotated[str | None, Field(description="Filter by text, gene or disease")] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
        response_format: Annotated[Format, Field()] = "markdown",
    ) -> str:
        """List hypotheses saved in the local database (newest first), from you and from watchlist runs."""
        res = svc().list_hypotheses(query, limit, offset)
        return _dump(res) if response_format == "json" else _md_list(res)

    @server.tool(name="discovery_get_hypothesis", annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=False)
    def get_hypothesis(hypothesis_id: Annotated[str, Field(description="Id from discovery_list_hypotheses")]) -> str:
        """Return one saved hypothesis with its Markdown text and reference verification."""
        return _dump(call(lambda: svc().get_hypothesis(hypothesis_id)))

    @server.tool(name="discovery_run_watchlist", annotations=writes, structured_output=False)
    async def run_watchlist(
        ctx: Context,
        config_path: Annotated[str | None, Field(description="Watchlist YAML; default ~/.local/share/discovery-agent/watchlist.yaml (created on first use)")] = None,
        only: Annotated[list[str] | None, Field(description="Only these watch names")] = None,
    ) -> str:
        """Run every watch in the watchlist once: re-rank opportunities, compare with the previous run in the
        local database, look for new papers/preprints/trials on the top opportunities, and record alerts.
        Returns the alerts and report paths. Can take a few minutes for several diseases."""
        return _dump(await blocking(ctx, lambda p: svc().run_watchlist(config_path=config_path, only=only,
                                                                       progress=p)))

    @server.tool(name="discovery_list_alerts", annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=False)
    def list_alerts(
        watch: Annotated[str | None, Field(description="Only this watch")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 30,
    ) -> str:
        """Recent monitoring alerts (new or dropped opportunities, score changes, new papers/preprints/trials)."""
        rows = svc().alerts(watch, limit)
        return "\n".join(f"- {r['created_at']} [{r['watch']}] {r['kind']}: {r['message']}" for r in rows) \
            or "No alerts yet. Run discovery_run_watchlist."

    @server.tool(name="discovery_status", annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=False)
    def status() -> str:
        """Where the local database and reports live, the watchlist, last runs and saved hypothesis count."""
        return _dump(svc().status())

    # -- prompts -------------------------------------------------------------
    lang_name = {"lt": "Lithuanian", "en": "English"}

    @server.prompt(name="investigate_disease", title="Investigate a disease")
    def investigate_disease(disease: str, language: str = "lt", candidates: str = "3") -> str:
        """Find and investigate under-explored opportunities for a disease, then save hypotheses."""
        return (
            f"Find under-explored therapeutic opportunities for **{disease}** and turn the best {candidates} into "
            f"evidence-graded hypotheses, written in {lang_name.get(language, language)}.\n\n"
            f"1. discovery_find_opportunities(disease='{disease}', language='{language}').\n"
            f"2. For each of the top {candidates}: discovery_analyze_pair, then investigate — look first for negative "
            "results and reasons for low attention (stopped trials, toxicity, contradicting genetics), then verify "
            "the direction of modulation, mechanism, related clinical-stage drugs, tractability and safety. Read "
            "abstracts, not just titles.\n"
            "3. Write each hypothesis: statement with direction; mechanistic rationale; supporting and "
            "contradicting evidence with A–D grades and references; confidence with reasons; why under-explored; "
            "≥2 alternative explanations with tests; ≥2 experiments with expected results if true/false; safety; "
            "competitive landscape; research-not-medical-advice note.\n"
            "4. discovery_save_hypothesis for each; fix any warnings it returns and save again.\n"
            "5. Finish with a short ranked summary.")

    @server.prompt(name="write_hypothesis", title="Write a hypothesis for one pair")
    def write_hypothesis(target: str, disease: str, language: str = "lt") -> str:
        """Investigate one target–disease pair and save a rigorous hypothesis."""
        return (
            f"Write a rigorous research hypothesis for **{target} – {disease}** in "
            f"{lang_name.get(language, language)}. Start with discovery_analyze_pair(target='{target}', "
            f"disease='{disease}', language='{language}'), investigate with the discovery_* tools (negative results "
            "and stopped trials first), cite evidence ids and references exactly as returned, and save it with "
            "discovery_save_hypothesis. Be sceptical and calibrate confidence conservatively.")

    @server.prompt(name="weekly_review", title="Weekly monitoring review")
    def weekly_review(language: str = "lt") -> str:
        """Run the watchlist, then investigate what changed and save hypotheses."""
        return (
            "Run discovery_run_watchlist. For each watch, read the alerts: investigate up to three new or changed "
            "top opportunities (discovery_analyze_pair + investigation tools), skip pairs already covered by recent "
            "hypotheses (discovery_list_hypotheses), and save new hypotheses with discovery_save_hypothesis. "
            f"Finish with a digest in {lang_name.get(language, language)}: what changed, what you concluded, "
            "confidence, and what to watch next.")

    return server


def client_config(target: str, *, data_dir: str | None = None) -> str:
    """A ready-to-paste MCP client configuration that runs this server with the current Python."""
    args = ["-m", "discovery_agent", "mcp"]
    env = {"DISCOVERY_DATA_DIR": data_dir} if data_dir else {}
    if target == "codex":
        lines = ["# ~/.codex/config.toml", "[mcp_servers.scientific-discovery]",
                 f"command = {json.dumps(sys.executable)}", f"args = {json.dumps(args)}"]
        if env:
            lines.append("env = { " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in env.items()) + " }")
        return "\n".join(lines)
    server = {"command": sys.executable, "args": args, **({"env": env} if env else {})}
    if target == "claude-code":
        server = {"type": "stdio", **server}
    return json.dumps({"mcpServers": {"scientific-discovery": server}}, indent=2)


SERVER_KEY = "scientific-discovery"
TOOL_NAMES = (
    "discovery_find_opportunities", "discovery_analyze_pair", "discovery_search_entities",
    "discovery_search_literature", "discovery_read_abstracts", "discovery_get_open_targets_evidence",
    "discovery_get_target_profile", "discovery_get_chembl_profile", "discovery_search_clinical_trials",
    "discovery_save_hypothesis", "discovery_list_hypotheses", "discovery_get_hypothesis",
    "discovery_run_watchlist", "discovery_list_alerts", "discovery_status",
)
PROMPT_NAMES = ("investigate_disease", "write_hypothesis", "weekly_review")


def claude_desktop_config_path() -> Path:
    """Default location of Claude Desktop's config file on this OS.

    Settings -> Developer -> Edit Config in Claude Desktop opens the file it really uses; pass that path
    explicitly if it differs (e.g. some Windows Store installs).
    """
    import os

    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / "Claude" / "claude_desktop_config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "Claude" / "claude_desktop_config.json"


def install_claude_desktop(config_path: str | Path | None = None, *, data_dir: str | None = None) -> dict:
    """Add (or update) this server in Claude Desktop's config, keeping every other setting and server.

    A timestamped backup is written next to the file before any change. Invalid JSON is never overwritten.
    """
    path = Path(config_path).expanduser() if config_path else claude_desktop_config_path()
    entry = json.loads(client_config("claude-desktop", data_dir=data_dir))["mcpServers"][SERVER_KEY]
    data: dict = {}
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if text.strip():
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ServiceError(f"{path} is not valid JSON ({exc}). Fix or move it first; nothing was changed.") from exc
        if not isinstance(data, dict):
            raise ServiceError(f"{path} does not contain a JSON object; nothing was changed.")
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ServiceError(f'"mcpServers" in {path} is not an object; nothing was changed.')
    previous = servers.get(SERVER_KEY)
    others = sorted(k for k in servers if k != SERVER_KEY)
    if previous == entry:
        return {"path": str(path), "backup": None, "status": "unchanged", "entry": entry, "other_servers": others}
    backup = None
    if text:
        backup = path.with_name(f"{path.name}.bak-{_now()}")
        backup.write_text(text, encoding="utf-8")
    servers[SERVER_KEY] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"path": str(path), "backup": str(backup) if backup else None,
            "status": "updated" if previous is not None else "added", "entry": entry, "other_servers": others}


def _launch_spec(config_path: str | Path | None) -> tuple[str, list[str], dict[str, str]]:
    """The command a client would run: from a client config file, or this installation by default."""
    import shutil

    if config_path is None:
        return sys.executable, ["-m", "discovery_agent", "mcp"], {}
    path = Path(config_path).expanduser()
    try:
        servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ServiceError(f"Cannot read {path}: {exc}") from exc
    entry = servers.get(SERVER_KEY)
    if not entry:
        raise ServiceError(f'{path} has no "{SERVER_KEY}" server; run: discovery-agent mcp --install claude-desktop')
    command = entry.get("command") or ""
    if not (Path(command).is_file() or shutil.which(command)):
        raise ServiceError(f"Command {command!r} from {path} was not found; use an absolute path.")
    return command, list(entry.get("args") or []), dict(entry.get("env") or {})


def self_test(config_path: str | Path | None = None, *, online: bool = False, timeout: float = 180.0) -> list[dict]:
    """Start the server over stdio exactly as a client would and exercise it. Returns one row per check."""
    import os

    import anyio
    from mcp import Client, StdioServerParameters

    checks: list[dict] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    try:
        command, args, env = _launch_spec(config_path)
    except ServiceError as exc:
        record("client config", False, str(exc))
        return checks
    record("client config", True, f"{command} {' '.join(args)}" + (f" (env: {', '.join(env)})" if env else ""))
    params = StdioServerParameters(command=command, args=args, env={**os.environ, **env})

    def text(result) -> str:
        return "".join(getattr(c, "text", "") for c in result.content)

    async def scenario() -> None:
        with anyio.fail_after(timeout):
            async with Client(params, read_timeout_seconds=timeout) as c:
                record("server starts (stdio)", True)
                names = {t.name for t in (await c.list_tools()).tools}
                missing = sorted(set(TOOL_NAMES) - names)
                record(f"{len(TOOL_NAMES)} tools", not missing, "missing: " + ", ".join(missing) if missing else "")
                prompts = {p.name for p in (await c.list_prompts()).prompts}
                record(f"{len(PROMPT_NAMES)} prompts", set(PROMPT_NAMES) <= prompts)
                r = await c.call_tool("discovery_status", {})
                ok = not r.is_error
                detail = text(r)[:300]
                if ok:
                    status = json.loads(text(r))
                    detail = f"data_dir={status['data_dir']}"
                record("discovery_status (local database)", ok, detail)
                if not online:
                    return
                probes = [
                    ("Open Targets", "discovery_search_entities",
                     {"text": "idiopathic pulmonary fibrosis", "entity": "disease", "limit": 3},
                     lambda s: "EFO_0000768" in s),
                    ("Europe PMC", "discovery_search_literature",
                     {"query": 'TITLE:"idiopathic pulmonary fibrosis"', "source": "articles", "since": "",
                      "max_results": 1}, lambda s: json.loads(s)["hit_count"] > 0),
                    ("ClinicalTrials.gov", "discovery_search_clinical_trials",
                     {"condition": "idiopathic pulmonary fibrosis", "term": "nintedanib"},
                     lambda s: json.loads(s)["total"] > 0),
                    ("ChEMBL + UniProt id", "discovery_get_chembl_profile", {"gene": "TERT"},
                     lambda s: json.loads(s)["potent_compounds"] > 0),
                ]
                for label, tool, arguments, good in probes:
                    try:
                        r = await c.call_tool(tool, arguments)
                        body = text(r)
                        record(f"online: {label}", not r.is_error and good(body), "" if not r.is_error else body[:200])
                    except Exception as exc:  # keep going: report every source
                        record(f"online: {label}", False, str(exc)[:200])

    try:
        anyio.run(scenario)
    except Exception as exc:
        record("server session", False, f"{type(exc).__name__}: {exc}"[:300])
    return checks


def main() -> None:
    build_server().run("stdio")


__all__ = ["DiscoveryService", "ServiceError", "build_server", "claude_desktop_config_path", "client_config",
           "install_claude_desktop", "main", "self_test"]
