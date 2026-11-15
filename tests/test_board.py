"""Tests for the merged board and surge, the number the digest ranks by.

Surge is a repo's period stars (GitHub's "N stars today/this week/this month")
over its total stars: the share of its whole life that arrived this period.
"""

import time

import pytest

from gittrack import db
from gittrack.board import combined_board, rank_trend
from gittrack.trending import TrendingRow


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


def put(conn, ts, since, rows):
    """rows: (name, stars, period_stars), in rank order."""
    db.insert_trending(conn, ts, since, "", [
        TrendingRow(rank=i, full_name=n, stars=s, period_stars=p)
        for i, (n, s, p) in enumerate(rows, 1)])
    conn.commit()


def by_name(board):
    return {r["full_name"]: r for r in board["board"]}


def test_surge_is_period_stars_over_total_stars(conn):
    put(conn, int(time.time()), "daily", [("a/x", 4000, 1000)])
    r = by_name(combined_board(conn, "", 7 * 86400))["a/x"]
    assert r["share"] == pytest.approx(0.25)
    assert (r["share_board"], r["period_stars"]) == ("daily", 1000)


def test_surge_takes_the_board_where_the_share_is_largest(conn):
    now = int(time.time())
    put(conn, now, "daily", [("a/x", 1000, 50)])
    put(conn, now, "weekly", [("a/x", 1000, 300)])
    put(conn, now, "monthly", [("a/x", 1000, 200)])
    r = by_name(combined_board(conn, "", 7 * 86400))["a/x"]
    assert (r["share"], r["share_board"], r["period_stars"]) == (0.3, "weekly", 300)


def test_surge_is_capped_at_one_when_period_exceeds_total(conn):
    # Stars removed after GitHub counted them can make period > total.
    put(conn, int(time.time()), "daily", [("a/x", 100, 140)])
    assert by_name(combined_board(conn, "", 86400))["a/x"]["share"] == 1.0


@pytest.mark.parametrize("stars,period", [(0, 10), (None, 10), (500, None), (500, 0)])
def test_unknown_or_zero_counts_give_no_surge_rather_than_a_crash(conn, stars, period):
    put(conn, int(time.time()), "daily", [("a/x", stars, period)])
    r = by_name(combined_board(conn, "", 86400))["a/x"]
    assert r["share"] is None and r["share_board"] is None


def test_rank_by_surge_orders_by_share_with_unknowns_last(conn):
    put(conn, int(time.time()), "daily", [
        ("big/one", 100_000, 2_000),     # 2%
        ("nodata/x", None, None),         # unknown
        ("small/gem", 2_000, 900),       # 45%
        ("mid/one", 10_000, 1_000),      # 10%
    ])
    b = combined_board(conn, "", 86400, rank_by="surge")["board"]
    assert [r["full_name"] for r in b] == ["small/gem", "mid/one", "big/one", "nodata/x"]
    assert [r["rank"] for r in b] == [1, 2, 3, 4]


def test_surge_ties_break_by_name_so_the_order_is_stable(conn):
    put(conn, int(time.time()), "daily", [("b/b", 1000, 100), ("a/a", 2000, 200)])
    b = combined_board(conn, "", 86400, rank_by="surge")["board"]
    assert [r["full_name"] for r in b] == ["a/a", "b/b"]


def test_only_the_latest_reading_of_each_board_counts(conn):
    now = int(time.time())
    put(conn, now - 3600, "daily", [("old/news", 1000, 900)])
    put(conn, now, "daily", [("new/one", 1000, 100)])
    names = set(by_name(combined_board(conn, "", 86400)))
    assert names == {"new/one"}


def test_star_score_normalises_by_board_length(conn):
    """Rank 1 of a 2-row board and rank 1 of a 4-row board are both worth 1.0."""
    now = int(time.time())
    put(conn, now, "daily", [("a/x", 10, 1), ("b/y", 10, 1)])
    put(conn, now, "weekly", [("c/z", 10, 1), ("d/w", 10, 1), ("a/x", 10, 1),
                              ("e/v", 10, 1)])
    b = by_name(combined_board(conn, "", 86400))
    assert b["c/z"]["score"] == 1.0
    assert b["a/x"]["score"] == pytest.approx(1.0 + 2 / 4)
    assert b["a/x"]["board_count"] == 2


def test_empty_database_gives_an_empty_board_with_a_hint(conn):
    out = combined_board(conn, "", 86400)
    assert out["board"] == [] and "gittrack trending" in out["empty"]


def test_rank_trend_reports_a_climb_as_positive():
    t = rank_trend([(1, 9), (2, 4), (3, 2)], board_size=10)
    assert t["net"] == 7 and t["points"] == [9, 4, 2]
    assert t["strength"] == pytest.approx(0.9)


def test_rank_trend_on_no_history():
    assert rank_trend([]) == {"points": [], "net": None, "strength": None,
                              "readings": 0}


def test_an_untracked_board_row_keeps_its_board_counts(conn):
    """auto_track off: the repo is on the board but not in repos. This used to
    raise KeyError('fork_rel') and take the board, digest and dashboard down."""
    put(conn, int(time.time()), "daily", [("a/x", 4000, 1000)])
    assert db.repo_by_name(conn, "a/x") is None
    r = by_name(combined_board(conn, "", 86400))["a/x"]
    assert r["stars"] == 4000
    assert r["fork_rel"] is None and r["fork_component"] is None


def test_digest_works_with_auto_track_off(conn):
    from gittrack import digest as dg
    put(conn, int(time.time()), "daily", [("small/gem", 3000, 2500)])
    names = [i["full_name"] for i in dg.build(conn)["items"]]
    assert names == ["small/gem"]
