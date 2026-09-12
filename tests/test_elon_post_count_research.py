from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from polymarket_bot.elon_post_count_research import (
    Bucket,
    _bucket_probabilities,
    _fee_per_share,
    _observation_times,
    _pmf_negative_binomial,
    _pmf_poisson,
    parse_bucket,
)


def test_parse_bucket_variants() -> None:
    assert parse_bucket("<40") == (0, 39)
    assert parse_bucket("40-64") == (40, 64)
    assert parse_bucket("240+") == (240, None)
    assert parse_bucket("125") == (125, 125)


def test_probability_mass_and_buckets_sum_to_one() -> None:
    pmf = _pmf_negative_binomial(70, 0.2)
    assert np.isclose(pmf.sum(), 1.0)
    buckets = [
        Bucket("<40", 0, 39, "y0", "n0", "m0", "c0", True, 0.05),
        Bucket("40-64", 40, 64, "y1", "n1", "m1", "c1", True, 0.05),
        Bucket("65+", 65, None, "y2", "n2", "m2", "c2", True, 0.05),
    ]
    probabilities = _bucket_probabilities(pmf, buckets)
    assert np.isclose(sum(probabilities), 1.0)
    assert all(0 <= value <= 1 for value in probabilities)


def test_poisson_mass_is_normalized() -> None:
    for mean in (0.1, 5, 100):
        pmf = _pmf_poisson(mean)
        assert np.isclose(pmf.sum(), 1.0)


def test_culture_fee_formula() -> None:
    assert np.isclose(_fee_per_share(0.5, 0.05, True), 0.0125)
    assert _fee_per_share(0.5, 0.05, False) == 0.0


def test_observation_times_do_not_escape_window() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 3, tzinfo=timezone.utc)
    rows = _observation_times(start, end)
    assert rows
    assert all(start <= stamp <= end for stamp, _ in rows)
    assert len({stamp for stamp, _ in rows}) == len(rows)
