"""Storage-layer tests: upsert semantics, compaction, signal streaks."""

import time

import pytest

from gittrack import db

DAY = 86400


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


def repo_payload(rid=1, full_name="acme/widget", stars=100, **kw):
    return {
        "id": rid, "full_name": full_name, "name": full_name.split("/")[-1],
        "owner": {"login": full_name.split("/")[0], "type": "Organization"},
        "description": "a widget", "language": "Rust", "topics": ["cli"],
        "license": {"spdx_id": "MIT"}, "homepage": None,
        "created_at": "2020-01-01T00:00:00Z", "pushed_at": "2024-01-01T00:00:00Z",
        "fork": False, "archived": False, "stargazers_count": stars,
        "forks_count": stars // 10, **kw,
    }


def test_upsert_is_idempotent_and_refreshes_metadata(conn):
    db.upsert_repo(conn, repo_payload())
    db.upsert_repo(conn, repo_payload(description="a better widget"))
    rows = conn.execute("SELECT * FROM repos").fetchall()
    assert len(rows) == 1
    assert rows[0]["description"] == "a better widget"


def test_rename_follows_the_numeric_id(conn):
    """GitHub repo ids survive renames; full_name is only the current label."""
    db.upsert_repo(conn, repo_payload(rid=7, full_name="old/name"))
    db.upsert_repo(conn, repo_payload(rid=7, full_name="new/name"))
    assert conn.execute("SELECT COUNT(*) c FROM repos").fetchone()["c"] == 1
    assert db.repo_by_name(conn, "new/name")["id"] == 7
    assert db.repo_by_name(conn, "old/name") is None


def test_watchlist_membership_survives_falling_out_of_the_top_n(conn):
    db.upsert_repo(conn, repo_payload(), source="watchlist")
    db.upsert_repo(conn, repo_payload(), source="top")   # a later discovery pass
    assert conn.execute("SELECT source FROM repos").fetchone()["source"] == "watchlist"


def test_repo_lookup_is_case_insensitive(conn):
    db.upsert_repo(conn, repo_payload(full_name="Acme/Widget"))
    assert db.repo_by_name(conn, "acme/widget") is not None


def test_snapshots_are_deduplicated_per_timestamp(conn):
    db.upsert_repo(conn, repo_payload())
    db.insert_snapshot(conn, 1, 1000, 100, 10)
    db.insert_snapshot(conn, 1, 1000, 105, 11)   # same ts, corrected value
    rows = db.load_series(conn, 1)
    assert len(rows) == 1 and rows[0].stars == 105


def test_load_series_excludes_untracked_repos_in_bulk_load(conn):
    db.upsert_repo(conn, repo_payload(rid=1, full_name="a/one"))
    db.upsert_repo(conn, repo_payload(rid=2, full_name="b/two"))
    db.insert_snapshot(conn, 1, 1000, 10, 1)
    db.insert_snapshot(conn, 2, 1000, 20, 2)
    db.set_tracked(conn, [2], False)
    assert set(db.load_all_series(conn)) == {1}
    # ...but the untracked repo's history is still there if asked for directly.
    assert len(db.load_series(conn, 2)) == 1


def test_null_forks_round_trip(conn):
    db.upsert_repo(conn, repo_payload())
    db.insert_snapshot(conn, 1, 1000, 100, None)
    assert db.load_series(conn, 1)[0].forks is None


