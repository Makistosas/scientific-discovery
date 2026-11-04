"""Literature sources: Europe PMC (articles, bioRxiv/medRxiv preprints, patents),
NCBI PubMed E-utilities and the bioRxiv API."""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable

from pydantic import BaseModel, Field

from ..http import HttpClient

EUROPEPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
BIORXIV_API = "https://api.biorxiv.org"

# Gene symbols that are also ordinary words / abbreviations: never search them alone.
_AMBIGUOUS_SYMBOLS = {
    "CAT", "SET", "MET", "REST", "MAX", "CAD", "SHE", "WAS", "AGO", "CAPS", "CLOCK", "PIGS", "SOS", "ACE",
    "IMPACT", "GAS", "TEC", "MICE", "NOV", "SON", "CAR", "ATM", "AIR", "ARC", "BAD", "BID", "CA2", "GC",
    "HR", "IL", "KIT", "MAL", "NHS", "PC", "PET", "PIK", "POLE", "RAN", "RET", "SDS", "STAR", "TANK", "TOP",
}


class Article(BaseModel):
    id: str
    source: str  # MED, PMC, PPR (preprint), PAT (patent), AGR, …
    title: str = ""
    pmid: str | None = None
    pmcid: str | None = None
    doi: str | None = None
    journal: str | None = None
    publisher: str | None = None
    year: int | None = None
    first_publication_date: str | None = None
    authors: str | None = None
    cited_by: int | None = None

    @property
    def is_preprint(self) -> bool:
        return self.source == "PPR"

    @property
    def is_patent(self) -> bool:
        return self.source == "PAT"

    @property
    def url(self) -> str:
        if self.pmid:
            return f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/"
        if self.doi:
            return f"https://doi.org/{self.doi}"
        return f"https://europepmc.org/article/{self.source}/{self.id}"


class SearchResult(BaseModel):
    query: str
    hit_count: int
    articles: list[Article] = Field(default_factory=list)


def _quote(term: str) -> str:
    return '"' + term.replace('"', "") + '"'


def gene_terms(symbol: str, name: str | None = None, synonyms: Iterable[str] = ()) -> list[str]:
    """Search terms for a gene, avoiding symbols that collide with common words."""
    terms: list[str] = []
    sym = symbol.strip()
    if sym and (len(sym) > 2 and sym.upper() not in _AMBIGUOUS_SYMBOLS):
        terms.append(sym)
    if name and len(name) > 4:
        terms.append(name)
    terms.extend(s for s in synonyms if s and len(s) > 3 and s.upper() not in _AMBIGUOUS_SYMBOLS)
    if not terms and sym:
        terms.append(sym)
    seen, out = set(), []
    for t in terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:4]


def disease_terms(name: str, synonyms: Iterable[str] = ()) -> list[str]:
    out = [name]
    out.extend(s for s in synonyms if s and s.lower() != name.lower())
    return out[:4]


def title_abstract_clause(terms: Iterable[str]) -> str:
    """Europe PMC clause matching any of the terms in title or abstract."""
    parts = []
    for t in terms:
        q = _quote(t)
        parts.append(f"TITLE:{q} OR ABSTRACT:{q}")
    return "(" + " OR ".join(parts) + ")"


def cooccurrence_query(genes: Iterable[str], diseases: Iterable[str]) -> str:
    return f"{title_abstract_clause(genes)} AND {title_abstract_clause(diseases)}"


def date_clause(since: date | str | None, until: date | str | None = None) -> str:
    if not since:
        return ""
    end = until or "3000-12-31"
    return f" AND FIRST_PDATE:[{since} TO {end}]"


class EuropePMCClient:
    def __init__(self, http: HttpClient):
        self.http = http

    def search(self, query: str, *, page_size: int = 25, sort: str | None = "P_PDATE_D desc",
               result_type: str = "lite", ttl_hours: float | None = None) -> SearchResult:
        params = {"query": query, "format": "json", "pageSize": max(1, min(page_size, 1000)),
                  "resultType": result_type}
        if sort:
            params["sort"] = sort
        data = self.http.get_json(EUROPEPMC_SEARCH, params, ttl_hours=ttl_hours)
        articles = [parse_article(r) for r in (data.get("resultList") or {}).get("result", [])]
        return SearchResult(query=query, hit_count=int(data.get("hitCount") or 0), articles=articles)

    def count(self, query: str, *, ttl_hours: float | None = None) -> int:
        params = {"query": query, "format": "json", "pageSize": 1, "resultType": "idlist"}
        data = self.http.get_json(EUROPEPMC_SEARCH, params, ttl_hours=ttl_hours)
        return int(data.get("hitCount") or 0)

    # Convenience wrappers ---------------------------------------------------
    def publications(self, genes: list[str], diseases: list[str], *, since: date | str | None = None,
                     page_size: int = 10, ttl_hours: float | None = None) -> SearchResult:
        q = f"{cooccurrence_query(genes, diseases)} AND (SRC:MED OR SRC:PMC){date_clause(since)}"
        return self.search(q, page_size=page_size, ttl_hours=ttl_hours)

    def preprints(self, genes: list[str], diseases: list[str], *, since: date | str | None = None,
                  page_size: int = 10, ttl_hours: float | None = None) -> SearchResult:
        q = f"{cooccurrence_query(genes, diseases)} AND SRC:PPR{date_clause(since)}"
        return self.search(q, page_size=page_size, result_type="core", ttl_hours=ttl_hours)

    def patents(self, genes: list[str], diseases: list[str], *, page_size: int = 10) -> SearchResult:
        # Patent abstracts are short; full-record matching is more sensitive here.
        g = " OR ".join(_quote(t) for t in genes)
        d = " OR ".join(_quote(t) for t in diseases)
        return self.search(f"({g}) AND ({d}) AND SRC:PAT", page_size=page_size)


