"""Wire-level check: the real Anthropic SDK against a local fake Messages API.

Verifies that the request the SDK actually sends (beta header, fallbacks, structured output schema, strict
tools, adaptive thinking) is what we intend, and that the SDK parses streamed and tool-use responses into
objects our code understands. No network access and no API key are needed.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

anthropic = pytest.importorskip("anthropic")

from discovery_agent.llm.client import FALLBACK_BETA  # noqa: E402
from discovery_agent.llm.investigator import Investigator  # noqa: E402
from discovery_agent.llm.synthesizer import HypothesisSynthesizer  # noqa: E402
from discovery_agent.pipeline import DiscoveryPipeline  # noqa: E402

from test_llm import ai_payload  # noqa: E402


class FakeAPI(BaseHTTPRequestHandler):
    requests: list = []
    responses: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAPI.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        kind, payload = FakeAPI.responses.pop(0)
        if kind == "sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in payload:
                self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
        else:
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


@pytest.fixture
def api():
    FakeAPI.requests, FakeAPI.responses = [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = anthropic.Anthropic(api_key="sk-test", base_url=f"http://127.0.0.1:{server.server_port}", max_retries=0)
    yield client
    server.shutdown()


def usage():
    return {"input_tokens": 10, "output_tokens": 20}


def sse_text(text):
    msg = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
           "stop_reason": None, "stop_sequence": None, "usage": usage()}
    return [
        {"type": "message_start", "message": msg},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": usage()},
        {"type": "message_stop"},
    ]


def json_message(content, stop):
    return {"id": "msg_2", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
            "stop_reason": stop, "stop_sequence": None, "usage": usage()}


def test_synthesizer_over_the_wire(api, sources, settings):
    opp = DiscoveryPipeline(sources, settings).analyze_pair("GENEA", "test fibrosis").opportunities[0]
    FakeAPI.responses.append(("sse", sse_text(json.dumps(ai_payload(["E1"])))))
    h = HypothesisSynthesizer(settings, api).synthesize(opp)
    req = FakeAPI.requests[0]
    assert req["path"].startswith("/v1/messages")
    assert FALLBACK_BETA in req["headers"].get("anthropic-beta", "")
    body = req["body"]
    assert body["model"] == "claude-opus-5-5" and body["stream"] is True
    assert body["fallbacks"] == "default" and body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert h.title.startswith("GENEA") and h.supporting[0].evidence_id == "E1"


def test_investigator_over_the_wire(api, sources, settings):
    opp = DiscoveryPipeline(sources, settings).analyze_pair("GENEA", "test fibrosis").opportunities[0]
    FakeAPI.responses += [
        ("json", json_message([{"type": "tool_use", "id": "toolu_1", "name": "search_literature",
                                "input": {"query": "GENEA", "source": "articles", "since": "", "max_results": 3}}],
                              "tool_use")),
        ("json", json_message([{"type": "text", "text": "Done: nothing alarming."}], "end_turn")),
    ]
    result = Investigator(settings, sources, api).investigate(opp)
    assert result.notes == "Done: nothing alarming." and result.tool_calls == 1
    first, second = FakeAPI.requests[0]["body"], FakeAPI.requests[1]["body"]
    assert first["fallbacks"] == "default" and first["cache_control"] == {"type": "ephemeral"}
    assert all(t["strict"] is True and t["input_schema"]["additionalProperties"] is False for t in first["tools"])
    # the assistant turn is echoed back and followed by the tool result
    assert second["messages"][1]["role"] == "assistant"
    assert second["messages"][1]["content"][0]["type"] == "tool_use"
    tool_result = second["messages"][2]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "toolu_1"
    assert "A relevant paper" in tool_result["content"]
