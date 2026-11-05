"""Data versions of the sources behind a run, for reproducible reports.

Open Targets (data release), ChEMBL (release), UniProt (release header), ClinicalTrials.gov (data timestamp).
Literature sources (Europe PMC, PubMed, bioRxiv) have no release; their date is the query date. Each lookup
is cached for a day and failures are reported as unknown rather than failing the run.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .. import __version__
from ..http import HttpClient

OT_GRAPHQL = "https://api.platform.opentargets.org/api/v4/graphql"
_TTL = 24.0


def _ot(http: HttpClient) -> str:
    q = "{ meta { dataVersion { year month iteration } apiVersion { x y z } } }"
    m = (http.post_json(OT_GRAPHQL, {"query": q}, ttl_hours=_TTL).get("data") or {}).get("meta") or {}
    d, a = m.get("dataVersion") or {}, m.get("apiVersion") or {}
    rel = f"{d.get('year')}.{d.get('month')}" + (f".{d['iteration']}" if d.get("iteration") else "")
    return f"{rel} (API {a.get('x')}.{a.get('y')}.{a.get('z')})"


def _chembl(http: HttpClient) -> str:
    s = http.get_json("https://www.ebi.ac.uk/chembl/api/data/status.json", ttl_hours=_TTL)
    return f"{s.get('chembl_db_version')} ({s.get('chembl_release_date')})"


def _uniprot(http: HttpClient) -> str:
    h = http.get_headers("https://rest.uniprot.org/uniprotkb/P04637.json", {"fields": "accession"}, ttl_hours=_TTL)
    return f"{h.get('x-uniprot-release')} ({h.get('x-uniprot-release-date')})"


def _trials(http: HttpClient) -> str:
    v = http.get_json("https://clinicaltrials.gov/api/v2/version", ttl_hours=_TTL)
    return f"data {v.get('dataTimestamp')} (API {v.get('apiVersion')})"


def data_versions(http: HttpClient) -> dict:
    out = {"tool": f"discovery-agent {__version__}",
           "queried": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "cache_ttl_hours": http.settings.cache_ttl_hours}
    for name, fn in (("open_targets", _ot), ("chembl", _chembl), ("uniprot", _uniprot),
                     ("clinicaltrials_gov", _trials)):
        try:
            out[name] = fn(http)
        except Exception as exc:  # a version lookup must never break a run
            out[name] = f"unknown ({str(exc)[:80]})"
    out["literature"] = "Europe PMC, PubMed, bioRxiv/medRxiv: live, as of the query date"
    return out


def sources_data_versions(sources) -> dict:
    """Versions for a :class:`Sources` bundle; test doubles without an HTTP client get only tool and date."""
    http = getattr(getattr(sources, "opentargets", None), "http", None)
    if isinstance(http, HttpClient):
        return data_versions(http)
    return {"tool": f"discovery-agent {__version__}",
            "queried": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}


_LABELS = {"open_targets": "Open Targets", "chembl": "ChEMBL", "uniprot": "UniProt",
           "clinicaltrials_gov": "ClinicalTrials.gov", "literature": "Literature", "tool": "Tool",
           "queried": "Queried", "cache_ttl_hours": "HTTP cache TTL (h)"}


def versions_markdown(versions: dict) -> str:
    return "\n".join(f"- {_LABELS.get(k, k)}: {v}" for k, v in versions.items())
