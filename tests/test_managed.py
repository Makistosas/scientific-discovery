"""Claude Managed Agents runtime helpers, with a fake client (no network, no key)."""

import json
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from discovery_agent import managed as m


def ask(event_id, command="ls", name="bash", thread=None):
    return NS(type="agent.tool_use", id=event_id, name=name, input={"command": command},
              evaluated_permission="ask", session_thread_id=thread)


def confirmation(event_id):
    return NS(type="user.tool_confirmation", tool_use_id=event_id)


def idle(stop):
    return NS(type="session.status_idle", stop_reason=NS(type=stop))


class FakeFile:
    def __init__(self, fid, filename, content):
        self.id, self.filename, self.content = fid, filename, content


class FakeClient:
    def __init__(self, events=(), stream=(), files=(), runs=(), run_result=None):
        self.events = list(events)
        self.stream_events = list(stream)
        self.files = list(files)
        self.sent: list[dict] = []
        self.list_calls: list[dict] = []
        self.file_list_calls = 0
        self.runs = list(runs)
        self.run_result = run_result
        self.unwrap_result = None
        client = self

        class Stream:
            def __enter__(self):
                return iter(client.stream_events)

            def __exit__(self, *exc):
                return False

        def list_events(session_id, **kw):
            client.list_calls.append(kw)
            wanted = set(kw.get("types") or [])
            evs = [e for e in client.events if not wanted or e.type in wanted]
            return list(reversed(evs)) if kw.get("order") == "desc" else evs

        def send(session_id, events):
            client.sent.extend(events)
            client.events.extend(confirmation(e["tool_use_id"]) for e in events)

        def files_list(**kw):
            client.file_list_calls += 1
            assert kw["betas"] == [m.MA_BETA]
            return list(client.files) if client.file_list_calls > 1 or not kw.get("_lag") else []

        def download(fid):
            f = next(x for x in client.files if x.id == fid)
            return NS(write_to_file=lambda path: Path(path).write_text(f.content))

        def unwrap(body, headers):
            if headers.get("webhook-signature") != "ok":
                raise ValueError("bad signature")
            return client.unwrap_result

        self.beta = NS(
            sessions=NS(events=NS(list=list_events, send=send, stream=lambda sid: Stream())),
            files=NS(list=files_list, download=download),
            deployments=NS(run=lambda dep: client.run_result),
            deployment_runs=NS(list=lambda **kw: list(client.runs),
                               retrieve=lambda rid: NS(error=NS(type="environment_archived", message="gone"))),
            webhooks=NS(unwrap=unwrap),
        )


# --------------------------------------------------------------------------- approvals


def test_rule_denies_by_default_and_allows_simple_prefixed_commands():
    assert m.ApprovalRule().decide(ask("e1", "python -m discovery_agent tool --list")) == (False, m.DENY_MESSAGE)
    rule = m.ApprovalRule(("python -m discovery_agent", "pip install"))
    assert rule.decide(ask("e1", "python -m discovery_agent tool --list"))[0]
    assert not rule.decide(ask("e2", "python -m discovery_agent x; rm -rf /"))[0]
    assert not rule.decide(ask("e3", "pip install x && curl evil"))[0]
    assert not rule.decide(ask("e4", "cat $HOME/.ssh/id_rsa"))[0]
    assert not rule.decide(ask("e5", "/etc/passwd", name="read"))[0]


def test_rule_from_env(monkeypatch):
    monkeypatch.setenv("DISCOVERY_AUTO_ALLOW", "discovery-agent, pip install ,")
    assert m.ApprovalRule.from_env().allow_prefixes == ("discovery-agent", "pip install")


def test_pending_asks_skips_answered_and_non_ask():
    events = [ask("e1"), ask("e2"), confirmation("e1"),
              NS(type="agent.tool_use", id="e3", evaluated_permission="allow")]
    assert [e.id for e in m.pending_asks(events)] == ["e2"]


