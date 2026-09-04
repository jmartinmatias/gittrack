#!/usr/bin/env python3
"""Seed a demo database with synthetic history so the dashboard can be exercised
before real snapshots have accumulated.

The repo names here are deliberately fictional - attaching invented numbers to
real projects would produce a screenshot that lies. Growth patterns, though, are
modelled on the real thing: a saturated giant, steady mid-tail projects, a
genuine breakout, a star-only pump, and a young rocket.

    python scripts/seed_demo.py demo.db
    gittrack --db demo.db serve
"""

from __future__ import annotations

import math
import random
import sys
import time

sys.path.insert(0, ".")

from gittrack import db

DAY = 86400
DAYS = 120
SNAPS_PER_DAY = 4

random.seed(20260903)


def repo(rid, full_name, lang, desc, age_days):
    owner, name = full_name.split("/")
    return {
        "id": rid, "full_name": full_name, "name": name,
        "owner": {"login": owner, "type": "Organization"},
        "description": desc, "language": lang, "topics": [],
        "license": {"spdx_id": "Apache-2.0"}, "homepage": None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(time.time() - age_days * DAY)),
        "pushed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - DAY)),
        "fork": False, "archived": False, "stargazers_count": 0, "forks_count": 0,
    }


def noisy(rate, frac=0.28):
    """Day-to-day noise; star arrivals are bursty, never metronomic."""
    return max(0.0, random.gauss(rate, rate * frac))


# (name, language, description, age_days, start_stars, fork_ratio, rate_fn[, fork_rate_fn])
# rate_fn(d) -> stars gained on day d, where d = 0 is DAYS ago. When fork_rate_fn is
# given it overrides the default "forks grow proportionally to stars".
PROFILES = [
    ("example-org/hyperkernel", "C", "A minimal research kernel", 4200, 291000, 0.11,
     lambda d: noisy(74)),
    ("sample-foundation/docs-everything", "Markdown", "Curated documentation index",
     3100, 218000, 0.14, lambda d: noisy(52)),
    ("demo-labs/ui-primitives", "TypeScript", "Unstyled accessible components",
     1400, 41000, 0.06, lambda d: noisy(38)),
    ("example-ai/tensorforge", "Python", "Training loop toolkit", 900, 22400, 0.09,
     lambda d: noisy(26)),
    ("sample-org/queuerunner", "Go", "Durable job queue for Postgres", 1600, 15800, 0.08,
     lambda d: noisy(11)),
    ("demo-tools/dotfiles-manager", "Shell", "Declarative dotfiles", 2200, 12600, 0.07,
     lambda d: noisy(6)),

    # A genuine breakout: quiet for months, then a real surge that forks confirm.
    ("example-ai/localdiffusion", "Python", "Run diffusion models on consumer GPUs",
     210, 2850, 0.13, lambda d: noisy(9) if d < DAYS - 9 else noisy(9 + 62 * (d - (DAYS - 9)) ** 1.6)),

    # Same star shape, but nobody forks it - promotion, not adoption. The fork
    # rate stays at its baseline right through the spike, which is what makes
    # fork confirmation collapse.
    ("demo-labs/awesome-agents", "Markdown", "A curated list of agent frameworks",
     150, 4100, 0.02, lambda d: noisy(11) if d < DAYS - 9 else noisy(11 + 58 * (d - (DAYS - 9)) ** 1.6),
     lambda d: noisy(0.2)),

    # Young and compounding: the profile you most want to catch.
    ("sample-org/vectorlite", "Rust", "Embedded vector index, single file", 52, 210, 0.12,
     lambda d: noisy(4 * math.exp(0.055 * max(0, d - (DAYS - 52))))),

    # Was hot, now cooling - acceleration below 1.
    ("example-org/shellcopilot", "Rust", "Natural language shell assistant", 400, 9200, 0.09,
     lambda d: noisy(max(4, 95 - 1.5 * max(0, d - (DAYS - 60))))),

    # Old spike, long since over: must NOT score as hot today.
    ("demo-tools/retro-emulator", "C++", "Cycle-accurate console emulator", 2600, 7400, 0.15,
     lambda d: noisy(340) if DAYS - 74 <= d < DAYS - 67 else noisy(5)),

    ("sample-foundation/protoparse", "Go", "Fast protobuf reflection", 1900, 6300, 0.10,
     lambda d: noisy(3)),
    ("demo-labs/sqlmigrate", "Python", "Migrations without an ORM", 1200, 3900, 0.11,
     lambda d: noisy(2)),
    ("example-org/wasmpack-lite", "Rust", "Smaller wasm bundles", 700, 2100, 0.08,
     lambda d: noisy(2.5)),
    ("sample-org/tuikit", "Go", "Terminal UI widgets", 980, 1750, 0.09, lambda d: noisy(1.6)),
    ("demo-tools/csvlens", "Rust", "Pager for CSV files", 620, 1180, 0.06, lambda d: noisy(1.9)),
    ("example-ai/promptlint", "TypeScript", "Static analysis for prompts", 190, 640, 0.05,
     lambda d: noisy(2.2)),
    ("sample-foundation/httpbench", "Go", "HTTP load generator", 1500, 520, 0.13,
     lambda d: noisy(0.7)),
    ("demo-labs/colorspace", "Rust", "Perceptual colour conversions", 800, 410, 0.07,
     lambda d: noisy(0.5)),
    ("example-org/tinylock", "C", "Futex-based lock primitives", 1100, 260, 0.10,
     lambda d: noisy(0.3)),
]


