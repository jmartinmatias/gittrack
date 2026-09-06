"""SQLite storage: repo metadata, the snapshot time series, run log, signals.

Timestamps are stored as INTEGER epoch seconds (UTC) throughout - cheap to store
and to do arithmetic on. Format only at the display boundary.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Slowly-changing repo identity/metadata. Keyed by GitHub's numeric repo id,
-- which survives renames and transfers; full_name is just the current label.
CREATE TABLE IF NOT EXISTS repos (
    id               INTEGER PRIMARY KEY,
    full_name        TEXT    NOT NULL,
    owner            TEXT    NOT NULL,
    name             TEXT    NOT NULL,
    description      TEXT,
    language         TEXT,
    topics           TEXT,          -- JSON array
    license          TEXT,
    homepage         TEXT,
    created_at       INTEGER,       -- repo creation, epoch s (age is a strong prior)
    pushed_at        INTEGER,
    is_fork          INTEGER NOT NULL DEFAULT 0,
    is_archived      INTEGER NOT NULL DEFAULT 0,
    owner_type       TEXT,
    first_seen       INTEGER NOT NULL,
    last_seen        INTEGER NOT NULL,
    source           TEXT    NOT NULL DEFAULT 'top',   -- top | watchlist
    tracked          INTEGER NOT NULL DEFAULT 1,
    etag             TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_repos_full_name ON repos(full_name);
CREATE INDEX IF NOT EXISTS idx_repos_tracked ON repos(tracked);

-- The time series. One row per repo per observation.
CREATE TABLE IF NOT EXISTS snapshots (
    repo_id     INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    stars       INTEGER NOT NULL,
    forks       INTEGER,          -- NULL when the source had no fork data (backfill)
    watchers    INTEGER,
    open_issues INTEGER,
    size_kb     INTEGER,
    PRIMARY KEY (repo_id, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts);

CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT    NOT NULL,
    started_at        INTEGER NOT NULL,
    finished_at       INTEGER,
    repos_seen        INTEGER NOT NULL DEFAULT 0,
    snapshots_written INTEGER NOT NULL DEFAULT 0,
    api_requests      INTEGER NOT NULL DEFAULT 0,
    error             TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_kind_started ON runs(kind, started_at);

-- A breakout signal, recorded whenever a repo's heat clears the configured bar.
CREATE TABLE IF NOT EXISTS signals (
    repo_id     INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    window_days REAL    NOT NULL,
    heat        REAL    NOT NULL,
    z           REAL,
    rel         REAL,
    accel       REAL,
    stars       INTEGER NOT NULL,
    PRIMARY KEY (repo_id, ts, window_days)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);

-- One row per repo per reading of a GitHub Trending board. A single reading is
-- a leaderboard; the series is a derivative (who entered, who is climbing, who
-- has held on).
CREATE TABLE IF NOT EXISTS trending (
    ts           INTEGER NOT NULL,
    since        TEXT    NOT NULL,          -- daily | weekly | monthly
    language     TEXT    NOT NULL DEFAULT '',   -- which board, not the repo's language
    rank         INTEGER NOT NULL,
    repo_id      INTEGER,
    full_name    TEXT    NOT NULL,
    stars        INTEGER,
    forks        INTEGER,
    period_stars INTEGER,                   -- GitHub's own "N stars today"
    repo_language TEXT,                     -- the repo's own language
    PRIMARY KEY (ts, since, language, full_name)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_trending_board ON trending(since, language, ts);
CREATE INDEX IF NOT EXISTS idx_trending_repo ON trending(full_name, since, language, ts);

-- Acceleration is derived, but derived from two things that both move: our own
-- snapshot rate and GitHub's period counts. Recomputing it later from history
-- would not reproduce today's answer, so it is stored when it is measured.
CREATE TABLE IF NOT EXISTS accel (
    repo_id   INTEGER NOT NULL,
    ts        INTEGER NOT NULL,
    language  TEXT    NOT NULL DEFAULT '',
    now_rate  REAL,
    long_rate REAL,
    ratio     REAL,
    horizon   TEXT,
    PRIMARY KEY (repo_id, ts, language)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_accel_ts ON accel(ts);

-- Repos the user has looked at. The digest skips these so it keeps surfacing
-- what is new rather than the same names every morning.
CREATE TABLE IF NOT EXISTS seen (
    full_name TEXT PRIMARY KEY COLLATE NOCASE,
    ts        INTEGER NOT NULL,
    note      TEXT
);
"""


