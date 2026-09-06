"""Terminal rendering: leaderboard, detail view, and JSON/CSV export."""

from __future__ import annotations

import csv
import io
import json
import math
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import metrics as mx

SPARK = "▁▂▃▄▅▆▇█"

SORT_KEYS = {
    "heat": lambda m: m.heat,
    "stars": lambda m: float(m.stars),
    "velocity": lambda m: m.velocity,
    "rel-velocity": lambda m: m.rel_velocity,
    "accel": lambda m: m.accel_ratio,
    "z": lambda m: m.z,
    "forks": lambda m: float(m.forks or 0),
    "doubling": lambda m: -m.doubling_days if m.doubling_days else None,
}


def compact(n: float | None, signed: bool = False) -> str:
    if n is None:
        return "-"
    sign = "+" if signed and n > 0 else "-" if n < 0 else ""
    a = abs(n)
    if a >= 1_000_000:
        body = f"{a / 1_000_000:.1f}M"
    elif a >= 10_000:
        body = f"{a / 1000:.0f}k"
    elif a >= 1000:
        body = f"{a / 1000:.1f}k"
    elif a >= 10:
        body = f"{a:.0f}"
    else:
        body = f"{a:.1f}".rstrip("0").rstrip(".")
    return sign + body


def pct(x: float | None) -> str:
    if x is None:
        return "-"
    return f"{x * 100:+.1f}%" if abs(x) < 10 else f"{x * 100:+.0f}%"


def sparkline(values: list[float], width: int = 12) -> str:
    """Unicode sparkline over the last `width` buckets."""
    if not values:
        return " " * width
    v = values[-width:]
    lo, hi = min(v), max(v)
    if math.isclose(hi, lo):
        return SPARK[3] * len(v)
    return "".join(SPARK[min(7, int((x - lo) / (hi - lo) * 7.999))] for x in v)


def daily_spark(series: list, days: int = 14) -> str:
    """Sparkline of daily star gains from raw observations."""
    if len(series) < 2:
        return ""
    s = mx.Series(series)
    end = float(s.last_ts)
    start = max(end - days * mx.DAY, float(s.first_ts))
    return sparkline(s.daily_velocities(start, end), width=days)


def _heat_style(heat: float | None) -> str:
    if heat is None:
        return "dim"
    if heat >= 70:
        return "bold red"
    if heat >= 50:
        return "bold yellow"
    if heat >= 30:
        return "green"
    return "dim"


def render_top(results: list[mx.RepoMetrics], *, by: str = "heat", limit: int = 25,
               sparks: dict[int, str] | None = None, console: Console | None = None,
               show_insufficient: bool = False) -> None:
    console = console or Console()
    window = results[0].window_days if results else 7
    width = console.width

    # Drop the lower-value columns first when the terminal is narrow, rather than
    # ellipsising every cell into uselessness.
    wide = width >= 132
    mid = width >= 104

    table = Table(title=f"gittrack - ranked by {by}, {window:g}d window",
                  title_style="bold", header_style="bold",
                  expand=False, pad_edge=False, padding=(0, 1))
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("repo", overflow="ellipsis", min_width=22,
                     max_width=40 if wide else 26)
    table.add_column("stars", justify="right", width=6)
    table.add_column(f"Δ{window:g}d", justify="right", width=7)
    table.add_column("rel", justify="right", width=7)
    table.add_column("/day", justify="right", width=6)
    table.add_column("z", justify="right", width=6)
    if mid:
        table.add_column("accel", justify="right", width=6)
        table.add_column("fork✓", justify="right", width=5)
    if wide:
        table.add_column("2x in", justify="right", width=6)
    table.add_column("heat", justify="right", width=4)
    if sparks:
        table.add_column("14d", style="cyan", width=14, overflow="crop")

    shown = 0
    for m in results:
        if shown >= limit:
            break
        if not m.has_window and not show_insufficient:
            continue
        shown += 1
        row = [
            str(shown),
            m.full_name,
            compact(m.stars),
            compact(m.delta_stars, signed=True),
            pct(m.rel_velocity),
            compact(m.velocity),
            _fmt_z(m.z),
        ]
        if mid:
            row.append(f"{m.accel_ratio:.1f}x" if m.accel_ratio is not None else "-")
            row.append(f"{m.fork_confirm:.2f}" if m.fork_confirm is not None else "-")
        if wide:
            row.append(f"{m.doubling_days:.0f}d"
                       if m.doubling_days and m.doubling_days < 3650 else "-")
        row.append(Text(f"{m.heat:.0f}" if m.heat is not None else "-",
                        style=_heat_style(m.heat)))
        if sparks:
            row.append(sparks.get(m.repo_id, ""))
        table.add_row(*row)

    console.print(table)
    if not wide:
        console.print("[dim]Widen the terminal for acceleration, fork confirmation "
                      "and doubling time.[/dim]")

    stalled = [m for m in results if not m.has_window]
    if stalled and not show_insufficient:
        console.print(
            f"[dim]{len(stalled)} repo(s) lack {window:g}d of history and are excluded "
            f"(--show-insufficient to list them).[/dim]"
        )


