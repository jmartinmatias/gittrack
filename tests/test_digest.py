"""Tests for the digest: the shortlist and its memory."""

import time

import pytest

from gittrack import db
from gittrack import digest as dg
from gittrack.trending import TrendingRow

DAY = 86400


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


def repo(rid, name, stars, forks, lang="Python", desc="x"):
    return {
        "id": rid, "full_name": name, "name": name.split("/")[1],
        "owner": {"login": name.split("/")[0], "type": "Organization"},
        "description": desc, "language": lang, "topics": [], "license": None,
        "homepage": None, "created_at": "2025-01-01T00:00:00Z", "pushed_at": None,
        "fork": False, "archived": False, "stargazers_count": stars,
        "forks_count": forks,
    }


def board(conn, ts, since, rows):
    """Put repos on a board with given (name, stars, forks, period_stars)."""
    trs = []
    for rank, (name, stars, forks, ps) in enumerate(rows, 1):
        rid = abs(hash(name)) % 10_000_000
        db.upsert_repo(conn, repo(rid, name, stars, forks), source="trending", now=ts)
        db.insert_snapshot(conn, rid, ts, stars, forks)
        trs.append(TrendingRow(rank=rank, full_name=name, repo_id=rid,
                               language="Python", stars=stars, forks=forks,
                               period_stars=ps))
    db.insert_trending(conn, ts, since, "", trs)
    conn.commit()


# ------------------------------------------------------------------- build


def test_giants_are_never_the_answer(conn):
    now = int(time.time())
    board(conn, now, "daily", [
        ("big/giant", 250_000, 20_000, 200_000),    # 80% surge but 250k stars
        ("small/gem", 3_000, 400, 2_500),           # 83% surge, tiny
    ])
    d = dg.build(conn)
    names = [i["full_name"] for i in d["items"]]
    assert "big/giant" not in names
    assert "small/gem" in names
    assert d["skipped_giants"] == 1


def test_seen_repos_are_skipped_until_included(conn):
    now = int(time.time())
    board(conn, now, "daily", [("a/one", 3_000, 400, 2_500), ("b/two", 4_000, 500, 3_000)])
    db.mark_seen(conn, "a/one", note="looked")
    conn.commit()
    d = dg.build(conn)
    assert [i["full_name"] for i in d["items"]] == ["b/two"]
    assert d["skipped_seen"] == 1
    both = dg.build(conn, include_seen=True)
    assert {i["full_name"] for i in both["items"]} == {"a/one", "b/two"}


def test_every_entry_carries_plain_reasons(conn):
    now = int(time.time())
    board(conn, now, "daily", [("a/one", 3_000, 400, 2_500)])
    d = dg.build(conn)
    it = d["items"][0]
    assert it["reasons"], "an entry with no stated reason is not explainable"
    assert any("surging" in r for r in it["reasons"])
    assert it["weight"] >= 2


def test_both_golden_zones_outrank_one(conn):
    now = int(time.time())
    board(conn, now, "daily", [
        ("only/size", 3_000, 10, 2_500),        # small + surging, almost no forks
        ("both/zones", 3_000, 900, 2_500),      # small + surging + well forked
    ])
    d = dg.build(conn)
    assert d["items"][0]["full_name"] == "both/zones"
    assert d["items"][0]["reasons"][0] == "small, surging and being forked"
    assert d["items"][1]["reasons"][0] == "small and surging"


def test_quiet_board_yields_empty_digest_not_error(conn):
    now = int(time.time())
    # 1% surge, nothing else notable
    board(conn, now, "daily", [("a/one", 50_000, 5_000, 500)])
    d = dg.build(conn)
    assert d["items"] == [] and d["candidates"] == 0


def test_limit_is_respected(conn):
    now = int(time.time())
    board(conn, now, "daily", [(f"o{i}/r{i}", 3_000 + i, 400, 2_500) for i in range(10)])
    assert len(dg.build(conn, limit=3)["items"]) == 3


# ------------------------------------------------------------- new_entrants


def test_new_entrants_remembers_the_previous_digest(conn):
    a = [{"full_name": "a/one"}, {"full_name": "b/two"}]
    assert [i["full_name"] for i in dg.new_entrants(conn, "", a)] == ["a/one", "b/two"]
    b = [{"full_name": "b/two"}, {"full_name": "c/three"}]
    assert [i["full_name"] for i in dg.new_entrants(conn, "", b)] == ["c/three"]
    assert dg.new_entrants(conn, "", b) == []          # nothing new second time


def test_new_entrants_is_per_language(conn):
    dg.new_entrants(conn, "", [{"full_name": "a/one"}])
    fresh = dg.new_entrants(conn, "rust", [{"full_name": "a/one"}])
    assert [i["full_name"] for i in fresh] == ["a/one"], "rust digest has its own memory"


# --------------------------------------------------------------------- seen


def test_seen_round_trip_and_case_insensitive(conn):
    db.mark_seen(conn, "Acme/Widget", note="n")
    conn.commit()
    assert "Acme/Widget" in db.seen_map(conn)
    assert db.unmark_seen(conn, "acme/widget") == 1
    assert db.seen_map(conn) == {}


def test_mark_seen_keeps_an_existing_note(conn):
    db.mark_seen(conn, "a/one", note="first look")
    db.mark_seen(conn, "a/one")            # re-mark without a note
    conn.commit()
    assert db.seen_map(conn)["a/one"]["note"] == "first look"


# ----------------------------------------------------------------- coverage


def test_coverage_reports_missed_runs_and_the_largest_gap(conn):
    now = 1_000_000
    # Hourly schedule, 10 hours, but a 4-hour hole in the middle.
    for h in (0, 1, 2, 6, 7, 8, 9, 10):
        rid = db.start_run(conn, "trending", now=now - (10 - h) * 3600)
        db.finish_run(conn, rid)
    cov = db.run_coverage(conn, "trending", 3600, now=now)
    assert cov["actual"] == 8
    assert cov["expected"] == 10
    assert cov["pct"] == pytest.approx(0.8)
    assert cov["largest_gap_h"] == pytest.approx(4.0)


def test_coverage_needs_two_runs_to_say_anything(conn):
    rid = db.start_run(conn, "trending")
    db.finish_run(conn, rid)
    cov = db.run_coverage(conn, "trending", 3600)
    assert cov["expected"] is None and cov["pct"] is None


def test_coverage_ignores_failed_runs(conn):
    now = 1_000_000
    for h in range(4):
        rid = db.start_run(conn, "trending", now=now - (4 - h) * 3600)
        db.finish_run(conn, rid, error="boom" if h == 1 else None)
    assert db.run_coverage(conn, "trending", 3600, now=now)["actual"] == 3
