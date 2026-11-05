"""Local MCP server: service logic with fake sources, the MCP layer in memory, and a stdio smoke test."""

import asyncio
import json
import os
import sys

import pytest

import conftest
from discovery_agent.mcp_server import DiscoveryService, ServiceError, client_config

mcp = pytest.importorskip("mcp")


@pytest.fixture
def service(tmp_path, settings):
    return DiscoveryService(settings.with_overrides(data_dir=tmp_path / "data"), sources=conftest.make_sources())


def text(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


# --------------------------------------------------------------------------- service


def test_find_opportunities_saves_report_and_dossiers(service):
    res = service.find_opportunities(disease="test fibrosis", top_k=3, enrich_k=3)
    assert [o["target"] for o in res["opportunities"]][0] == "GENEA"
    assert os.path.exists(res["report_path"])
    dossier = service.store.get_dossier("ENSG00000000001|EFO_0000001")
    assert dossier and dossier["evidence_ids"][0] == "E1" and "draft_markdown" in dossier
    with pytest.raises(ServiceError, match="exactly one"):
        service.find_opportunities()


def test_analyze_pair_and_save_hypothesis_grounding(service, monkeypatch):
    res = service.analyze_pair("GENEA", "test fibrosis")
    assert res["symbol"] == "GENEA" and os.path.exists(res["report_path"])
    monkeypatch.setattr(service, "_exists", lambda kind, ref: ref != "99999999")
    md = ("GENEA LoF raises risk (E1, E2); see PMID:11111111 (in dossier), PMID:12345678 and PMID:99999999; "
          "also E99 and NCT01234567. " * 2)
    saved = service.save_hypothesis(target="GENEA", disease="test fibrosis", title="GENEA activation",
                                    markdown=md, modulation="activation", confidence="moderate")
    v = saved["verification"]
    assert {r["id"]: r["how"] for r in v["verified"]} == {"11111111": "dossier", "12345678": "online",
                                                         "NCT01234567": "online"}
    assert [r["id"] for r in v["not_found"]] == ["99999999"]
    assert any("E99" in w for w in saved["warnings"]) and any("99999999" in w for w in saved["warnings"])
    assert open(saved["path"], encoding="utf-8").read().startswith(f"<!-- id: {saved['id']}")
    listing = service.list_hypotheses()
    assert listing["total"] == 1 and listing["items"][0]["title"] == "GENEA activation"
    assert service.get_hypothesis(saved["id"])["modulation"] == "activation"
    assert service.list_hypotheses(query="nothing-matches")["total"] == 0
    with pytest.raises(ServiceError):
        service.get_hypothesis("missing")


def test_save_without_dossier_warns_and_offline_verification(service):
    saved = service.save_hypothesis(target="GENED", disease="test fibrosis", title="t", markdown="x" * 60 + " PMID:123456",
                                    verify_references=False)
    assert any("discovery_analyze_pair" in w for w in saved["warnings"])
    assert saved["verification"]["unchecked"] == [{"type": "pmid", "id": "123456"}]


def test_run_tool_and_errors(service):
    lit = service.run_tool("search_literature", {"query": "GENEA", "source": "articles", "since": "", "max_results": 2})
    assert lit["hit_count"] == 2
    ev = service.run_tool("get_open_targets_evidence", {"group": "genetic"}, target="GENEA", disease="test fibrosis")
    assert ev["n_rows"] == 2
    with pytest.raises(ServiceError, match="needs a target"):
        service.run_tool("get_open_targets_evidence", {"group": "genetic"})
    with pytest.raises(ServiceError):
        service.analyze_pair("NOPE", "test fibrosis")


def test_watchlist_template_run_and_status(service):
    status = service.status()
    assert os.path.exists(status["watchlist"]) and status["watches"][0]["name"] == "ipf"
    cfg = service.data_dir / "w.yaml"
    cfg.write_text("language: en\nwatches:\n  - name: fib\n    disease: test fibrosis\n    top_k: 3\n    enrich_k: 3\n")
    res = service.run_watchlist(config_path=str(cfg))
    assert res["runs"][0]["error"] is None and res["runs"][0]["alerts"][0]["kind"] == "baseline"
    assert service.alerts("fib")[0]["kind"] == "baseline"
    with pytest.raises(ServiceError, match="not found"):
        service.run_watchlist(config_path=str(service.data_dir / "missing.yaml"))


def test_client_config_snippets(tmp_path):
    desktop = json.loads(client_config("claude-desktop"))
    server = desktop["mcpServers"]["scientific-discovery"]
    assert server["command"] == sys.executable and server["args"] == ["-m", "discovery_agent", "mcp"]
    code = json.loads(client_config("claude-code", data_dir=str(tmp_path)))
    assert code["mcpServers"]["scientific-discovery"]["type"] == "stdio"
    assert code["mcpServers"]["scientific-discovery"]["env"]["DISCOVERY_DATA_DIR"] == str(tmp_path)
    codex = client_config("codex")
    assert "[mcp_servers.scientific-discovery]" in codex and 'args = ["-m", "discovery_agent", "mcp"]' in codex


# --------------------------------------------------------------------------- MCP layer (in memory)


def test_mcp_tools_prompts_and_errors_in_memory(service):
    from mcp import Client

    from discovery_agent.mcp_server import build_server

    server = build_server(service)
    progress: list[str] = []

    async def on_progress(p, total, message):
        progress.append(message)

    async def scenario():
        async with Client(server) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            assert len(tools) == 18 and all(n.startswith("discovery_") for n in tools)
            assert "ctx" not in tools["discovery_find_opportunities"].input_schema["properties"]
            assert tools["discovery_list_hypotheses"].annotations.read_only_hint is True
            r = await c.call_tool("discovery_find_opportunities", {"disease": "test fibrosis", "top_k": 3,
                                                                   "enrich_k": 3}, progress_callback=on_progress)
            assert not r.is_error and "GENEA" in text(r) and "discovery_analyze_pair" in text(r)
            r = await c.call_tool("discovery_find_opportunities", {"disease": "test fibrosis", "top_k": 3,
                                                                   "enrich_k": 3, "response_format": "json"})
            assert json.loads(text(r))["opportunities"][0]["target"] == "GENEA"
            r = await c.call_tool("discovery_analyze_pair", {"target": "GENEA", "disease": "test fibrosis"})
            assert "E1" in text(r)
            r = await c.call_tool("discovery_analyze_pair", {"target": "NOPE", "disease": "test fibrosis"})
            assert r.is_error and "discovery_search_entities" in text(r)
            r = await c.call_tool("discovery_save_hypothesis", {
                "target": "GENEA", "disease": "test fibrosis", "title": "GENEA", "markdown": "E1 " * 30,
                "verify_references": False})
            hid = json.loads(text(r))["id"]
            r = await c.call_tool("discovery_list_hypotheses", {})
            assert hid in text(r)
            r = await c.call_tool("discovery_get_hypothesis", {"hypothesis_id": "nope"})
            assert r.is_error
            r = await c.call_tool("discovery_list_alerts", {})
            assert "No alerts" in text(r)
            prompts = {p.name for p in (await c.list_prompts()).prompts}
            assert prompts == {"investigate_disease", "write_hypothesis", "weekly_review"}
            p = await c.get_prompt("write_hypothesis", {"target": "GENEA", "disease": "test fibrosis"})
            assert "discovery_investigate_pair" in p.messages[0].content.text
            r = await c.call_tool("discovery_investigate_pair", {"target": "GENED", "disease": "test fibrosis"})
            assert not r.is_error and json.loads(text(r))["direction"]["modulation"] == "inhibition"

    asyncio.run(scenario())
    assert progress  # pipeline progress reached the client


# --------------------------------------------------------------------------- stdio smoke test


def test_stdio_server_starts_and_lists_tools(tmp_path):
    from mcp import Client, StdioServerParameters

    env = {**os.environ, "DISCOVERY_DATA_DIR": str(tmp_path / "data"), "DISCOVERY_CACHE_DIR": str(tmp_path / "c")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "discovery_agent", "mcp"], env=env)

    async def scenario():
        async with Client(params, read_timeout_seconds=60) as c:
            return [t.name for t in (await c.list_tools()).tools]

    names = asyncio.run(scenario())
    assert "discovery_find_opportunities" in names and "discovery_save_hypothesis" in names


