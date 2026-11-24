"""The pilot arithmetic (src/mba_bot/pilot_math.py). Research only.

PROVES: the mean, spread and standard error of a known sample; that the resampled chance of a positive total is about one half for a zero-mean sample, near zero for a clearly negative one and near one for a clearly positive one;
that it is seeded; that a longer pilot separates better; and the trades-per-year arithmetic.
DOES NOT PROVE anything about the strategies."""
from __future__ import annotations

import random

import pytest

from mba_bot import pilot_math as PM


def sample(mean, sd, n=2000, seed=1):
    rng = random.Random(seed)
    return [rng.gauss(mean, sd) for _ in range(n)]


def test_mean_spread_and_standard_error():
    p = PM.pilot([1.0, 3.0] * 50, n=100, reps=200)
    assert p["mean"] == pytest.approx(2.0) and p["sd"] == pytest.approx((100 / 99) ** 0.5) and p["se_at_n"] == pytest.approx(p["sd"] / 10)
    lo, hi = p["interval_95_of_the_mean"]
    assert lo < 2.0 < hi and hi - lo == pytest.approx(2 * 1.96 * p["se_at_n"])
    assert PM.pilot([1.0]) == {"trades": 1}


def test_the_chance_of_a_positive_total_follows_the_sign_and_size_of_the_mean():
    zero = PM.pilot(sample(0.0, 1.2), n=100, reps=4000)
    assert 0.4 < zero["p_total_positive"] < 0.6
    neg = PM.pilot(sample(-0.3, 1.2), n=100, reps=4000)
    assert neg["p_total_positive"] < 0.05 and neg["p_total_below_bad"] > 0.8
    pos = PM.pilot(sample(0.3, 1.2), n=100, reps=4000)
    assert pos["p_total_positive"] > 0.95
    small = PM.pilot(sample(-0.1, 1.2), n=100, reps=4000)
    assert 0.1 < small["p_total_positive"] < 0.4  # a modest loss per trade still leaves a real chance of a positive pilot


def test_it_is_seeded_and_a_longer_pilot_separates_better():
    s = sample(-0.1, 1.2)
    assert PM.pilot(s, reps=500) == PM.pilot(s, reps=500)
    assert PM.pilot(s, n=400, reps=3000)["p_total_positive"] < PM.pilot(s, n=100, reps=3000)["p_total_positive"]


def test_trades_per_pair_year_and_months():
    assert PM.trades_per_pair_year([0] * 800, 5.0, 4) == pytest.approx(40.0)
    assert PM.months_for(100, 40.0) == pytest.approx(30.0) and PM.months_for(100, 0.0) is None


def test_the_bad_threshold_is_used_as_given():
    s = sample(-0.1, 1.2)
    deep = PM.pilot(s, n=100, reps=3000, bad=-30.0)
    shallow = PM.pilot(s, n=100, reps=3000, bad=-5.0)
    assert deep["p_total_below_bad"] < PM.pilot(s, n=100, reps=3000)["p_total_below_bad"] < shallow["p_total_below_bad"] and deep["bad"] == -30.0