def _fmt_z(z: float | None) -> str:
    """Keep the column narrow; a z of 300 and a z of 30 mean the same thing."""
    if z is None:
        return "-"
    if abs(z) >= 100:
        return f"{'+' if z > 0 else '-'}99+"
    return f"{z:.1f}"


def render_trending(result: dict, streaks: dict[str, tuple[int, int | None]],
                   limit: int = 25, console: Console | None = None) -> None:
    """The current board plus what changed since the previous reading."""
    console = console or Console()
    rows, d = result["current"], result["diff"]
    board = result["since"]
    lang = result["language"] or "all languages"
    entered = {r.full_name for r in d.entered}

    title = f"GitHub Trending - {board}, {lang}"
    table = Table(title=title, title_style="bold", header_style="bold",
                  expand=False, pad_edge=False, padding=(0, 1))
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("move", justify="right", width=6)
    table.add_column("repo", overflow="ellipsis", min_width=24, max_width=38)
    table.add_column("language", max_width=12)
    table.add_column("stars", justify="right", width=7)
    table.add_column("today", justify="right", width=7)
    table.add_column("of total", justify="right", width=8)
    table.add_column("streak", justify="right", width=6)

    for r in rows[:limit]:
        if r.full_name in entered:
            move = Text("NEW", style="bold green")
        elif r.full_name in d.moved:
            n = d.moved[r.full_name]
            move = Text(f"{'↑' if n > 0 else '↓'}{abs(n)}",
                        style="green" if n > 0 else "red")
        else:
            move = Text("=", style="dim")

        # What fraction of the repo's whole star count arrived in this period.
        # A high number on a large repo is a much louder signal than a raw count.
        share = (r.period_stars / r.stars) if (r.period_stars and r.stars) else None
        n_streak = streaks.get(r.full_name, (0, None))[0]

        table.add_row(
            str(r.rank), move, r.full_name, r.language or "-",
            compact(r.stars),
            Text(compact(r.period_stars, signed=True), style="green"),
            f"{share * 100:.1f}%" if share else "-",
            Text(str(n_streak) if n_streak > 1 else "-",
                 style="bold yellow" if n_streak >= 3 else "dim"),
        )
    console.print(table)

    if result["first_reading"]:
        reason = result.get("no_baseline_reason") or "no earlier reading"
        console.print(f"[dim]Nothing to compare against: {reason}. "
                      "Movement appears once a second reading exists.[/dim]")
        return
    bits = []
    if d.entered:
        bits.append(f"[green]{len(d.entered)} entered[/green]")
    if d.left:
        bits.append(f"[red]{len(d.left)} dropped off[/red]")
    climbers = d.climbers[:3]
    if climbers:
        bits.append("climbing: " + ", ".join(f"{n} ↑{v}" for n, v in climbers))
    if bits:
        console.print("  " + "   ".join(bits))
    if d.left:
        console.print(f"[dim]  gone: {', '.join(sorted(d.left)[:6])}"
                      f"{' …' if len(d.left) > 6 else ''}[/dim]")


