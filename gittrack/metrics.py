"""Velocity, abnormality and the composite heat score.

Design notes that matter for correctness:

*   **Snapshots are irregular.** Cron misses runs, rate limits bite, laptops
    sleep. So every window is evaluated by *interpolating* between the two
    bracketing observations rather than assuming evenly spaced samples. We never
    extrapolate past the observed range - outside it, the answer is "unknown",
    not a guess.

*   **The baseline excludes the current window.** Asking "is the last 7 days
    abnormal?" against a 90-day baseline that *contains* those 7 days lets the
    spike contaminate its own reference. The baseline runs
    [now - baseline_days, now - window_days).

*   **Median/MAD, not mean/stdev.** Star velocity is heavy-tailed; one past
    Hacker News hit inflates the standard deviation so much that the next real
    surge scores z ~ 1. The median absolute deviation ignores that.

*   **New repos are not breakouts.** A repo discovered yesterday has no
    baseline, so its z is None and it is reported as insufficient data - never as
    an infinite score.
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass, field

from .db import Obs

DAY = 86400.0
# Additive smoothing on the acceleration ratio: without it, a repo going from
# 0.0 to 4 stars/day reports an infinite speed-up.
ACCEL_EPS = 0.5


class Series:
    """A repo's observations, ascending, with O(log n) interpolated lookup."""

    __slots__ = ("_ts", "obs")

    def __init__(self, obs: list[Obs]):
        self.obs = obs
        self._ts = [o.ts for o in obs]

    def __len__(self) -> int:
        return len(self.obs)

    @property
    def first_ts(self) -> int | None:
        return self._ts[0] if self._ts else None

    @property
    def last_ts(self) -> int | None:
        return self._ts[-1] if self._ts else None

    @property
    def span_days(self) -> float:
        if len(self._ts) < 2:
            return 0.0
        return (self._ts[-1] - self._ts[0]) / DAY

    def _bracket(self, t: float):
        """The two observations straddling `t`, plus the interpolation weight."""
        if not self.obs or t < self._ts[0] or t > self._ts[-1]:
            return None
        i = bisect_left(self._ts, t)
        if i < len(self._ts) and self._ts[i] == t:
            return self.obs[i], self.obs[i], 0.0
        lo, hi = self.obs[i - 1], self.obs[i]
        span = hi.ts - lo.ts
        return lo, hi, ((t - lo.ts) / span if span > 0 else 1.0)

    def stars_at(self, t: float) -> float | None:
        """Star count at `t`, linearly interpolated. None outside the observed range."""
        br = self._bracket(t)
        if br is None:
            return None
        lo, hi, w = br
        return lo.stars + (hi.stars - lo.stars) * w

    def forks_at(self, t: float) -> float | None:
        """Fork count at `t`. None outside the range, or if either end lacks fork data."""
        br = self._bracket(t)
        if br is None:
            return None
        lo, hi, w = br
        if lo.forks is None or hi.forks is None:
            return None
        return lo.forks + (hi.forks - lo.forks) * w

    def daily_velocities(self, start: float, end: float) -> list[float]:
        """Stars/day for each whole-day bucket in [start, end)."""
        out: list[float] = []
        t = start
        while t + DAY <= end + 1:
            a, b = self.stars_at(t), self.stars_at(t + DAY)
            if a is not None and b is not None:
                out.append(b - a)
            t += DAY
        return out


def robust_z(x: float, sample: list[float], min_n: int = 5) -> float | None:
    """Modified z-score using median and MAD.

    The scale is floored at sqrt(median) because star arrivals behave like a
    counting process: even a perfectly steady repo has day-to-day noise of order
    sqrt(rate). Without that floor a repo doing an unwavering 3 stars/day would
    report z in the hundreds the first day it did 4.
    """
    if len(sample) < min_n:
        return None
    med = statistics.median(sample)
    mad = statistics.median([abs(s - med) for s in sample]) * 1.4826
    scale = max(mad, math.sqrt(max(med, 1.0)), 1.0)
    return (x - med) / scale