# --------------------------------------------------------------------------- install & self-test


def test_install_claude_desktop_merges_backs_up_and_is_idempotent(tmp_path):
    from discovery_agent.mcp_server import SERVER_KEY, install_claude_desktop

    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    res = install_claude_desktop(cfg)  # no file yet: created, nothing to back up
    assert res["status"] == "added" and res["backup"] is None and cfg.exists()

    cfg.write_text(json.dumps({"mcpServers": {"filesystem": {"command": "npx", "args": ["x"]}},
                               "globalShortcut": "Ctrl+Space"}))
    res = install_claude_desktop(cfg, data_dir=str(tmp_path / "data"))
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert res["status"] == "added" and res["other_servers"] == ["filesystem"]
    assert data["globalShortcut"] == "Ctrl+Space" and set(data["mcpServers"]) == {"filesystem", SERVER_KEY}
    assert data["mcpServers"][SERVER_KEY]["env"]["DISCOVERY_DATA_DIR"] == str(tmp_path / "data")
    assert json.loads(open(res["backup"], encoding="utf-8").read())["globalShortcut"] == "Ctrl+Space"

    again = install_claude_desktop(cfg, data_dir=str(tmp_path / "data"))
    assert again["status"] == "unchanged" and again["backup"] is None
    assert install_claude_desktop(cfg)["status"] == "updated"


