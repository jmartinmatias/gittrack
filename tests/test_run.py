"""Tests for the run command's two halves.

The API snapshot spends rate-limit quota; the trending sweep spends none. They
must fail independently, or the free half goes stale whenever the paid half is
throttled — which, unauthenticated, is most of the time.
"""

from argparse import Namespace
from contextlib import contextmanager

import pytest

from gittrack import cli, db, ingest
from gittrack import trending as tr
from gittrack.github import GitHubError

CONFIG = """\
db_path = "t.db"

[trending]
enabled = true
since = ["daily", "weekly"]
languages = ["", "rust"]
auto_track = false
request_delay = 0.0
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "gittrack.toml").write_text(CONFIG)
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    conn.close()

    @contextmanager
    def fake_client(cfg):
        yield object()

    monkeypatch.setattr(cli, "_client", fake_client)
    monkeypatch.setattr(ingest, "analyze", lambda *a, **k: [])
    monkeypatch.setattr(ingest, "record_signals", lambda *a, **k: 0)
    return Namespace(config=str(tmp_path / "gittrack.toml"),
                     db=str(tmp_path / "t.db"), cmd="run",
                     discover=False, no_discover=False, no_trending=False,
                     trending_only=False)


def _fake_trending(calls):
    def inner(conn, cfg, since="daily", language="", ts=None, **kw):
        calls.append((since, language))
        return {"since": since, "language": language, "ts": ts, "rows": 3,
                "repos_added": 0, "diff": tr.Diff(entered=[], left=[], moved={}),
                "current": [], "first_reading": True, "base_ts": None,
                "compare_period": None, "no_baseline_reason": None}
    return inner


def test_trending_still_runs_when_the_snapshot_fails(env, monkeypatch):
    """A throttled API must not take the boards down with it."""
    calls = []
    monkeypatch.setattr(ingest, "refresh", lambda *a, **k:
                        (_ for _ in ()).throw(GitHubError("rate limit exhausted")))
    monkeypatch.setattr(ingest, "refresh_trending", _fake_trending(calls))

    rc = cli.cmd_run(env)
    assert calls == [("daily", ""), ("daily", "rust"),
                     ("weekly", ""), ("weekly", "rust")]
    assert rc == 1, "the snapshot failure must still surface in the exit code"


def test_successful_run_returns_zero(env, monkeypatch):
    calls = []
    monkeypatch.setattr(ingest, "refresh", lambda *a, **k: {
        "discover": False, "repos_seen": 5, "repos_added": 0,
        "snapshots_written": 5, "api_requests": 1, "ts": 1_700_000_000})
    monkeypatch.setattr(ingest, "refresh_trending", _fake_trending(calls))
    assert cli.cmd_run(env) == 0
    assert len(calls) == 4


def test_one_bad_board_does_not_cost_the_others(env, monkeypatch):
    """A single board failing must not abort the sweep."""
    calls = []
    good = _fake_trending(calls)

    def flaky(conn, cfg, since="daily", language="", ts=None, **kw):
        if (since, language) == ("daily", "rust"):
            raise RuntimeError("layout changed")
        return good(conn, cfg, since=since, language=language, ts=ts, **kw)

    monkeypatch.setattr(ingest, "refresh", lambda *a, **k: {
        "discover": False, "repos_seen": 1, "repos_added": 0,
        "snapshots_written": 1, "api_requests": 1, "ts": 1_700_000_000})
    monkeypatch.setattr(ingest, "refresh_trending", flaky)

    assert cli.cmd_run(env) == 0
    assert calls == [("daily", ""), ("weekly", ""), ("weekly", "rust")]


def test_no_trending_flag_skips_the_sweep(env, monkeypatch):
    calls = []
    monkeypatch.setattr(ingest, "refresh", lambda *a, **k: {
        "discover": False, "repos_seen": 1, "repos_added": 0,
        "snapshots_written": 1, "api_requests": 1, "ts": 1_700_000_000})
    monkeypatch.setattr(ingest, "refresh_trending", _fake_trending(calls))
    env.no_trending = True
    assert cli.cmd_run(env) == 0
    assert calls == []


def test_politeness_delay_is_applied_between_boards_but_not_after_the_last(
        env, monkeypatch, tmp_path):
    (tmp_path / "gittrack.toml").write_text(CONFIG.replace("request_delay = 0.0",
                                                           "request_delay = 1.5"))
    sleeps = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(ingest, "refresh", lambda *a, **k: {
        "discover": False, "repos_seen": 1, "repos_added": 0,
        "snapshots_written": 1, "api_requests": 1, "ts": 1_700_000_000})
    monkeypatch.setattr(ingest, "refresh_trending", _fake_trending([]))

    cli.cmd_run(env)
    # Four boards -> three waits, none trailing.
    assert sleeps == [1.5, 1.5, 1.5]


def test_trending_only_skips_the_api_pass_entirely(env, monkeypatch):
    """The hourly job: boards refresh, no rate-limit quota is touched."""
    calls, refreshed = [], []
    monkeypatch.setattr(ingest, "refresh",
                        lambda *a, **k: refreshed.append(1) or {})
    monkeypatch.setattr(ingest, "refresh_trending", _fake_trending(calls))
    env.trending_only = True
    assert cli.cmd_run(env) == 0
    assert refreshed == [], "the API pass must not run"
    assert len(calls) == 4, "every board must still be swept"


def test_trending_sweep_is_logged_as_a_run(env, monkeypatch, tmp_path):
    """A scheduled job has to be able to answer 'did I actually fire?'."""
    monkeypatch.setattr(ingest, "refresh_trending", _fake_trending([]))
    env.trending_only = True
    cli.cmd_run(env)

    conn = db.connect(tmp_path / "t.db")
    row = db.last_successful_run(conn, "trending")
    assert row is not None, "the sweep must leave a run record"
    assert row["snapshots_written"] == 4       # four boards swept
    assert row["repos_seen"] == 12             # 4 boards x 3 rows
    assert row["api_requests"] == 0            # and it cost no quota
    conn.close()


def test_a_sweep_where_every_board_fails_is_recorded_as_an_error(env, monkeypatch,
                                                                 tmp_path):
    def always_fail(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(ingest, "refresh_trending", always_fail)
    env.trending_only = True
    cli.cmd_run(env)

    conn = db.connect(tmp_path / "t.db")
    assert db.last_successful_run(conn, "trending") is None
    err = conn.execute("SELECT error FROM runs WHERE kind='trending'").fetchone()
    assert "every board failed" in err["error"]
    conn.close()
