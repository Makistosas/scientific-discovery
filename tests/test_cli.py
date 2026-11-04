import json

from click.testing import CliRunner

import conftest
from discovery_agent import cli


def patch_sources(monkeypatch):
    fake = conftest.make_sources()
    monkeypatch.setattr(cli.Sources, "create", classmethod(lambda cls, settings=None, http=None: fake))
    return fake


def test_discover_disease(tmp_path, monkeypatch):
    patch_sources(monkeypatch)
    out = tmp_path / "rep"
    res = CliRunner().invoke(cli.main, ["--lang", "en", "--cache-dir", str(tmp_path / "c"), "discover",
                                        "--disease", "test fibrosis", "--top", "3", "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert "GENEA" in res.output
    data = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert data["result"]["mode"] == "disease"
    assert (out / "report.md").exists()


def test_discover_requires_exactly_one_query(monkeypatch, tmp_path):
    patch_sources(monkeypatch)
    res = CliRunner().invoke(cli.main, ["--cache-dir", str(tmp_path), "discover"])
    assert res.exit_code != 0 and "exactly one" in res.output


def test_hypothesis_command_lithuanian(tmp_path, monkeypatch):
    patch_sources(monkeypatch)
    res = CliRunner().invoke(cli.main, ["--cache-dir", str(tmp_path / "c"), "hypothesis", "-t", "GENEA",
                                        "-d", "test fibrosis", "--out", str(tmp_path / "h")])
    assert res.exit_code == 0, res.output
    assert "Hipotezė" in res.output and "Alternatyvūs paaiškinimai" in res.output


def test_watch_once_and_alerts(tmp_path, monkeypatch):
    patch_sources(monkeypatch)
    cfg = tmp_path / "watch.yaml"
    db = tmp_path / "db.sqlite"
    cfg.write_text(f"language: en\noutput_dir: {tmp_path / 'out'}\ndatabase: {db}\n"
                   "watches:\n  - name: fib\n    disease: test fibrosis\n    top_k: 3\n    enrich_k: 3\n")
    res = CliRunner().invoke(cli.main, ["--cache-dir", str(tmp_path / "c"), "watch", str(cfg), "--once"])
    assert res.exit_code == 0, res.output
    assert "fib:" in res.output
    res = CliRunner().invoke(cli.main, ["alerts", "--db", str(db)])
    assert res.exit_code == 0 and "baseline" in res.output


def test_search(monkeypatch, tmp_path):
    patch_sources(monkeypatch)
    res = CliRunner().invoke(cli.main, ["--cache-dir", str(tmp_path), "search", "fibrosis"])
    assert res.exit_code == 0 and "EFO_0000001" in res.output


def test_tool_command(monkeypatch, tmp_path):
    patch_sources(monkeypatch)
    runner = CliRunner()
    res = runner.invoke(cli.main, ["--cache-dir", str(tmp_path), "tool", "--list"])
    names = [json.loads(line)["name"] for line in res.output.splitlines()]
    assert res.exit_code == 0 and "search_literature" in names and "record_evidence" not in names
    res = runner.invoke(cli.main, ["--cache-dir", str(tmp_path), "tool", "search_literature",
                                   '{"query": "GENEA", "source": "articles", "since": "", "max_results": 3}'])
    assert res.exit_code == 0 and json.loads(res.output)["hit_count"] == 2
    res = runner.invoke(cli.main, ["--cache-dir", str(tmp_path), "tool", "get_open_targets_evidence",
                                   '{"group": "genetic"}', "-t", "GENEA", "-d", "test fibrosis"])
    assert res.exit_code == 0 and json.loads(res.output)["n_rows"] == 2
    res = runner.invoke(cli.main, ["--cache-dir", str(tmp_path), "tool", "get_open_targets_evidence",
                                   '{"group": "genetic"}'])
    assert res.exit_code == 2 and "needs a target" in res.output
    res = runner.invoke(cli.main, ["--cache-dir", str(tmp_path), "tool", "record_evidence", "{}"])
    assert res.exit_code != 0


def test_watch_with_json_state_override(tmp_path, monkeypatch):
    patch_sources(monkeypatch)
    cfg = tmp_path / "watch.yaml"
    cfg.write_text("language: en\nwatches:\n  - name: fib\n    disease: test fibrosis\n    top_k: 3\n    enrich_k: 3\n")
    state = tmp_path / "mem" / "state"
    res = CliRunner().invoke(cli.main, ["--cache-dir", str(tmp_path / "c"), "watch", str(cfg), "--once",
                                        "--database", str(state), "--output-dir", str(tmp_path / "outputs")])
    assert res.exit_code == 0, res.output
    assert (state / "fib.json").exists() and (tmp_path / "outputs" / "fib" / "latest" / "report.md").exists()
    res = CliRunner().invoke(cli.main, ["alerts", "--db", str(state)])
    assert "baseline" in res.output


def test_managed_commands_need_lock_file(tmp_path, monkeypatch):
    from discovery_agent import managed as m

    monkeypatch.setattr(m, "make_client", lambda: object())
    res = CliRunner().invoke(cli.main, ["managed", "--lock", str(tmp_path / "missing.json"), "run", "--no-wait"])
    assert res.exit_code != 0 and "ant apply" in res.output
