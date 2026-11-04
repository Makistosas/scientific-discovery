"""ClinicalTrials.gov (API v2) client."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..http import HttpClient

CTGOV_API = "https://clinicaltrials.gov/api/v2/studies"
STOPPED_STATUSES = {"TERMINATED", "WITHDRAWN", "SUSPENDED"}


class Trial(BaseModel):
    nct_id: str
    title: str = ""
    status: str | None = None
    phases: list[str] = Field(default_factory=list)
    why_stopped: str | None = None
    start_date: str | None = None
    sponsor: str | None = None
    interventions: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)

    @property
    def stopped(self) -> bool:
        return (self.status or "").upper() in STOPPED_STATUSES

    @property
    def url(self) -> str:
        return f"https://clinicaltrials.gov/study/{self.nct_id}"


class TrialSearch(BaseModel):
    total: int = 0
    trials: list[Trial] = Field(default_factory=list)

    @property
    def stopped(self) -> list[Trial]:
        return [t for t in self.trials if t.stopped]


class ClinicalTrialsClient:
    FIELDS = ("NCTId,BriefTitle,OverallStatus,Phase,WhyStopped,StartDate,InterventionName,"
              "LeadSponsorName,Condition")

    def __init__(self, http: HttpClient):
        self.http = http

    def search(self, *, condition: str | None = None, term: str | None = None, intervention: str | None = None,
               page_size: int = 20, interventional_only: bool = True) -> TrialSearch:
        params: dict = {"countTotal": "true", "pageSize": max(1, min(page_size, 100)), "fields": self.FIELDS}
        if condition:
            params["query.cond"] = condition
        if term:
            params["query.term"] = term
        if intervention:
            params["query.intr"] = intervention
        if interventional_only:
            params["filter.advanced"] = "AREA[StudyType]INTERVENTIONAL"
        data = self.http.get_json(CTGOV_API, params)
        return TrialSearch(total=int(data.get("totalCount") or 0),
                           trials=[parse_study(s) for s in data.get("studies") or []])


def parse_study(s: dict) -> Trial:
    p = s.get("protocolSection") or {}
    ident = p.get("identificationModule") or {}
    status = p.get("statusModule") or {}
    design = p.get("designModule") or {}
    arms = p.get("armsInterventionsModule") or {}
    return Trial(
        nct_id=ident.get("nctId", ""), title=ident.get("briefTitle", ""),
        status=status.get("overallStatus"), why_stopped=status.get("whyStopped"),
        start_date=(status.get("startDateStruct") or {}).get("date"),
        phases=list(design.get("phases") or []),
        sponsor=((p.get("sponsorCollaboratorsModule") or {}).get("leadSponsor") or {}).get("name"),
        interventions=[i.get("name", "") for i in arms.get("interventions") or []],
        conditions=list((p.get("conditionsModule") or {}).get("conditions") or []),
    )
