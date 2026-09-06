"""The merged trending board and the per-repo signals it carries.

Lives outside the web server so the CLI, the digest and the dashboard all read
the same numbers from the same code.
"""

from __future__ import annotations

import time

from . import db
from . import metrics as mx
from . import trending as tr

ALL_BOARDS = ("daily", "weekly", "monthly")


def rank_trend(series: list[tuple[int, int]], board_size: int | None = None) -> dict:
    """Sparkline data plus the net move, from one repo's rank history.

    `strength` normalises the position to 0..1 (1 = top of the board) so a bar can
    show standing at a glance without the reader decoding a rank number against a
    board length that changes daily.
    """
    if not series:
        return {"points": [], "net": None, "strength": None, "readings": 0}
    ranks = [r for _, r in series]
    net = ranks[0] - ranks[-1]          # rank 1 is best, so a drop is a climb
    strength = None
    if board_size:
        strength = max(0.0, (board_size - ranks[-1] + 1) / board_size)
    return {"points": ranks, "net": net, "strength": strength,
            "readings": len(ranks),
            "first_ts": series[0][0], "last_ts": series[-1][0]}



def fork_signals(conn, names: list[str], period: float) -> dict:
    """Fork movement per repo, derived from our own snapshot series.

    GitHub publishes no forks board — Trending ranks by stars alone — so this is
    computed rather than scraped. Two different things come out of it:

      * `fork_ratio` (forks / stars) is available immediately and says how
        *used* a repo is versus merely bookmarked.
      * `fork_rel` (fork growth over the window) needs snapshot history and
        reports None until the series spans the window. It is left None rather
        than defaulted to zero, so "no data yet" never masquerades as "flat".
    """
    out = {}
    for name in names:
        row = db.repo_by_name(conn, name)
        if row is None:
            continue
        obs = db.load_series(conn, row["id"])
        rec = {"forks": None, "stars": None, "fork_ratio": None,
               "fork_delta": None, "fork_rel": None, "star_rel": None,
               "fork_confirm": None, "span_days": 0.0}
        if not obs:
            out[name] = rec
            continue
        last = obs[-1]
        rec["forks"], rec["stars"] = last.forks, last.stars
        if last.forks is not None and last.stars:
            rec["fork_ratio"] = last.forks / last.stars

        series = mx.Series(obs)
        rec["span_days"] = series.span_days
        now = float(series.last_ts)
        t0 = now - period
        f0, s0 = series.forks_at(t0), series.stars_at(t0)
        if f0 is not None and last.forks is not None:
            rec["fork_delta"] = last.forks - f0
            rec["fork_rel"] = rec["fork_delta"] / max(f0, 1.0)
        if s0 is not None:
            rec["star_rel"] = (last.stars - s0) / max(s0, 1.0)
        if rec["fork_rel"] is not None and rec["star_rel"]:
            rec["fork_confirm"] = rec["fork_rel"] / rec["star_rel"]
        out[name] = rec
    return out



