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


def test_managed_agents_files_match_official_schemas():
    """Validate managed_agents/ against the published `ant apply` JSON schemas."""
    import json
    import urllib.request
    from pathlib import Path

    import yaml

    jsonschema = pytest.importorskip("jsonschema")
    root = Path(__file__).resolve().parents[1] / "managed_agents"
    files = {"agent": ["agents/discovery-lead.md", "agents/evidence-investigator.md"],
             "environment": ["environments/discovery-cloud.yaml"],
             "memory_store": ["memory_stores/discovery-memory.yaml"],
             "deployment": ["deployments/weekly-discovery.md"]}
    for kind, paths in files.items():
        url = f"https://platform.claude.com/schemas/ant/beta/{kind}.json"
        validator = jsonschema.Draft202012Validator(json.load(urllib.request.urlopen(url, timeout=30)))
        for rel in paths:
            text = (root / rel).read_text(encoding="utf-8")
            if rel.endswith(".md"):
                _, fm, body = text.split("---", 2)
                data = yaml.safe_load(fm)
                if kind == "agent":
                    data["system"] = body.strip()
            else:
                data = yaml.safe_load(text)
            errors = [e.message for e in validator.iter_errors(data)]
            assert not errors, f"{rel}: {errors[:3]}"