@dataclass(frozen=True, slots=True)
class Obs:
    """One observation of one repo. `forks` is None when the source lacked it."""

    ts: int
    stars: int
    forks: int | None


def connect(path: str | Path) -> sqlite3.Connection:
    path = str(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # CREATE TABLE IF NOT EXISTS will not add a column to a table that already
    # exists, so columns introduced after a table shipped need an explicit step.
    _add_column_if_missing(conn, "trending", "repo_language", "TEXT")
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str,
                           decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# --------------------------------------------------------------------------- repos


def upsert_repo(conn: sqlite3.Connection, r: dict, source: str = "top", now: int | None = None) -> None:
    """Insert or refresh repo metadata from a GitHub API repo object."""
    now = now or int(time.time())
    owner = (r.get("owner") or {})
    conn.execute(
        """
        INSERT INTO repos (id, full_name, owner, name, description, language, topics,
                           license, homepage, created_at, pushed_at, is_fork, is_archived,
                           owner_type, first_seen, last_seen, source, tracked)
        VALUES (:id, :full_name, :owner, :name, :description, :language, :topics,
                :license, :homepage, :created_at, :pushed_at, :is_fork, :is_archived,
                :owner_type, :now, :now, :source, 1)
        ON CONFLICT(id) DO UPDATE SET
            full_name   = excluded.full_name,
            owner       = excluded.owner,
            name        = excluded.name,
            -- Repos arrive from two sources with different completeness: the API
            -- carries everything, a trending row carries only what the board
            -- shows. Writing excluded.* unconditionally let the poorer source
            -- erase the richer one on every sweep, and created_at was not
            -- updated at all, so a repo first seen on a board could never
            -- acquire an age. COALESCE keeps whichever source actually knows.
            description = COALESCE(excluded.description, repos.description),
            language    = COALESCE(excluded.language, repos.language),
            topics      = CASE WHEN excluded.topics IN ('[]', '', 'null')
                               THEN repos.topics ELSE excluded.topics END,
            license     = COALESCE(excluded.license, repos.license),
            homepage    = COALESCE(excluded.homepage, repos.homepage),
            created_at  = COALESCE(excluded.created_at, repos.created_at),
            pushed_at   = COALESCE(excluded.pushed_at, repos.pushed_at),
            owner_type  = COALESCE(excluded.owner_type, repos.owner_type),
            is_archived = excluded.is_archived,
            last_seen   = excluded.last_seen,
            tracked     = 1,
            -- Provenance is sticky: how a repo first reached us is worth keeping.
            -- An explicit watchlist entry always wins; otherwise an earlier
            -- 'watchlist' or 'trending' origin survives later 'top' refreshes, so
            -- a repo that falls out of the top N neither loses its origin nor
            -- silently stops being tracked.
            source      = CASE
                              WHEN excluded.source = 'watchlist' THEN 'watchlist'
                              WHEN repos.source IN ('watchlist', 'trending')
                                  THEN repos.source
                              ELSE excluded.source
                          END
        """,
        {
            "id": r["id"],
            "full_name": r["full_name"],
            "owner": owner.get("login", r["full_name"].split("/")[0]),
            "name": r.get("name") or r["full_name"].split("/")[-1],
            "description": r.get("description"),
            "language": r.get("language"),
            "topics": json.dumps(r.get("topics") or []),
            "license": ((r.get("license") or {}) or {}).get("spdx_id"),
            "homepage": r.get("homepage"),
            "created_at": _iso_to_epoch(r.get("created_at")),
            "pushed_at": _iso_to_epoch(r.get("pushed_at")),
            "is_fork": 1 if r.get("fork") else 0,
            "is_archived": 1 if r.get("archived") else 0,
            "owner_type": owner.get("type"),
            "now": now,
            "source": source,
        },
    )


def set_tracked(conn: sqlite3.Connection, repo_ids: list[int], tracked: bool) -> int:
    if not repo_ids:
        return 0
    qs = ",".join("?" * len(repo_ids))
    cur = conn.execute(
        f"UPDATE repos SET tracked = ? WHERE id IN ({qs})", [1 if tracked else 0, *repo_ids]
    )
    return cur.rowcount


def tracked_repos(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM repos WHERE tracked = 1 ORDER BY full_name"
    ).fetchall()


def tracked_repos_by_staleness(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Tracked repos, least-recently-snapshotted first.

    Used to rotate the per-run budget for repos the bulk search no longer returns,
    so a universe that only grows can never outrun the core rate limit.
    """
    return conn.execute(
        "SELECT r.*, COALESCE(MAX(s.ts), 0) AS last_snap FROM repos r"
        " LEFT JOIN snapshots s ON s.repo_id = r.id"
        " WHERE r.tracked = 1 GROUP BY r.id ORDER BY last_snap ASC"
    ).fetchall()


def repo_by_name(conn: sqlite3.Connection, full_name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM repos WHERE full_name = ? COLLATE NOCASE", (full_name,)
    ).fetchone()


def set_etag(conn: sqlite3.Connection, repo_id: int, etag: str | None) -> None:
    conn.execute("UPDATE repos SET etag = ? WHERE id = ?", (etag, repo_id))


# ----------------------------------------------------------------------- snapshots


def insert_snapshot(
    conn: sqlite3.Connection,
    repo_id: int,
    ts: int,
    stars: int,
    forks: int | None,
    watchers: int | None = None,
    open_issues: int | None = None,
    size_kb: int | None = None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO snapshots (repo_id, ts, stars, forks, watchers, open_issues, size_kb)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (repo_id, ts, stars, forks, watchers, open_issues, size_kb),
    )


def last_snapshot(conn: sqlite3.Connection, repo_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM snapshots WHERE repo_id = ? ORDER BY ts DESC LIMIT 1", (repo_id,)
    ).fetchone()


def load_series(conn: sqlite3.Connection, repo_id: int, since: int = 0) -> list[Obs]:
    rows = conn.execute(
        "SELECT ts, stars, forks FROM snapshots WHERE repo_id = ? AND ts >= ? ORDER BY ts",
        (repo_id, since),
    ).fetchall()
    return [Obs(r["ts"], r["stars"], r["forks"]) for r in rows]


def load_all_series(conn: sqlite3.Connection, since: int = 0) -> dict[int, list[Obs]]:
    """Every tracked repo's series since `since`, grouped by repo id (ascending ts)."""
    rows = conn.execute(
        "SELECT s.repo_id, s.ts, s.stars, s.forks FROM snapshots s"
        " JOIN repos r ON r.id = s.repo_id"
        " WHERE r.tracked = 1 AND s.ts >= ?"
        " ORDER BY s.repo_id, s.ts",
        (since,),
    ).fetchall()
    out: dict[int, list[Obs]] = defaultdict(list)
    for r in rows:
        out[r["repo_id"]].append(Obs(r["ts"], r["stars"], r["forks"]))
    return dict(out)


def compact(conn: sqlite3.Connection, keep_full_days: float = 30.0, now: int | None = None) -> int:
    """Downsample snapshots older than `keep_full_days` to one row per UTC day.

    Keeps the earliest snapshot in each day bucket. Returns rows deleted.
    """
    now = now or int(time.time())
    cutoff = int(now - keep_full_days * 86400)
    cur = conn.execute(
        """
        DELETE FROM snapshots
        WHERE ts < ?
          AND (repo_id, ts) NOT IN (
              SELECT repo_id, MIN(ts) FROM snapshots WHERE ts < ?
              GROUP BY repo_id, ts / 86400
          )
        """,
        (cutoff, cutoff),
    )
    return cur.rowcount


# ---------------------------------------------------------------------- runs/signals


def start_run(conn: sqlite3.Connection, kind: str, now: int | None = None) -> int:
    now = now or int(time.time())
    cur = conn.execute("INSERT INTO runs (kind, started_at) VALUES (?, ?)", (kind, now))
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    repos_seen: int = 0,
    snapshots_written: int = 0,
    api_requests: int = 0,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, repos_seen = ?, snapshots_written = ?,"
        " api_requests = ?, error = ? WHERE id = ?",
        (int(time.time()), repos_seen, snapshots_written, api_requests, error, run_id),
    )
    conn.commit()


def last_successful_run(conn: sqlite3.Connection, kind: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs WHERE kind = ? AND finished_at IS NOT NULL AND error IS NULL"
        " ORDER BY started_at DESC LIMIT 1",
        (kind,),
    ).fetchone()


def record_signal(conn: sqlite3.Connection, repo_id: int, ts: int, window_days: float,
                  heat: float, z: float | None, rel: float | None, accel: float | None,
                  stars: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO signals (repo_id, ts, window_days, heat, z, rel, accel, stars)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (repo_id, ts, window_days, heat, z, rel, accel, stars),
    )


def hot_since(conn: sqlite3.Connection, repo_id: int, max_gap_days: float = 2.0) -> int | None:
    """Start of the repo's current unbroken streak of breakout signals."""
    rows = conn.execute(
        "SELECT ts FROM signals WHERE repo_id = ? ORDER BY ts DESC", (repo_id,)
    ).fetchall()
    if not rows:
        return None
    gap = max_gap_days * 86400
    streak_start = rows[0]["ts"]
    for prev, cur in itertools.pairwise(rows):
        if prev["ts"] - cur["ts"] > gap:
            break
        streak_start = cur["ts"]
    return streak_start


# --------------------------------------------------------------------- trending


def insert_trending(conn: sqlite3.Connection, ts: int, since: str, language: str,
                    rows: list) -> int:
    conn.executemany(
        "INSERT OR REPLACE INTO trending"
        " (ts, since, language, rank, repo_id, full_name, stars, forks, period_stars,"
        "  repo_language)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(ts, since, language, r.rank, r.repo_id, r.full_name, r.stars, r.forks,
          r.period_stars, r.language) for r in rows],
    )
    return len(rows)


