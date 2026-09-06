"""Tests for universe pruning."""

import time

import pytest

from gittrack import db
from gittrack.trending import TrendingRow

DAY = 86400


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


def repo(rid, name, source):
    payload = {
        "id": rid, "full_name": name, "name": name.split("/")[1],
        "owner": {"login": name.split("/")[0], "type": None}, "description": None,
        "language": None, "topics": [], "license": None, "homepage": None,
        "created_at": None, "pushed_at": None, "fork": False, "archived": False,
        "stargazers_count": 100, "forks_count": 10,
    }
    return payload, source


def test_prunes_only_trending_repos_that_left_every_board(conn):
    now = int(time.time())
    for rid, (name, src) in enumerate([("a/stale", "trending"), ("b/fresh", "trending"),
                                       ("c/top", "top"), ("d/watch", "watchlist")], 1):
        payload, source = repo(rid, name, src)
        db.upsert_repo(conn, payload, source=source, now=now)
    # a/stale last seen 20 days ago; b/fresh seen yesterday.
    db.insert_trending(conn, now - 20 * DAY, "daily", "",
                       [TrendingRow(rank=1, full_name="a/stale", repo_id=1)])
    db.insert_trending(conn, now - DAY, "daily", "",
                       [TrendingRow(rank=1, full_name="b/fresh", repo_id=2)])
    conn.commit()

    pruned = db.prune_stale(conn, days=14, now=now)
    assert pruned == ["a/stale"]
    tracked = {r["full_name"] for r in db.tracked_repos(conn)}
    assert tracked == {"b/fresh", "c/top", "d/watch"}, \
        "top-N and watchlist repos have their own exit mechanisms"


def test_a_trending_repo_never_listed_is_pruned_too(conn):
    """No trending row at all reads as 'last seen never', which is stale."""
    now = int(time.time())
    payload, source = repo(1, "a/ghost", "trending")
    db.upsert_repo(conn, payload, source=source, now=now)
    conn.commit()
    assert db.prune_stale(conn, days=14, now=now) == ["a/ghost"]


def test_dry_run_reports_without_changing_anything(conn):
    now = int(time.time())
    payload, source = repo(1, "a/stale", "trending")
    db.upsert_repo(conn, payload, source=source, now=now)
    conn.commit()
    assert db.prune_stale(conn, days=14, now=now, dry_run=True) == ["a/stale"]
    assert {r["full_name"] for r in db.tracked_repos(conn)} == {"a/stale"}


def test_history_is_kept_after_pruning(conn):
    now = int(time.time())
    payload, source = repo(1, "a/stale", "trending")
    db.upsert_repo(conn, payload, source=source, now=now)
    db.insert_snapshot(conn, 1, now - 20 * DAY, 100, 10)
    conn.commit()
    db.prune_stale(conn, days=14, now=now)
    assert len(db.load_series(conn, 1)) == 1
    assert db.repo_by_name(conn, "a/stale") is not None
