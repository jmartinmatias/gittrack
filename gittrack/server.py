"""The local dashboard: a small JSON API plus one static page.

Deliberately stdlib-only - this serves one person on localhost, so a framework
would be dependency weight with nothing to show for it. A fresh SQLite
connection per request keeps the threaded server honest.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db, ingest
from . import digest as dg
from . import metrics as mx
from . import relations as rel
from . import trending as tr
from .board import ALL_BOARDS, combined_board, rank_trend
from .config import Config

_rank_trend = rank_trend

log = logging.getLogger("gittrack.server")
WEB_DIR = Path(__file__).parent / "web"


def _overview(cfg: Config, window_days: float, spark_days: int = 30) -> dict:
    conn = db.connect(cfg.db_path)
    try:
        results = ingest.analyze(conn, cfg, window_days=window_days)
        repos = {r["id"]: r for r in db.tracked_repos(conn)}
        since = int(time.time() - (spark_days + 2) * 86400)
        series = db.load_all_series(conn, since=since)
        hot = {r["repo_id"]: r["ts"] for r in conn.execute(
            "SELECT repo_id, MIN(ts) AS ts FROM signals GROUP BY repo_id").fetchall()}

        rows = []
        for m in results:
            r = repos.get(m.repo_id)
            if r is None:
                continue
            obs = series.get(m.repo_id, [])
            spark: list[float] = []
            if len(obs) >= 2:
                s = mx.Series(obs)
                end = float(s.last_ts)
                start = max(end - spark_days * mx.DAY, float(s.first_ts))
                spark = s.daily_velocities(start, end)
            d = m.to_dict()
            d.update({
                "language": r["language"],
                "description": r["description"],
                "owner_type": r["owner_type"],
                "url": f"https://github.com/{r['full_name']}",
                "created_at": r["created_at"],
                "source": r["source"],
                "spark": [round(v, 2) for v in spark],
                "hot_since": db.hot_since(conn, m.repo_id) if m.repo_id in hot else None,
            })
            rows.append(d)

        return {
            "generated_at": int(time.time()),
            "window_days": window_days,
            "breakout_heat": cfg.scoring.breakout_heat,
            "breakout_min_z": cfg.scoring.breakout_min_z,
            "stats": db.stats(conn),
            "repos": rows,
        }
    finally:
        conn.close()


def _repo_detail(cfg: Config, name: str, window_days: float, days: float = 180) -> dict:
    conn = db.connect(cfg.db_path)
    try:
        row = db.repo_by_name(conn, name)
        if row is None:
            return {"error": f"{name} is not tracked"}
        since = int(time.time() - days * 86400)
        obs = db.load_series(conn, row["id"], since=since)
        m = mx.compute(
            row["id"], obs, window_days=window_days,
            baseline_days=cfg.metrics.baseline_days,
            min_observations=cfg.metrics.min_observations,
            min_baseline_days=cfg.metrics.min_baseline_days,
            created_at=row["created_at"], full_name=row["full_name"],
        )
        cohort = ingest.analyze(conn, cfg, window_days=window_days)
        for c in cohort:
            if c.repo_id == m.repo_id:
                m.rel_pct, m.heat, m.heat_parts = c.rel_pct, c.heat, c.heat_parts
                break

        daily_ts, daily_gain = [], []
        if len(obs) >= 2:
            s = mx.Series(obs)
            end = float(s.last_ts)
            t = max(end - days * mx.DAY, float(s.first_ts))
            while t + mx.DAY <= end + 1:
                a, b = s.stars_at(t), s.stars_at(t + mx.DAY)
                if a is not None and b is not None:
                    daily_ts.append(int(t + mx.DAY))
                    daily_gain.append(round(b - a, 2))
                t += mx.DAY

        return {
            "repo": {
                "full_name": row["full_name"], "description": row["description"],
                "language": row["language"], "license": row["license"],
                "homepage": row["homepage"], "created_at": row["created_at"],
                "pushed_at": row["pushed_at"], "first_seen": row["first_seen"],
                "owner_type": row["owner_type"], "source": row["source"],
                "url": f"https://github.com/{row['full_name']}",
            },
            "metrics": m.to_dict(),
            "hot_since": db.hot_since(conn, row["id"]),
            "series": {
                "ts": [o.ts for o in obs],
                "stars": [o.stars for o in obs],
                "forks": [o.forks for o in obs],
            },
            "daily": {"ts": daily_ts, "gain": daily_gain},
        }
    finally:
        conn.close()


def _trending(cfg: Config, since: str, language: str, over: float,
              compare: float | None) -> dict:
    """Latest stored board, its movement, and the durability summary.

    Read-only: the dashboard never triggers a fetch. Scraping on page load would
    hammer GitHub on every refresh and make the view depend on network luck.
    """
    conn = db.connect(cfg.db_path)
    try:
        boards = [dict(b) for b in db.trending_boards(conn)]
        times = db.trending_times(conn, since, language, limit=1)
        if not times:
            return {"board": [], "boards": boards, "readings": 0,
                    "since": since, "language": language, "over": over,
                    "compare": compare, "ts": None, "base_ts": None, "gone": [],
                    "over_label": tr.humanise_period(over),
                    "compare_label": tr.humanise_period(compare) if compare else None,
                    "empty": f"No readings stored for the {since} board yet. "
                             f"Add it to [trending] since in gittrack.toml, or run: "
                             f"gittrack trending --since {since}"}
        now_ts = times[0]

        current = db.trending_at(conn, now_ts, since, language)
        if compare:
            base_ts = db.nearest_reading(conn, since, language, now_ts - compare,
                                         tolerance=compare / 2)
        else:
            earlier = db.trending_times(conn, since, language, limit=2)
            base_ts = earlier[1] if len(earlier) > 1 else None
        base = {r["full_name"]: r["rank"]
                for r in (db.trending_at(conn, base_ts, since, language)
                          if base_ts else [])}

        # Cross-board presence. A repo on daily AND weekly AND monthly is
        # sustained; one on daily alone is a single day's spike. Both look
        # identical if you only ever look at one board.
        ALL_BOARDS = ("daily", "weekly", "monthly")

        board_ranks = {b: db.latest_board_ranks(conn, b, language)
                       for b in ALL_BOARDS}

        summary = ingest.trending_summary(conn, since=since, language=language,
                                          period=over, now=now_ts)
        agg = {r["full_name"]: r for r in summary["repos"]}
        hist = db.trending_rank_series(conn, since, language, int(now_ts - over))
        accel = db.latest_accel(conn, language)
        # The per-board view now uses the same layout as the combined one, so it
        # needs the same inputs: per-board period counts (which is what makes the
        # acceleration glyph work here too) and each board's size for scoring.
        period_by_board, board_sizes = {}, {}
        for b in ALL_BOARDS:
            bt = db.trending_times(conn, b, language, limit=1)
            if not bt:
                board_sizes[b] = 0
                continue
            brows = db.trending_at(conn, bt[0], b, language)
            board_sizes[b] = len(brows)
            for br in brows:
                period_by_board.setdefault(br["full_name"], {})[b] = br["period_stars"]

        rows = []
        for r in current:
            name = r["full_name"]
            a = agg.get(name, {})
            prev_rank = base.get(name)
            streak, streak_from = db.trending_streak(conn, name, since, language)
            also = {b: board_ranks[b].get(name) for b in ALL_BOARDS}
            n_board = len(current)
            rows.append({
                "accel": accel.get(name),
                "trend": _rank_trend(hist.get(name, []), n_board),
                "period_by_board": period_by_board.get(name, {}),
                "fork_ratio": (r["forks"] / r["stars"])
                              if r["forks"] is not None and r["stars"] else None,
                "fork_rel": None,      # needs snapshot history; see _fork_signals
                # Normalised position on this board, so the score column means the
                # same thing here as it does merged: 1.0 is the top of the board.
                "score": round((n_board - r["rank"] + 1) / n_board, 4) if n_board else 0,
                "also_on": also,
                "board_count": sum(1 for v in also.values() if v is not None),
                "rank": r["rank"], "full_name": name, "repo_id": r["repo_id"],
                "language": r["repo_language"], "stars": r["stars"],
                "forks": r["forks"], "period_stars": r["period_stars"],
                "share": min(1.0, r["period_stars"] / r["stars"])
                         if r["period_stars"] and r["stars"] else None,
                "prev_rank": prev_rank,
                # Rank 1 is best, so climbing is a decrease. Report it positive.
                "move": (prev_rank - r["rank"]) if prev_rank is not None else None,
                "is_new": base_ts is not None and prev_rank is None,
                "streak": streak, "streak_from": streak_from,
                "appearances": a.get("appearances"), "hold": a.get("hold"),
                "best_rank": a.get("best_rank"), "avg_rank": a.get("avg_rank"),
                "stars_gained": a.get("stars_gained"),
                "url": f"https://github.com/{name}",
            })

        gone = [n for n in base if n not in {r["full_name"] for r in current}]
        return {
            "since": since, "language": language, "over": over, "compare": compare,
            "ts": now_ts, "base_ts": base_ts, "readings": summary["readings"],
            "window_readings": summary["readings"], "boards": boards,
            "board": rows, "gone": gone,
            "over_label": tr.humanise_period(over),
            "compare_label": tr.humanise_period(compare) if compare else None,
            "all_boards": list(ALL_BOARDS),
            "board_sizes": board_sizes,
            "rank_by": "rank",
            "board_readings": {b: len(board_ranks[b]) for b in ALL_BOARDS},
        }
    finally:
        conn.close()




def _digest(cfg: Config, language: str, include_seen: bool) -> dict:
    conn = db.connect(cfg.db_path)
    try:
        d = dg.build(conn, language=language, over=cfg.digest.over_days * 86400,
                     limit=cfg.digest.limit, max_stars=cfg.digest.max_stars,
                     include_seen=include_seen)
        d["coverage"] = db.run_coverage(conn, "trending", 3600)
        d["seen"] = {k: {"ts": v["ts"], "note": v["note"]}
                     for k, v in db.seen_map(conn).items()}
        return d
    finally:
        conn.close()


def _trending_combined(cfg: Config, language: str, over: float,
                       rank_by: str = "stars") -> dict:
    conn = db.connect(cfg.db_path)
    try:
        return combined_board(conn, language, over, rank_by)
    finally:
        conn.close()


def _relations(cfg: Config, since: str, language: str) -> dict:
    """Themes, shared owners and co-movement for the repos currently on a board."""
    conn = db.connect(cfg.db_path)
    try:
        boards = ALL_BOARDS if since in ("combined", "all") else (since,)
        names: set[str] = set()
        for b in boards:
            t = db.trending_times(conn, b, language, limit=1)
            if t:
                names |= {r["full_name"] for r in db.trending_at(conn, t[0], b, language)}
        if not names:
            return {"since": since, "language": language, "repos": 0,
                    "themes": [], "owners": [],
                    "comovement": {"readings": 0, "enough": False, "cohorts": [],
                                   "note": "no readings stored"}}

        # Descriptions live on the repo record, not on the trending row.
        repos = []
        for n in sorted(names):
            row = db.repo_by_name(conn, n)
            repos.append({"full_name": n,
                          "description": row["description"] if row else None})

        # Co-movement is measured on the daily board: it is the one that actually
        # churns, so it is the only one where "together" can mean anything.
        series = db.trending_rank_series(conn, "daily", language, 0)
        stamps = sorted({t for v in series.values() for t, _ in v})
        index = {t: i for i, t in enumerate(stamps)}
        presence = {n: {index[t] for t, _ in v} for n, v in series.items()}

        return {
            "since": since, "language": language, "repos": len(repos),
            "themes": rel.themes(repos),
            "owners": rel.owners(repos),
            "comovement": rel.comovement(presence, len(stamps)),
        }
    finally:
        conn.close()


def _trending_repo(cfg: Config, name: str, since: str, language: str,
                   days: float) -> dict:
    conn = db.connect(cfg.db_path)
    try:
        start = int(time.time() - days * 86400)
        # "combined" is our own merged view, not a board GitHub publishes, so there
        # is no history filed under that name. Resolve it to the real board the
        # repo currently stands highest on.
        if since in ("combined", "all"):
            best, since = None, "daily"
            for b in ALL_BOARDS:
                rk = db.latest_board_ranks(conn, b, language).get(name)
                if rk is not None and (best is None or rk < best):
                    best, since = rk, b
        hist = db.trending_rank_history(conn, name, since, language, start)
        if not hist:
            return {"error": f"{name} has no trending history on the {since} board"}
        row = db.repo_by_name(conn, name)
        acc = db.accel_history(conn, name, language, start)
        return {
            "full_name": name,
            "board": since,
            "accel_history": {
                "ts": [a["ts"] for a in acc],
                "ratio": [a["ratio"] for a in acc],
                "now_rate": [a["now_rate"] for a in acc],
                "long_rate": [a["long_rate"] for a in acc],
                "horizon": acc[-1]["horizon"] if acc else None,
            },
            "url": f"https://github.com/{name}",
            "description": row["description"] if row else None,
            "language": row["language"] if row else None,
            "ts": [h["ts"] for h in hist],
            "rank": [h["rank"] for h in hist],
            "period_stars": [h["period_stars"] for h in hist],
            "stars": [h["stars"] for h in hist],
        }
    finally:
        conn.close()


# ------------------------------------------------------------------ fetch jobs
#
# A fetch is a write that talks to the network, so it must not run inside a
# request handler: a trending sweep across three boards takes several seconds and
# a snapshot pass can sleep on a rate limit for minutes. The button starts a
# worker and returns immediately; the page polls for the outcome.

_JOB_LOCK = threading.Lock()
_JOB: dict = {"running": False, "kind": None, "started_at": None,
              "finished_at": None, "result": None, "error": None, "steps": []}


def _job_snapshot() -> dict:
    with _JOB_LOCK:
        return dict(_JOB, steps=list(_JOB["steps"]))


def _run_fetch(cfg: Config, kind: str, since: str, language: str) -> None:
    """Worker body. Never raises — failures are reported through the job record."""
    conn = db.connect(cfg.db_path)
    steps, added_total, rows_total = [], 0, 0
    try:
        if kind == "trending":
            # A combined view needs all three boards; a single board needs one.
            boards = list(ALL_BOARDS) if since in ("combined", "all") else [since]
            for i, b in enumerate(boards):
                try:
                    r = ingest.refresh_trending(conn, cfg, since=b, language=language)
                    rows_total += r["rows"]
                    added_total += r["repos_added"]
                    steps.append(f"{b}/{language or 'all'}: {r['rows']} rows, "
                                 f"+{r['repos_added']} new repos")
                except Exception as exc:
                    steps.append(f"{b}/{language or 'all'}: FAILED — {exc}")
                with _JOB_LOCK:
                    _JOB["steps"] = list(steps)
                # Be a polite scraper; skip the wait after the last board.
                if i < len(boards) - 1:
                    time.sleep(cfg.trending.request_delay)
            # The baseline needs every board read first, so this runs once at
            # the end rather than per board.
            try:
                n_acc = ingest.record_acceleration(conn, cfg, language=language)
                if n_acc:
                    steps.append(f"acceleration recorded for {n_acc} repos")
            except Exception as exc:
                steps.append(f"acceleration skipped: {exc}")
            result = {"rows": rows_total, "repos_added": added_total,
                      "boards": len(boards)}
        else:
            res = ingest.refresh(conn, gh_client(cfg), cfg)
            steps.append(f"snapshot: {res['repos_seen']} repos, "
                         f"{res['snapshots_written']} snapshots, "
                         f"{res['api_requests']} API calls"
                         + (f", {res['deferred']} deferred" if res.get("deferred") else ""))
            results = ingest.analyze(conn, cfg)
            n = ingest.record_signals(conn, results, cfg, ts=res["ts"])
            if n:
                steps.append(f"{n} breakout signal(s) recorded")
            result = res
        with _JOB_LOCK:
            _JOB.update(result=result, error=None, steps=list(steps))
    except Exception as exc:
        with _JOB_LOCK:
            _JOB.update(result=None, error=f"{type(exc).__name__}: {exc}",
                        steps=list(steps))
    finally:
        conn.close()
        with _JOB_LOCK:
            _JOB.update(running=False, finished_at=int(time.time()))


def gh_client(cfg: Config):
    from .github import GitHubClient, resolve_token
    return GitHubClient(token=resolve_token(cfg.github.token_env, cfg.github.use_gh_cli),
                        api_base=cfg.github.api_base)


def _start_fetch(cfg: Config, kind: str, since: str, language: str) -> tuple[dict, int]:
    with _JOB_LOCK:
        if _JOB["running"]:
            return {"error": "a fetch is already running", **dict(_JOB)}, 409
        _JOB.update(running=True, kind=kind, started_at=int(time.time()),
                    finished_at=None, result=None, error=None, steps=[])
    threading.Thread(target=_run_fetch, args=(cfg, kind, since, language),
                     daemon=True).start()
    return _job_snapshot(), 202


class Handler(BaseHTTPRequestHandler):
    cfg: Config = None  # type: ignore[assignment]
    server_version = "gittrack"

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # This page renders untrusted repo names/descriptions; the CSP is a second
        # line of defence behind using textContent everywhere in the client.
        # No inline script remains, so script-src can be 'self'. Styles keep
        # 'unsafe-inline' because charts set style attributes on generated SVG.
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self'; connect-src 'self'; img-src data:")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(json.dumps(payload, default=float).encode(), "application/json", status)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)

        def num(key, default):
            try:
                return float(q.get(key, [default])[0])
            except (TypeError, ValueError):
                return default

        try:
            if u.path in ("/", "/index.html"):
                self._send((WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/app.css":
                self._send((WEB_DIR / "app.css").read_bytes(), "text/css; charset=utf-8")
            elif u.path == "/app.js":
                self._send((WEB_DIR / "app.js").read_bytes(),
                           "application/javascript; charset=utf-8")
            elif u.path == "/api/overview":
                self._json(_overview(self.cfg, num("window", self.cfg.metrics.default_window_days)))
            elif u.path == "/api/fetch/status":
                self._json(_job_snapshot())
            elif u.path == "/api/digest":
                self._json(_digest(self.cfg,
                                   language=(q.get("language") or [""])[0],
                                   include_seen=(q.get("include_seen") or ["0"])[0] == "1"))
            elif u.path == "/api/trending":
                def period(key, default):
                    raw = (q.get(key) or [None])[0]
                    if not raw:
                        return default
                    try:
                        return tr.parse_period(raw)
                    except ValueError:
                        return default
                since = (q.get("since") or ["daily"])[0]
                if since in ("combined", "all"):
                    rb = (q.get("rank_by") or ["stars"])[0]
                    if rb not in ("stars", "forks", "both", "surge"):
                        rb = "stars"
                    self._json(_trending_combined(
                        self.cfg, language=(q.get("language") or [""])[0],
                        over=period("over", 7 * 86400), rank_by=rb))
                    return
                self._json(_trending(
                    self.cfg,
                    since=since,
                    language=(q.get("language") or [""])[0],
                    over=period("over", 7 * 86400),
                    compare=period("compare", None)))
            elif u.path == "/api/relations":
                self._json(_relations(
                    self.cfg,
                    since=(q.get("since") or ["combined"])[0],
                    language=(q.get("language") or [""])[0]))
            elif u.path == "/api/trending/repo":
                name = (q.get("name") or [""])[0]
                if not name:
                    self._json({"error": "name is required"}, 400)
                    return
                data = _trending_repo(
                    self.cfg, name,
                    since=(q.get("since") or ["daily"])[0],
                    language=(q.get("language") or [""])[0],
                    days=num("days", 90))
                self._json(data, 404 if "error" in data else 200)
            elif u.path == "/api/repo":
                name = (q.get("name") or [""])[0]
                if not name:
                    self._json({"error": "name is required"}, 400)
                    return
                data = _repo_detail(self.cfg, name,
                                    num("window", self.cfg.metrics.default_window_days),
                                    num("days", 180))
                self._json(data, 404 if "error" in data else 200)
            else:
                self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as exc:  # a failed request must not take the server down
            log.exception("request failed: %s", self.path)
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/seen":
            # Marking a repo reviewed is a write, hence POST. `undo=1` clears it.
            name = (q.get("name") or [""])[0]
            if not name:
                self._json({"error": "name is required"}, 400)
                return
            conn = db.connect(self.cfg.db_path)
            try:
                if (q.get("undo") or ["0"])[0] == "1":
                    db.unmark_seen(conn, name)
                else:
                    db.mark_seen(conn, name, note=(q.get("note") or [None])[0])
                conn.commit()
            finally:
                conn.close()
            self._json({"ok": True, "name": name})
            return
        if u.path != "/api/fetch":
            self._json({"error": "not found"}, 404)
            return
        # Fetching writes to the database and reaches out to the network, so it is
        # a POST: never something a prefetch or a stray GET can trigger.
        kind = (q.get("kind") or ["trending"])[0]
        if kind not in ("trending", "snapshot"):
            self._json({"error": "kind must be trending or snapshot"}, 400)
            return
        try:
            payload, status = _start_fetch(
                self.cfg, kind,
                since=(q.get("since") or ["combined"])[0],
                language=(q.get("language") or [""])[0])
            self._json(payload, status)
        except Exception as exc:
            log.exception("fetch failed to start")
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8787,
          open_browser: bool = True) -> None:
    Handler.cfg = cfg
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"gittrack dashboard on {url}   (ctrl-c to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