def test_compact_keeps_one_row_per_day_beyond_the_window(conn):
    db.upsert_repo(conn, repo_payload())
    now = int(time.time())
    # 60 days of hourly snapshots.
    for d in range(60):
        for h in range(24):
            db.insert_snapshot(conn, 1, now - d * DAY - h * 3600, 1000 + d, 100)
    before = db.stats(conn)["snapshots"]
    db.compact(conn, keep_full_days=30, now=now)
    conn.commit()
    after = db.load_series(conn, 1)

    recent = [o for o in after if o.ts >= now - 30 * DAY]
    old = [o for o in after if o.ts < now - 30 * DAY]
    assert len(recent) == pytest.approx(30 * 24, abs=48), "recent detail was lost"
    # One row per day bucket in the compacted region.
    assert len(old) == len({o.ts // DAY for o in old})
    assert len(after) < before


def test_hot_since_finds_the_current_streak_not_an_old_one(conn):
    db.upsert_repo(conn, repo_payload())
    now = int(time.time())
    old = [now - 40 * DAY, now - 39 * DAY]          # an old, ended streak
    current = [now - 3 * DAY, now - 2 * DAY, now - DAY, now]
    for ts in old + current:
        db.record_signal(conn, 1, ts, 7.0, 70.0, 5.0, 0.4, 2.0, 5000)
    assert db.hot_since(conn, 1) == now - 3 * DAY


def test_hot_since_is_none_without_signals(conn):
    db.upsert_repo(conn, repo_payload())
    assert db.hot_since(conn, 1) is None


def test_run_log_records_failures(conn):
    rid = db.start_run(conn, "snapshot")
    db.finish_run(conn, rid, error="boom")
    assert db.last_successful_run(conn, "snapshot") is None
    rid2 = db.start_run(conn, "snapshot")
    db.finish_run(conn, rid2, repos_seen=5)
    assert db.last_successful_run(conn, "snapshot")["repos_seen"] == 5


def test_staleness_ordering_puts_never_snapshotted_first(conn):
    """The straggler budget rotates oldest-first, so nothing starves."""
    now = int(time.time())
    for rid, name in [(1, "a/fresh"), (2, "b/stale"), (3, "c/never")]:
        db.upsert_repo(conn, repo_payload(rid=rid, full_name=name))
    db.insert_snapshot(conn, 1, now, 10, 1)
    db.insert_snapshot(conn, 2, now - 10 * DAY, 10, 1)
    order = [r["full_name"] for r in db.tracked_repos_by_staleness(conn)]
    assert order == ["c/never", "b/stale", "a/fresh"]


def test_staleness_ordering_excludes_untracked(conn):
    db.upsert_repo(conn, repo_payload(rid=1, full_name="a/one"))
    db.upsert_repo(conn, repo_payload(rid=2, full_name="b/two"))
    db.set_tracked(conn, [2], False)
    assert [r["id"] for r in db.tracked_repos_by_staleness(conn)] == [1]


def test_trending_provenance_survives_later_top_refreshes(conn):
    """How a repo first reached us is worth keeping - a later 'top' upsert
    must not erase that it was discovered on the trending board."""
    db.upsert_repo(conn, repo_payload(), source="trending")
    db.upsert_repo(conn, repo_payload(), source="top")
    assert conn.execute("SELECT source FROM repos").fetchone()["source"] == "trending"


def test_explicit_watchlist_overrides_trending_provenance(conn):
    db.upsert_repo(conn, repo_payload(), source="trending")
    db.upsert_repo(conn, repo_payload(), source="watchlist")
    assert conn.execute("SELECT source FROM repos").fetchone()["source"] == "watchlist"


def test_top_repo_appearing_on_trending_keeps_its_origin(conn):
    db.upsert_repo(conn, repo_payload(), source="top")
    db.upsert_repo(conn, repo_payload(), source="top")   # refresh_trending's path
    assert conn.execute("SELECT source FROM repos").fetchone()["source"] == "top"


def test_accel_round_trips_and_latest_wins(conn):
    db.upsert_repo(conn, repo_payload())
    for ts, ratio in ((1000, 1.5), (2000, 3.0)):
        db.insert_accel(conn, 1, ts, "", {"now_rate": 30.0, "long_rate": 10.0,
                                          "ratio": ratio, "horizon": "weekly"})
    conn.commit()
    latest = db.latest_accel(conn, "")
    assert latest["acme/widget"]["ratio"] == 3.0
    assert len(db.accel_history(conn, "acme/widget")) == 2


def test_accel_is_kept_per_language_board(conn):
    db.upsert_repo(conn, repo_payload())
    db.insert_accel(conn, 1, 1000, "", {"now_rate": 1.0, "long_rate": 1.0,
                                        "ratio": 1.0, "horizon": "daily"})
    db.insert_accel(conn, 1, 1000, "rust", {"now_rate": 9.0, "long_rate": 1.0,
                                            "ratio": 9.0, "horizon": "daily"})
    conn.commit()
    assert db.latest_accel(conn, "")["acme/widget"]["ratio"] == 1.0
    assert db.latest_accel(conn, "rust")["acme/widget"]["ratio"] == 9.0


def test_a_partial_source_does_not_erase_richer_metadata(conn):
    """Trending rows carry no created_at, licence, homepage or pushed_at.
    Writing them unconditionally erased what the API pass had filled in."""
    full = repo_payload()
    full.update(homepage="https://acme.dev", topics=["cli", "rust"])
    db.upsert_repo(conn, full, source="top")

    # Now the same repo arrives from a trending board: name and language only.
    sparse = {
        "id": 1, "full_name": "acme/widget", "name": "widget",
        "owner": {"login": "acme", "type": None},
        "description": "a widget", "language": "Rust", "topics": [],
        "license": None, "homepage": None, "created_at": None, "pushed_at": None,
        "fork": False, "archived": False, "stargazers_count": 100, "forks_count": 10,
    }
    db.upsert_repo(conn, sparse, source="trending")

    row = db.repo_by_name(conn, "acme/widget")
    assert row["created_at"] is not None, "age was lost"
    assert row["pushed_at"] is not None, "pushed_at was lost"
    assert row["license"] == "MIT"
    assert row["homepage"] == "https://acme.dev"
    assert row["owner_type"] == "Organization"
    assert "cli" in row["topics"]


def test_a_richer_source_still_fills_in_what_was_missing(conn):
    """The reverse: a repo first seen on a board must gain an age when the API
    pass finally reaches it, which the old clause never did."""
    sparse = {
        "id": 2, "full_name": "b/two", "name": "two",
        "owner": {"login": "b", "type": None}, "description": None,
        "language": None, "topics": [], "license": None, "homepage": None,
        "created_at": None, "pushed_at": None, "fork": False, "archived": False,
        "stargazers_count": 5, "forks_count": 1,
    }
    db.upsert_repo(conn, sparse, source="trending")
    assert db.repo_by_name(conn, "b/two")["created_at"] is None

    db.upsert_repo(conn, repo_payload(rid=2, full_name="b/two"), source="top")
    row = db.repo_by_name(conn, "b/two")
    assert row["created_at"] is not None
    assert row["language"] == "Rust"
    assert row["source"] == "trending", "provenance must still be sticky"
