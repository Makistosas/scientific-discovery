"""Genetic safety: what does lifelong genetic modulation of the target do to *other* diseases?

A drug that inhibits a target mimics loss-of-function (LoF) carriers; an activator mimics gain-of-function
(GoF). Phenome-wide genetic evidence with an annotated direction tells which other diseases the intended
modulation would raise (on-target liabilities) or lower (repurposing opportunities). Rows without a direction
only show pleiotropy. Mouse knockout lethality and the warnings / FAERS signals of drugs already acting on the
target complete the picture.
"""

from __future__ import annotations

import re
from collections import defaultdict

from ..models import DiseaseInfo, GeneticSafety, Modulation

# effect of the intended modulation, expressed as the genetic direction it mimics
_MIMICS = {Modulation.INHIBITION: "LoF", Modulation.ACTIVATION: "GoF"}
_SEVERE_WARNINGS = ("black box", "withdrawn")


_PREFIXES = ("hyper", "hypo", "familial", "early", "late", "juvenile", "adult")
_GENERIC = {"disease", "disorder", "syndrome", "deficiency", "measurement", "levels", "level", "type",
            "chronic", "acute", "primary", "secondary", "inherited", "autosomal", "dominant", "recessive"}


_UMBRELLA = re.compile(r"^(hereditary|genetic|metabolic|inherited|rare|monogenic|congenital|syndromic)"
                       r"( genetic)? (disease|disorder)s?$")


def _stems(name: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z]+", name.lower()):
        for p in _PREFIXES:
            if w.startswith(p) and len(w) > len(p) + 4:
                w = w[len(p):]
        if len(w) >= 5 and w not in _GENERIC:
            out.add(w[:7])
    return out


def related_to(disease: DiseaseInfo) -> set[str]:
    """Word stems of the index disease, its synonyms and parents ("hypercholesterolemia" ~ "hypocholesterolemia"):
    genetic effects on these traits are the intended effect or the same disease, not a side effect."""
    stems: set[str] = set()
    for n in [disease.name, *disease.synonyms, *disease.parents]:
        stems |= _stems(n)
    return stems


def _effect(dir_target: str | None, dir_trait: str | None, mimicked: str) -> str | None:
    """'risk' if the intended modulation would increase the other disease, 'protect' if it would decrease it."""
    if dir_target not in ("LoF", "GoF") or dir_trait not in ("risk", "protect"):
        return None
    same = dir_target == mimicked
    if dir_trait == "risk":
        return "risk" if same else "protect"
    return "protect" if same else "risk"


def assess_genetic_safety(data: dict, disease: DiseaseInfo, modulation: Modulation) -> GeneticSafety:
    related = related_to(disease)
    own = {disease.id, *disease.descendants}
    own |= {g["id"] for g in data.get("genetic_diseases") or []
            if _stems(g["name"] or "") & related or _UMBRELLA.match((g["name"] or "").lower())}
    names = {g["id"]: g["name"] for g in data.get("genetic_diseases") or []}
    mimicked = _MIMICS.get(modulation)
    out = GeneticSafety(mimics=mimicked)
    votes: dict[str, dict[str, float]] = defaultdict(lambda: {"risk": 0.0, "protect": 0.0})
    sources: dict[str, set[str]] = defaultdict(set)
    for r in data.get("genetic_rows") or []:
        did = r.get("disease_id")
        if not did or did in own or did not in names:
            continue
        if mimicked:
            eff = _effect(r.get("directionOnTarget"), r.get("directionOnTrait"), mimicked)
            if eff:
                votes[did][eff] += r.get("score") or 0.0
                sources[did].add(r.get("source") or "?")
    for did, v in votes.items():
        if v["risk"] > 0 and v["risk"] >= 2 * v["protect"]:
            out.liabilities.append({"disease": names[did], "id": did, "weight": round(v["risk"], 2),
                                    "sources": sorted(sources[did])})
        elif v["protect"] > 0 and v["protect"] >= 2 * v["risk"]:
            out.benefits.append({"disease": names[did], "id": did, "weight": round(v["protect"], 2),
                                 "sources": sorted(sources[did])})
    out.liabilities.sort(key=lambda x: -x["weight"])
    out.benefits.sort(key=lambda x: -x["weight"])
    directed = {x["id"] for x in out.liabilities + out.benefits}
    out.other_genetic = [g["name"] for g in data.get("genetic_diseases") or []
                         if g["id"] not in own and g["id"] not in directed and g["genetic_score"] >= 0.3][:8]
    out.mouse_lethal = list(data.get("mouse_lethal") or [])[:3]
    for d in data.get("drugs") or []:
        for w in d.get("warnings") or []:
            if any(k in (w.get("type") or "").lower() for k in _SEVERE_WARNINGS):
                out.drug_warnings.append({"drug": d["name"], "type": w.get("type"), "class": w.get("class")})
        if d.get("adverse_events"):
            out.adverse_events.append({"drug": d["name"], "events": [e["name"] for e in d["adverse_events"][:4]]})
    out.adverse_events = out.adverse_events[:3]
    return out


def genetic_safety_risk(gs: GeneticSafety | None) -> tuple[float, list[str]]:
    """Extra safety risk (0..0.4) from on-target genetic liabilities, mouse lethality and drug warnings."""
    if gs is None:
        return 0.0, []
    risk, notes = 0.0, []
    if gs.liabilities:
        risk += min(0.2, 0.08 * len(gs.liabilities))
        notes.append("genetic_liability")
    if gs.mouse_lethal and gs.mimics == "LoF":
        risk += 0.05
        notes.append("mouse_lethal")
    if gs.drug_warnings:
        risk += 0.1
        notes.append("drug_warning")
    return round(min(0.4, risk), 4), notes
