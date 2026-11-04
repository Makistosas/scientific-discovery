"""Client-side helpers for the Claude Managed Agents deployment (see ``managed_agents/``).

The agent, its worker, the environment, the memory store and the weekly deployment are
version-controlled files synced with ``ant apply``. This module is the *data plane*:

* ``run``     – trigger the deployment now (smoke test), follow the session, download outputs;
* ``latest``  – find the newest deployment run and download what it produced;
* ``answer``  – answer tool calls that paused for approval in an unattended session;
* ``serve_webhook`` – a small HTTPS-behind-a-proxy endpoint for Console-registered webhooks:
  answers paused calls on ``session.status_idled`` and downloads outputs when a run finishes.

Paused calls are **denied by default** (nobody is watching a scheduled run). Set
``DISCOVERY_AUTO_ALLOW`` to a comma-separated list of bash command prefixes to allow, e.g.
``python -m discovery_agent,discovery-agent,pip install``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

MA_BETA = "managed-agents-2026-04-01"
DEFAULT_LOCK = Path("managed_agents/claude-lock.json")
DENY_MESSAGE = ("Unattended scheduled run: this call needed human approval and was denied automatically. "
                "Use the discovery-agent CLI (python -m discovery_agent ...) or read-only commands instead.")
TOOL_EVENT_TYPES = ("agent.tool_use", "agent.mcp_tool_use")


class ManagedError(RuntimeError):
    pass


def make_client():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ManagedError("pip install 'scientific-discovery-agent[ai]' (needs the anthropic SDK)") from exc
    return anthropic.Anthropic()


def console_url(session_id: str, workspace: str | None = None) -> str:
    ws = workspace or os.environ.get("ANTHROPIC_WORKSPACE_SLUG") or "default"
    return f"https://platform.claude.com/workspaces/{ws}/sessions/{session_id}"


# ----------------------------------------------------------------------------- lock file


def load_lock(path: Path | str = DEFAULT_LOCK) -> dict[str, str]:
    """Map of resource file path (as ``ant apply`` printed it) -> resource ID."""
    p = Path(path)
    if not p.exists():
        raise ManagedError(f"{p} not found: run `ant apply` in managed_agents/ first (see managed_agents/README.md)")
    data = json.loads(p.read_text(encoding="utf-8"))
    return {k: v["id"] for k, v in (data.get("resources") or {}).items() if isinstance(v, dict) and v.get("id")}


def deployment_id(lock_path: Path | str = DEFAULT_LOCK, name: str | None = None) -> str:
    ids = load_lock(lock_path)
    matches = [(k, v) for k, v in ids.items() if "/deployments/" in k or k.startswith("deployments/")]
    if name:
        matches = [(k, v) for k, v in matches if name in k]
    if not matches:
        raise ManagedError(f"No deployment in {lock_path}")
    if len(matches) > 1:
        raise ManagedError(f"Several deployments in {lock_path}; pass --deployment-id or --name: "
                           + ", ".join(k for k, _ in matches))
    return matches[0][1]


# ----------------------------------------------------------------------------- approvals


@dataclass
class ApprovalRule:
    """Decides paused tool calls in unattended sessions. Deny unless a prefix rule allows it."""

    allow_prefixes: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "ApprovalRule":
        raw = os.environ.get("DISCOVERY_AUTO_ALLOW", "")
        return cls(tuple(p.strip() for p in raw.split(",") if p.strip()))

    def decide(self, event: Any) -> tuple[bool, str | None]:
        name = _get(event, "name")
        if name == "bash" and self.allow_prefixes:
            command = str((_get(event, "input") or {}).get("command", "")).strip()
            # one simple command only: no chaining, substitution or redirection tricks
            if not re.search(r"[;&|`$<>]|\n", command) and command.startswith(self.allow_prefixes):
                return True, None
        return False, DENY_MESSAGE


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def pending_asks(events: Iterable[Any]) -> list[Any]:
    """Tool-use events that paused for approval and have not been answered yet."""
    asks, answered = [], set()
    for ev in events:
        etype = _get(ev, "type")
        if etype in TOOL_EVENT_TYPES and _get(ev, "evaluated_permission") == "ask":
            asks.append(ev)
        elif etype == "user.tool_confirmation":
            answered.add(_get(ev, "tool_use_id"))
    return [a for a in asks if _get(a, "id") not in answered]


def answer_pending(client: Any, session_id: str, rule: ApprovalRule | None = None,
                   answered: set[str] | None = None) -> list[tuple[str, str]]:
    """Answer every unanswered paused call. ``answered`` remembers ids sent earlier in this process."""
    rule = rule or ApprovalRule.from_env()
    answered = answered if answered is not None else set()
    events = client.beta.sessions.events.list(
        session_id, types=[*TOOL_EVENT_TYPES, "user.tool_confirmation"], order="asc", limit=100)
    decisions = []
    confirmations = []
    for ev in pending_asks(events):
        if _get(ev, "id") in answered:
            continue
        answered.add(_get(ev, "id"))
        allow, message = rule.decide(ev)
        confirmation = {"type": "user.tool_confirmation", "tool_use_id": _get(ev, "id"),
                        "result": "allow" if allow else "deny"}
        if not allow and message:
            confirmation["deny_message"] = message
        confirmations.append(confirmation)
        decisions.append((_get(ev, "id"), confirmation["result"]))
        log.info("paused call %s (%s) -> %s", _get(ev, "id"), _get(ev, "name"), confirmation["result"])
    if confirmations:
        client.beta.sessions.events.send(session_id, events=confirmations)
    return decisions


# ----------------------------------------------------------------------------- outputs


def _safe_relpath(name: str) -> Path:
    parts = [p for p in Path(name.replace("\\", "/")).parts if p not in ("", ".", "..", "/")]
    return Path(*parts) if parts else Path("output")


def download_outputs(client: Any, session_id: str, dest: Path | str, *, retries: int = 3,
                     sleep: Callable[[float], None] = time.sleep) -> list[Path]:
    """Download everything the session wrote to /mnt/session/outputs/."""
    dest = Path(dest)
    files: list[Any] = []
    for attempt in range(retries):
        files = list(client.beta.files.list(scope_id=session_id, betas=[MA_BETA]))
        if files:
            break
        sleep(2.0)  # brief indexing lag after the session goes idle
    saved = []
    for f in files:
        target = dest / _safe_relpath(_get(f, "filename") or _get(f, "id"))
        target.parent.mkdir(parents=True, exist_ok=True)
        client.beta.files.download(_get(f, "id")).write_to_file(target)
        saved.append(target)
    return saved


# ----------------------------------------------------------------------------- following a session


@dataclass
class FollowResult:
    stop: str = "unknown"          # end_turn | budget_reached | retries_exhausted | terminated
    open_runs: set[str] = field(default_factory=set)
    decisions: list[tuple[str, str]] = field(default_factory=list)


def follow_session(client: Any, session_id: str, rule: ApprovalRule | None = None,
                   on_text: Callable[[str], None] | None = None) -> FollowResult:
    """Stream a running session until its work is done, answering paused calls as they come."""
    rule = rule or ApprovalRule.from_env()
    result = FollowResult()
    answered: set[str] = set()
    with client.beta.sessions.events.stream(session_id) as stream:
        # consolidate: calls that paused before the stream opened
        result.decisions += answer_pending(client, session_id, rule, answered)
        for ev in stream:
            etype = _get(ev, "type")
            if etype == "agent.message" and on_text:
                for block in _get(ev, "content") or []:
                    if _get(block, "type") == "text":
                        on_text(_get(block, "text", ""))
            elif etype in TOOL_EVENT_TYPES and _get(ev, "evaluated_permission") == "ask":
                result.decisions += answer_pending(client, session_id, rule, answered)
            elif etype == "workflow_run.created":
                result.open_runs.add(_get(ev, "workflow_run_id"))
            elif etype == "workflow_run.status_ended":
                result.open_runs.discard(_get(ev, "workflow_run_id"))
            elif etype == "session.status_terminated":
                result.stop = "terminated"
                break
            elif etype == "session.status_idle":
                stop = _get(_get(ev, "stop_reason"), "type")
                if stop == "requires_action":
                    result.decisions += answer_pending(client, session_id, rule, answered)
                    continue
                if stop == "end_turn" and result.open_runs:
                    continue  # the agent takes a turn on each run's ending notice
                result.stop = stop or "unknown"
                break
    return result


def run_now(client: Any, dep_id: str, *, wait: bool = True, dest: Path | str | None = None,
            rule: ApprovalRule | None = None, on_text: Callable[[str], None] | None = None,
            progress: Callable[[str], None] = print) -> dict:
    run = client.beta.deployments.run(dep_id)
    error = _get(run, "error")
    if error:
        raise ManagedError(f"Deployment run failed: {_get(error, 'type')}: {_get(error, 'message')}")
    session_id = _get(run, "session_id")
    progress(f"Session {session_id}: {console_url(session_id)}")
    out: dict = {"run_id": _get(run, "id"), "session_id": session_id, "files": []}
    if wait:
        followed = follow_session(client, session_id, rule, on_text)
        out["stop"] = followed.stop
        out["decisions"] = followed.decisions
        progress(f"Session finished: {followed.stop}")
        if dest is not None:
            out["files"] = download_outputs(client, session_id, Path(dest) / session_id)
    return out


def latest_run(client: Any, dep_id: str) -> Any:
    runs = list(client.beta.deployment_runs.list(deployment_id=dep_id, limit=20))
    if not runs:
        raise ManagedError("This deployment has no runs yet")
    return max(runs, key=lambda r: str(_get(r, "created_at")))


# ----------------------------------------------------------------------------- webhooks


def handle_webhook_event(client: Any, event: Any, *, dest: Path | str, rule: ApprovalRule | None = None) -> str:
    """React to one verified webhook event. Returns a short description of what was done."""
    data = _get(event, "data")
    dtype, rid = _get(data, "type"), _get(data, "id")
    if dtype == "session.status_idled":
        idles = list(client.beta.sessions.events.list(rid, types=["session.status_idle"], order="desc", limit=1))
        stop = _get(_get(idles[0], "stop_reason"), "type") if idles else None
        if stop == "requires_action":
            decisions = answer_pending(client, rid, rule)
            return f"answered {len(decisions)} paused call(s)"
        if stop == "end_turn":
            files = download_outputs(client, rid, Path(dest) / rid)
            return f"downloaded {len(files)} output file(s)"
        if stop == "budget_reached":
            log.warning("session %s paused at its budget; raise or remove the budget to resume", rid)
            return "budget reached"
        return f"idle ({stop})"
    if dtype == "deployment_run.failed":
        run = client.beta.deployment_runs.retrieve(rid)
        error = _get(run, "error")
        log.error("deployment run %s failed: %s: %s", rid, _get(error, "type"), _get(error, "message"))
        return "deployment run failed"
    return f"ignored {dtype}"


def make_webhook_server(client: Any, *, host: str = "0.0.0.0", port: int = 8080, dest: Path | str = "reports/managed",
                        rule: ApprovalRule | None = None) -> ThreadingHTTPServer:
    """Webhook endpoint. Put it behind HTTPS (port 443) and register its URL in Console -> Webhooks.

    Needs ANTHROPIC_WEBHOOK_SIGNING_KEY (the whsec_ secret) for signature verification.
    """
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # route through logging
            log.info("webhook: " + fmt, *args)

        def do_POST(self):  # noqa: N802 (http.server API)
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
            try:
                event = client.beta.webhooks.unwrap(body, headers={k.lower(): v for k, v in self.headers.items()})
            except Exception:
                self.send_response(400)
                self.end_headers()
                return
            event_id = _get(event, "id")
            if event_id in seen:  # retries carry the same id
                self.send_response(204)
                self.end_headers()
                return
            try:
                log.info("%s", handle_webhook_event(client, event, dest=dest, rule=rule))
            except Exception:
                log.exception("webhook handling failed")
                self.send_response(500)  # let Anthropic retry the delivery
                self.end_headers()
                return
            seen.append(event_id)
            del seen[:-1000]
            self.send_response(204)
            self.end_headers()

    return ThreadingHTTPServer((host, port), Handler)
