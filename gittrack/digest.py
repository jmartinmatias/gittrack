"""The shortlist: what deserves attention right now, and why.

This is the product. Five charts and a fourteen-column table are evidence; the
digest is the answer. It is built only from signals that work on day one - the
merged board, surge, acceleration, fork adoption and rank movement - and every
entry carries its reasons in plain words, so a reader can disagree with it.

Two exclusions do most of the work. Saturated giants (over 100k stars) are never
the answer: a 250k-star project cannot gain 60% of itself in a week. And repos
the user has already marked seen are skipped, so the list is new each morning
rather than the same names in the same order.
"""

from __future__ import annotations

from . import db
from .board import combined_board

# Same thresholds the dashboard draws its golden zones with.
GOLD_SURGE = 0.25
GOLD_LO, GOLD_HI = 1000, 20000


def build(conn, language: str = "", over: float = 7 * 86400, limit: int = 7,
          max_stars: int = 100_000, include_seen: bool = False) -> dict:
    board = combined_board(conn, language, over, rank_by="surge")["board"]
    seen = {} if include_seen else db.seen_map(conn)

    ratios = sorted(r["fork_ratio"] for r in board if r.get("fork_ratio") is not None)
    med_fs = ratios[len(ratios) // 2] if ratios else None

    items, skipped_seen, skipped_giants = [], 0, 0
    for r in board:
        stars = r.get("stars") or 0
        if stars > max_stars:
            skipped_giants += 1
            continue
        if r["full_name"] in seen:
            skipped_seen += 1
            continue

        share = r.get("share") or 0.0
        reasons: list[str] = []
        weight = 0

        in_size = GOLD_LO <= stars <= GOLD_HI and share >= GOLD_SURGE
        in_adopt = (med_fs is not None and (r.get("fork_ratio") or 0) >= med_fs
                    and share >= GOLD_SURGE)
        if in_size and in_adopt:
            reasons.append("small, surging and being forked")
            weight += 3
        elif in_size:
            reasons.append("small and surging")
            weight += 2
        elif in_adopt:
            reasons.append("surging and being forked")
            weight += 2

        a = r.get("accel")
        if a and a.get("ratio"):
            if a["ratio"] >= 2.0:
                reasons.append(f"{a['ratio']:.1f}x its own {a['horizon']} pace")
                weight += 2
            elif a["ratio"] >= 1.5:
                reasons.append(f"{a['ratio']:.1f}x its own {a['horizon']} pace")
                weight += 1

        if share >= 0.5:
            reasons.append(f"{share * 100:.0f}% of its stars arrived this period")
            weight += 1

        t = r.get("trend") or {}
        if (t.get("net") or 0) >= 5:
            reasons.append(f"climbed {t['net']} places on the {t.get('board', 'daily')} board")
            weight += 1

        if (r.get("board_count") or 0) >= 2:
            reasons.append(f"on {r['board_count']} boards at once")
            weight += 1

        if not reasons:
            continue
        items.append({
            "full_name": r["full_name"], "url": r["url"],
            "stars": stars, "forks": r.get("forks"),
            "language": r.get("language"), "share": share,
            "accel": a, "board_count": r.get("board_count"),
            "reasons": reasons, "weight": weight,
        })

    items.sort(key=lambda d: (-d["weight"], -d["share"], d["full_name"]))
    return {
        "items": items[:limit],
        "candidates": len(items),
        "considered": len(board),
        "skipped_seen": skipped_seen,
        "skipped_giants": skipped_giants,
        "language": language,
        "over": over,
        "max_stars": max_stars,
    }


def new_entrants(conn, language: str, items: list[dict]) -> list[dict]:
    """Which of these were not in the previous digest - the ones worth a ping.

    The previous list is remembered in the meta table under a per-language key,
    so daily and per-language digests do not confuse each other.
    """
    key = f"digest_last:{language or 'all'}"
    previous = set(db.get_meta(conn, key, []) or [])
    fresh = [it for it in items if it["full_name"] not in previous]
    db.set_meta(conn, key, [it["full_name"] for it in items])
    conn.commit()
    return fresh