def trending_times(conn: sqlite3.Connection, since: str, language: str = "",
                   limit: int = 2) -> list[int]:
    """Timestamps of the most recent readings of one board, newest first."""
    return [r["ts"] for r in conn.execute(
        "SELECT DISTINCT ts FROM trending WHERE since = ? AND language = ?"
        " ORDER BY ts DESC LIMIT ?", (since, language, limit)).fetchall()]


def trending_at(conn: sqlite3.Connection, ts: int, since: str,
                language: str = "") -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM trending WHERE ts = ? AND since = ? AND language = ?"
        " ORDER BY rank", (ts, since, language)).fetchall()


def trending_streak(conn: sqlite3.Connection, full_name: str, since: str,
                    language: str = "") -> tuple[int, int | None]:
    """(consecutive readings on the board, first ts of that streak).

    Walks the board's own reading times backwards, so a repo that dropped off and
    returned reports only its current run — not its lifetime total.
    """
    times = [r["ts"] for r in conn.execute(
        "SELECT DISTINCT ts FROM trending WHERE since = ? AND language = ?"
        " ORDER BY ts DESC", (since, language)).fetchall()]
    if not times:
        return 0, None
    present = {r["ts"] for r in conn.execute(
        "SELECT ts FROM trending WHERE full_name = ? AND since = ? AND language = ?",
        (full_name, since, language)).fetchall()}
    n, first = 0, None
    for t in times:
        if t not in present:
            break
        n += 1
        first = t
    return n, first


