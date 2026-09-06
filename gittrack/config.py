"""Configuration loading.

Config lives in a TOML file (default ``gittrack.toml`` in the working directory).
Every value has a usable default, so a missing file is not an error.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_CONFIG_NAME = "gittrack.toml"
DEFAULT_DB_NAME = "gittrack.db"


@dataclass
class GitHubConfig:
    # Name of the env var holding the token. The token itself never goes in the file.
    token_env: str = "GITHUB_TOKEN"
    # Fall back to `gh auth token` when the env var is empty.
    use_gh_cli: bool = True
    api_base: str = "https://api.github.com"


@dataclass
class UniverseConfig:
    # How many repos, ranked by absolute stars, form the tracked universe.
    top_n: int = 100
    # Never look below this star count when discovering.
    min_stars: int = 50
    # Always tracked regardless of rank: ["owner/repo", ...]
    watchlist: list[str] = field(default_factory=list)
    exclude_archived: bool = True
    exclude_forks: bool = True
    # Repos whose full_name matches any of these substrings are skipped at discovery.
    exclude_name_contains: list[str] = field(default_factory=list)
    # Re-run discovery only if the last one is older than this.
    discover_every_hours: float = 24.0
    # Per-run cap on individually-fetched repos (watchlist entries and repos that
    # have dropped out of the top N). The bulk search path is unaffected. Repos
    # are rotated least-recently-snapshotted first, so nothing starves.
    max_straggler_fetches: int = 200


@dataclass
class TrendingConfig:
    enabled: bool = True
    # Boards to read each cycle: daily / weekly / monthly.
    since: list[str] = field(default_factory=lambda: ["daily"])
    # Language boards to read in addition to the all-languages one ("" is all).
    languages: list[str] = field(default_factory=lambda: [""])
    # Add repos seen on trending to the tracked universe. This is the point:
    # trending is a free feed of the small-and-accelerating band.
    auto_track: bool = True
    # Seconds to wait between board fetches. A run can sweep two dozen boards;
    # this keeps it a polite visitor rather than a burst.
    request_delay: float = 1.2


@dataclass
class NotifyConfig:
    # Post a macOS notification when a repo newly enters the digest.
    macos: bool = True
    # Slack incoming-webhook URL. Empty disables it.
    slack_webhook: str = ""
    # Only notify for repos that were not in the previous digest.
    on_new_only: bool = True


@dataclass
class DigestConfig:
    # Repos above this many stars are never the answer: they are saturated.
    max_stars: int = 100_000
    limit: int = 7
    # Window the digest's rank trend and fork growth are measured over.
    over_days: float = 7.0


@dataclass
class MetricsConfig:
    default_window_days: float = 7.0
    # How far back the "what is normal for this repo" baseline reaches.
    baseline_days: float = 90.0
    # Gates below which a repo reports "insufficient data" instead of a fake score.
    min_observations: int = 8
    min_baseline_days: float = 14.0


@dataclass
class ScoringConfig:
    w_z: float = 0.45
    w_rel: float = 0.30
    w_accel: float = 0.25
    # A repo must clear this heat to be recorded as a breakout signal.
    breakout_heat: float = 60.0
    breakout_min_z: float = 3.0


@dataclass
class ServerConfig:
    port: int = 8787
    host: str = "127.0.0.1"


@dataclass
class Config:
    db_path: str = DEFAULT_DB_NAME
    github: GitHubConfig = field(default_factory=GitHubConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    trending: TrendingConfig = field(default_factory=TrendingConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    digest: DigestConfig = field(default_factory=DigestConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    source_path: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("source_path", None)
        return d


def _merge(section_cls, raw: dict):
    """Build a dataclass from raw TOML, ignoring unknown keys loudly-but-safely."""
    known = {f for f in section_cls.__dataclass_fields__}
    kwargs = {k: v for k, v in raw.items() if k in known}
    unknown = set(raw) - known
    if unknown:
        import warnings

        warnings.warn(
            f"ignoring unknown config keys in [{section_cls.__name__}]: {sorted(unknown)}",
            stacklevel=2,
        )
    return section_cls(**kwargs)


def find_config(explicit: str | None = None) -> Path | None:
    if explicit:
        return Path(explicit)
    cwd = Path.cwd() / DEFAULT_CONFIG_NAME
    if cwd.exists():
        return cwd
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    alt = xdg / "gittrack" / "config.toml"
    return alt if alt.exists() else None


def load(explicit: str | None = None) -> Config:
    path = find_config(explicit)
    if path is None or not path.exists():
        return Config()
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    cfg = Config(
        db_path=raw.get("db_path", DEFAULT_DB_NAME),
        github=_merge(GitHubConfig, raw.get("github", {})),
        universe=_merge(UniverseConfig, raw.get("universe", {})),
        trending=_merge(TrendingConfig, raw.get("trending", {})),
        notify=_merge(NotifyConfig, raw.get("notify", {})),
        digest=_merge(DigestConfig, raw.get("digest", {})),
        metrics=_merge(MetricsConfig, raw.get("metrics", {})),
        scoring=_merge(ScoringConfig, raw.get("scoring", {})),
        server=_merge(ServerConfig, raw.get("server", {})),
        source_path=str(path),
    )
    # Resolve a relative db_path against the config file's directory, so running
    # gittrack from a subdirectory still finds the same database.
    if not os.path.isabs(cfg.db_path):
        cfg.db_path = str((path.parent / cfg.db_path).resolve())
    return cfg


DEFAULT_CONFIG_TOML = """\
# gittrack configuration

db_path = "gittrack.db"

[github]
# The token is read from this environment variable (never stored in this file).
# Falls back to `gh auth token` when unset.
token_env = "GITHUB_TOKEN"
use_gh_cli = true

[universe]
# Size of the tracked universe, ranked by absolute stars.
# 100 is a calibration set: these repos are saturated and rarely spike, but they
# define what "normal" growth looks like. Raise to 1000-10000 to actually hunt.
top_n = 100
min_stars = 50
watchlist = []
exclude_archived = true
exclude_forks = true
exclude_name_contains = []
discover_every_hours = 24.0
# Per-run cap on individually-fetched repos (those the bulk search no longer
# returns). Raise once authenticated; 200/run is safe on any budget.
max_straggler_fetches = 200

[trending]
# GitHub Trending costs no API quota and surfaces repos you are not yet
# tracking - which is the one thing polling a top-N universe cannot do.
enabled = true
since = ["daily"]
languages = [""]      # "" is the all-languages board; add e.g. "rust", "python"
auto_track = true     # add trending repos to the tracked universe
request_delay = 1.2   # seconds between board fetches (politeness, not a limit)

[digest]
# The shortlist: what deserves attention right now, and why.
max_stars = 100000    # saturated giants are never the answer
limit = 7
over_days = 7.0

[notify]
# Tell me when something new enters the digest.
macos = true          # macOS notification centre
slack_webhook = ""    # an incoming-webhook URL; empty disables
on_new_only = true    # only for repos not in the previous digest

[metrics]
default_window_days = 7.0
baseline_days = 90.0
min_observations = 8
min_baseline_days = 14.0

[scoring]
# Relative weights of the three heat components (normalised internally).
w_z = 0.45      # abnormality vs the repo's own history
w_rel = 0.30    # cross-sectional rank of relative growth
w_accel = 0.25  # is the rate itself increasing
breakout_heat = 60.0
breakout_min_z = 3.0

[server]
host = "127.0.0.1"
port = 8787
"""