def parse_article(r: dict) -> Article:
    year = r.get("pubYear")
    publisher = (r.get("bookOrReportDetails") or {}).get("publisher")
    journal = r.get("journalTitle") or ((r.get("journalInfo") or {}).get("journal") or {}).get("title")
    return Article(
        id=str(r.get("id") or r.get("pmid") or r.get("doi") or ""),
        source=r.get("source") or "",
        title=(r.get("title") or "").strip(),
        pmid=r.get("pmid"), pmcid=r.get("pmcid"), doi=r.get("doi"),
        journal=journal, publisher=publisher,
        year=int(year) if year and str(year).isdigit() else None,
        first_publication_date=r.get("firstPublicationDate"),
        authors=r.get("authorString"),
        cited_by=r.get("citedByCount"),
    )


class PubMedClient:
    def __init__(self, http: HttpClient):
        self.http = http

    def _base_params(self) -> dict:
        params = {"tool": "scientific-discovery-agent"}
        if self.http.settings.ncbi_api_key:
            params["api_key"] = self.http.settings.ncbi_api_key
        if self.http.settings.contact_email:
            params["email"] = self.http.settings.contact_email
        return params

    @staticmethod
    def cooccurrence_term(genes: Iterable[str], diseases: Iterable[str]) -> str:
        g = " OR ".join(f'"{t}"[tiab]' for t in genes)
        d = " OR ".join(f'"{t}"[tiab]' for t in diseases)
        return f"({g}) AND ({d})"

    def search(self, term: str, *, retmax: int = 20, mindate: str | None = None,
               maxdate: str | None = None) -> tuple[int, list[str]]:
        params = {**self._base_params(), "db": "pubmed", "term": term, "retmode": "json",
                  "retmax": retmax, "sort": "pub_date"}
        if mindate:
            params.update({"datetype": "pdat", "mindate": mindate, "maxdate": maxdate or "3000"})
        data = self.http.get_json(f"{EUTILS}/esearch.fcgi", params)
        res = data.get("esearchresult") or {}
        return int(res.get("count") or 0), list(res.get("idlist") or [])

    def count(self, term: str) -> int:
        return self.search(term, retmax=0)[0]

    def summaries(self, pmids: list[str]) -> list[Article]:
        if not pmids:
            return []
        params = {**self._base_params(), "db": "pubmed", "id": ",".join(pmids), "retmode": "json"}
        data = self.http.get_json(f"{EUTILS}/esummary.fcgi", params)
        result = data.get("result") or {}
        out = []
        for pmid in result.get("uids", []):
            r = result.get(pmid) or {}
            doi = next((a.get("value") for a in r.get("articleids", []) if a.get("idtype") == "doi"), None)
            m = re.match(r"(\d{4})", r.get("pubdate") or "")
            out.append(Article(
                id=pmid, source="MED", pmid=pmid, doi=doi, title=r.get("title") or "",
                journal=r.get("fulljournalname") or r.get("source"),
                year=int(m.group(1)) if m else None,
                first_publication_date=r.get("sortpubdate"),
                authors=", ".join(a.get("name", "") for a in (r.get("authors") or [])[:6]),
            ))
        return out


class BioRxivClient:
    """bioRxiv/medRxiv API: preprint metadata and whether a preprint was later published.

    The bioRxiv API has no keyword search, so preprints are *found* via Europe PMC
    (which indexes bioRxiv/medRxiv) and *checked* here.
    """

    def __init__(self, http: HttpClient):
        self.http = http

    @staticmethod
    def server_for(publisher: str | None, doi: str | None = None) -> str | None:
        p = (publisher or "").lower()
        if "medrxiv" in p:
            return "medrxiv"
        if "biorxiv" in p:
            return "biorxiv"
        if doi and doi.startswith("10.1101/"):
            return "biorxiv"
        return None

    def details(self, doi: str, server: str = "biorxiv") -> dict | None:
        data = self.http.get_json(f"{BIORXIV_API}/details/{server}/{doi}")
        coll = data.get("collection") or []
        return coll[-1] if coll else None  # latest version

    def published_doi(self, doi: str, server: str = "biorxiv") -> str | None:
        rec = self.details(doi, server)
        if not rec:
            return None
        pub = rec.get("published")
        return pub if pub and pub.upper() != "NA" else None

    def recent(self, server: str, start: str, end: str, cursor: int = 0) -> list[dict]:
        data = self.http.get_json(f"{BIORXIV_API}/details/{server}/{start}/{end}/{cursor}")
        return data.get("collection") or []