def nearest_reading(conn: sqlite3.Connection, since: str, language: str,
                    target_ts: float, tolerance: float | None = None) -> int | None:
    """The reading of this board closest in time to `target_ts`.

    Readings are irregular (the scheduler drifts, runs fail), so "one week ago"
    has to mean "the reading nearest one week ago", not an exact timestamp.
    `tolerance` bounds how far off that is allowed to be before we report that
    there is no comparable reading.
    """
    row = conn.execute(
        "SELECT ts, ABS(ts - ?) AS d FROM (SELECT DISTINCT ts FROM trending"
        " WHERE since = ? AND language = ?) ORDER BY d ASC LIMIT 1",
        (int(target_ts), since, language)).fetchone()
    if row is None:
        return None
    if tolerance is not None and row["d"] > tolerance:
        return None
    return row["ts"]


def trending_aggregate(conn: sqlite3.Connection, since: str, language: str,
                       start_ts: int, end_ts: int) -> list[sqlite3.Row]:
    """Per-repo summary across every reading of a board in [start_ts, end_ts].

    `appearances` over `readings` is the durability signal: a repo on 14 of the
    last 14 daily boards is compounding, one on 1 of 14 had an afternoon.
    """
    return conn.execute(
        """
        SELECT full_name,
               MAX(repo_id)                AS repo_id,
               COUNT(*)                    AS appearances,
               MIN(rank)                   AS best_rank,
               MAX(rank)                   AS worst_rank,
               AVG(rank)                   AS avg_rank,
               MIN(ts)                     AS first_ts,
               MAX(ts)                     AS last_ts,
               MAX(period_stars)           AS peak_period_stars,
               MAX(stars) - MIN(stars)     AS stars_gained,
               MAX(stars)                  AS stars,
               MAX(repo_language)          AS repo_language
        FROM trending
        WHERE since = ? AND language = ? AND ts BETWEEN ? AND ?
        GROUP BY full_name
        """, (since, language, start_ts, end_ts)).fetchall()


