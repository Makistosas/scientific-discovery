"""Consistency checks for the `ant apply` resource files in managed_agents/."""

import re
from pathlib import Path

import yaml

from discovery_agent.monitor import WatchConfig

ROOT = Path(__file__).resolve().parents[1] / "managed_agents"


def frontmatter(path: Path) -> tuple[dict, str]:
    _, fm, body = path.read_text(encoding="utf-8").split("---", 2)
    return yaml.safe_load(fm), body.strip()


def test_agents_reference_existing_files_and_models():
    lead, lead_prompt = frontmatter(ROOT / "agents" / "discovery-lead.md")
    worker, worker_prompt = frontmatter(ROOT / "agents" / "evidence-investigator.md")
    assert lead["model"]["id"] == "claude-opus-5-5" and worker["model"]["id"] == "claude-sonnet-5-5"
    ma = lead["multiagent"]
    assert ma["type"] == "multiagent_20261001"
    for member in ("subagents", "workflows"):
        assert ma[member]["type"] == "enabled" and ma[member]["inline_agents"] == {"type": "disabled"}
        for ref in ma[member]["predefined_agents"]:
            assert (ROOT / "agents" / ref).resolve().exists()
    assert "multiagent" not in worker  # one level of delegation only
    # workflows are on, so the prompt must say when (conditionally) to start a run
    assert "workflow run" in lead_prompt and "more than six candidates" in lead_prompt
    for agent in (lead, worker):
        configs = {c["name"]: c for c in agent["tools"][0]["configs"]}
        assert configs.get("web_search", {}).get("enabled", False) is False
        assert configs.get("web_fetch", {}).get("enabled", False) is False
        assert agent["tools"][0]["default_config"]["permission_policy"] == {"type": "auto"}
    assert "python -m discovery_agent tool" in worker_prompt


def test_deployment_is_complete_and_secret_free():
    dep, body = frontmatter(ROOT / "deployments" / "weekly-discovery.md")
    assert body == ""  # kickoff is the outcome in initial_events, not a message
    for key in ("agent", "environment_id"):
        assert (ROOT / "deployments" / dep[key]).resolve().exists()
    repo = next(r for r in dep["resources"] if r.get("type") == "github_repository")
    assert repo["url"].endswith("/scientific-discovery") and "authorization_token" not in repo
    assert repo["checkout"] == {"type": "branch", "name": "main"}
    mem = next(r for r in dep["resources"] if "path" in r)
    assert (ROOT / "deployments" / mem["path"]).resolve().exists() and mem["access"] == "read_write"
    assert dep["schedule"]["timezone"] == "Europe/Vilnius"
    assert dep["budget"]["max_list_cost"] == {"amount": "2000", "currency": "USD"}
    outcome = dep["initial_events"][0]
    assert outcome["type"] == "user.define_outcome" and outcome["rubric"]["type"] == "text"
    criteria = re.findall(r"^\s*\d+\.", outcome["rubric"]["content"], re.M)
    assert 5 <= len(criteria) <= 10  # explicit, independently gradeable criteria
    text = (ROOT / "deployments" / "weekly-discovery.md").read_text(encoding="utf-8").lower()
    assert "token:" not in text and "api_key" not in text


def test_environment_allows_every_host_the_pipeline_calls():
    from discovery_agent.http import HOST_MIN_INTERVAL

    env = yaml.safe_load((ROOT / "environments" / "discovery-cloud.yaml").read_text(encoding="utf-8"))
    net = env["config"]["networking"]
    assert net["type"] == "limited" and net["allow_package_managers"] is True
    assert set(HOST_MIN_INTERVAL) <= set(net["allowed_hosts"])
    pip = " ".join(env["config"]["packages"]["pip"])
    for dep in ("requests", "pydantic", "click", "PyYAML", "setuptools"):
        assert dep in pip


def test_watchlist_loads_and_prompt_paths_match():
    cfg = WatchConfig.load(ROOT / "watchlist.yaml")
    assert cfg.watches and cfg.language in ("lt", "en")
    _, lead_prompt = frontmatter(ROOT / "agents" / "discovery-lead.md")
    store = yaml.safe_load((ROOT / "memory_stores" / "discovery-memory.yaml").read_text(encoding="utf-8"))
    assert f"/mnt/memory/{store['name']}/state" in lead_prompt
    assert "managed_agents/watchlist.yaml" in lead_prompt
