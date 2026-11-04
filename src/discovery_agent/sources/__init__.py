"""Data-source clients. :class:`Sources` bundles them around one shared HTTP client."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..http import HttpClient
from .chemistry import ChemblClient, PdbClient, UniProtClient
from .clinical import ClinicalTrialsClient
from .literature import BioRxivClient, EuropePMCClient, PubMedClient
from .opentargets import OpenTargetsClient


@dataclass
class Sources:
    opentargets: OpenTargetsClient
    europepmc: EuropePMCClient
    pubmed: PubMedClient
    biorxiv: BioRxivClient
    chembl: ChemblClient
    uniprot: UniProtClient
    pdb: PdbClient
    trials: ClinicalTrialsClient

    @classmethod
    def create(cls, settings: Settings | None = None, http: HttpClient | None = None) -> "Sources":
        http = http or HttpClient(settings or Settings.from_env())
        return cls(
            opentargets=OpenTargetsClient(http),
            europepmc=EuropePMCClient(http),
            pubmed=PubMedClient(http),
            biorxiv=BioRxivClient(http),
            chembl=ChemblClient(http),
            uniprot=UniProtClient(http),
            pdb=PdbClient(http),
            trials=ClinicalTrialsClient(http),
        )


__all__ = [
    "Sources", "OpenTargetsClient", "EuropePMCClient", "PubMedClient", "BioRxivClient",
    "ChemblClient", "UniProtClient", "PdbClient", "ClinicalTrialsClient",
]
