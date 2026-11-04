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
    data = json.loads((out / "result.json").read_text())
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
