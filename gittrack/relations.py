"""Relationships between repos on a board.

Three kinds, in descending order of how much they can be trusted:

1. **Same owner.** Exact, no false positives. One organisation pushing several
   repos onto the board at once is a deliberate act, not a coincidence.
2. **Shared vocabulary.** Repos whose name and description share an uncommon
   term. This is what surfaces a *wave* — when a third of the board is talking
   about the same thing, that is the story, not any individual repo.
3. **Co-movement.** Repos that appear on and drop off the board together,
   reported as *cohorts* rather than pairs. Pairs badly overstate this: eight
   repos that turned over in one board refresh produce twenty-eight pairs at a
   perfect score, which reads like twenty-eight discoveries and is really one
   event. Grouping by identical presence says the true thing — "these eight
   moved as a block" — and a large block is usually the board refreshing rather
   than any affinity between its members, so the size is reported too.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

# Words that carry no signal in a corpus of software repos: either English
# glue, or terms so generic here that everything shares them.
STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "with", "to", "of", "in", "on", "at",
    "by", "from", "as", "is", "are", "be", "was", "were", "it", "its", "this",
    "that", "these", "those", "you", "your", "our", "my", "we", "they", "them",
    "can", "will", "just", "not", "no", "any", "all", "more", "most", "other",
    "use", "used", "using", "make", "makes", "made", "build", "built", "run",
    "running", "new", "best", "free", "simple", "easy", "fast", "small", "big",
    "based", "into", "out", "up", "down", "over", "under", "own", "one", "two",
    "project", "projects", "repo", "repository", "library", "framework", "tool",
    "tools", "app", "application", "software", "system", "platform", "service",
    "support", "supports", "like", "via", "when", "what", "how", "why", "who",
    "get", "gets", "set", "than", "then", "also", "but", "so", "if", "each",
    "every", "some", "you're", "don't",
}

_TOKEN = re.compile(r"[a-z][a-z0-9+#._-]{2,}")
# The minimum number of repos a term must appear in before it counts as a theme.
MIN_THEME_REPOS = 3
# Co-movement needs enough readings before "always together" means anything.
MIN_COMOVE_READINGS = 8


def tokenise(name: str, description: str | None) -> set[str]:
    """Terms from a repo's own name plus its description.

    The owner is dropped: `anthropics/skills` and `mattpocock/skills` share the
    concept "skills", and including owners would instead make every repo by one
    author look thematically linked to itself.
    """
    tail = name.split("/")[-1]
    text = f"{tail} {description or ''}".lower().replace("-", " ").replace("_", " ")
    return {t for t in _TOKEN.findall(text) if t not in STOPWORDS and len(t) > 2}


def themes(repos: list[dict], min_repos: int = MIN_THEME_REPOS,
           limit: int = 12) -> list[dict]:
    """Terms shared by several repos, most-shared first.

    `share` is the fraction of the board carrying the term — the number that says
    whether something is a wave or a coincidence.
    """
    toks = {r["full_name"]: tokenise(r["full_name"], r.get("description"))
            for r in repos}
    counts = Counter(t for s in toks.values() for t in s)
    total = max(1, len(repos))
    out = []
    for term, n in counts.most_common():
        if n < min_repos:
            continue
        out.append({
            "term": term, "count": n, "share": round(n / total, 4),
            "repos": sorted(k for k, s in toks.items() if term in s),
        })
    out.sort(key=lambda d: (-d["count"], d["term"]))
    return out[:limit]


def owners(repos: list[dict]) -> list[dict]:
    by = defaultdict(list)
    for r in repos:
        by[r["full_name"].split("/")[0]].append(r["full_name"])
    return sorted(
        ({"owner": o, "count": len(v), "repos": sorted(v)}
         for o, v in by.items() if len(v) > 1),
        key=lambda d: (-d["count"], d["owner"]),
    )


def comovement(presence: dict[str, set], readings: int,
               min_readings: int = MIN_COMOVE_READINGS,
               min_size: int = 2, limit: int = 8) -> dict:
    """Groups of repos whose presence on the board is identical.

    Repos present in *every* reading are excluded: permanence is not
    correlation, and they would swamp the result. Groups are keyed on the exact
    set of readings a repo appeared in, so a "cohort" is a set of repos that
    arrived and departed in lockstep.
    """
    if readings < min_readings:
        return {"readings": readings, "enough": False, "cohorts": [],
                "note": f"needs {min_readings} readings, has {readings}"}

    churning = {n: s for n, s in presence.items() if 0 < len(s) < readings}
    by_signature: dict[tuple, list[str]] = defaultdict(list)
    for name, seen in churning.items():
        by_signature[tuple(sorted(seen))].append(name)

    cohorts = []
    for sig, members in by_signature.items():
        if len(members) < min_size:
            continue
        cohorts.append({
            "repos": sorted(members),
            "size": len(members),
            "present": len(sig),
            "of": readings,
            # A big block moving as one is almost always the board turning over,
            # not the repos having anything to do with each other. Say so.
            "likely_board_refresh": len(members) >= 5,
        })
    cohorts.sort(key=lambda d: (-d["size"], -d["present"]))
    return {"readings": readings, "enough": True, "churning": len(churning),
            "cohorts": cohorts[:limit]}