def combined_board(conn, language: str, over: float,
                   rank_by: str = "stars") -> dict:
    """One time-agnostic board merged from daily + weekly + monthly.

    Each board contributes a normalised position: rank 1 scores 1.0, last scores
    ~0, absent scores 0. Summing them means a repo has to be *both* highly placed
    *and* present on more than one horizon to reach the top - which is exactly
    "trending the most" rather than "trending loudest today".

    Positions are normalised by each board's own length because the boards are
    not the same size (19 / 23 / 20 today), so a raw rank would quietly weight
    the shortest board highest.
    """
    if True:
        ranks, sizes, detail, period = {}, {}, {}, {}
        for b in ALL_BOARDS:
            times = db.trending_times(conn, b, language, limit=1)
            if not times:
                ranks[b], sizes[b] = {}, 0
                continue
            rows = db.trending_at(conn, times[0], b, language)
            ranks[b] = {r["full_name"]: r["rank"] for r in rows}
            sizes[b] = len(rows)
            for r in rows:
                d = detail.setdefault(r["full_name"], {})
                for k in ("repo_language", "stars", "forks", "repo_id"):
                    if d.get(k) is None:
                        d[k] = r[k]
                period.setdefault(r["full_name"], {})[b] = r["period_stars"]

        hist = {b: db.trending_rank_series(conn, b, language, int(time.time() - over))
                for b in ALL_BOARDS}
        accel = db.latest_accel(conn, language)
        names = set().union(*[set(v) for v in ranks.values()]) if ranks else set()
        out = []
        for name in names:
            d = detail.get(name, {})
            per, score = {}, 0.0
            for b in ALL_BOARDS:
                rk = ranks[b].get(name)
                per[b] = rk
                if rk and sizes[b]:
                    score += (sizes[b] - rk + 1) / sizes[b]
            shares = {}
            for b, ps in period.get(name, {}).items():
                if ps and d.get("stars"):
                    # A period count can exceed the current total when stars are
                    # removed after being counted, which would render as "more
                    # than all of its stars arrived" — clamp rather than show it.
                    shares[b] = min(1.0, ps / d["stars"])
            best_share_board = max(shares, key=shares.get) if shares else None
            # Sparkline the board the repo currently stands highest on — that is
            # the one whose movement actually describes it.
            present = [(rk, b) for b, rk in per.items() if rk]
            home = min(present)[1] if present else "daily"
            trend = rank_trend(hist[home].get(name, []), sizes.get(home) or None)
            trend["board"] = home
            # Hold on the repo's home board: the share of that board's readings in
            # which it held a place. Computed from history already in hand rather
            # than by re-running the summary three times.
            board_reads = len({t for series in hist[home].values() for t, _ in series})
            appearances = len(hist[home].get(name, []))
            hold = (appearances / board_reads) if board_reads else None
            streak, streak_from = db.trending_streak(conn, name, "daily", language)
            out.append({
                "accel": accel.get(name),
                "trend": trend,
                "full_name": name, "repo_id": d.get("repo_id"),
                "language": d.get("repo_language"), "stars": d.get("stars"),
                "forks": d.get("forks"),
                "also_on": per,
                "board_count": sum(1 for v in per.values() if v is not None),
                "score": round(score, 4),
                "period_by_board": period.get(name, {}),
                "share": shares.get(best_share_board) if best_share_board else None,
                "share_board": best_share_board,
                "period_stars": period.get(name, {}).get(best_share_board),
                "streak": streak, "streak_from": streak_from,
                "hold": hold, "appearances": appearances,
                "hold_board": home, "board_readings": board_reads,
                "url": f"https://github.com/{name}",
            })
        # Fork movement, computed from our snapshots (no GitHub forks board exists).
        forks = fork_signals(conn, [r["full_name"] for r in out], over)
        for r in out:
            r.update(forks.get(r["full_name"], {}))
            r["star_component"] = r["score"] / len(ALL_BOARDS)

        # Cross-sectional fork rank, over the repos that actually have history.
        with_fork = sorted((r["fork_rel"] for r in out if r["fork_rel"] is not None))
        fork_ready = len(with_fork)
        for r in out:
            if r["fork_rel"] is not None and with_fork:
                r["fork_component"] = mx.pct_rank(with_fork, r["fork_rel"])
            else:
                r["fork_component"] = None
            # 60/40: stars lead because that is what the boards actually rank,
            # forks adjust because they are the adoption signal rather than the
            # attention one. Unknown fork data is neutral, never a penalty.
            fc = 0.5 if r["fork_component"] is None else r["fork_component"]
            r["score_both"] = round(0.6 * r["star_component"] + 0.4 * fc, 4)

        key = {"stars": lambda r: (-r["score"], r["full_name"]),
               "forks": lambda r: (-(r["fork_rel"] if r["fork_rel"] is not None else -1),
                                   -(r["fork_ratio"] or 0), r["full_name"]),
               # Pure strength: what share of the repo's whole life arrived now.
               "surge": lambda r: (-(r["share"] or 0), r["full_name"]),
               "both": lambda r: (-r["score_both"], r["full_name"])}[rank_by]
        out.sort(key=key)
        for i, r in enumerate(out, 1):
            r["rank"] = i

        return {
            "since": "combined", "language": language, "over": over,
            "rank_by": rank_by, "fork_ready": fork_ready,
            "combined": True, "board": out, "gone": [],
            "boards": [dict(b) for b in db.trending_boards(conn)],
            "all_boards": list(ALL_BOARDS),
            "board_sizes": sizes,
            "readings": sum(1 for b in ALL_BOARDS if sizes[b]),
            "over_label": tr.humanise_period(over),
            "compare_label": None, "base_ts": None, "ts": None,
            "empty": None if out else
                     "No readings stored yet. Run: gittrack trending",
        }
