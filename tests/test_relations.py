"""Tests for repo-to-repo relationships."""


from gittrack import relations as rel


def R(name, desc=None):
    return {"full_name": name, "description": desc}


# ------------------------------------------------------------------ tokenise


def test_owner_is_not_a_theme():
    """Otherwise every repo by one author looks thematically linked to itself."""
    t = rel.tokenise("anthropics/skills", "Agent skills")
    assert "anthropics" not in t
    assert {"skills", "agent"} <= t


def test_hyphens_and_underscores_split_into_terms():
    t = rel.tokenise("x/code-graph_rag", None)
    assert {"code", "graph", "rag"} <= t


def test_stopwords_and_short_tokens_are_dropped():
    t = rel.tokenise("x/thing", "A tool for the best use of it")
    assert not ({"the", "for", "best", "use", "tool", "of", "it"} & t)


# -------------------------------------------------------------------- themes


def test_theme_needs_several_repos():
    repos = [R("a/one", "agent stuff"), R("b/two", "agent things"),
             R("c/three", "unrelated")]
    assert rel.themes(repos, min_repos=3) == []
    assert [t["term"] for t in rel.themes(repos, min_repos=2)] == ["agent"]


def test_theme_reports_share_of_the_board():
    repos = [R(f"o{i}/agent-{i}", "an agent") for i in range(3)] + [R("z/other", "x")]
    t = rel.themes(repos, min_repos=3)[0]
    assert t["count"] == 3
    assert t["share"] == 0.75          # 3 of 4
    assert t["repos"] == ["o0/agent-0", "o1/agent-1", "o2/agent-2"]


def test_a_term_repeated_within_one_repo_counts_once():
    """Themes measure how many repos share a term, not how often it is said."""
    repos = [R("a/agent", "agent agent agent agent"), R("b/two", "agent"),
             R("c/three", "agent")]
    assert rel.themes(repos, min_repos=3)[0]["count"] == 3


def test_themes_are_ordered_by_reach():
    repos = ([R(f"o{i}/agent", "agent rag") for i in range(5)]
             + [R(f"p{i}/rag", "rag") for i in range(2)])
    terms = [t["term"] for t in rel.themes(repos, min_repos=3)]
    assert terms[0] == "rag" and terms[1] == "agent"    # rag reaches 7, agent 5


# -------------------------------------------------------------------- owners


def test_owners_needs_more_than_one_repo():
    repos = [R("acme/a"), R("acme/b"), R("solo/c")]
    got = rel.owners(repos)
    assert len(got) == 1
    assert got[0]["owner"] == "acme" and got[0]["count"] == 2


# ---------------------------------------------------------------- co-movement


def test_comovement_is_gated_until_there_are_enough_readings():
    out = rel.comovement({"a/1": {0}, "b/2": {0}}, readings=4)
    assert out["enough"] is False and out["cohorts"] == []
    assert "needs 8" in out["note"]


def test_always_present_repos_are_not_reported_as_moving_together():
    """Permanence is not correlation - it would swamp everything else."""
    pres = {"a/always": set(range(10)), "b/always": set(range(10))}
    out = rel.comovement(pres, readings=10)
    assert out["enough"] is True and out["cohorts"] == []


def test_a_block_is_one_cohort_not_many_pairs():
    """Eight repos turning over together are one event, not 28 discoveries."""
    pres = {f"o{i}/r": {2, 3, 4} for i in range(8)}
    out = rel.comovement(pres, readings=10)
    assert len(out["cohorts"]) == 1
    c = out["cohorts"][0]
    assert c["size"] == 8 and c["present"] == 3 and c["of"] == 10
    assert c["likely_board_refresh"] is True


def test_a_small_cohort_is_not_flagged_as_a_refresh():
    pres = {"a/1": {1, 2}, "b/2": {1, 2}}
    c = rel.comovement(pres, readings=10)["cohorts"][0]
    assert c["size"] == 2 and c["likely_board_refresh"] is False


def test_different_presence_makes_different_cohorts():
    pres = {"a/1": {1, 2}, "b/2": {1, 2}, "c/3": {7, 8}, "d/4": {7, 8}}
    out = rel.comovement(pres, readings=10)
    assert len(out["cohorts"]) == 2
    assert {tuple(c["repos"]) for c in out["cohorts"]} == {
        ("a/1", "b/2"), ("c/3", "d/4")}


def test_a_lone_repo_is_not_a_cohort():
    pres = {"a/1": {1, 2}, "b/2": {3, 4}}
    assert rel.comovement(pres, readings=10)["cohorts"] == []
