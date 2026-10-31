"""Tests for trustmerge.levels and trustmerge.provenance."""

from fixtures import git

from trustmerge.levels import PROD, UAT, UNKNOWN, UNVERIFIED, rank, stronger
from trustmerge.provenance import Config, Evidence, assess, infer_evidence, resolve_ref


# ── levels helpers ────────────────────────────────────────────────────


def test_levels_order():
    assert rank(PROD) < rank(UAT) < rank(UNVERIFIED) < rank(UNKNOWN)
    assert stronger(UAT, PROD) == PROD
    assert stronger(UNKNOWN, UNVERIFIED) == UNVERIFIED


# ── provenance inference ──────────────────────────────────────────────


def test_hotfix_on_prod_is_prod(scenario):
    ev = infer_evidence(scenario.repo, scenario.hotfix)
    assert ev.level == PROD
    assert ev.source == "inferred"
    assert "prod" in ev.reached


def test_backport_on_uat_is_uat_with_related_note(scenario):
    ev = assess(scenario.repo, scenario.backport)
    assert ev.level == UAT
    assert "uat" in ev.reached
    assert len(ev.related) >= 1
    assert "same logical change" in ev.related[0]


def test_dev_only_commit_is_unverified(scenario):
    ev = assess(scenario.repo, scenario.dev_change)
    assert ev.level == UNVERIFIED
    assert ev.reached == ("dev",)


def test_commit_on_no_env_branch_is_unknown(scenario):
    repo = scenario.repo
    git(repo, "switch", "-q", "-c", "stray", "prod")
    (repo / "x.txt").write_text("x\n")
    git(repo, "add", "x.txt")
    git(repo, "commit", "-m", "Stray work")
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "switch", "-q", "prod")
    ev = assess(repo, sha)
    assert ev.level == UNKNOWN
    assert ev.reached == ()


def test_clean_cherry_pick_to_uat_is_uat_by_patch_id(scenario):
    repo = scenario.repo
    git(repo, "switch", "-q", "dev")
    (repo / "n.txt").write_text("n\n")
    git(repo, "add", "n.txt")
    git(repo, "commit", "-m", "Add n")
    original = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "switch", "-q", "uat")
    git(repo, "cherry-pick", original)
    git(repo, "switch", "-q", "prod")
    ev = assess(repo, original)
    assert ev.level == UAT
    assert "identical change" in ev.detail
    assert "uat" in ev.reached


def test_custom_branch_names(scenario):
    repo = scenario.repo
    git(repo, "branch", "-m", "prod", "production")
    ev = assess(repo, scenario.hotfix, Config(prod="production"))
    assert ev.level == PROD


def test_resolve_ref_missing(scenario):
    assert resolve_ref(scenario.repo, "nope") is None
    assert resolve_ref(scenario.repo, "uat") == "refs/heads/uat"


def test_evidence_dataclass_is_frozen():
    ev = Evidence("abc", PROD, "inferred", "test")
    try:
        ev.level = UNKNOWN  # type: ignore[misc]
        assert False, "should not be mutable"
    except AttributeError:
        pass


def test_regression_b9_resolve_ref_descendant_and_diverged(scenario):
    repo = scenario.repo
    # Create remote ref origin/feature pointing to scenario.initial
    git(repo, "update-ref", "refs/remotes/origin/feature", scenario.initial)
    # Create local ref feature pointing to scenario.hotfix (which is a descendant of initial)
    git(repo, "update-ref", "refs/heads/feature", scenario.hotfix)

    # Local is descendant of remote -> must choose local
    assert resolve_ref(repo, "feature") == "refs/heads/feature"

    # Now make remote a descendant of local
    git(repo, "update-ref", "refs/heads/feature", scenario.initial)
    git(repo, "update-ref", "refs/remotes/origin/feature", scenario.hotfix)
    # Remote is descendant of local -> must choose remote
    assert resolve_ref(repo, "feature") == "refs/remotes/origin/feature"

    # Now diverge: local points to hotfix, remote points to dev_change
    git(repo, "update-ref", "refs/heads/feature", scenario.hotfix)
    git(repo, "update-ref", "refs/remotes/origin/feature", scenario.dev_change)
    # Diverged -> must prefer origin
    assert resolve_ref(repo, "feature") == "refs/remotes/origin/feature"