def render_trending_summary(summary: dict, limit: int = 25,
                            console: Console | None = None) -> None:
    """Durability view: who kept their place on the board over a window."""
    from .trending import humanise_period

    console = console or Console()
    period = humanise_period(summary["period"])
    lang = summary["language"] or "all languages"
    readings = summary["readings"]

    table = Table(
        title=f"Trending over {period} - {summary['since']}, {lang} "
              f"({readings} reading{'s' if readings != 1 else ''})",
        title_style="bold", header_style="bold", expand=False,
        pad_edge=False, padding=(0, 1))
    table.add_column("repo", overflow="ellipsis", min_width=22, max_width=34)
    table.add_column("language", max_width=11)
    table.add_column("on board", justify="right", width=9)
    table.add_column("hold", justify="right", width=6)
    table.add_column("best", justify="right", width=5)
    table.add_column("avg", justify="right", width=5)
    table.add_column("now", justify="right", width=5)
    table.add_column("stars", justify="right", width=7)
    table.add_column("gained", justify="right", width=8)
    table.add_column("peak/day", justify="right", width=9)

    for r in summary["repos"][:limit]:
        hold = r["hold"]
        table.add_row(
            r["full_name"],
            r["repo_language"] or "-",
            f"{r['appearances']}/{readings}",
            Text(f"{hold * 100:.0f}%",
                 style="bold yellow" if hold >= 0.8 else "dim" if hold < 0.3 else ""),
            str(r["best_rank"]),
            f"{r['avg_rank']:.0f}",
            str(r["current_rank"]) if r["current_rank"] else Text("-", style="dim"),
            compact(r["stars"]),
            Text(compact(r["stars_gained"], signed=True), style="green")
            if r["stars_gained"] else Text("-", style="dim"),
            compact(r["peak_period_stars"]),
        )
    console.print(table)
    if not summary["repos"]:
        console.print(f"[dim]No readings stored in the last {period}. "
                      "Run `gittrack trending` a few times first.[/dim]")
    else:
        console.print("[dim]  hold = share of readings in the window the repo held "
                      "a place. High hold + climbing rank is the durable signal; "
                      "one appearance is an afternoon.[/dim]")


def render_digest(d: dict, console: Console | None = None) -> None:
    """The shortlist, one repo per block, reasons in plain words."""
    console = console or Console()
    items = d["items"]
    console.print()
    if not items:
        console.print("[dim]nothing stands out right now"
                      + (f" ({d['skipped_seen']} already seen)" if d["skipped_seen"] else "")
                      + "[/dim]")
        return
    console.print(Text(f"{len(items)} worth a look", style="bold"), end="  ")
    console.print(Text(f"of {d['considered']} on the boards; "
                       f"{d['skipped_giants']} giants and {d['skipped_seen']} seen skipped",
                       style="dim"))
    console.print()
    for i, it in enumerate(items, 1):
        head = Text()
        head.append(f"{i:>2}. ", style="dim")
        head.append(it["full_name"], style="bold")
        head.append(f"  {compact(it['stars'])}\u2605", style="dim")
        if it.get("language"):
            head.append(f"  {it['language']}", style="dim")
        console.print(head)
        for why in it["reasons"]:
            console.print(Text(f"      \u2022 {why}", style="green" if why.startswith(
                ("small, surging", "small and", "surging and")) else ""))
        console.print(Text(f"      {it['url']}", style="dim"))
    console.print()
    console.print("[dim]mark one reviewed with: gittrack seen owner/name[/dim]")


