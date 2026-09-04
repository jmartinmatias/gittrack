"""GitHub Trending: fetch, parse, and diff successive snapshots.

Trending is GitHub's own answer to "what is gaining traction right now", and it
costs no API quota — it is a public HTML page. Two things make it worth
snapshotting rather than just reading:

  * It surfaces repos you are **not** already tracking. Polling a top-N universe
    can only ever re-rank repos you already chose; trending is a free feed of
    exactly the small-and-accelerating band worth hunting.
  * A single reading is a leaderboard. A *series* of readings is a derivative:
    who just entered, who is climbing, and who has held on for days rather than
    spiking for an afternoon.

The page is scraped, so the selectors here are the fragile part. `parse` reports
how many rows it recovered so a silent layout change surfaces as "0 of 25
parsed" rather than as an empty trending table.
"""

from __future__ import annotations

import html as htmllib
import logging
import re
from dataclasses import asdict, dataclass

import httpx

log = logging.getLogger("gittrack.trending")

BASE = "https://github.com/trending"
# A browser UA: GitHub serves a different (and much thinner) page to obvious bots.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

SINCE_VALUES = ("daily", "weekly", "monthly")

# Period specs used for OUR comparison window, which is a different axis from
# GitHub's own board period (`since`). "1d" means "compare against the reading
# nearest 24 hours ago"; "3w" means three weeks back.
_PERIOD_SPEC = re.compile(r"^\s*(\d+(?:\.\d+)?)?\s*([hdwmy])?\s*$", re.I)
_UNIT_SECONDS = {
    "h": 3600.0,
    "d": 86400.0,
    "w": 7 * 86400.0,
    "m": 30 * 86400.0,      # calendar-agnostic: 30 days
    "y": 365 * 86400.0,
}


def parse_period(spec: str | float | int) -> float:
    """Turn a period spec into seconds.

    Accepts ``3d``, ``2w``, ``6m``, ``1y``, ``12h``, a bare unit (``w`` == 1 week)
    and a bare number (days). Raises ValueError on anything else, so a typo
    surfaces immediately rather than silently comparing against the wrong window.
    """
    if isinstance(spec, (int, float)):
        return float(spec) * _UNIT_SECONDS["d"]
    m = _PERIOD_SPEC.match(str(spec))
    if not m or (m.group(1) is None and m.group(2) is None):
        raise ValueError(
            f"bad period {spec!r}. Use a number and a unit: "
            "7d, 2w, 3m, 1y, 12h (bare numbers are days)")
    qty = float(m.group(1)) if m.group(1) else 1.0
    if qty <= 0:
        raise ValueError(f"period must be positive, got {spec!r}")
    return qty * _UNIT_SECONDS[(m.group(2) or "d").lower()]


def humanise_period(seconds: float) -> str:
    for unit, label in (("y", "year"), ("m", "month"), ("w", "week"), ("d", "day"),
                        ("h", "hour")):
        size = _UNIT_SECONDS[unit]
        if seconds >= size and abs(seconds / size - round(seconds / size)) < 1e-6:
            n = round(seconds / size)
            return f"{n} {label}{'s' if n != 1 else ''}"
    return f"{seconds / 86400:.1f} days"

_BLOCK = '<article class="Box-row">'
_NAME = re.compile(r'<h2[^>]*class="[^"]*lh-condensed[^"]*"[^>]*>\s*<a[^>]*href="/([^"?]+)"', re.S)
_RID = re.compile(r'repository_id&quot;:(\d+)')
_DESC = re.compile(r'<p[^>]*class="col-9[^"]*"[^>]*>\s*(.*?)\s*</p>', re.S)
_LANG = re.compile(r'itemprop="programmingLanguage"[^>]*>\s*([^<]+?)\s*<')
# The counts sit after an inline SVG, so skip to the end of it before reading digits.
_STARS = re.compile(r'href="/[^"]+/stargazers".*?</svg>\s*([\d,]+)\s*</a>', re.S)
_FORKS = re.compile(r'href="/[^"]+/forks".*?</svg>\s*([\d,]+)\s*</a>', re.S)
_PERIOD = re.compile(r'([\d,]+)\s+stars?\s+(?:today|this week|this month)')


