"""Smoke tests against the real public APIs. Run with: pytest -m live"""

import pytest

from discovery_agent.config import Settings
from discovery_agent.hypothesis import build_hypothesis
from discovery_agent.pipeline import DiscoveryPipeline
from discovery_agent.sources import Sources

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def live_sources(tmp_path_factory):
    settings = Settings(cache_dir=tmp_path_factory.mktemp("cache"), language="en", max_workers=3)
    return Sources.create(settings), settings


def test_open_targets_resolution(live_sources):
    sources, _ = live_sources
    assert sources.opentargets.resolve_disease("idiopathic pulmonary fibrosis").id == "EFO_0000768"
    assert sources.opentargets.resolve_target("LRRK2") == "ENSG00000188906"


def test_small_live_discovery(live_sources):
    sources, settings = live_sources
    result = DiscoveryPipeline(sources, settings).discover_disease("idiopathic pulmonary fibrosis", top_k=2,
                                                                   candidates=50, enrich_k=2)
    assert result.opportunities
    opp = result.opportunities[0]
    assert opp.enriched and opp.evidence
    h = build_hypothesis(opp, "en")
    assert h.statement and h.alternative_explanations
