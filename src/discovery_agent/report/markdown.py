"""Markdown rendering for rankings, hypotheses and monitoring digests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from ..analysis.quality import check_hypothesis
from ..i18n import tr
from ..models import Evidence, Hypothesis, Opportunity, Stance

_DIR_ICON = {"inhibition": "⬇", "activation": "⬆", "unknown": "?"}


def _cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def _refs(e: Evidence, limit: int = 3) -> str:
    out = []
    for r in e.references[:limit]:
        out.append(f"[{_cell(r.label)}]({r.url})" if r.url else _cell(r.label))
    return "; ".join(out)


def _fmt(v: float | None, digits: int = 2) -> str:
    return "–" if v is None else f"{v:.{digits}f}"


def evidence_basis(opp: Opportunity, lang: str, n: int = 2) -> str:
    scores = {k: v for k, v in opp.association.datatype_scores.items() if k != "literature" and v >= 0.05}
    top = sorted(scores.items(), key=lambda kv: -kv[1])[:n]
    return ", ".join(f"{tr(lang, f'dt.{k}')} {v:.2f}" for k, v in top) or "–"


def ranking_table(opps: Iterable[Opportunity], lang: str) -> str:
    head = [tr(lang, k) for k in ("rep.col.rank", "rep.col.target", "rep.col.disease", "rep.col.opportunity",
                                   "rep.col.evidence", "rep.col.basis", "rep.col.gap", "rep.col.tract",
                                   "rep.col.safety", "rep.col.direction", "rep.col.pubs")]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for i, o in enumerate(opps, 1):
        s = o.scores
        mod = o.direction.modulation.value
        lines.append("| " + " | ".join([
            str(i), f"**{o.target.symbol}**", _cell(o.disease.name), f"**{s.opportunity:.3f}**", _fmt(s.evidence),
            evidence_basis(o, lang), _fmt(s.gap), _fmt(s.tractability), _fmt(s.safety_risk),
            f"{_DIR_ICON[mod]} {tr(lang, f'mod.{mod}')} ({o.direction.confidence:.2f})",
            "–" if o.attention.publications is None else str(o.attention.publications),
        ]) + " |")
    return "\n".join(lines)


def evidence_table(opp: Opportunity, lang: str) -> str:
    head = [tr(lang, k) for k in ("rep.col.id", "rep.col.source", "rep.col.aspect", "rep.col.reliability",
                                   "rep.col.stance", "rep.col.summary", "rep.col.refs")]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for e in opp.evidence:
        lines.append("| " + " | ".join([
            e.id, _cell(e.source), tr(lang, f"aspect.{e.aspect.value}"),
            f"**{e.reliability.value}** – {_cell(e.reliability_rationale)}", tr(lang, f"stance.{e.stance.value}"),
            _cell(e.summary), _refs(e),
        ]) + " |")
    return "\n".join(lines)


def render_hypothesis(h: Hypothesis, opp: Opportunity, lang: str | None = None, level: int = 2) -> str:
    lang = lang or h.language
    hx = "#" * level
    h3 = "#" * (level + 1)
    ev = opp.evidence_by_id()
    s = opp.scores
    out = [f"{hx} {h.title}", "",
           f"> **{tr(lang, 'rep.h.hypothesis')}:** {h.statement}", "",
           f"`{h.id}` · {tr(lang, 'rep.generated_by')}: {h.generated_by} · "
           f"Open Targets: [{opp.target.symbol}](https://platform.opentargets.org/evidence/{opp.target.ensembl_id}/{opp.disease.id})",
           ""]
    out += [f"{h3} {tr(lang, 'rep.h.scores')}", "",
            f"| {tr(lang, 'rep.col.opportunity')} | {tr(lang, 'rep.col.evidence')} | {tr(lang, 'rep.col.gap')} | "
            f"{tr(lang, 'rep.col.tract')} | {tr(lang, 'rep.col.safety')} | {tr(lang, 'rep.col.direction')} |",
            "|---|---|---|---|---|---|",
            f"| **{s.opportunity:.3f}** | {s.evidence:.2f} | {s.gap:.2f} | {s.tractability:.2f} | {s.safety_risk:.2f} | "
            f"{tr(lang, f'mod.{h.modulation.value}')} ({opp.direction.confidence:.2f}) |", ""]
    out += [f"{h3} {tr(lang, 'rep.h.mechanism')}", "", h.mechanism_rationale, ""]
    out += [f"{h3} {tr(lang, 'rep.h.confidence')}", "",
            f"**{tr(lang, f'level.{h.confidence.level.value}')}** ({h.confidence.score:.2f}). {h.confidence.rationale}", ""]

    def appraisal_list(items, title):
        if not items:
            return []
        lines = [f"{h3} {title}", ""]
        for a in items:
            e = ev.get(a.evidence_id)
            tag = f"**{a.evidence_id}**" + (f" ({e.reliability.value}, {_cell(e.source)})" if e else "")
            lines.append(f"- {tag}: {a.interpretation}" + (f" — _{a.reliability_comment}_" if a.reliability_comment else ""))
        return lines + [""]

    out += appraisal_list(h.supporting, tr(lang, "rep.h.supporting"))
    out += appraisal_list(h.contradicting, tr(lang, "rep.h.contradicting"))

    if h.alternative_explanations:
        out += [f"{h3} {tr(lang, 'rep.h.alternatives')}", ""]
        for i, a in enumerate(h.alternative_explanations, 1):
            cites = f" [{', '.join(a.evidence_ids)}]" if a.evidence_ids else ""
            out.append(f"{i}. **{a.title}** ({tr(lang, 'rep.plausibility')}: {tr(lang, f'plaus.{a.plausibility}')}){cites}. "
                       f"{a.explanation} _{tr(lang, 'rep.how_to_test')}:_ {a.how_to_test}")
        out.append("")
    if h.key_uncertainties:
        out += [f"{h3} {tr(lang, 'rep.h.uncertainties')}", ""] + [f"- {u}" for u in h.key_uncertainties] + [""]
    if h.proposed_experiments:
        out += [f"{h3} {tr(lang, 'rep.h.experiments')}", ""]
        for i, x in enumerate(h.proposed_experiments, 1):
            out.append(f"{i}. **{x.title}** ({tr(lang, 'rep.priority')}: {tr(lang, f'prio.{x.priority}')}). {x.design}")
            if x.expected_if_true or x.expected_if_false:
                out.append(f"   - {tr(lang, 'rep.if_true')}: {x.expected_if_true}")
                out.append(f"   - {tr(lang, 'rep.if_false')}: {x.expected_if_false}")
        out.append("")
    if h.safety_considerations:
        out += [f"{h3} {tr(lang, 'rep.h.safety')}", ""] + [f"- {x}" for x in h.safety_considerations] + [""]
    if h.competitive_landscape:
        out += [f"{h3} {tr(lang, 'rep.h.landscape')}", "", h.competitive_landscape, ""]
    if h.novelty_assessment:
        out += [f"{h3} {tr(lang, 'rep.h.novelty')}", "", h.novelty_assessment, ""]
    out += [f"{h3} {tr(lang, 'rep.h.evidence_table')}", "", evidence_table(opp, lang), "",
            f"_{tr(lang, 'rep.reliability_legend')}_", ""]
    if h.ungrounded_citations:
        out += [f"> ⚠ {tr(lang, 'rep.ungrounded', ids=', '.join(h.ungrounded_citations))}", ""]
    q = check_hypothesis(h, opp, lang)
    out += [f"{h3} {tr(lang, 'rep.h.quality')}", ""]
    if q.problems:
        out += [tr(lang, "rep.quality.revise", score=q.score), ""]
        out += [f"- {'⛔' if c.severity == 'error' else '⚠'} {c.message}" for c in q.problems] + [""]
    else:
        out += [tr(lang, "rep.quality.ok", score=q.score), ""]
    if opp.warnings:
        out += [f"{h3} {tr(lang, 'rep.h.warnings')}", ""] + [f"- {_cell(w)}" for w in opp.warnings] + [""]
    return "\n".join(out)


def render_report(title: str, opps: list[Opportunity], hypotheses: dict[str, Hypothesis], lang: str,
                  *, candidates_considered: int | None = None) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = [f"# {title}", "", f"_{tr(lang, 'rep.generated', ts=ts)}_", ""]
    out += [f"## {tr(lang, 'rep.h.method')}", "", tr(lang, "rep.method"), ""]
    if candidates_considered:
        out.append(f"Open Targets: {candidates_considered} → {len(opps)}.")
        out.append("")
    out += [f"## {tr(lang, 'rep.h.ranking')}", "", ranking_table(opps, lang), ""]
    for opp in opps:
        h = hypotheses.get(opp.key)
        if h:
            out += [render_hypothesis(h, opp, lang), "---", ""]
    out += [f"_{tr(lang, 'rep.disclaimer')}_", ""]
    return "\n".join(out)


def render_digest(name: str, lines: list[str], lang: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = [f"# {tr(lang, 'dig.title', name=name)}", "", f"_{ts}_", ""]
    body += [f"- {_cell(line)}" for line in lines] if lines else [tr(lang, "dig.nothing")]
    return "\n".join(body) + "\n"


def contradicting(opp: Opportunity) -> list[Evidence]:
    return [e for e in opp.evidence if e.stance is Stance.CONTRADICTS]