def test_install_refuses_invalid_json(tmp_path):
    from discovery_agent.mcp_server import install_claude_desktop

    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text('{"mcpServers": {')
    with pytest.raises(ServiceError, match="not valid JSON"):
        install_claude_desktop(cfg)
    assert cfg.read_text(encoding="utf-8") == '{"mcpServers": {'
    cfg.write_text('{"mcpServers": []}')
    with pytest.raises(ServiceError, match="not an object"):
        install_claude_desktop(cfg)


def test_self_test_offline_against_installed_config(tmp_path, monkeypatch):
    from discovery_agent.mcp_server import install_claude_desktop, self_test

    monkeypatch.setenv("DISCOVERY_CACHE_DIR", str(tmp_path / "cache"))
    cfg = tmp_path / "claude_desktop_config.json"
    install_claude_desktop(cfg, data_dir=str(tmp_path / "data"))
    checks = self_test(cfg)
    assert all(c["ok"] for c in checks), checks
    assert [c["check"] for c in checks][:2] == ["client config", "server starts (stdio)"]
    assert str(tmp_path / "data") in checks[-1]["detail"]

    cfg.write_text(json.dumps({"mcpServers": {}}))
    failed = self_test(cfg)
    assert len(failed) == 1 and not failed[0]["ok"] and "--install" in failed[0]["detail"]


def test_key_sentences_flag_null_results():
    from discovery_agent.mcp_server import key_sentences

    text = ("Intro. GENEA variants associate with fibrosis. The GENEA inhibitor did not improve survival. "
            "Something else entirely.")
    out = key_sentences(text, ["genea", "fibrosis"])
    assert [s["negative"] for s in out] == [False, True]
    assert out[0]["text"].startswith("GENEA variants")


def test_investigate_pair_bundle_is_compact_and_saves_dossier(service):
    res = service.investigate_pair("GENED", "test fibrosis")
    assert res["direction"]["modulation"] == "inhibition" and res["locus"]["supported"]
    assert res["contradicting"] and all(set(e) >= {"id", "grade", "stance", "summary"} for e in res["evidence"])
    paper = res["papers"][0]
    assert paper["pmid"] == "55555555" and paper["negative_cue"] is True
    assert service.store.get_dossier(res["key"]) is not None
    assert len(json.dumps(res)) < 12000


def test_investigate_pair_survives_literature_outage(service):
    def down(*a, **k):
        raise RuntimeError("HTTP 503")

    service.sources.europepmc.search = down
    res = service.investigate_pair("GENEA", "test fibrosis")
    assert res["evidence"] and "503" in res["papers"][0]["error"]
