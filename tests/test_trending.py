"""Tests for the trending scraper, diff, and streak tracking."""

import time

import pytest

from gittrack import db
from gittrack import trending as tr

DAY = 86400

# A trimmed but structurally faithful sample of two GitHub Trending rows.
SAMPLE = """
<article class="Box-row">
  <a href="/login?return_to=%2Facme%2Fwidget" data-hydro-click="{&quot;event_type&quot;:&quot;x&quot;,
     &quot;payload&quot;:{&quot;repository_id&quot;:12345,&quot;auth_type&quot;:&quot;LOG_IN&quot;}}"></a>
  <h2 class="h3 lh-condensed"><a href="/acme/widget">acme / widget</a></h2>
  <p class="col-9 color-fg-muted my-1 pr-4">A &amp; useful <b>thing</b></p>
  <div class="f6 color-fg-muted mt-2">
    <span itemprop="programmingLanguage">Rust</span>
    <a href="/acme/widget/stargazers"><svg class="octicon"><path d="M8"></path></svg>
      12,345</a>
    <a href="/acme/widget/forks"><svg class="octicon"><path d="M5"></path></svg>
      1,001</a>
    <span class="d-inline-block float-sm-right">2,500 stars today</span>
  </div>
</article>
<article class="Box-row">
  <a href="/login?return_to=%2Fbeta%2Fgizmo" data-hydro-click="{&quot;payload&quot;:{&quot;repository_id&quot;:678}}"></a>
  <h2 class="h3 lh-condensed"><a href="/beta/gizmo">beta / gizmo</a></h2>
  <p class="col-9 color-fg-muted my-1 pr-4">No language here</p>
  <div class="f6 color-fg-muted mt-2">
    <a href="/beta/gizmo/stargazers"><svg class="octicon"><path d="M8"></path></svg>
      900</a>
    <a href="/beta/gizmo/forks"><svg class="octicon"><path d="M5"></path></svg>
      12</a>
    <span class="d-inline-block float-sm-right">80 stars today</span>
  </div>
</article>
"""


# ------------------------------------------------------------------- parsing


def test_parses_every_field():
    rows = tr.parse(SAMPLE)
    assert len(rows) == 2
    a = rows[0]
    assert (a.rank, a.full_name, a.repo_id) == (1, "acme/widget", 12345)
    assert (a.stars, a.forks, a.period_stars) == (12345, 1001, 2500)
    assert a.language == "Rust"
    assert a.description == "A & useful thing"      # entities and tags stripped


def test_missing_language_is_none_not_a_crash():
    assert tr.parse(SAMPLE)[1].language is None


def test_ranks_follow_page_order():
    assert [r.rank for r in tr.parse(SAMPLE)] == [1, 2]


def test_empty_page_yields_no_rows():
    assert tr.parse("<html><body>nothing here</body></html>") == []


def test_layout_change_is_visible_rather_than_silent(caplog):
    """A Box-row we cannot parse must warn, not quietly return an empty board."""
    broken = '<article class="Box-row"><div>no h2 link at all</div></article>'
    assert tr.parse(broken) == []
    assert any("layout" in r.message for r in caplog.records)


def test_repo_payload_shape_is_upsertable():
    row = tr.parse(SAMPLE)[0]
    p = row.as_repo_payload()
    assert p["id"] == 12345 and p["full_name"] == "acme/widget"
    assert p["owner"]["login"] == "acme" and p["name"] == "widget"
    assert p["created_at"] is None      # trending does not expose it


def test_fetch_rejects_a_bad_period():
    with pytest.raises(ValueError):
        tr.fetch(since="hourly")


# ---------------------------------------------------------------------- diff


def rows(*specs):
    return [tr.TrendingRow(rank=i, full_name=n) for i, n in enumerate(specs, 1)]


def test_diff_detects_entries_and_exits():
    d = tr.diff(rows("a/1", "b/2"), rows("b/2", "c/3"))
    assert [r.full_name for r in d.entered] == ["c/3"]
    assert d.left == ["a/1"]


def test_climbing_is_positive_falling_is_negative():
    # b/2 goes from rank 3 to rank 1 (better), a/1 from 1 to 2 (worse).
    d = tr.diff(rows("a/1", "x/x", "b/2"), rows("b/2", "a/1", "x/x"))
    assert d.moved["b/2"] == 2
    assert d.moved["a/1"] == -1
    assert d.climbers[0] == ("b/2", 2)


def test_unmoved_repos_are_not_reported_as_moved():
    d = tr.diff(rows("a/1", "b/2"), rows("a/1", "b/2"))
    assert d.moved == {} and not d.entered and not d.left


def test_first_reading_has_everything_as_new():
    d = tr.diff([], rows("a/1", "b/2"))
    assert len(d.entered) == 2 and d.left == [] and d.moved == {}


# ------------------------------------------------------------------ storage


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


def test_streak_counts_only_the_current_run(conn):
    """A repo that dropped off and came back reports its current run, not its total."""
    now = int(time.time())
    times = [now - 4 * DAY, now - 3 * DAY, now - 2 * DAY, now - DAY, now]
    present = {
        now - 4 * DAY: ["a/1"],
        now - 3 * DAY: ["a/1"],
        now - 2 * DAY: [],           # dropped off, breaking the streak
        now - DAY: ["a/1"],
        now: ["a/1"],
    }
    for t in times:
        board = present[t] + ["filler/x"]
        db.insert_trending(conn, t, "daily", "", rows(*board))
    conn.commit()
    n, first = db.trending_streak(conn, "a/1", "daily")
    assert n == 2 and first == now - DAY
    assert db.trending_streak(conn, "filler/x", "daily")[0] == 5


