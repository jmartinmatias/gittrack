"""Command line interface."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from rich.console import Console

from . import config as cfgmod
from . import db, ingest, notify, report
from . import digest as dg
from . import metrics as mx
from . import trending as tr
from .github import GitHubClient, GitHubError, resolve_token

console = Console()
err = Console(stderr=True)


# ------------------------------------------------------------------ helpers


def _client(cfg: cfgmod.Config) -> GitHubClient:
    token = resolve_token(cfg.github.token_env, cfg.github.use_gh_cli)
    if not token:
        err.print(
            "[yellow]No GitHub token found.[/yellow] Unauthenticated limits are "
            "60 req/hr and 10 searches/min - enough to try this out, not to run it.\n"
            f"  Fix: [bold]gh auth login[/bold]  or  "
            f"[bold]export {cfg.github.token_env}=ghp_...[/bold]"
        )
    return GitHubClient(token=token, api_base=cfg.github.api_base)


def _open(args) -> tuple[cfgmod.Config, "db.sqlite3.Connection"]:
    cfg = cfgmod.load(args.config)
    if args.db:
        cfg.db_path = args.db
    if not Path(cfg.db_path).exists() and args.cmd not in ("init",):
        err.print(f"[red]No database at {cfg.db_path}.[/red] Run [bold]gittrack init[/bold] first.")
        raise SystemExit(2)
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    return cfg, conn


def _sorted(results: list[mx.RepoMetrics], by: str) -> list[mx.RepoMetrics]:
    key = report.SORT_KEYS[by]

    def sort_key(m):
        v = key(m)
        # Unknowns sort last regardless of direction.
        return (0 if v is None else 1, v if v is not None else 0.0)

    return sorted(results, key=sort_key, reverse=True)


def _filter(results, args, repos: dict):
    out = []
    for m in results:
        r = repos.get(m.repo_id)
        if args.min_stars and m.stars < args.min_stars:
            continue
        if args.max_stars and m.stars > args.max_stars:
            continue
        if args.language and (not r or (r["language"] or "").lower() != args.language.lower()):
            continue
        if args.max_age_days and (m.age_days is None or m.age_days > args.max_age_days):
            continue
        if args.min_z is not None and (m.z is None or m.z < args.min_z):
            continue
        out.append(m)
    return out


# ------------------------------------------------------------------ commands


def cmd_init(args) -> int:
    cfg = cfgmod.load(args.config)
    if args.db:
        cfg.db_path = args.db
    path = Path(args.config or cfgmod.DEFAULT_CONFIG_NAME)
    if path.exists() and not args.force:
        console.print(f"[dim]{path} already exists, leaving it alone.[/dim]")
    else:
        path.write_text(cfgmod.DEFAULT_CONFIG_TOML)
        console.print(f"[green]wrote[/green] {path}")
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    console.print(f"[green]initialised[/green] {cfg.db_path}")
    console.print(
        "\nNext: [bold]gittrack run[/bold] to take the first snapshot, then keep it on a "
        "schedule:\n  [dim]0 * * * * cd "
        f"{Path.cwd()} && {sys.argv[0]} run >> gittrack.log 2>&1[/dim]"
    )
    return 0


def cmd_run(args) -> int:
    cfg, conn = _open(args)
    discover = None
    if args.discover:
        discover = True
    elif args.no_discover:
        discover = False
    # The two halves of a run are independent: the API snapshot spends rate-limit
    # quota, trending spends none. A snapshot failure (an exhausted limit, most
    # often) must not stop the trending boards from refreshing - otherwise the
    # part that costs nothing goes stale because of the part that costs quota.
    res, snapshot_error = None, None
    if args.trending_only:
        console.print("[dim]trending only - skipping the API snapshot pass[/dim]")
    else:
        with _client(cfg) as gh:
            try:
                res = ingest.refresh(conn, gh, cfg, discover=discover)
            except GitHubError as exc:
                snapshot_error = exc

    if res is not None and cfg.universe.untrack_after_days > 0:
        pruned = db.prune_stale(conn, cfg.universe.untrack_after_days)
        conn.commit()
        if pruned:
            console.print(f"[green]pruned[/green] {len(pruned)} repo(s) no board has "
                          f"listed for {cfg.universe.untrack_after_days:g}d")
    if res is not None:
        console.print(
            f"[green]{'discover' if res['discover'] else 'snapshot'}[/green]  "
            f"repos={res['repos_seen']}  new={res['repos_added']}  "
            f"snapshots={res['snapshots_written']}  api_calls={res['api_requests']}"
        )
    elif snapshot_error is not None:
        err.print(f"[red]snapshot failed:[/red] {snapshot_error}")
        err.print("[dim]continuing to the trending boards, which cost no API quota[/dim]")

    ts = res["ts"] if res is not None else int(time.time())

    if cfg.trending.enabled and not args.no_trending:
        boards = [(sc, lg) for sc in cfg.trending.since
                  for lg in cfg.trending.languages]
        # Log the sweep as its own run, so `gittrack status` can answer "did the
        # hourly job actually fire?" for a trending-only schedule too.
        sweep_id = db.start_run(conn, "trending", now=ts)
        swept = rows_seen = added = 0
        for i, (since, lang) in enumerate(boards):
            try:
                t = ingest.refresh_trending(conn, cfg, since=since, language=lang, ts=ts)
            except Exception as exc:
                # Trending is scraped, so treat a failure as non-fatal and keep
                # going: one bad board must not cost us the other twenty-three.
                err.print(f"[yellow]trending {since}/{lang or 'all'} skipped: "
                          f"{exc}[/yellow]")
                continue
            finally:
                # Be a polite scraper - this is someone else's server, and a run
                # can sweep two dozen boards. No wait after the last one.
                if i < len(boards) - 1:
                    time.sleep(cfg.trending.request_delay)
            swept += 1
            rows_seen += t["rows"]
            added += t["repos_added"]
            d = t["diff"]
            console.print(
                f"[green]trending[/green] {since}/{lang or 'all'}  "
                f"rows={t['rows']}  entered={len(d.entered)}  "
                f"left={len(d.left)}  new_repos={t['repos_added']}")
        db.finish_run(conn, sweep_id, repos_seen=rows_seen, snapshots_written=swept,
                      api_requests=0,
                      error=None if swept else "every board failed")
        if swept:
            for lang in cfg.trending.languages:
                n_acc = ingest.record_acceleration(conn, cfg, language=lang, ts=ts)
                if n_acc and not lang:
                    console.print(f"[green]acceleration[/green] recorded for "
                                  f"{n_acc} repos")
            # The point of the sweep: tell the user if something new surfaced.
            d = dg.build(conn, over=cfg.digest.over_days * 86400,
                         limit=cfg.digest.limit, max_stars=cfg.digest.max_stars)
            fresh = dg.new_entrants(conn, "", d["items"])
            if fresh and cfg.notify.hourly and not args.no_notify:
                sent = notify.send(cfg, fresh, f"{len(fresh)} new in the gittrack digest")
                console.print(f"[green]digest[/green] {len(fresh)} new entrant(s): "
                              + ", ".join(f["full_name"] for f in fresh)
                              + f"  [dim](notified via "
                              f"{', '.join(k for k, v in sent.items() if v) or 'nothing'})[/dim]")
            elif fresh:
                console.print(f"[green]digest[/green] {len(fresh)} new entrant(s)")
            # One full digest a day at the chosen hour, once, regardless of novelty.
            # Remembered per calendar day so a sweep that lands late still sends
            # it and a second sweep that hour does not send it twice.
            if cfg.notify.daily_hour >= 0 and not args.no_notify and d["items"]:
                import datetime as _dt
                now_local = _dt.datetime.now()
                today = now_local.date().isoformat()
                if (now_local.hour >= cfg.notify.daily_hour
                        and db.get_meta(conn, "daily_digest_sent") != today):
                    sent = notify.send(cfg, d["items"],
                                       f"gittrack daily digest, {today}")
                    db.set_meta(conn, "daily_digest_sent", today)
                    conn.commit()
                    console.print(f"[green]daily digest[/green] sent via "
                                  f"{', '.join(k for k, v in sent.items() if v) or 'nothing'}")

    results = ingest.analyze(conn, cfg)
    n = ingest.record_signals(conn, results, cfg, ts=ts)
    ready = sum(1 for m in results if m.has_window)
    if ready == 0:
        console.print(
            f"[dim]No repo has {cfg.metrics.default_window_days:g}d of history yet - "
            "velocity appears once snapshots span the window.[/dim]"
        )
    elif n:
        console.print(f"[bold yellow]{n} breakout signal(s) recorded.[/bold yellow]")
    # Non-zero so the failure is visible in the log, but only after the trending
    # half has had its turn.
    return 1 if snapshot_error is not None else 0


def cmd_top(args) -> int:
    cfg, conn = _open(args)
    results = ingest.analyze(conn, cfg, window_days=args.window)
    repos = {r["id"]: r for r in db.tracked_repos(conn)}
    results = _filter(results, args, repos)
    results = _sorted(results, args.by)

    if args.record:
        ingest.record_signals(conn, results, cfg)

    if args.json:
        print(report.to_json(results, args.limit))
        return 0
    if args.csv:
        print(report.to_csv(results, args.limit), end="")
        return 0

    sparks = None
    if args.spark:
        lookback = int(time.time() - 16 * 86400)
        series = db.load_all_series(conn, since=lookback)
        sparks = {rid: report.daily_spark(obs, days=14) for rid, obs in series.items()}
    report.render_top(results, by=args.by, limit=args.limit, sparks=sparks,
                      console=console, show_insufficient=args.show_insufficient)
    return 0


def cmd_trending(args) -> int:
    cfg, conn = _open(args)
    since_list = [args.since] if args.since else cfg.trending.since
    langs = [args.language] if args.language is not None else cfg.trending.languages

    try:
        compare = tr.parse_period(args.compare) if args.compare else None
        over = tr.parse_period(args.over) if args.over else None
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        return 2

    rc = 0
    # --over is a read-only summary of stored readings; it never fetches.
    if over is not None:
        for since in since_list:
            for lang in langs:
                summary = ingest.trending_summary(conn, since=since, language=lang,
                                                  period=over)
                if args.json:
                    print(json.dumps(summary, indent=2, default=float))
                else:
                    report.render_trending_summary(summary, limit=args.limit,
                                                   console=console)
        return 0

    for since in since_list:
        for lang in langs:
            try:
                res = ingest.refresh_trending(
                    conn, cfg, since=since, language=lang,
                    track=False if args.no_track else None,
                    compare_period=compare, fetch=not args.no_fetch)
            except Exception as exc:
                err.print(f"[red]trending {since}/{lang or 'all'}: {exc}[/red]")
                rc = 1
                continue
            if args.json:
                print(report.trending_to_json(res))
                continue
            streaks = {r.full_name: db.trending_streak(conn, r.full_name, since, lang)
                       for r in res["current"]}
            report.render_trending(res, streaks, limit=args.limit, console=console)
            if res["base_ts"]:
                console.print(f"[dim]  compared against the reading of "
                              f"{ingest.iso(res['base_ts'])}"
                              + (f" (~{tr.humanise_period(compare)} ago)"
                                 if compare else "") + "[/dim]")
            if res["repos_added"]:
                console.print(f"[green]+{res['repos_added']}[/green] new repo(s) "
                              f"added to the tracked universe")
    return rc


def cmd_digest(args) -> int:
    cfg, conn = _open(args)
    d = dg.build(conn, language=args.language or "",
                 over=cfg.digest.over_days * 86400,
                 limit=args.limit or cfg.digest.limit,
                 max_stars=cfg.digest.max_stars,
                 include_seen=args.include_seen)
    if args.json:
        print(json.dumps(d, indent=2, default=float))
        return 0
    report.render_digest(d, console=console)
    if args.notify:
        fresh = dg.new_entrants(conn, d["language"], d["items"])
        if fresh:
            sent = notify.send(cfg, fresh, f"{len(fresh)} new in the gittrack digest")
            console.print(f"[dim]notified: {', '.join(k for k, v in sent.items() if v) or 'nothing configured'}[/dim]")
        else:
            console.print("[dim]nothing new since the last digest; no notification sent[/dim]")
    return 0


def cmd_scorecard(args) -> int:
    _cfg, conn = _open(args)
    sc = dg.scorecard(conn, language=args.language or "", horizon=args.horizon * 3600)
    if args.json:
        print(json.dumps(sc, indent=2, default=float))
        return 0
    report.render_scorecard(sc, console=console)
    return 0


def cmd_seen(args) -> int:
    _cfg, conn = _open(args)
    if args.list or not args.repos:
        rows = conn.execute("SELECT * FROM seen ORDER BY ts DESC").fetchall()
        if not rows:
            console.print("[dim]nothing marked seen yet[/dim]")
        for r in rows:
            console.print(f"  {r['full_name']}  [dim]{ingest.iso(r['ts'])}"
                          + (f"  {r['note']}" if r["note"] else "") + "[/dim]")
        return 0
    for name in args.repos:
        if args.undo:
            n = db.unmark_seen(conn, name)
            console.print(f"[green]unmarked[/green] {name}" if n else f"[dim]{name} was not marked[/dim]")
        else:
            db.mark_seen(conn, name, note=args.note)
            console.print(f"[green]seen[/green] {name}")
    conn.commit()
    return 0


def cmd_show(args) -> int:
    cfg, conn = _open(args)
    row = db.repo_by_name(conn, args.repo)
    if row is None:
        err.print(f"[red]{args.repo} is not tracked.[/red] "
                  f"Add it with [bold]gittrack watch add {args.repo}[/bold].")
        return 2
    obs = db.load_series(conn, row["id"])
    m = mx.compute(
        row["id"], obs,
        window_days=args.window or cfg.metrics.default_window_days,
        baseline_days=cfg.metrics.baseline_days,
        min_observations=cfg.metrics.min_observations,
        min_baseline_days=cfg.metrics.min_baseline_days,
        created_at=row["created_at"], full_name=row["full_name"],
    )
    # Score against the live cohort so the percentile is meaningful.
    cohort = ingest.analyze(conn, cfg, window_days=m.window_days)
    for c in cohort:
        if c.repo_id == m.repo_id:
            m.rel_pct, m.heat, m.heat_parts = c.rel_pct, c.heat, c.heat_parts
            break
    if args.json:
        print(report.to_json([m]))
        return 0
    report.render_show(m, row, obs, db.hot_since(conn, row["id"]), console=console)
    return 0


def cmd_watch(args) -> int:
    cfg, conn = _open(args)
    if args.action == "list":
        rows = conn.execute(
            "SELECT full_name, source, tracked FROM repos WHERE source = 'watchlist'"
            " ORDER BY full_name"
        ).fetchall()
        if not rows:
            console.print("[dim]watchlist is empty[/dim]")
        for r in rows:
            console.print(f"  {r['full_name']}" + ("" if r["tracked"] else " [dim](untracked)[/dim]"))
        return 0

    if args.action == "add":
        with _client(cfg) as gh:
            for name in args.repos:
                try:
                    repo, etag = gh.get_repo(name)
                except GitHubError as exc:
                    err.print(f"[red]{name}: {exc}[/red]")
                    continue
                if repo is None:
                    continue
                db.upsert_repo(conn, repo, source="watchlist")
                db.set_etag(conn, repo["id"], etag)
                ingest._snapshot_from_payload(conn, repo, int(time.time()))
                console.print(f"[green]watching[/green] {repo['full_name']} "
                              f"({repo['stargazers_count']:,}★)")
        conn.commit()
        _persist_watchlist(cfg, conn)
        return 0

    if args.action == "rm":
        ids = []
        for name in args.repos:
            row = db.repo_by_name(conn, name)
            if row is None:
                err.print(f"[yellow]{name} not tracked[/yellow]")
                continue
            ids.append(row["id"])
            console.print(f"[green]untracked[/green] {row['full_name']} "
                          "[dim](history kept)[/dim]")
        db.set_tracked(conn, ids, False)
        conn.execute(
            "UPDATE repos SET source = 'top' WHERE source = 'watchlist' AND tracked = 0")
        conn.commit()
        _persist_watchlist(cfg, conn)
        return 0
    return 2


def _persist_watchlist(cfg: cfgmod.Config, conn) -> None:
    """Mirror the watchlist back into the config file so it survives a fresh DB."""
    path = cfgmod.find_config(cfg.source_path)
    if path is None or not path.exists():
        return
    names = [r["full_name"] for r in conn.execute(
        "SELECT full_name FROM repos WHERE source = 'watchlist' AND tracked = 1"
        " ORDER BY full_name").fetchall()]
    text = path.read_text()
    new_line = "watchlist = " + json.dumps(names)
    import re
    if re.search(r"^watchlist\s*=.*$", text, flags=re.M):
        text = re.sub(r"^watchlist\s*=.*$", new_line, text, count=1, flags=re.M)
        path.write_text(text)


def cmd_backfill(args) -> int:
    cfg, conn = _open(args)
    with _client(cfg) as gh:
        for name in args.repos:
            console.print(f"[dim]backfilling {name} …[/dim]")
            try:
                res = ingest.backfill(
                    conn, gh, cfg, name,
                    progress=lambda n: console.print(f"[dim]  {n:,} stars[/dim]", end="\r"),
                )
            except (ValueError, GitHubError) as exc:
                err.print(f"[red]{name}: {exc}[/red]")
                continue
            console.print(
                f"[green]{res['repo']}[/green]  {res['stamps']:,} star timestamps "
                f"since {res['from']} → {res['snapshots_written']:,} daily snapshots"
            )
    return 0


def cmd_status(args) -> int:
    cfg, conn = _open(args)
    s = db.stats(conn)
    console.print()
    console.print(f"[bold]database[/bold]      {cfg.db_path}")
    console.print(f"[bold]config[/bold]        {cfg.source_path or '(defaults)'}")
    console.print(f"[bold]tracked[/bold]       {s['repos_tracked']} of {s['repos_total']} known repos")
    console.print(f"[bold]snapshots[/bold]     {s['snapshots']:,}")
    console.print(f"[bold]coverage[/bold]      {ingest.iso(s['first_ts'])} → {ingest.iso(s['last_ts'])}"
                  + (f"  ({(s['last_ts'] - s['first_ts']) / 86400:.1f} days)"
                     if s["first_ts"] and s["last_ts"] else ""))
    console.print(f"[bold]signals[/bold]       {s['signals']}")
    console.print(f"[bold]trending[/bold]      {s['trending_readings']} reading(s), "
                  f"{s['trending_repos']} distinct repos seen")

    chans = notify.channels_configured(cfg)
    console.print("[bold]notify[/bold]        "
                  + (", ".join(chans) if chans else "[yellow]no channel configured[/yellow]")
                  + (f"  daily at {cfg.notify.daily_hour:02d}:00" if cfg.notify.daily_hour >= 0 else "")
                  + ("  hourly on new entrants" if cfg.notify.hourly else ""))
    cov = db.run_coverage(conn, "trending", 3600)
    if cov["expected"]:
        style = "green" if cov["pct"] >= 0.9 else "yellow" if cov["pct"] >= 0.6 else "red"
        console.print(f"[bold]coverage[/bold]      [{style}]{cov['actual']} of ~{cov['expected']} "
                      f"hourly sweeps landed ({cov['pct'] * 100:.0f}%)[/{style}]  "
                      f"largest gap {cov['largest_gap_h']}h"
                      + ("  [dim]- the machine sleeps; see README[/dim]"
                         if cov["largest_gap_h"] >= 3 else ""))

    for kind in ("discover", "snapshot", "trending"):
        r = db.last_successful_run(conn, kind)
        if r:
            label = f"last {kind}"
            console.print(f"[bold]{label}[/bold]{' ' * max(1, 19 - len(label))}"
                          f"{ingest.iso(r['started_at'])}  "
                          + (f"boards={r['snapshots_written']} rows={r['repos_seen']}"
                             if kind == "trending"
                             else f"repos={r['repos_seen']} api={r['api_requests']}"))
    failed = conn.execute(
        "SELECT started_at, kind, error FROM runs WHERE error IS NOT NULL"
        " ORDER BY started_at DESC LIMIT 3").fetchall()
    for f in failed:
        console.print(f"[red]failed[/red]        {ingest.iso(f['started_at'])} "
                      f"{f['kind']}: {f['error'][:80]}")

    if args.rate_limit:
        with _client(cfg) as gh:
            try:
                for name, b in gh.rate_limit().items():
                    if name in ("core", "search", "graphql"):
                        console.print(f"[bold]{name:<13}[/bold] {b['remaining']}/{b['limit']}"
                                      f"  resets {ingest.iso(b['reset'])}")
            except GitHubError as exc:
                err.print(f"[red]{exc}[/red]")
    console.print()
    return 0


def cmd_prune(args) -> int:
    cfg, conn = _open(args)
    days = args.days if args.days is not None else cfg.universe.untrack_after_days
    if days <= 0:
        console.print("[dim]pruning is disabled (untrack_after_days = 0)[/dim]")
        return 0
    names = db.prune_stale(conn, days, dry_run=args.dry_run)
    conn.commit()
    verb = "would untrack" if args.dry_run else "untracked"
    console.print(f"[green]{verb}[/green] {len(names)} repo(s) no board has listed "
                  f"for {days:g} days [dim](history kept)[/dim]")
    for n in names[:20]:
        console.print(f"  {n}")
    if len(names) > 20:
        console.print(f"  [dim]... and {len(names) - 20} more[/dim]")
    return 0


def cmd_compact(args) -> int:
    _cfg, conn = _open(args)
    before = db.stats(conn)["snapshots"]
    n = db.compact(conn, keep_full_days=args.keep_days)
    conn.commit()
    conn.execute("VACUUM")
    console.print(f"[green]compacted[/green] {n:,} rows removed "
                  f"({before:,} → {before - n:,}); "
                  f"full resolution kept for the last {args.keep_days:g} days")
    return 0


def cmd_serve(args) -> int:
    cfg, conn = _open(args)
    conn.close()
    from .server import serve
    serve(cfg, host=args.host or cfg.server.host, port=args.port or cfg.server.port,
          open_browser=not args.no_browser)
    return 0


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gittrack",
        description="Track GitHub star/fork velocity and surface repos gaining "
                    "abnormal traction early.",
    )
    p.add_argument("--config", help=f"config file (default: ./{cfgmod.DEFAULT_CONFIG_NAME})")
    p.add_argument("--db", help="override the database path")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create the config file and database")
    s.add_argument("--force", action="store_true", help="overwrite an existing config")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("run", help="discover + snapshot (this is what cron calls)")
    s.add_argument("--discover", action="store_true", help="force a discovery pass")
    s.add_argument("--no-discover", action="store_true", help="snapshot only")
    s.add_argument("--no-trending", action="store_true", help="skip the trending boards")
    s.add_argument("--trending-only", action="store_true",
                   help="snapshot the trending boards only; skip the API pass "
                        "entirely (costs no rate-limit quota)")
    s.add_argument("--no-notify", action="store_true",
                   help="do not send notifications for new digest entrants")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("digest", help="what deserves attention right now, and why")
    s.add_argument("--limit", type=int)
    s.add_argument("--language", help='a language board; "" (default) is all')
    s.add_argument("--include-seen", action="store_true",
                   help="include repos already marked seen")
    s.add_argument("--notify", action="store_true",
                   help="notify about entrants new since the last digest")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_digest)

    s = sub.add_parser("scorecard", help="did the digest's picks outgrow the board "
                                         "afterwards? the only honest test")
    s.add_argument("--horizon", type=float, default=24.0, metavar="HOURS",
                   help="how long after a pick to measure (default 24)")
    s.add_argument("--language", help='language board; "" (default) is all')
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_scorecard)

    s = sub.add_parser("seen", help="mark repos as reviewed so the digest moves on")
    s.add_argument("repos", nargs="*", metavar="owner/name")
    s.add_argument("--note", help="why, for your future self")
    s.add_argument("--undo", action="store_true", help="unmark instead")
    s.add_argument("--list", action="store_true")
    s.set_defaults(func=cmd_seen)

    s = sub.add_parser("trending", help="read GitHub Trending and diff it against "
                                        "the last reading (no API quota)")
    s.add_argument("--since", choices=list(tr.SINCE_VALUES),
                   help="board to read (default from config)")
    s.add_argument("--language", help='language board, e.g. rust. "" is all languages')
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--compare", metavar="PERIOD",
                   help="diff against the reading nearest PERIOD ago instead of the "
                        "previous one: 1d, 3d, 1w, 2w, 1m, 6m, 1y (bare numbers are days)")
    s.add_argument("--over", metavar="PERIOD",
                   help="summarise every stored reading in the last PERIOD instead of "
                        "fetching: who held their place, best/average rank, stars gained")
    s.add_argument("--no-track", action="store_true",
                   help="do not add these repos to the tracked universe")
    s.add_argument("--no-fetch", action="store_true",
                   help="compare stored readings only; do not hit the network")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_trending)

    s = sub.add_parser("top", help="the leaderboard")
    s.add_argument("--by", choices=sorted(report.SORT_KEYS), default="heat")
    s.add_argument("--window", type=float, metavar="DAYS", help="window in days (default from config)")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--min-stars", type=int)
    s.add_argument("--max-stars", type=int, help="the interesting band is usually under ~20000")
    s.add_argument("--language")
    s.add_argument("--max-age-days", type=float, help="only repos younger than this")
    s.add_argument("--min-z", type=float)
    s.add_argument("--spark", action="store_true", help="add a 14-day sparkline column")
    s.add_argument("--show-insufficient", action="store_true",
                   help="include repos without enough history")
    s.add_argument("--record", action="store_true", help="persist breakout signals")
    s.add_argument("--json", action="store_true")
    s.add_argument("--csv", action="store_true")
    s.set_defaults(func=cmd_top)

    s = sub.add_parser("show", help="everything known about one repo")
    s.add_argument("repo", help="owner/name")
    s.add_argument("--window", type=float, metavar="DAYS")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("watch", help="manage the always-tracked watchlist")
    s.add_argument("action", choices=["add", "rm", "list"])
    s.add_argument("repos", nargs="*", metavar="owner/name")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser(
        "backfill",
        help="reconstruct star history from per-star timestamps (quota-hungry, opt-in)")
    s.add_argument("repos", nargs="+", metavar="owner/name")
    s.set_defaults(func=cmd_backfill)

    s = sub.add_parser("status", help="database, coverage and run health")
    s.add_argument("--rate-limit", action="store_true", help="also query the API rate limit")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("prune", help="untrack trending-sourced repos no board has "
                                     "listed recently (history kept)")
    s.add_argument("--days", type=float, help="default from config")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_prune)

    s = sub.add_parser("compact", help="downsample old snapshots to one per day")
    s.add_argument("--keep-days", type=float, default=30.0)
    s.set_defaults(func=cmd_compact)

    s = sub.add_parser("serve", help="the local dashboard")
    s.add_argument("--port", type=int)
    s.add_argument("--host")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except KeyboardInterrupt:
        err.print("[dim]interrupted[/dim]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
