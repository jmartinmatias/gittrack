"""Tests for the velocity/abnormality engine.

These use synthetic histories because the whole value of the tool rests on this
math being right: a wrong z-score means either missed breakouts or a firehose of
false alarms.
"""

import math

import pytest

from gittrack import metrics as mx
from gittrack.db import Obs

DAY = 86400
NOW = 1_700_000_000  # fixed epoch so tests are deterministic


def series(daily_stars, start=NOW - 120 * DAY, step=DAY, forks=None):
    """Build observations from a list of cumulative star counts, one per step."""
    return [
        Obs(start + i * step, s, (forks[i] if forks else s // 10))
        for i, s in enumerate(daily_stars)
    ]


def ramp(days, per_day, base=1000):
    return [base + i * per_day for i in range(days)]


# --------------------------------------------------------------- interpolation


def test_interpolates_between_irregular_samples():
    obs = [Obs(NOW, 100, 10), Obs(NOW + 10 * DAY, 200, 20)]
    s = mx.Series(obs)
    assert s.stars_at(NOW + 5 * DAY) == pytest.approx(150.0)
    assert s.stars_at(NOW + 1 * DAY) == pytest.approx(110.0)


def test_never_extrapolates_outside_observed_range():
    s = mx.Series([Obs(NOW, 100, 10), Obs(NOW + DAY, 110, 11)])
    assert s.stars_at(NOW - 1) is None
    assert s.stars_at(NOW + DAY + 1) is None


def test_exact_hit_returns_the_observation():
    s = mx.Series([Obs(NOW, 100, 10), Obs(NOW + DAY, 110, 11)])
    assert s.stars_at(NOW) == 100.0
    assert s.stars_at(NOW + DAY) == 110.0


def test_irregular_cadence_gives_the_same_answer_as_regular():
    """A missed cron run must not change the computed 7-day velocity."""
    dense = series(ramp(40, 10))
    sparse_obs = [o for i, o in enumerate(dense) if i % 3 == 0]  # 2 of every 3 missing
    a = mx.compute(1, dense, window_days=7, min_baseline_days=5, min_observations=3)
    b = mx.compute(1, sparse_obs, window_days=7, min_baseline_days=5, min_observations=3)
    assert a.velocity == pytest.approx(10.0)
    assert b.velocity == pytest.approx(10.0, abs=0.01)


# ------------------------------------------------------------------- velocity


def test_absolute_and_relative_velocity_on_a_linear_ramp():
    m = mx.compute(1, series(ramp(60, 50, base=1000)), window_days=7,
                   min_baseline_days=5, min_observations=3)
    assert m.has_window
    assert m.delta_stars == pytest.approx(350.0)     # 7 days x 50
    assert m.velocity == pytest.approx(50.0)
    start_stars = 1000 + 52 * 50
    assert m.rel_velocity == pytest.approx(350 / start_stars, rel=1e-6)


def test_doubling_time_matches_the_relative_rate():
    # +10% over 7 days -> doubling in 7*ln2/ln(1.1) ~= 51 days
    obs = [Obs(NOW - 7 * DAY, 1000, 100), Obs(NOW, 1100, 110)]
    m = mx.compute(1, obs, window_days=7)
    assert m.doubling_days == pytest.approx(7 * math.log(2) / math.log(1.1), rel=1e-6)


# ---------------------------------------------------------------- abnormality


def test_steady_high_velocity_is_not_abnormal():
    """A repo reliably gaining 200/day is fast but perfectly normal for itself."""
    m = mx.compute(1, series(ramp(120, 200)), window_days=7)
    assert m.has_baseline
    assert abs(m.z) < 1.5, f"steady repo scored z={m.z}"


def test_a_genuine_spike_scores_high():
    counts = ramp(100, 20)                       # 100 days at 20/day
    last = counts[-1]
    counts += [last + (i + 1) * 400 for i in range(14)]   # then 400/day
    m = mx.compute(1, series(counts), window_days=7)
    assert m.has_baseline
    assert m.z > 8, f"spike only scored z={m.z}"
    assert m.velocity == pytest.approx(400.0)


def test_baseline_excludes_the_current_window():
    """The spike must not be allowed to inflate its own reference distribution."""
    counts = ramp(100, 20) 
    last = counts[-1]
    counts += [last + (i + 1) * 400 for i in range(7)]
    s = mx.Series(series(counts))
    now = float(s.last_ts)
    baseline = s.daily_velocities(now - 90 * DAY, now - 7 * DAY)
    assert max(baseline) == pytest.approx(20.0), "spike leaked into the baseline"


def test_flat_repo_with_one_extra_star_is_not_a_breakout():
    """The sqrt(median) noise floor stops a zero-variance repo scoring infinity."""
    counts = [1000] * 100 + [1001] * 7
    m = mx.compute(1, series(counts), window_days=7)
    assert m.z is not None
    assert m.z < 3, f"one star scored z={m.z}"


def test_past_spike_does_not_mask_a_new_one():
    """Median/MAD ignores an old outlier; mean/stdev would not."""
    counts = ramp(40, 10)
    counts += [counts[-1] + (i + 1) * 900 for i in range(5)]   # old HN hit
    counts += [counts[-1] + (i + 1) * 10 for i in range(48)]   # back to normal
    counts += [counts[-1] + (i + 1) * 500 for i in range(7)]   # new surge
    m = mx.compute(1, series(counts), window_days=7)
    assert m.z > 8, f"old spike masked the new one (z={m.z})"


# ------------------------------------------------------------------ gating


def test_new_repo_reports_insufficient_data_not_a_huge_score():
    m = mx.compute(1, series(ramp(3, 500)), window_days=7)
    assert not m.has_window
    assert m.z is None and m.velocity is None
    assert "history" in m.note


def test_window_without_baseline_still_reports_velocity():
    m = mx.compute(1, series(ramp(10, 40)), window_days=7, min_baseline_days=14)
    assert m.has_window and not m.has_baseline
    assert m.velocity == pytest.approx(40.0)
    assert m.z is None
    assert "baseline" in m.note


# ------------------------------------------------------------- acceleration


def test_acceleration_ratio_detects_a_doubling_rate():
    counts = ramp(60, 50)
    counts += [counts[-1] + (i + 1) * 100 for i in range(7)]
    m = mx.compute(1, series(counts), window_days=7)
    assert m.prior_velocity == pytest.approx(50.0, abs=1.0)
    assert m.accel_ratio == pytest.approx(2.0, abs=0.05)


def test_acceleration_from_zero_is_finite():
    counts = [1000] * 30 + [1000 + (i + 1) * 40 for i in range(7)]
    m = mx.compute(1, series(counts), window_days=7)
    assert m.accel_ratio is not None and math.isfinite(m.accel_ratio)


# ------------------------------------------------------------------- forks


def test_fork_confirmation_near_one_when_forks_track_stars():
    stars = ramp(30, 100, base=1000)
    forks = ramp(30, 10, base=100)          # same 10% relative growth
    m = mx.compute(1, series(stars, forks=forks), window_days=7)
    assert m.fork_confirm == pytest.approx(1.0, rel=0.05)


def test_fork_confirmation_near_zero_for_a_star_only_pump():
    stars = ramp(30, 100, base=1000)
    forks = [100] * 30                       # nobody actually uses it
    m = mx.compute(1, series(stars, forks=forks), window_days=7)
    assert m.fork_confirm == pytest.approx(0.0, abs=1e-9)


def test_missing_fork_data_yields_none_not_zero():
    """Backfilled rows have NULL forks; that must read as unknown, not as a pump."""
    obs = [Obs(NOW - i * DAY, 1000 + (30 - i) * 100, None) for i in range(30, -1, -1)]
    m = mx.compute(1, obs, window_days=7)
    assert m.fork_confirm is None
    assert m.has_window and m.velocity == pytest.approx(100.0)


# ------------------------------------------------------------------ scoring


def test_score_cohort_ranks_the_breakout_first():
    steady = mx.compute(1, series(ramp(120, 200)), window_days=7, full_name="steady/steady")
    counts = ramp(100, 20)
    counts += [counts[-1] + (i + 1) * 400 for i in range(14)]
    spike = mx.compute(2, series(counts), window_days=7, full_name="spike/spike")
    young = mx.compute(3, series(ramp(3, 500)), window_days=7, full_name="new/new")

    out = mx.score_cohort([steady, spike, young])
    assert out == [steady, spike, young], "scoring is in place, returning the list"
    assert young.heat is None, "a repo with no window must not get a score"
    assert spike.heat > steady.heat
    assert 0 <= spike.heat <= 100


def test_fork_quality_penalises_a_star_only_spike():
    counts = ramp(100, 20)
    counts += [counts[-1] + (i + 1) * 400 for i in range(14)]
    real = mx.compute(1, series(counts), window_days=7)                       # forks = stars/10
    pump = mx.compute(2, series(counts, forks=[100] * len(counts)), window_days=7)
    mx.score_cohort([real, pump])
    assert real.heat > pump.heat


def test_weights_must_be_positive():
    with pytest.raises(ValueError):
        mx.score_cohort([], w_z=0, w_rel=0, w_accel=0)


# ------------------------------------------------------------------- robust_z


def test_robust_z_needs_a_minimum_sample():
    assert mx.robust_z(10.0, [1.0, 2.0]) is None


def test_pct_rank_bounds():
    sample = [1.0, 2.0, 3.0, 4.0]
    assert mx.pct_rank(sample, 0.0) == 0.0
    assert mx.pct_rank(sample, 4.0) == 1.0
    assert mx.pct_rank([], 5.0) == 0.0


# ------------------------------------------------------------- acceleration


def test_acceleration_uses_the_longest_horizon_available():
    """A monthly average says more about 'normal' than yesterday does."""
    a = mx.acceleration(200.0, {"daily": 100, "weekly": 700, "monthly": 3000})
    assert a["horizon"] == "monthly"
    assert a["long_rate"] == pytest.approx(100.0)     # 3000 / 30


def test_acceleration_needs_only_one_board():
    """The whole point: a repo on a single board still gets an answer."""
    a = mx.acceleration(300.0, {"monthly": 3000})
    assert a is not None and a["ratio"] == pytest.approx(3.0, rel=0.01)


def test_acceleration_reports_speeding_up_and_slowing_down():
    fast = mx.acceleration(400.0, {"weekly": 700})     # 400/d vs 100/d
    slow = mx.acceleration(25.0, {"weekly": 700})      # 25/d vs 100/d
    assert fast["ratio"] > 1 and slow["ratio"] < 1


def test_acceleration_needs_a_baseline_worth_dividing_by():
    """A repo averaging a fraction of a star a day has no meaningful pace."""
    assert mx.acceleration(50.0, {"monthly": 3}) is None      # 0.1/day


def test_acceleration_from_a_standing_start_is_finite():
    a = mx.acceleration(500.0, {"weekly": 7})           # baseline 1/day
    assert a is not None and math.isfinite(a["ratio"])


@pytest.mark.parametrize("args", [
    (None, {"weekly": 700}),      # no measured rate
    (100.0, {}),                  # no board figure
    (100.0, {"weekly": None}),    # board present but count missing
])
def test_acceleration_returns_none_without_both_halves(args):
    assert mx.acceleration(*args) is None