@dataclass
class TrendingRow:
    rank: int
    full_name: str
    repo_id: int | None = None
    description: str | None = None
    language: str | None = None
    stars: int | None = None
    forks: int | None = None
    period_stars: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def as_repo_payload(self) -> dict:
        """Shape a trending row like a GitHub API repo object for db.upsert_repo.

        created_at is unknown here; the regular snapshot pass fills it in on the
        next cycle via a conditional request.
        """
        owner, _, name = self.full_name.partition("/")
        return {
            "id": self.repo_id, "full_name": self.full_name, "name": name,
            "owner": {"login": owner, "type": None},
            "description": self.description, "language": self.language,
            "topics": [], "license": None, "homepage": None,
            "created_at": None, "pushed_at": None,
            "fork": False, "archived": False,
            "stargazers_count": self.stars or 0, "forks_count": self.forks,
        }


def _int(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(s.replace(",", ""))
    except ValueError:
        return None


def _text(raw: str | None) -> str | None:
    if not raw:
        return None
    return htmllib.unescape(re.sub(r"<[^>]+>", "", raw)).strip() or None


def parse(page: str) -> list[TrendingRow]:
    blocks = page.split(_BLOCK)[1:]
    rows: list[TrendingRow] = []
    for i, b in enumerate(blocks, 1):
        name = _NAME.search(b)
        if not name:
            continue
        rid = _RID.search(b)
        rows.append(TrendingRow(
            rank=i,
            full_name=name.group(1).strip().strip("/"),
            repo_id=_int(rid.group(1)) if rid else None,
            description=_text(_DESC.search(b).group(1) if _DESC.search(b) else None),
            language=(_LANG.search(b).group(1) if _LANG.search(b) else None),
            stars=_int(_STARS.search(b).group(1) if _STARS.search(b) else None),
            forks=_int(_FORKS.search(b).group(1) if _FORKS.search(b) else None),
            period_stars=_int(_PERIOD.search(b).group(1) if _PERIOD.search(b) else None),
        ))
    if blocks and not rows:
        log.warning("found %d trending blocks but parsed 0 rows — the page layout "
                    "has probably changed", len(blocks))
    return rows


def fetch(since: str = "daily", language: str = "", spoken: str = "",
          timeout: float = 20.0) -> str:
    if since not in SINCE_VALUES:
        raise ValueError(f"since must be one of {SINCE_VALUES}, got {since!r}")
    url = f"{BASE}/{language}" if language else BASE
    params = {"since": since}
    if spoken:
        params["spoken_language_code"] = spoken
    r = httpx.get(url, params=params, headers={"User-Agent": UA, "Accept": "text/html"},
                  timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return r.text


def fetch_rows(since: str = "daily", language: str = "", spoken: str = "") -> list[TrendingRow]:
    return parse(fetch(since=since, language=language, spoken=spoken))


@dataclass
class Diff:
    entered: list[TrendingRow]
    left: list[str]
    moved: dict[str, int]          # full_name -> rank improvement (+ is climbing)
    prev_ts: int | None = None

    @property
    def climbers(self) -> list[tuple[str, int]]:
        return sorted(((k, v) for k, v in self.moved.items() if v > 0),
                      key=lambda kv: -kv[1])


def diff(previous: list[TrendingRow], current: list[TrendingRow],
         prev_ts: int | None = None) -> Diff:
    """What changed between two readings of the same trending board."""
    prev_rank = {r.full_name: r.rank for r in previous}
    cur_rank = {r.full_name: r.rank for r in current}
    entered = [r for r in current if r.full_name not in prev_rank]
    left = [n for n in prev_rank if n not in cur_rank]
    # Rank 1 is best, so an improvement is a decrease; report it as positive.
    moved = {n: prev_rank[n] - cur_rank[n] for n in cur_rank
             if n in prev_rank and prev_rank[n] != cur_rank[n]}
    return Diff(entered=entered, left=left, moved=moved, prev_ts=prev_ts)
