import pytest

from discovery_agent.analysis.attention import attention_trend, relative_share
from discovery_agent.analysis.critique import alternative_explanations
from discovery_agent.analysis.scoring import ScoringWeights, attention_score
from discovery_agent.models import AttentionMetrics
from discovery_agent.pipeline import DiscoveryPipeline


def test_relative_share():
    assert relative_share(30, 3000) == pytest.approx(0.01)
    assert relative_share(None, 3000) is None and relative_share(5, 0) is None


def test_trend_is_relative_to_field_growth():
    assert attention_trend(30, 10)[0] == "rising"
    # the whole field tripled too: the pair only keeps pace
    label, ratio = attention_trend(30, 10, field_recent=3000, field_prior=1000)
    assert label == "stable" and ratio == pytest.approx(0.94, abs=0.01)
    assert attention_trend(4, 20)[0] == "declining"
    assert attention_trend(1, 2)[0] == "sparse"
    assert attention_trend(None, 2) == (None, None)


def test_same_count_weighs_more_in_a_small_field():
    w = ScoringWeights()
    big = attention_score(AttentionMetrics(publications=30, relative_share=30 / 300_000), w, enriched=True)
    small = attention_score(AttentionMetrics(publications=30, relative_share=30 / 2_000), w, enriched=True)
    absolute = attention_score(AttentionMetrics(publications=30), w, enriched=True)
    assert big < absolute < small


def test_rising_trend_raises_attention():
    w = ScoringWeights()
    base = AttentionMetrics(publications=20, relative_share=0.001)
    rising = base.model_copy(update={"trend": "rising"})
    assert attention_score(rising, w, enriched=True) > attention_score(base, w, enriched=True)


def test_pipeline_fills_relative_attention(sources, settings):
    opp = DiscoveryPipeline(sources, settings).analyze_pair("GENEA", "test fibrosis").opportunities[0]
    a = opp.attention
    assert a.disease_publications == 5000 and a.gene_publications == 120
    assert a.relative_share == pytest.approx(3 / 5000)
    assert a.publications_prior == 1 and a.trend == "sparse"
    lit = next(e for e in opp.evidence if e.source == "Europe PMC / PubMed")
    assert "0.06 %" in lit.summary or "0.06%" in lit.summary
    negative = next(x for x in alternative_explanations(opp, "en") if "120 papers" in x.explanation)
    assert "lack of tools" in negative.explanation
