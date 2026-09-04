"""Discovery, snapshotting, analysis and (opt-in) historical backfill."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from . import db
from . import metrics as mx
from . import trending as tr
from .config import Config
from .github import GitHubClient

log = logging.getLogger("gittrack.ingest")

# GitHub's stargazers endpoint stops paginating at 400 pages x 100 entries.
STARGAZER_PAGE_CAP = 400
STARGAZER_STAR_CAP = STARGAZER_PAGE_CAP * 100


def _qualifiers(cfg: Config) -> str:
    q = []
    if cfg.universe.exclude_forks:
        q.append("fork:false")
    if cfg.universe.exclude_archived:
        q.append("archived:false")
    return " ".join(q)


def _excluded(cfg: Config, full_name: str) -> bool:
    low = full_name.lower()
    return any(frag.lower() in low for frag in cfg.universe.exclude_name_contains)


def _snapshot_from_payload(conn, repo: dict, ts: int) -> None:
    db.insert_snapshot(
        conn,
        repo_id=repo["id"],
        ts=ts,
        stars=repo.get("stargazers_count", 0),
        forks=repo.get("forks_count"),
        watchers=repo.get("subscribers_count"),
        open_issues=repo.get("open_issues_count"),
        size_kb=repo.get("size"),
    )


def refresh(conn, client: GitHubClient, cfg: Config, *, discover: bool | None = None,
            ts: int | None = None) -> dict:
    """One ingest cycle: bulk-snapshot the top N, then cover everything else.

    The search endpoint returns 100 complete repo objects per request, so the
    entire top-100 universe is snapshotted with a single API call. Repos that are
    tracked but not in the current search result (watchlist entries, or repos
    that have since dropped out of the top N) are fetched individually, with
    conditional requests so an unchanged repo costs no rate-limit quota.
    """
    ts = ts or int(time.time())
    if discover is None:
        last = db.last_successful_run(conn, "discover")
        discover = (last is None or
                    (ts - last["started_at"]) >= cfg.universe.discover_every_hours * 3600)

    run_id = db.start_run(conn, "discover" if discover else "snapshot", now=ts)
    seen_ids: set[int] = set()
    written = 0
    added = 0
    error = None

    try:
        # --- bulk path: the top N via search -------------------------------
        items = client.top_repositories(
            cfg.universe.top_n, cfg.universe.min_stars, _qualifiers(cfg)
        )
        known = {r["id"] for r in conn.execute("SELECT id FROM repos").fetchall()}
        for item in items:
            if _excluded(cfg, item["full_name"]):
                continue
            is_new = item["id"] not in known
            if is_new and not discover:
                # Outside a discovery cycle the universe does not grow.
                continue
            db.upsert_repo(conn, item, source="top", now=ts)
            _snapshot_from_payload(conn, item, ts)
            seen_ids.add(item["id"])
            written += 1
            added += 1 if is_new else 0

        # --- watchlist -----------------------------------------------------
        if discover:
            for full_name in cfg.universe.watchlist:
                existing = db.repo_by_name(conn, full_name)
                if existing is not None and existing["id"] in seen_ids:
                    conn.execute("UPDATE repos SET source = 'watchlist' WHERE id = ?",
                                 (existing["id"],))
                    continue
                try:
                    repo, etag = client.get_repo(full_name)
                except Exception as exc:  # a typo in the watchlist must not kill the run
                    log.warning("watchlist repo %s: %s", full_name, exc)
                    continue
                if repo is None:
                    continue
                db.upsert_repo(conn, repo, source="watchlist", now=ts)
                db.set_etag(conn, repo["id"], etag)
                _snapshot_from_payload(conn, repo, ts)
                seen_ids.add(repo["id"])
                written += 1

        # --- everything else we track but search did not return ------------
        conn.commit()
        # Least-recently-snapshotted first, capped, so a universe that only grows
        # cannot outrun the rate limit. Anything skipped is picked up next run.
        stragglers = [r for r in db.tracked_repos_by_staleness(conn)
                      if r["id"] not in seen_ids]
        skipped = max(0, len(stragglers) - cfg.universe.max_straggler_fetches)
        if skipped:
            log.info("deferring %d stale repo(s) to the next run (cap=%d)",
                     skipped, cfg.universe.max_straggler_fetches)
        stragglers = stragglers[:cfg.universe.max_straggler_fetches]
        for r in stragglers:
            try:
                repo, etag = client.get_repo(r["full_name"], etag=r["etag"])
            except Exception as exc:
                log.warning("snapshot %s: %s", r["full_name"], exc)
                continue
            if repo is None:
                # 304: nothing about the repo changed, so carry the last values
                # forward rather than leaving a hole in the series.
                last = db.last_snapshot(conn, r["id"])
                if last is not None:
                    db.insert_snapshot(conn, r["id"], ts, last["stars"], last["forks"],
                                       last["watchers"], last["open_issues"], last["size_kb"])
                    written += 1
                continue
            db.upsert_repo(conn, repo, source=r["source"], now=ts)
            db.set_etag(conn, repo["id"], etag)
            _snapshot_from_payload(conn, repo, ts)
            written += 1
            seen_ids.add(repo["id"])

        conn.commit()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        conn.commit()
        db.finish_run(conn, run_id, len(seen_ids), written, client.requests_made, error)
        raise

    db.finish_run(conn, run_id, len(seen_ids), written, client.requests_made, None)
    return {
        "discover": discover,
        "repos_seen": len(seen_ids),
        "repos_added": added,
        "snapshots_written": written,
        "api_requests": client.requests_made,
        "deferred": skipped,
        "ts": ts,
    }


def refresh_trending(conn, cfg: Config, since: str = "daily", language: str = "",
                     ts: int | None = None, track: bool | None = None,
                     compare_period: float | None = None, fetch: bool = True) -> dict:
    """Read one trending board, store it, and diff it against the last reading.

    Costs no GitHub API quota - it is a public HTML page. When auto_track is on,
    every repo seen is added to the tracked universe and gets a snapshot from the
    board's own star/fork counts, so a repo that trends today already has a data
    point before the next API cycle touches it.
    """
    ts = ts or int(time.time())
    track = cfg.trending.auto_track if track is None else track

    if fetch:
        rows = tr.fetch_rows(since=since, language=language)
        if not rows:
            raise RuntimeError(
                f"trending board '{since}' returned no parseable rows - "
                "the page layout may have changed")
    else:
        # Offline: treat the newest stored reading as "current".
        latest = db.trending_times(conn, since, language, limit=1)
        if not latest:
            raise RuntimeError(f"no stored readings for board '{since}'")
        ts = latest[0]
        rows = [tr.TrendingRow(rank=r["rank"], full_name=r["full_name"],
                               repo_id=r["repo_id"], stars=r["stars"],
                               forks=r["forks"], period_stars=r["period_stars"],
                               language=r["repo_language"])
                for r in db.trending_at(conn, ts, since, language)]
        track = False

    # What we diff against: the previous reading by default, or the reading
    # nearest `compare_period` ago. Readings are irregular, so "a week ago" means
    # the nearest reading to that point - within half a period, else we say there
    # is nothing comparable rather than diffing against the wrong week.
    any_reading = bool(db.trending_times(conn, since, language, limit=1))
    if compare_period:
        base_ts = db.nearest_reading(conn, since, language, ts - compare_period,
                                     tolerance=compare_period / 2)
    else:
        times = db.trending_times(conn, since, language, limit=2 if not fetch else 1)
        # Offline, the newest reading *is* the current one, so step back one.
        times = times[1:] if not fetch else times
        base_ts = times[0] if times else None

    prev = ([tr.TrendingRow(rank=r["rank"], full_name=r["full_name"],
                            repo_id=r["repo_id"], stars=r["stars"], forks=r["forks"],
                            period_stars=r["period_stars"],
                            language=r["repo_language"])
             for r in db.trending_at(conn, base_ts, since, language)]
            if base_ts else [])

    if fetch:
        db.insert_trending(conn, ts, since, language, rows)

    added = 0
    if track:
        known = {r["id"] for r in conn.execute("SELECT id FROM repos").fetchall()}
        for r in rows:
            if r.repo_id is None:
                continue        # cannot key it reliably without the numeric id
            if r.repo_id not in known:
                added += 1
            db.upsert_repo(conn, r.as_repo_payload(),
                           source="trending" if r.repo_id not in known else "top",
                           now=ts)
            if r.stars is not None:
                db.insert_snapshot(conn, r.repo_id, ts, r.stars, r.forks)
    conn.commit()

    d = tr.diff(prev, rows, prev_ts=base_ts)
    return {"since": since, "language": language, "ts": ts, "rows": len(rows),
            "repos_added": added, "diff": d, "current": rows,
            "compare_period": compare_period, "base_ts": base_ts,
            "first_reading": base_ts is None,
            # Distinguish "nothing stored yet" from "nothing stored that far back" -
            # they call for completely different actions.
            "no_baseline_reason": (
                None if base_ts is not None
                else "no readings stored yet" if not any_reading
                else f"no reading within {tr.humanise_period(compare_period / 2)} of "
                     f"{tr.humanise_period(compare_period)} ago"
                     if compare_period else "no earlier reading")}


ACCEL_BOARDS = ("daily", "weekly", "monthly")
# Below this our own snapshot span is too short for a rate to mean anything.
ACCEL_MIN_SPAN_DAYS = 0.25


def record_acceleration(conn, cfg: Config, language: str = "",
                        ts: int | None = None) -> int:
    """Measure and store each board repo's current pace against its own average.

    Run after a trending sweep, once, rather than per board: the baseline should
    be the longest horizon a repo appears on, which is only knowable after every
    board has been read.
    """
    ts = ts or int(time.time())
    period: dict[str, dict] = {}
    for b in ACCEL_BOARDS:
        times = db.trending_times(conn, b, language, limit=1)
        if not times:
            continue
        for r in db.trending_at(conn, times[0], b, language):
            period.setdefault(r["full_name"], {})[b] = r["period_stars"]

    written = 0
    for name, by_board in period.items():
        repo = db.repo_by_name(conn, name)
        if repo is None:
            continue
        obs = db.load_series(conn, repo["id"])
        if len(obs) < 3:
            continue
        series = mx.Series(obs)
        span = series.span_days
        if span < ACCEL_MIN_SPAN_DAYS:
            continue
        start = series.stars_at(float(series.last_ts) - span * 86400)
        if start is None:
            continue
        now_rate = (obs[-1].stars - start) / span
        a = mx.acceleration(now_rate, by_board)
        if a is None:
            continue
        db.insert_accel(conn, repo["id"], ts, language, a)
        written += 1
    conn.commit()
    return written


def trending_summary(conn, since: str = "daily", language: str = "",
                     period: float = 7 * 86400, now: int | None = None) -> dict:
    """Aggregate every reading of a board over a period.

    Durability is the point here. A repo on 14 of the last 14 daily boards is
    compounding; one that appeared once had an afternoon. Both look identical on
    any single reading.
    """
    now = now or int(time.time())
    start = int(now - period)
    rows = db.trending_aggregate(conn, since, language, start, now)
    readings = db.trending_reading_count(conn, since, language, start, now)
    latest = db.trending_times(conn, since, language, limit=1)
    current = ({r["full_name"]: r["rank"]
                for r in db.trending_at(conn, latest[0], since, language)}
               if latest else {})
    out = []
    for r in rows:
        d = dict(r)
        d["current_rank"] = current.get(r["full_name"])
        d["hold"] = (r["appearances"] / readings) if readings else 0.0
        out.append(d)
    out.sort(key=lambda d: (-d["appearances"], d["best_rank"]))
    return {"since": since, "language": language, "period": period,
            "start_ts": start, "end_ts": now, "readings": readings, "repos": out}


def analyze(conn, cfg: Config, window_days: float | None = None) -> list[mx.RepoMetrics]:
    """Compute per-repo metrics for every tracked repo, then score the cohort."""
    window_days = window_days or cfg.metrics.default_window_days
    # Load enough history for the baseline plus the prior window used by acceleration.
    lookback = max(cfg.metrics.baseline_days, 2 * window_days) + 2
    since = int(time.time() - lookback * 86400)

    repos = {r["id"]: r for r in db.tracked_repos(conn)}
    series = db.load_all_series(conn, since=since)

    out: list[mx.RepoMetrics] = []
    for rid, repo in repos.items():
        obs = series.get(rid, [])
        out.append(mx.compute(
            rid, obs,
            window_days=window_days,
            baseline_days=cfg.metrics.baseline_days,
            min_observations=cfg.metrics.min_observations,
            min_baseline_days=cfg.metrics.min_baseline_days,
            created_at=repo["created_at"],
            full_name=repo["full_name"],
        ))
    mx.score_cohort(out, cfg.scoring.w_z, cfg.scoring.w_rel, cfg.scoring.w_accel)
    return out


def record_signals(conn, results: list[mx.RepoMetrics], cfg: Config,
                   ts: int | None = None) -> int:
    """Persist breakouts so the dashboard can say how long a repo has been hot."""
    ts = ts or int(time.time())
    n = 0
    for m in results:
        if m.heat is None or m.heat < cfg.scoring.breakout_heat:
            continue
        if m.z is None or m.z < cfg.scoring.breakout_min_z:
            continue
        db.record_signal(conn, m.repo_id, ts, m.window_days, m.heat, m.z,
                         m.rel_velocity, m.accel_ratio, m.stars)
        n += 1
    conn.commit()
    return n


def backfill(conn, client: GitHubClient, cfg: Config, full_name: str,
             progress=None) -> dict:
    """Reconstruct a repo's star history from per-star timestamps.

    Opt-in and quota-hungry (one request per 100 stars), but it turns a repo with
    no recorded history into one with a full curve immediately. Only viable below
    GitHub's 40,000-star pagination ceiling - above that the endpoint cannot reach
    recent stargazers at all, so there is nothing useful to recover.

    Backfilled snapshots carry NULL forks: the endpoint has no fork history, and
    inventing a flat fork line would make every backfilled repo look like a
    star-only pump.
    """
    if not client.authenticated:
        # The stargazers endpoint rejects anonymous requests outright, so say so
        # rather than letting a raw 401 surface from deep in the pager.
        raise ValueError(
            "backfill needs a GitHub token: the stargazers endpoint rejects "
            "anonymous requests. Run `gh auth login` or set GITHUB_TOKEN.")
    repo, etag = client.get_repo(full_name)
    if repo is None:
        raise ValueError(f"{full_name}: no data")
    stars_now = repo.get("stargazers_count", 0)
    if stars_now > STARGAZER_STAR_CAP:
        raise ValueError(
            f"{full_name} has {stars_now:,} stars; the stargazers endpoint stops at "
            f"{STARGAZER_STAR_CAP:,} and only reaches the OLDEST of them, so recent "
            f"history cannot be recovered. Snapshot it forward instead."
        )

    db.upsert_repo(conn, repo, source=(db.repo_by_name(conn, full_name) or {"source": "watchlist"})["source"])
    db.set_etag(conn, repo["id"], etag)
    repo_id = repo["id"]

    stamps: list[int] = []
    for iso in client.iter_stargazers(full_name, max_pages=STARGAZER_PAGE_CAP):
        try:
            stamps.append(int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()))
        except ValueError:
            continue
        if progress and len(stamps) % 1000 == 0:
            progress(len(stamps))
    stamps.sort()
    if not stamps:
        raise ValueError(f"{full_name}: stargazers endpoint returned no timestamps")

    # Anchor the reconstructed curve to the live count: unstarred and deleted
    # accounts vanish from the list, so the collected total undershoots.
    offset = stars_now - len(stamps)

    # One synthetic snapshot per UTC day boundary, plus a final point at now.
    written = 0
    day = 86400
    first_day = (stamps[0] // day) * day
    last_day = (stamps[-1] // day) * day
    i = 0
    count = 0
    t = first_day
    while t <= last_day:
        while i < len(stamps) and stamps[i] <= t:
            i += 1
            count += 1
        db.insert_snapshot(conn, repo_id, t, max(offset + count, 0), None)
        written += 1
        t += day
    db.insert_snapshot(conn, repo_id, int(time.time()), stars_now, repo.get("forks_count"))
    written += 1
    conn.commit()
    return {"repo": full_name, "stars": stars_now, "stamps": len(stamps),
            "snapshots_written": written,
            "from": datetime.fromtimestamp(first_day, timezone.utc).date().isoformat()}


def iso(ts: int | float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")