def trending_reading_count(conn: sqlite3.Connection, since: str, language: str,
                           start_ts: int, end_ts: int) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT ts) AS n FROM trending"
        " WHERE since = ? AND language = ? AND ts BETWEEN ? AND ?",
        (since, language, start_ts, end_ts)).fetchone()["n"]


def latest_board_ranks(conn: sqlite3.Connection, since: str,
                       language: str = "") -> dict[str, int]:
    """full_name -> rank on the most recent reading of one board."""
    times = trending_times(conn, since, language, limit=1)
    if not times:
        return {}
    return {r["full_name"]: r["rank"]
            for r in trending_at(conn, times[0], since, language)}


def trending_rank_history(conn: sqlite3.Connection, full_name: str, since: str,
                          language: str = "", start_ts: int = 0) -> list[sqlite3.Row]:
    """One repo's rank across every reading of a board, oldest first."""
    return conn.execute(
        "SELECT ts, rank, stars, forks, period_stars FROM trending"
        " WHERE full_name = ? AND since = ? AND language = ? AND ts >= ?"
        " ORDER BY ts", (full_name, since, language, start_ts)).fetchall()


def trending_rank_series(conn: sqlite3.Connection, since: str, language: str,
                         start_ts: int) -> dict[str, list[tuple[int, int]]]:
    """Every repo's rank across a board's readings since `start_ts`.

    One query for the whole board rather than one per repo: a 50-row board would
    otherwise be 50 round trips just to draw the sparklines.
    """
    rows = conn.execute(
        "SELECT full_name, ts, rank FROM trending"
        " WHERE since = ? AND language = ? AND ts >= ?"
        " ORDER BY full_name, ts", (since, language, start_ts)).fetchall()
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for r in rows:
        out[r["full_name"]].append((r["ts"], r["rank"]))
    return dict(out)


def trending_boards(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Which (since, language) boards have stored readings, and how many."""
    return conn.execute(
        "SELECT since, language, COUNT(DISTINCT ts) AS readings, MAX(ts) AS last_ts"
        " FROM trending GROUP BY since, language ORDER BY since, language").fetchall()


def trending_leaderboard(conn: sqlite3.Connection, since: str, language: str = "",
                         limit: int = 50) -> list[sqlite3.Row]:
    """Latest reading joined with each repo's streak length, longest streak first."""
    times = trending_times(conn, since, language, limit=1)
    if not times:
        return []
    return conn.execute(
        "SELECT * FROM trending WHERE ts = ? AND since = ? AND language = ?"
        " ORDER BY rank LIMIT ?", (times[0], since, language, limit)).fetchall()


def insert_accel(conn: sqlite3.Connection, repo_id: int, ts: int, language: str,
                 a: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO accel (repo_id, ts, language, now_rate, long_rate,"
        " ratio, horizon) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (repo_id, ts, language, a["now_rate"], a["long_rate"], a["ratio"],
         a["horizon"]))


def latest_accel(conn: sqlite3.Connection, language: str = "") -> dict[str, dict]:
    """Most recent acceleration per repo, keyed by full_name."""
    rows = conn.execute(
        "SELECT r.full_name, a.* FROM accel a JOIN repos r ON r.id = a.repo_id"
        " WHERE a.language = ? AND a.ts = ("
        "   SELECT MAX(ts) FROM accel WHERE repo_id = a.repo_id AND language = a.language)",
        (language,)).fetchall()
    return {r["full_name"]: {"now_rate": r["now_rate"], "long_rate": r["long_rate"],
                             "ratio": r["ratio"], "horizon": r["horizon"],
                             "ts": r["ts"]} for r in rows}


def accel_history(conn: sqlite3.Connection, full_name: str, language: str = "",
                  since: int = 0) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.ts, a.now_rate, a.long_rate, a.ratio, a.horizon FROM accel a"
        " JOIN repos r ON r.id = a.repo_id"
        " WHERE r.full_name = ? COLLATE NOCASE AND a.language = ? AND a.ts >= ?"
        " ORDER BY a.ts", (full_name, language, since)).fetchall()


# ------------------------------------------------------------------------ seen


