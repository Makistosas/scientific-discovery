"""Sentence-level literature evidence.

Two inputs:

* Europe PMC text-mining sentences (through Open Targets) in which the target and the disease co-occur;
* the open-access full text of a chosen article (JATS XML), searched for sentences about the target.

Each sentence is flagged when it reports a null or negative result and, as a *hint* only, when it pairs a
perturbation with an outcome ("knockdown … attenuated fibrosis" → inhibition looks beneficial). The hints are
regular-expression heuristics: they never vote on the direction of modulation, they point a reader to the
sentences worth checking.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

NEGATIVE_CUES = ("no significant", "not significant", "did not", "no association", "not associated", "no effect",
                 "failed to", "no difference", "not improve", "lack of", "negative result", "was not", "were not",
                 "no evidence", "not replicate", "did not replicate", "no change")

_LOSS = r"(?:knock(?:ing|ed)?[- ]?(?:down|out)|deficien\w*|silenc\w*|inhibit(?:ion|or|ors|ing|ed)?|blockade|" \
        r"antagonis\w*|deplet\w*|delet\w*|loss of|siRNA|shRNA|ablat\w*|-/-|null mice)"
_GAIN = r"(?:overexpress\w*|activat(?:ion|or|ors|ing|ed)|agonis\w*|restor\w*|supplement\w*|gain of|transgenic)"
_GOOD = r"(?:attenuat\w*|ameliorat\w*|alleviat\w*|reduc\w*|decreas\w*|protect\w*|prevent\w*|improv\w*|rescu\w*|" \
        r"suppress\w*|resolv\w*|lower\w*)"
_BAD = r"(?:exacerbat\w*|aggravat\w*|worsen\w*|promot\w*|enhanc\w*|increas\w*|accelerat\w*|augment\w*|" \
       r"more severe|induc\w*)"
_CUES = [
    ("inhibition_benefit", re.compile(rf"\b{_LOSS}\b.{{0,120}}?\b{_GOOD}", re.I)),
    ("inhibition_harm", re.compile(rf"\b{_LOSS}\b.{{0,120}}?\b{_BAD}", re.I)),
    ("activation_benefit", re.compile(rf"\b{_GAIN}\b.{{0,120}}?\b{_GOOD}", re.I)),
    ("activation_harm", re.compile(rf"\b{_GAIN}\b.{{0,120}}?\b{_BAD}", re.I)),
]
_SECTION_WEIGHT = {"results": 3, "discussion": 2, "conclusion": 2, "abstract": 2, "title": 1, "methods": 0}
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(])")


# a negation of a harm ("not associated with safety concerns") is not a negative result
_BENIGN_AFTER = re.compile(r"^.{0,40}?(safety|adverse|toxic|side[- ]effect|serious|concern|harm|worse)", re.I)


def is_negative(text: str) -> bool:
    low = text.lower()
    for cue in NEGATIVE_CUES:
        start = low.find(cue)
        while start != -1:
            if not _BENIGN_AFTER.match(low[start + len(cue):]):
                return True
            start = low.find(cue, start + 1)
    return False


def classify(text: str) -> dict:
    cue = next((name for name, rx in _CUES if rx.search(text)), None)
    return {"negative": is_negative(text), "cue": cue}


def _clip(text: str, n: int = 360) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def select_sentences(rows: list[dict], limit: int = 6) -> list[dict]:
    """Best text-mining sentences: results/discussion first, negative findings and cues up, one per paper
    before a second one, duplicates removed."""
    seen: set[str] = set()
    scored = []
    for r in rows:
        text = _clip(r.get("text") or "")
        key = text.lower()[:160]
        if not text or key in seen:
            continue
        seen.add(key)
        c = classify(text)
        section = (r.get("section") or "other").lower()
        score = _SECTION_WEIGHT.get(section, 1) + c["negative"] + (1 if c["cue"] else 0) + (r.get("year") or 0) / 10000
        scored.append((score, {"pmid": r.get("pmid"), "year": r.get("year"), "section": section, "text": text, **c}))
    scored.sort(key=lambda x: -x[0])
    first, rest, used = [], [], set()
    for _, s in scored:
        (rest if s["pmid"] in used else first).append(s)
        used.add(s["pmid"])
    return (first + rest)[:limit]


def _text(el: ET.Element) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def full_text_sentences(xml: str, terms: list[str], context_terms: list[str] = (), limit: int = 10) -> dict:
    """Sentences of a JATS full text that mention any of ``terms`` (the gene), grouped by section."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        return {"error": f"could not parse full text: {exc}"}
    title = _text(root.find(".//article-title")) if root.find(".//article-title") is not None else ""
    low_terms = [t.lower() for t in terms if t]
    low_ctx = [t.lower() for t in context_terms if t]
    out = []
    body = root.find(".//body")
    sections = body.findall(".//sec") if body is not None else []
    for sec in sections or ([body] if body is not None else []):
        head = sec.find("title")
        name = _text(head) if head is not None else ""
        kind = next((k for k in _SECTION_WEIGHT if k in name.lower()), "other")
        for para in sec.findall("p"):
            for sent in _SPLIT.split(_text(para)):
                low = sent.lower()
                if not any(re.search(rf"\b{re.escape(t)}\b", low) for t in low_terms):
                    continue
                c = classify(sent)
                score = (_SECTION_WEIGHT.get(kind, 1) + 2 * any(t in low for t in low_ctx)
                         + 2 * c["negative"] + (1 if c["cue"] else 0))
                out.append((score, {"section": name or kind, "text": _clip(sent), **c}))
    out.sort(key=lambda x: -x[0])
    return {"title": title, "sentences": [s for _, s in out[:limit]], "matches": len(out)}
