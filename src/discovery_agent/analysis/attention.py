"""Relative research attention and its time trend.

Absolute co-mention counts favour small fields: 300 papers on a gene in Alzheimer disease (≈ 300 000 papers)
is a modest share, while 300 papers in a rare disease means the pair is the field's main topic. The relative
share (co-mentions ÷ disease papers) corrects for field size. The trend compares the last three years with the
three years before, divided by the growth of the disease literature itself, so general publication growth does
not look like rising interest.
"""

from __future__ import annotations

RISING, STABLE, DECLINING, SPARSE = "rising", "stable", "declining", "sparse"


def relative_share(co_mentions: int | None, disease_total: int | None) -> float | None:
    if co_mentions is None or not disease_total:
        return None
    return round(min(1.0, co_mentions / disease_total), 6)


def growth(recent: int | None, prior: int | None) -> float | None:
    if recent is None or prior is None:
        return None
    return (recent + 1) / (prior + 1)


def attention_trend(recent: int | None, prior: int | None, field_recent: int | None = None,
                    field_prior: int | None = None, *, min_count: int = 5) -> tuple[str | None, float | None]:
    """Label the pair's publication trend relative to its disease field.

    Returns (label, ratio); ratio is pair growth divided by field growth (1.0 = grows like the field).
    """
    pair = growth(recent, prior)
    if pair is None:
        return None, None
    field = growth(field_recent, field_prior) or 1.0
    ratio = round(pair / field, 2)
    if (recent or 0) + (prior or 0) < min_count:
        return SPARSE, ratio
    if ratio >= 2.0:
        return RISING, ratio
    if ratio <= 0.5:
        return DECLINING, ratio
    return STABLE, ratio