def test_streak_is_zero_for_an_unseen_repo(conn):
    db.insert_trending(conn, 1000, "daily", "", rows("a/1"))
    assert db.trending_streak(conn, "never/seen", "daily") == (0, None)


def test_boards_are_stored_independently(conn):
    db.insert_trending(conn, 1000, "daily", "", rows("a/1"))
    db.insert_trending(conn, 1000, "weekly", "", rows("b/2"))
    db.insert_trending(conn, 1000, "daily", "rust", rows("c/3"))
    assert [r["full_name"] for r in db.trending_at(conn, 1000, "daily", "")] == ["a/1"]
    assert [r["full_name"] for r in db.trending_at(conn, 1000, "weekly", "")] == ["b/2"]
    assert [r["full_name"] for r in db.trending_at(conn, 1000, "daily", "rust")] == ["c/3"]


def test_reinserting_a_reading_is_idempotent(conn):
    for _ in range(3):
        db.insert_trending(conn, 1000, "daily", "", rows("a/1", "b/2"))
    assert len(db.trending_at(conn, 1000, "daily", "")) == 2


def test_trending_times_are_newest_first(conn):
    for t in (100, 300, 200):
        db.insert_trending(conn, t, "daily", "", rows("a/1"))
    assert db.trending_times(conn, "daily", "", limit=3) == [300, 200, 100]


# ------------------------------------------------------------------- periods


@pytest.mark.parametrize("spec,days", [
    ("1d", 1), ("3d", 3), ("7", 7), ("d", 1), ("w", 7), ("2w", 14),
    ("1m", 30), ("6m", 180), ("1y", 365), ("12h", 0.5), (" 3 d ", 3), ("2W", 14),
])
def test_period_specs(spec, days):
    assert tr.parse_period(spec) == pytest.approx(days * 86400)


@pytest.mark.parametrize("bad", ["", "3x", "-2d", "abc", "0d", "0", "d w"])
def test_bad_period_specs_are_rejected(bad):
    with pytest.raises(ValueError):
        tr.parse_period(bad)


def test_humanise_round_trips_whole_units():
    assert tr.humanise_period(tr.parse_period("2w")) == "2 weeks"
    assert tr.humanise_period(tr.parse_period("1d")) == "1 day"
    assert tr.humanise_period(tr.parse_period("6m")) == "6 months"


# ----------------------------------------------------------- nearest reading


def test_nearest_reading_picks_the_closest_not_the_newest(conn):
    now = 1_000_000
    for t in (now - 10 * DAY, now - 7 * DAY, now - DAY):
        db.insert_trending(conn, t, "daily", "", rows("a/1"))
    got = db.nearest_reading(conn, "daily", "", now - 7 * DAY - 3600)
    assert got == now - 7 * DAY


def test_nearest_reading_respects_tolerance(conn):
    now = 1_000_000
    db.insert_trending(conn, now - 30 * DAY, "daily", "", rows("a/1"))
    # Asking for "1 week ago" must not silently match a month-old reading.
    assert db.nearest_reading(conn, "daily", "", now - 7 * DAY,
                              tolerance=3.5 * DAY) is None
    assert db.nearest_reading(conn, "daily", "", now - 7 * DAY) == now - 30 * DAY


def test_nearest_reading_on_empty_board(conn):
    assert db.nearest_reading(conn, "daily", "", 123) is None


# ------------------------------------------------------------------ aggregate


def test_aggregate_counts_appearances_and_rank_extremes(conn):
    now = 1_000_000
    boards = {
        now - 2 * DAY: ["a/1", "b/2"],
        now - DAY: ["b/2", "a/1"],
        now: ["a/1"],
    }
    for t, names in boards.items():
        db.insert_trending(conn, t, "daily", "", rows(*names))
    conn.commit()
    agg = {r["full_name"]: r for r in
           db.trending_aggregate(conn, "daily", "", now - 7 * DAY, now)}
    assert agg["a/1"]["appearances"] == 3
    assert agg["a/1"]["best_rank"] == 1 and agg["a/1"]["worst_rank"] == 2
    assert agg["b/2"]["appearances"] == 2
    assert db.trending_reading_count(conn, "daily", "", now - 7 * DAY, now) == 3


def test_aggregate_window_excludes_older_readings(conn):
    now = 1_000_000
    db.insert_trending(conn, now - 40 * DAY, "daily", "", rows("old/one"))
    db.insert_trending(conn, now, "daily", "", rows("new/one"))
    conn.commit()
    names = {r["full_name"] for r in
             db.trending_aggregate(conn, "daily", "", now - 7 * DAY, now)}
    assert names == {"new/one"}


def test_migration_adds_repo_language_to_a_preexisting_table():
    """A database created before repo_language existed must gain the column."""
    c = db.connect(":memory:")
    c.execute("""CREATE TABLE trending (
        ts INTEGER NOT NULL, since TEXT NOT NULL, language TEXT NOT NULL DEFAULT '',
        rank INTEGER NOT NULL, repo_id INTEGER, full_name TEXT NOT NULL,
        stars INTEGER, forks INTEGER, period_stars INTEGER,
        PRIMARY KEY (ts, since, language, full_name)) WITHOUT ROWID""")
    db.migrate(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(trending)").fetchall()}
    assert "repo_language" in cols
    db.migrate(c)   # idempotent
    c.close()