def mark_seen(conn: sqlite3.Connection, full_name: str, note: str | None = None,
              ts: int | None = None) -> None:
    conn.execute(
        "INSERT INTO seen (full_name, ts, note) VALUES (?, ?, ?)"
        " ON CONFLICT(full_name) DO UPDATE SET ts = excluded.ts,"
        " note = COALESCE(excluded.note, seen.note)",
        (full_name, ts or int(time.time()), note))


def unmark_seen(conn: sqlite3.Connection, full_name: str) -> int:
    return conn.execute("DELETE FROM seen WHERE full_name = ? COLLATE NOCASE",
                        (full_name,)).rowcount


def seen_map(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["full_name"]: r for r in conn.execute("SELECT * FROM seen").fetchall()}


# ---------------------------------------------------------------- meta / memory


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value)))


# -------------------------------------------------------------------- coverage


def run_coverage(conn: sqlite3.Connection, kind: str, interval_s: float,
                 window_s: float = 7 * 86400, now: int | None = None) -> dict:
    """How many scheduled runs actually happened.

    A laptop that sleeps overnight silently drops readings; the numbers still
    look fine and the gap is invisible. This makes it visible: expected runs
    over the window versus the runs that landed, and the largest hole.
    """
    now = now or int(time.time())
    rows = conn.execute(
        "SELECT started_at FROM runs WHERE kind = ? AND error IS NULL"
        " AND started_at >= ? ORDER BY started_at",
        (kind, now - window_s)).fetchall()
    stamps = [r["started_at"] for r in rows]
    if len(stamps) < 2:
        return {"kind": kind, "actual": len(stamps), "expected": None,
                "pct": None, "largest_gap_h": None}
    span = now - stamps[0]
    expected = max(1, round(span / interval_s))
    gaps = [b - a for a, b in itertools.pairwise(stamps)] + [now - stamps[-1]]
    return {
        "kind": kind,
        "actual": len(stamps),
        "expected": expected,
        "pct": round(min(1.0, len(stamps) / expected), 3),
        "largest_gap_h": round(max(gaps) / 3600, 1),
        "since": stamps[0],
    }


# ------------------------------------------------------------------- pruning


def prune_stale(conn: sqlite3.Connection, days: float, now: int | None = None,
                dry_run: bool = False) -> list[str]:
    """Untrack trending-sourced repos no board has listed for `days`.

    Only repos that reached us *through a board* are candidates: the top-N set
    is refreshed by every discovery pass and the watchlist is explicit, so both
    already have a mechanism for leaving. A repo that arrived via trending and
    has not been on any board for two weeks was an afternoon, not a trend, and
    keeping it costs an API call on every enrich pass forever.

    History is kept: tracked is set to 0, nothing is deleted.
    """
    now = now or int(time.time())
    cutoff = now - days * 86400
    rows = conn.execute(
        """
        SELECT r.full_name, r.id FROM repos r
        WHERE r.tracked = 1 AND r.source = 'trending'
          AND COALESCE((SELECT MAX(ts) FROM trending t WHERE t.repo_id = r.id), 0) < ?
        ORDER BY r.full_name
        """, (cutoff,)).fetchall()
    names = [r["full_name"] for r in rows]
    if names and not dry_run:
        set_tracked(conn, [r["id"] for r in rows], False)
    return names


def stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS n_snap, MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM snapshots"
    ).fetchone()
    tracked = conn.execute("SELECT COUNT(*) AS n FROM repos WHERE tracked = 1").fetchone()["n"]
    total = conn.execute("SELECT COUNT(*) AS n FROM repos").fetchone()["n"]
    n_sig = conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"]
    n_acc = conn.execute("SELECT COUNT(*) AS n FROM accel").fetchone()["n"]
    tr = conn.execute(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT ts) AS readings,"
        " COUNT(DISTINCT full_name) AS repos FROM trending").fetchone()
    return {
        "accel_rows": n_acc,
        "trending_rows": tr["n"] or 0,
        "trending_readings": tr["readings"] or 0,
        "trending_repos": tr["repos"] or 0,
        "repos_tracked": tracked,
        "repos_total": total,
        "snapshots": row["n_snap"] or 0,
        "first_ts": row["first_ts"],
        "last_ts": row["last_ts"],
        "signals": n_sig,
    }


def _iso_to_epoch(s: str | None) -> int | None:
    if not s:
        return None
    from datetime import datetime

    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None
