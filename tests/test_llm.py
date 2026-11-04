"""AI layer tests with a fake Anthropic client (no network, no API key)."""

import json
from types import SimpleNamespace as NS

import pytest

from discovery_agent.agent import DiscoveryAgent
from discovery_agent.llm.client import FALLBACK_BETA, LLMRefusal, sanitize_for_echo
from discovery_agent.llm.investigator import Investigator
from discovery_agent.llm.synthesizer import HYPOTHESIS_SCHEMA, HypothesisSynthesizer
from discovery_agent.models import EvidenceType, Modulation
from discovery_agent.pipeline import DiscoveryPipeline


def text_block(text):
    return NS(type="text", text=text)


def tool_block(i, name, inp):
    return NS(type="tool_use", id=f"toolu_{i}", name=name, input=inp)


def message(content, stop="end_turn", model="claude-opus-5-5"):
    return NS(content=content, stop_reason=stop, stop_details=None, model=model)


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.msg


class FakeClient:
    def __init__(self, stream_message=None, create_messages=()):
        self.stream_message = stream_message
        self.create_messages = list(create_messages)
        self.stream_calls, self.create_calls = [], []
        self.beta = NS(messages=NS(stream=self._stream, create=self._create))

    def _stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        return FakeStream(self.stream_message)

    def _create(self, **kwargs):
        self.create_calls.append(json.loads(json.dumps(kwargs, default=lambda o: o.__dict__)))
        return self.create_messages.pop(0)


def ai_payload(ids):
    return {
        "title": "GENEA activation for test fibrosis",
        "statement": "Activating GENEA may treat test fibrosis (E1).",
        "modulation": "activation",
        "mechanism_rationale": "LoF variants increase risk (E1, E99).",
        "supporting": [{"evidence_id": ids[0], "interpretation": "burden test", "reliability_comment": "robust"},
                       {"evidence_id": "E77", "interpretation": "made up", "reliability_comment": "?"}],
        "contradicting": [],
        "confidence": {"level": "moderate", "score": 1.7, "rationale": "Strong genetics, unclear tissue."},
        "alternative_explanations": [{"title": "Locus confounding", "explanation": "LD", "plausibility": "low",
                                      "how_to_test": "coloc", "evidence_ids": [ids[0], "E55"]}],
        "key_uncertainties": ["tissue"],
        "proposed_experiments": [{"title": "CRISPRa", "design": "activate", "expected_if_true": "better",
                                  "expected_if_false": "same", "priority": "urgent"}],
        "safety_considerations": ["none known"],
        "competitive_landscape": "No drugs.",
        "novelty_assessment": "Few papers.",
    }


@pytest.fixture
def opp(sources, settings):
    return DiscoveryPipeline(sources, settings).analyze_pair("GENEA", "test fibrosis").opportunities[0]


def test_synthesizer_request_shape_and_grounding(opp, settings):
    client = FakeClient(stream_message=message([text_block(json.dumps(ai_payload(["E1"])))]))
    h = HypothesisSynthesizer(settings, client).synthesize(opp)
    call = client.stream_calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["fallbacks"] == "default" and call["betas"] == [FALLBACK_BETA]
    assert call["output_config"]["format"]["schema"] is HYPOTHESIS_SCHEMA
    assert call["output_config"]["effort"] == "high"
    assert "<dossier>" in call["messages"][0]["content"]
    # grounding: unknown evidence ids removed and reported
    assert [a.evidence_id for a in h.supporting] == ["E1"]
    assert h.alternative_explanations[0].evidence_ids == ["E1"]
    assert {"E77", "E55", "E99"} <= set(h.ungrounded_citations)
    assert h.confidence.score == 1.0 and h.proposed_experiments[0].priority == "medium"
    assert h.modulation is Modulation.ACTIVATION and h.generated_by == "claude:claude-opus-5-5"


def test_schema_is_strict():
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array":
            walk(node["items"])
    walk(HYPOTHESIS_SCHEMA)