def pct_rank(sorted_sample: list[float], x: float) -> float:
    """Fraction of the sample at or below x, in [0, 1]."""
    if not sorted_sample:
        return 0.0
    return bisect_right(sorted_sample, x) / len(sorted_sample)


@dataclass
class RepoMetrics:
    repo_id: int
    full_name: str = ""
    stars: int = 0
    forks: int | None = 0
    window_days: float = 7.0

    # Coverage / gating
    n_obs: int = 0
    span_days: float = 0.0
    age_days: float | None = None
    last_ts: int | None = None
    has_window: bool = False
    has_baseline: bool = False
    note: str = ""

    # Absolute + relative velocity over the window
    delta_stars: float | None = None
    velocity: float | None = None          # stars / day
    rel_velocity: float | None = None      # fraction of starting stars
    doubling_days: float | None = None

    delta_forks: float | None = None
    fork_velocity: float | None = None
    fork_rel: float | None = None
    fork_confirm: float | None = None      # fork_rel / rel_velocity; ~1 = real adoption

    # Abnormality + acceleration
    z: float | None = None
    baseline_median: float | None = None
    prior_velocity: float | None = None
    accel_ratio: float | None = None
    accel_abs: float | None = None

    # Cohort-relative (filled in by score_cohort)
    rel_pct: float | None = None
    heat: float | None = None
    heat_parts: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def compute(
    repo_id: int,
    obs: list[Obs],
    *,
    window_days: float = 7.0,
    baseline_days: float = 90.0,
    min_observations: int = 8,
    min_baseline_days: float = 14.0,
    created_at: int | None = None,
    full_name: str = "",
) -> RepoMetrics:
    s = Series(obs)
    m = RepoMetrics(repo_id=repo_id, full_name=full_name, window_days=window_days,
                    n_obs=len(obs), span_days=s.span_days)
    if not obs:
        m.note = "no observations"
        return m

    now = float(s.last_ts)
    m.last_ts = int(now)
    m.stars, m.forks = obs[-1].stars, obs[-1].forks
    if created_at:
        m.age_days = (now - created_at) / DAY

    # ---- current window -----------------------------------------------------
    t0 = now - window_days * DAY
    s0 = s.stars_at(t0)
    if s0 is None:
        m.note = f"needs {window_days:g}d of history, has {s.span_days:.1f}d"
        return m
    m.has_window = True

    m.delta_stars = m.stars - s0
    m.velocity = m.delta_stars / window_days
    m.rel_velocity = m.delta_stars / max(s0, 1.0)
    if m.rel_velocity > 0:
        m.doubling_days = window_days * math.log(2) / math.log1p(m.rel_velocity)

    f0 = s.forks_at(t0)
    if f0 is not None and m.forks is not None:
        m.delta_forks = m.forks - f0
        m.fork_velocity = m.delta_forks / window_days
        m.fork_rel = m.delta_forks / max(f0, 1.0)
        if m.rel_velocity > 0:
            m.fork_confirm = m.fork_rel / m.rel_velocity

    # ---- acceleration: this window vs the one before it ---------------------
    prior = s.stars_at(now - 2 * window_days * DAY)
    if prior is not None:
        m.prior_velocity = (s0 - prior) / window_days
        m.accel_abs = m.velocity - m.prior_velocity
        m.accel_ratio = (m.velocity + ACCEL_EPS) / (max(m.prior_velocity, 0.0) + ACCEL_EPS)

    # ---- abnormality vs the repo's own past ---------------------------------
    b_start = max(now - baseline_days * DAY, float(s.first_ts))
    b_end = now - window_days * DAY
    baseline_span = (b_end - b_start) / DAY
    if baseline_span >= min_baseline_days and len(obs) >= min_observations:
        dv = s.daily_velocities(b_start, b_end)
        if dv:
            m.baseline_median = statistics.median(dv)
            m.z = robust_z(m.velocity, dv)
            m.has_baseline = m.z is not None
    if not m.has_baseline and not m.note:
        m.note = (f"baseline needs {min_baseline_days:g}d before the window, "
                  f"has {max(baseline_span, 0.0):.1f}d")
    return m