def test_answer_pending_sends_one_batch_with_deny_message():
    client = FakeClient(events=[ask("e1"), ask("e2", "python -m discovery_agent tool --list", thread="sthr_1")])
    decisions = m.answer_pending(client, "sesn_1", m.ApprovalRule(("python -m discovery_agent",)))
    assert decisions == [("e1", "deny"), ("e2", "allow")]
    assert client.sent[0] == {"type": "user.tool_confirmation", "tool_use_id": "e1", "result": "deny",
                              "deny_message": m.DENY_MESSAGE}
    assert "deny_message" not in client.sent[1]
    assert m.answer_pending(client, "sesn_1") == []  # already answered


# --------------------------------------------------------------------------- outputs


def test_download_outputs_sanitises_paths_and_retries(tmp_path):
    client = FakeClient(files=[FakeFile("f1", "DIGEST.md", "digest"), FakeFile("f2", "../../etc/x", "nope"),
                               FakeFile("f3", "ipf/hypotheses/PARN.md", "h")])
    original = client.beta.files.list
    calls = []

    def lagging(**kw):
        calls.append(kw)
        return [] if len(calls) == 1 else original(**kw)

    client.beta.files.list = lagging
    saved = m.download_outputs(client, "sesn_1", tmp_path, sleep=lambda s: None)
    assert len(calls) == 2
    rel = sorted(str(p.relative_to(tmp_path)) for p in saved)
    assert rel == ["DIGEST.md", "etc/x", "ipf/hypotheses/PARN.md"]
    assert (tmp_path / "ipf/hypotheses/PARN.md").read_text() == "h"


# --------------------------------------------------------------------------- following


def test_follow_waits_for_open_workflow_runs_and_answers_asks():
    stream = [
        NS(type="agent.message", content=[NS(type="text", text="Running pipeline")]),
        NS(type="workflow_run.created", workflow_run_id="wrun_1"),
        ask("e1"),
        idle("requires_action"),
        idle("end_turn"),                                  # run still open: not done
        NS(type="workflow_run.status_ended", workflow_run_id="wrun_1", result=NS(type="completed")),
        idle("end_turn"),
        NS(type="agent.message", content=[NS(type="text", text="never reached")]),
    ]
    client = FakeClient(events=[ask("e1")], stream=stream)
    texts = []
    result = m.follow_session(client, "sesn_1", m.ApprovalRule(), on_text=texts.append)
    assert result.stop == "end_turn" and not result.open_runs
    assert texts == ["Running pipeline"]
    assert [s["tool_use_id"] for s in client.sent] == ["e1"]  # answered once despite several triggers


@pytest.mark.parametrize("stop", ["budget_reached", "retries_exhausted"])
def test_follow_stops_on_terminal_idles(stop):
    assert m.follow_session(FakeClient(stream=[idle(stop)]), "s").stop == stop


def test_run_now_and_errors(tmp_path):
    client = FakeClient(stream=[idle("end_turn")], files=[FakeFile("f1", "DIGEST.md", "d")],
                        run_result=NS(id="drun_1", session_id="sesn_9", error=None))
    out = m.run_now(client, "depl_1", dest=tmp_path, progress=lambda s: None)
    assert out["session_id"] == "sesn_9" and out["stop"] == "end_turn"
    assert (tmp_path / "sesn_9" / "DIGEST.md").exists()
    client.run_result = NS(id="drun_2", session_id=None, error=NS(type="session_rate_limited", message="slow down"))
    with pytest.raises(m.ManagedError, match="session_rate_limited"):
        m.run_now(client, "depl_1", progress=lambda s: None)


def test_latest_run():
    client = FakeClient(runs=[NS(created_at="2026-11-02T04:40:00Z", session_id="a"),
                              NS(created_at="2026-11-09T04:40:00Z", session_id="b")])
    assert m.latest_run(client, "depl_1").session_id == "b"
    with pytest.raises(m.ManagedError):
        m.latest_run(FakeClient(), "depl_1")