def render_show(m: mx.RepoMetrics, repo_row, series: list, hot_since: int | None,
                console: Console | None = None) -> None:
    console = console or Console()
    console.print()
    console.print(Text(m.full_name, style="bold"), end="  ")
    console.print(Text(f"{m.stars:,}★  {(m.forks or 0):,}⑂", style="dim"))
    if repo_row["description"]:
        console.print(Text(repo_row["description"], style="italic dim"))
    console.print()

    t = Table(show_header=False, box=None, pad_edge=False)
    t.add_column(style="dim", width=22)
    t.add_column()

    def row(k, v):
        t.add_row(k, v)

    row("language", repo_row["language"] or "-")
    row("created", _fmt(repo_row["created_at"]) + (
        f"  ({m.age_days / 365.25:.1f}y old)" if m.age_days else ""))
    row("last pushed", _fmt(repo_row["pushed_at"]))
    row("tracked since", _fmt(repo_row["first_seen"]))
    t.add_row("", "")
    row(f"Δ stars ({m.window_days:g}d)", f"{compact(m.delta_stars, signed=True)}"
        f"   ({compact(m.velocity)}/day)")
    row("relative velocity", pct(m.rel_velocity))
    row("prior window /day", compact(m.prior_velocity))
    row("acceleration", f"{m.accel_ratio:.2f}x" if m.accel_ratio else "-")
    row("baseline median /day", compact(m.baseline_median))
    row("robust z", f"{m.z:.2f}" if m.z is not None else "- (insufficient baseline)")
    row("doubling time", f"{m.doubling_days:.0f} days" if m.doubling_days
        and m.doubling_days < 3650 else "-")
    t.add_row("", "")
    row(f"Δ forks ({m.window_days:g}d)", compact(m.delta_forks, signed=True))
    row("fork confirmation", f"{m.fork_confirm:.2f}" if m.fork_confirm is not None else "-")
    t.add_row("", "")
    row("heat", Text(f"{m.heat:.1f}" if m.heat is not None else "-",
                     style=_heat_style(m.heat)))
    if m.heat_parts:
        parts = "  ".join(f"{k}={v:.2f}" for k, v in m.heat_parts.items())
        row("  components", Text(parts, style="dim"))
    row("cohort rank (rel)", f"{m.rel_pct * 100:.0f}th pct" if m.rel_pct is not None else "-")
    if hot_since:
        row("hot since", _fmt(hot_since))
    t.add_row("", "")
    row("observations", f"{m.n_obs} over {m.span_days:.1f} days")
    if m.note:
        row("note", Text(m.note, style="yellow"))
    console.print(t)

    spark = daily_spark(series, days=30)
    console.print()
    if spark.strip():
        console.print(Text("  daily stars, last 30d  ", style="dim"), end="")
        console.print(Text(spark, style="cyan"))
    else:
        # A blank row read as a broken chart; say which it is.
        console.print(Text(f"  daily stars: needs a full day of snapshots, "
                           f"has {m.span_days:.1f}d", style="dim"))
    console.print()


def _fmt(ts) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def to_json(results: list[mx.RepoMetrics], limit: int | None = None) -> str:
    rows = [m.to_dict() for m in results[:limit]]
    return json.dumps(rows, indent=2, default=float)


def trending_to_json(result: dict) -> str:
    d = result["diff"]
    return json.dumps({
        "since": result["since"], "language": result["language"],
        "ts": result["ts"], "first_reading": result["first_reading"],
        "repos_added": result["repos_added"],
        "entered": [r.to_dict() for r in d.entered],
        "left": d.left, "moved": d.moved,
        "board": [r.to_dict() for r in result["current"]],
    }, indent=2)


CSV_FIELDS = ["full_name", "stars", "forks", "window_days", "delta_stars", "velocity",
              "rel_velocity", "doubling_days", "delta_forks", "fork_confirm", "z",
              "baseline_median", "prior_velocity", "accel_ratio", "rel_pct", "heat",
              "age_days", "n_obs", "span_days", "note"]


def to_csv(results: list[mx.RepoMetrics], limit: int | None = None) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    w.writeheader()
    for m in results[:limit]:
        w.writerow(m.to_dict())
    return buf.getvalue()