def score_cohort(
    metrics: list[RepoMetrics],
    w_z: float = 0.45,
    w_rel: float = 0.30,
    w_accel: float = 0.25,
) -> list[RepoMetrics]:
    """Attach cross-sectional rank and the composite heat score, in place.

    Heat blends three independent questions, then modulates by a quality factor:

        z      - is this fast *for this repo*?          (own history)
        rel    - is this fast *compared to everyone*?   (cohort rank)
        accel  - is the rate itself still climbing?     (window vs prior window)
        x quality - are forks moving too?               (adoption vs promotion)

    The cohort rank matters because it cancels market-wide effects: if every
    tracked repo gains stars on a Tuesday, nobody's rank moves.
    """
    total = w_z + w_rel + w_accel
    if total <= 0:
        raise ValueError("scoring weights must sum to a positive number")
    w_z, w_rel, w_accel = w_z / total, w_rel / total, w_accel / total

    rels = sorted(m.rel_velocity for m in metrics if m.rel_velocity is not None)

    for m in metrics:
        if not m.has_window:
            m.heat = None
            continue
        m.rel_pct = pct_rank(rels, m.rel_velocity) if rels else 0.0

        # z of 10+ saturates: past that it is "extremely abnormal" either way.
        z_c = _clamp((m.z or 0.0) / 10.0, 0.0, 1.0)
        rel_c = _clamp(m.rel_pct, 0.0, 1.0)
        # An 8x speed-up saturates the acceleration component.
        acc_c = (_clamp(math.log2(m.accel_ratio) / 3.0, 0.0, 1.0)
                 if m.accel_ratio and m.accel_ratio > 0 else 0.0)

        # Forks growing at >=50% of the star rate reads as genuine adoption.
        # Unknown (no fork signal yet) is treated as neutral, not as a penalty.
        quality = 0.5 if m.fork_confirm is None else _clamp(m.fork_confirm / 0.5, 0.0, 1.0)

        core = w_z * z_c + w_rel * rel_c + w_accel * acc_c
        m.heat = 100.0 * core * (0.6 + 0.4 * quality)
        m.heat_parts = {"z": z_c, "rel": rel_c, "accel": acc_c, "quality": quality}

    return metrics


HORIZON_DAYS = {"daily": 1.0, "weekly": 7.0, "monthly": 30.0}


def acceleration(now_rate: float | None, period_stars: dict[str, int | None],
                 min_long_rate: float = 0.5) -> dict | None:
    """Current pace against the repo's own longer-horizon average.

    `now_rate` is stars/day measured from our snapshots; the baseline comes from
    GitHub's own "stars this week/month" count, which is far steadier than
    anything a few hours of local history can produce. Pairing the two means a
    repo needs to be on only *one* board to have an acceleration, instead of two.

    The longest horizon available is used as the baseline: comparing today
    against a monthly average says more than comparing it against yesterday.
    """
    if now_rate is None or not period_stars:
        return None
    horizons = {b: v for b, v in period_stars.items()
                if v is not None and b in HORIZON_DAYS}
    if not horizons:
        return None
    horizon = max(horizons, key=lambda b: HORIZON_DAYS[b])
    long_rate = horizons[horizon] / HORIZON_DAYS[horizon]
    if long_rate < min_long_rate:
        return None                      # no meaningful baseline to accelerate against
    return {
        "now_rate": round(now_rate, 3),
        "long_rate": round(long_rate, 3),
        # Additive smoothing keeps a repo going from 0 to 4/day off infinity.
        "ratio": round((now_rate + ACCEL_EPS) / (long_rate + ACCEL_EPS), 4),
        "horizon": horizon,
    }


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x
