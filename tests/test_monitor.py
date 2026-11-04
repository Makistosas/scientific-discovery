from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

import conftest
from discovery_agent.agent import DiscoveryAgent
from discovery_agent.monitor import Store, WatchConfig, Watcher, WatchItem
from discovery_agent.sources.clinical import Trial
from discovery_agent.sources.literature import Article


def make_watcher(tmp_path, sources, settings, **watch_kwargs):
    cfg = WatchConfig(output_dir=tmp_path / "out", database=tmp_path / "db.sqlite", language="en",
                      watches=[WatchItem(name="fib", disease="test fibrosis", top_k=5, enrich_k=5, **watch_kwargs)])
    watcher = Watcher(cfg, settings, sources=sources,
                      agent_factory=lambda w: DiscoveryAgent(settings, sources=sources))
    return watcher, cfg.watches[0]


def test_watch_item_validation():
    with pytest.raises(ValidationError):
        WatchItem(name="x", disease="a", target="b")
    with pytest.raises(ValidationError):
        WatchItem(name="x")
    with pytest.raises(ValidationError):
        WatchItem(name="bad name/..", disease="a")


def test_config_load(tmp_path):
    path = tmp_path / "w.yaml"
    path.write_text("language: en\nwatches:\n  - name: a\n    disease: test fibrosis\n  - name: b\n    target: GENEA\n")
    cfg = WatchConfig.load(path)
    assert [w.name for w in cfg.watches] == ["a", "b"] and cfg.interval_hours == 24


def test_first_run_is_baseline_then_changes_alert(tmp_path, sources, settings):
    watcher, watch = make_watcher(tmp_path, sources, settings)
    first = watcher.run_watch(watch)
    assert first.error is None and first.first_run
    assert [a["kind"] for a in first.alerts] == ["baseline"]
    assert first.report_path.exists() and first.digest_path.exists()
    assert watcher.store.seen_count("fib") > 0

    # second run: new paper + new trial for GENEA, and GENEA's attention changes its score
    sources.europepmc.extra_articles.append(Article(id="X1", source="MED", pmid="77777777",
                                                    title="GENEA breakthrough", year=2026))
    sources.trials.extra.append(Trial(nct_id="NCT99999999", title="GENEA first-in-human", status="RECRUITING"))
    conftest.PUB_COUNTS["GENEA"] = 250
    try:
        second = watcher.run_watch(watch)
    finally:
        conftest.PUB_COUNTS["GENEA"] = 3
    kinds = [a["kind"] for a in second.alerts]
    assert "new_publication" in kinds and "new_trial" in kinds and "score_change" in kinds
    assert "baseline" not in kinds
    assert watcher.store.recent_alerts("fib")
    assert len(list((tmp_path / "out" / "fib" / "runs").iterdir())) == 2


def test_diff_detects_new_and_dropped(tmp_path, sources, settings):
    watcher, watch = make_watcher(tmp_path, sources, settings)
    run = DiscoveryAgent(settings, sources=sources).run_disease("test fibrosis", top_k=5, enrich_k=5)
    opps = run.result.opportunities
    previous = {"OLD|KEY": {"symbol": "OLD", "disease": "x", "opportunity": 0.5}}
    alerts, changed = watcher.diff(previous, opps, watch)
    kinds = sorted(a["kind"] for a in alerts)
    assert kinds.count("new_opportunity") == len(opps) and "dropped" in kinds
    assert set(changed) == {o.key for o in opps}


def test_due_and_run_forever(tmp_path, sources, settings):
    sleeps = []
    watcher, watch = make_watcher(tmp_path, sources, settings)
    watcher._sleep = sleeps.append
    assert watcher.due(watch)
    watcher.run_forever(poll_seconds=7, max_cycles=2)
    assert sleeps == [7]
    assert not watcher.due(watch)  # just ran
    assert watcher.due(watch, now=datetime.now(timezone.utc) + timedelta(hours=25))


def test_failed_run_is_recorded(tmp_path, sources, settings):
    watcher, watch = make_watcher(tmp_path, sources, settings)

    def boom(*a, **k):
        raise RuntimeError("Open Targets down")

    sources.opentargets.disease_associations = boom
    summary = watcher.run_watch(watch)
    assert "Open Targets down" in summary.error
    assert watcher.store.last_run("fib") is None  # no successful run
    assert watcher.store.last_run("fib", status="error") is not None


def test_store_mark_seen(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    assert store.mark_seen("w", [("a", "publication"), ("b", "trial")]) == ["a", "b"]
    assert store.mark_seen("w", [("a", "publication"), ("c", "trial")]) == ["c"]
    assert store.seen_count("w") == 3