# Trending churn archetypes. Each entry is (name, language, base_rank, behaviour).
#   "anchor"  - holds a place nearly every day (the durable, compounding project)
#   "climber" - enters low and works its way up
#   "flash"   - one or two days then gone (an afternoon on Hacker News)
#   "yo-yo"   - drops off and returns, so its *current* streak is short
TRENDING_ARCHETYPES = [
    ("example-ai/localdiffusion", "Python", 2, "anchor"),
    ("sample-org/vectorlite", "Rust", 9, "climber"),
    ("demo-labs/awesome-agents", "Markdown", 4, "anchor"),
    ("example-org/shellcopilot", "Rust", 14, "yo-yo"),
    ("demo-tools/csvlens", "Rust", 18, "yo-yo"),
    ("example-ai/promptlint", "TypeScript", 21, "flash"),
    ("sample-foundation/protoparse", "Go", 12, "flash"),
    ("demo-labs/sqlmigrate", "Python", 16, "flash"),
    ("example-org/wasmpack-lite", "Rust", 7, "climber"),
    ("sample-org/tuikit", "Go", 23, "flash"),
    ("demo-tools/retro-emulator", "C++", 11, "flash"),
    ("example-org/tinylock", "C", 24, "flash"),
]


def seed_trending(conn, days=30):
    """Daily trending readings, so --compare and --over have something to chew on."""
    from gittrack.trending import TrendingRow

    now = int(time.time())
    ids = {r["full_name"]: r["id"]
           for r in conn.execute("SELECT id, full_name FROM repos").fetchall()}
    readings = 0
    for d in range(days, -1, -1):
        ts = now - d * DAY
        age = days - d                      # 0 = oldest reading
        board = []
        for name, lang, base, kind in TRENDING_ARCHETYPES:
            if kind == "anchor":
                present, rank = random.random() < 0.95, base + random.randint(-1, 1)
            elif kind == "climber":
                # Works up the board over the window.
                present = age >= days * 0.35
                rank = max(1, int(base - (age - days * 0.35) * 0.45))
            elif kind == "yo-yo":
                present, rank = (age % 5) < 3, base + random.randint(-3, 3)
            else:                            # flash
                present = random.random() < 0.12
                rank = base + random.randint(-4, 4)
            if present:
                board.append((max(1, rank), name, lang))
        board.sort()
        rows = []
        for i, (_, n, lang) in enumerate(board, 1):
            rid = ids.get(n)
            # Take the star/fork counts from the snapshot series at this instant,
            # so the trending board agrees with the rest of the demo data.
            snap = conn.execute(
                "SELECT stars, forks FROM snapshots WHERE repo_id = ? AND ts <= ?"
                " ORDER BY ts DESC LIMIT 1", (rid, ts)).fetchone() if rid else None
            stars = snap["stars"] if snap else None
            gain = conn.execute(
                "SELECT MAX(stars) - MIN(stars) AS g FROM snapshots"
                " WHERE repo_id = ? AND ts BETWEEN ? AND ?",
                (rid, ts - DAY, ts)).fetchone() if rid else None
            rows.append(TrendingRow(
                rank=i, full_name=n, repo_id=rid, language=lang,
                stars=stars, forks=snap["forks"] if snap else None,
                period_stars=(gain["g"] if gain and gain["g"] else
                              random.randint(40, 600))))
        if rows:
            db.insert_trending(conn, ts, "daily", "", rows)
            readings += 1
    conn.commit()
    return readings


def main(path="demo.db"):
    conn = db.connect(path)
    db.migrate(conn)
    conn.execute("DELETE FROM snapshots")
    conn.execute("DELETE FROM repos")
    conn.execute("DELETE FROM signals")

    now = int(time.time())
    t0 = now - DAYS * DAY

    for rid, prof in enumerate(PROFILES, 1):
        name, lang, desc, age, start, fork_ratio, rate = prof[:7]
        fork_rate = prof[7] if len(prof) > 7 else None
        conn.execute("DELETE FROM repos WHERE id = ?", (rid,))
        db.upsert_repo(conn, repo(rid, name, lang, desc, age), source="top", now=t0)

        stars = float(start)
        forks = float(start * fork_ratio)
        for d in range(DAYS):
            gained = rate(d)
            # Forks lag stars and are noisier; a "pump" profile has a tiny ratio.
            base = fork_rate(d) if fork_rate else gained * fork_ratio
            gained_forks = max(0.0, random.gauss(base, base * 0.5))
            for k in range(SNAPS_PER_DAY):
                frac = (k + 1) / SNAPS_PER_DAY
                ts = t0 + d * DAY + int(k * DAY / SNAPS_PER_DAY)
                db.insert_snapshot(
                    conn, rid, ts,
                    int(stars + gained * frac),
                    int(forks + gained_forks * frac),
                    watchers=None, open_issues=None, size_kb=None,
                )
            stars += gained
            forks += gained_forks

    conn.commit()
    conn.execute("DELETE FROM trending")
    n_readings = seed_trending(conn)
    s = db.stats(conn)
    print(f"seeded {path}: {s['repos_tracked']} repos, {s['snapshots']:,} snapshots, "
          f"{DAYS} days of synthetic history, {n_readings} trending readings")
    print("NOTE: repo names and numbers here are fictional - for UI work only.")
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo.db")