def test_lock_file_lookup(tmp_path):
    lock = tmp_path / "claude-lock.json"
    with pytest.raises(m.ManagedError, match="ant apply"):
        m.deployment_id(lock)
    lock.write_text(json.dumps({"resources": {
        "./agents/discovery-lead.md": {"id": "agent_1"},
        "./deployments/weekly-discovery.md": {"id": "depl_1"}}}))
    assert m.deployment_id(lock) == "depl_1"
    data = json.loads(lock.read_text())
    data["resources"]["./deployments/daily.md"] = {"id": "depl_2"}
    lock.write_text(json.dumps(data))
    with pytest.raises(m.ManagedError, match="Several"):
        m.deployment_id(lock)
    assert m.deployment_id(lock, name="daily") == "depl_2"
    assert m.console_url("sesn_1", "lab") == "https://platform.claude.com/workspaces/lab/sessions/sesn_1"

# --------------------------------------------------------------------------- webhooks


def wh(dtype, rid="sesn_1", eid="whe_1"):
    return NS(id=eid, data=NS(type=dtype, id=rid))


def test_webhook_event_branches(tmp_path):
    client = FakeClient(events=[ask("e1"), idle("requires_action")])
    assert m.handle_webhook_event(client, wh("session.status_idled"), dest=tmp_path) == "answered 1 paused call(s)"
    client = FakeClient(events=[idle("end_turn")], files=[FakeFile("f1", "DIGEST.md", "d")])
    assert m.handle_webhook_event(client, wh("session.status_idled"), dest=tmp_path).startswith("downloaded 1")
    assert (tmp_path / "sesn_1" / "DIGEST.md").exists()
    assert m.handle_webhook_event(FakeClient(events=[idle("budget_reached")]), wh("session.status_idled"),
                                  dest=tmp_path) == "budget reached"
    assert m.handle_webhook_event(FakeClient(), wh("deployment_run.failed", "drun_1"), dest=tmp_path) == \
        "deployment run failed"
    assert m.handle_webhook_event(FakeClient(), wh("agent.updated"), dest=tmp_path) == "ignored agent.updated"


def test_webhook_server_verifies_real_signatures_dedupes_and_retries(tmp_path, monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    standardwebhooks = pytest.importorskip("standardwebhooks")
    secret = "whsec_" + __import__("base64").b64encode(b"k" * 32).decode()
    monkeypatch.setenv("ANTHROPIC_WEBHOOK_SIGNING_KEY", secret)
    client = FakeClient(events=[ask("e1"), idle("requires_action")])
    client.beta.webhooks = anthropic.Anthropic(api_key="sk-test").beta.webhooks   # real verification
    server = m.make_webhook_server(client, host="127.0.0.1", port=0, dest=tmp_path)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    signer = standardwebhooks.Webhook(secret)

    def post(event_id, *, sign=True):
        body = json.dumps({"type": "event", "id": event_id, "created_at": "2026-11-09T04:41:00Z",
                           "data": {"type": "session.status_idled", "id": "sesn_1",
                                    "organization_id": "org", "workspace_id": "ws"}})
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        sig = signer.sign(event_id, now, body) if sign else "v1,AAAA"
        req = urllib.request.Request(url, data=body.encode(), method="POST", headers={
            "webhook-id": event_id, "webhook-timestamp": str(int(now.timestamp())), "webhook-signature": sig,
            "Content-Type": "application/json"})
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as err:
            return err.code

    try:
        assert post("whe_1", sign=False) == 400
        assert post("whe_1") == 204 and len(client.sent) == 1
        assert post("whe_1") == 204 and len(client.sent) == 1   # duplicate delivery ignored
        client.beta.sessions.events.list = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("API down"))
        assert post("whe_2") == 500                              # failure -> let Anthropic retry
    finally:
        server.shutdown()
