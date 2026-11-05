"""Feedback on hypotheses and staleness review.

A hypothesis keeps a *snapshot* of what the pipeline knew when it was saved: scores, attention counts,
trials, clinical stage, direction, genetic liabilities and negative-result papers. A review re-analyses the
pair and compares: new papers or trials, a stopped trial, a drug reaching a later stage, a changed direction,
a new on-target liability, a new negative-result paper or a large score change make the hypothesis *stale*,
i.e. due for a re-read. Feedback (supported / refuted / testing / parked) is recorded by the user; refuted and
parked hypotheses are not reviewed, and feedback is shown wherever the pair appears again.
"""

from __future__ import annotations

from ..i18n import tr
from ..models import Opportunity
from ..sources.opentargets import stage_rank

FEEDBACK_STATUSES = ("proposed", "testing", "supported", "refuted", "parked")
CLOSED = ("refuted", "parked")


def snapshot_from_opportunity(opp: Opportunity, versions: dict | None = None) -> dict:
    a, s, gs = opp.attention, opp.scores, opp.genetic_safety
    return {
        "open_targets_release": (versions or {}).get("open_targets"),
        "opportunity": s.opportunity, "evidence": s.evidence, "gap": s.gap, "safety_risk": s.safety_risk,
        "publications": a.publications, "publications_recent": a.publications_recent, "trials": a.trials,
        "trials_stopped": a.trials_stopped, "patents": a.patents, "max_clinical_stage": a.max_clinical_stage,
        "direction": opp.direction.modulation.value, "direction_confidence": opp.direction.confidence,
        "liabilities": [r["disease"] for r in (gs.liabilities if gs else [])],
        "negative_pmids": sorted({x["pmid"] for x in opp.text_sentences if x.get("negative") and x.get("pmid")}),
        "pmids": sorted({r.pmid for e in opp.evidence for r in e.references if r.pmid}),
    }


def _grew(old, new, *, abs_min: int, rel_min: float = 0.0) -> bool:
    if old is None or new is None:
        return False
    return new - old >= abs_min and (old == 0 or (new - old) / old >= rel_min)


def compare(old: dict, new: dict, lang: str) -> list[dict]:
    """Reasons why a hypothesis saved with snapshot ``old`` should be re-read given ``new``."""
    out: list[dict] = []

    def add(code: str, **kw) -> None:
        out.append({"code": code, "message": tr(lang, f"stale.{code}", **kw)})

    if _grew(old.get("publications"), new.get("publications"), abs_min=3, rel_min=0.2):
        add("papers", old=old["publications"], new=new["publications"])
    if _grew(old.get("trials"), new.get("trials"), abs_min=1):
        add("trials", old=old["trials"], new=new["trials"])
    if _grew(old.get("trials_stopped"), new.get("trials_stopped"), abs_min=1):
        add("stopped", n=new["trials_stopped"] - old["trials_stopped"])
    if stage_rank(new.get("max_clinical_stage")) > stage_rank(old.get("max_clinical_stage")):
        add("stage", old=old.get("max_clinical_stage") or "–", new=new.get("max_clinical_stage"))
    if old.get("direction") != new.get("direction") and (new.get("direction_confidence") or 0) >= 0.3:
        add("direction", old=old.get("direction"), new=new.get("direction"))
    liab = [x for x in new.get("liabilities") or [] if x not in (old.get("liabilities") or [])]
    if liab:
        add("liability", items="; ".join(liab[:3]))
    neg = [p for p in new.get("negative_pmids") or [] if p not in (old.get("negative_pmids") or [])]
    if neg:
        add("negative", pmids=", ".join(neg[:3]))
    if old.get("open_targets_release") and new.get("open_targets_release") \
            and old["open_targets_release"] != new["open_targets_release"]:
        add("release", old=old["open_targets_release"], new=new["open_targets_release"])
    o, n = old.get("opportunity") or 0.0, new.get("opportunity") or 0.0
    if o > 0 and abs(n - o) / o >= 0.3:
        add("score", old=round(o, 3), new=round(n, 3))
    return out