def test_synthesizer_refusal(opp, settings):
    msg = message([], stop="refusal")
    msg.stop_details = NS(category="bio", explanation="declined")
    with pytest.raises(LLMRefusal, match="bio"):
        HypothesisSynthesizer(settings, FakeClient(stream_message=msg)).synthesize(opp)


def test_sanitize_for_echo_drops_blocks_before_fallback():
    blocks = [NS(type="thinking"), NS(type="tool_use"), NS(type="text"), NS(type="fallback"), NS(type="tool_use")]
    kept = [b.type for b in sanitize_for_echo(blocks)]
    assert kept == ["text", "fallback", "tool_use"]
    assert len(sanitize_for_echo(blocks[:3])) == 3


def test_investigator_loop_records_only_grounded_findings(opp, settings, sources):
    turn1 = message([text_block("Checking literature."),
                     tool_block(1, "search_literature", {"query": "GENEA", "source": "articles", "since": "",
                                                         "max_results": 5}),
                     tool_block(2, "get_open_targets_evidence", {"group": "genetic"})], stop="tool_use")
    turn2 = message([
        tool_block(3, "record_evidence", {
            "summary": "Paper 55555555 reports GENEA loss in patients.", "source": "Europe PMC",
            "aspect": "causality", "stance": "supports", "reliability": "C", "reliability_rationale": "single study",
            "references": [{"label": "PMID:55555555", "url": "https://pubmed.ncbi.nlm.nih.gov/55555555/"}]}),
        tool_block(4, "record_evidence", {
            "summary": "Hallucinated", "source": "x", "aspect": "safety", "stance": "contradicts", "reliability": "B",
            "reliability_rationale": "r", "references": [{"label": "PMID:99999999", "url": "https://example.org/99999999"}]}),
        tool_block(5, "get_target_profile", {"gene": "NOPE"}),
    ], stop="tool_use")
    turn3 = message([text_block("Notes: genetics solid; no negative trials found.")])
    client = FakeClient(create_messages=[turn1, turn2, turn3])
    before = len(opp.evidence)
    result = Investigator(settings, sources, client).investigate_and_merge(opp)

    assert result.turns == 3 and result.tool_calls == 5 and result.rejected_records == 1
    assert result.notes.startswith("Notes:")
    assert len(opp.evidence) == before + 1
    new = opp.evidence[-1]
    assert new.type is EvidenceType.AGENT_FINDING and new.id == f"E{before + 1}"
    # all tool results of one turn go back in a single user message, matched by id
    second = client.create_calls[1]["messages"]
    results = second[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_1", "toolu_2"]
    third = client.create_calls[2]["messages"][-1]["content"]
    errors = {r["tool_use_id"]: r.get("is_error", False) for r in third}
    assert errors == {"toolu_3": False, "toolu_4": True, "toolu_5": True}
    assert client.create_calls[0]["fallbacks"] == "default"
    assert all(t["strict"] for t in client.create_calls[0]["tools"])


def test_agent_falls_back_to_rules_when_ai_fails(sources, settings):
    msg = message([], stop="refusal")
    agent = DiscoveryAgent(settings, sources=sources, use_ai=True, llm_client=FakeClient(stream_message=msg))
    run = agent.run_pair("GENEA", "test fibrosis")
    key = run.result.opportunities[0].key
    assert run.hypotheses[key].generated_by == "rules"
    assert key in run.ai_errors


def test_agent_uses_ai_hypothesis(sources, settings):
    client = FakeClient(stream_message=message([text_block(json.dumps(ai_payload(["E1"])))]))
    agent = DiscoveryAgent(settings, sources=sources, use_ai=True, llm_client=client)
    run = agent.run_disease("test fibrosis", top_k=3, enrich_k=3)
    generated = [h.generated_by for h in run.hypotheses.values()]
    assert generated.count("claude:claude-opus-5-5") == len(run.result.opportunities) == 2
