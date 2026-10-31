from fixtures import git
from trustmerge.identity import extract_references, find_equivalents


def head(repo):
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def test_extract_cherry_pick_trailer():
    h = "a" * 40
    assert extract_references(f"Fix bug\n\n(cherry picked from commit {h})\n") == [h]


def test_extract_django_backport_line():
    msg = "[4.2.x] Fixed #34567 -- Fix thing.\n\nBackport of 0123abcd4567 from main.\n"
    assert extract_references(msg) == ["0123abcd4567"]


def test_extract_cpython_style():
    h = "0123456789abcdef0123456789abcdef01234567"
    msg = f"[3.12] gh-1234: Fix crash (GH-5678)\n\n(cherry picked from commit {h})\n"
    refs = extract_references(msg)
    assert "GH-5678" in refs
    assert h in refs
    assert "gh-1234" not in refs


def test_extract_nothing():
    assert extract_references("Just a normal commit message") == []


def test_extract_deduplicates():
    assert extract_references("Fix (GH-1)\n\nRefs (GH-1)") == ["GH-1"]


def test_edited_backport_found_by_reference(scenario):
    matches = find_equivalents(scenario.repo, scenario.hotfix, ["uat"])
    assert [(m.commit, m.branch, m.method) for m in matches] == [
        (scenario.backport, "uat", "reference")
    ]


def test_unrelated_branch_has_no_match(scenario):
    assert find_equivalents(scenario.repo, scenario.hotfix, ["dev"]) == []


def test_commit_does_not_match_itself(scenario):
    assert find_equivalents(scenario.repo, scenario.hotfix, ["prod"]) == []


def test_clean_cherry_pick_found_by_patch_id(scenario):
    repo = scenario.repo
    git(repo, "branch", "rel", scenario.initial)
    git(repo, "switch", "-q", "rel")
    (repo / "other.txt").write_text("unrelated\n")
    git(repo, "add", "other.txt")
    git(repo, "commit", "-m", "Unrelated change")
    git(repo, "cherry-pick", scenario.hotfix)
    picked = head(repo)
    assert picked != scenario.hotfix
    matches = find_equivalents(repo, scenario.hotfix, ["rel"])
    assert [(m.commit, m.method) for m in matches] == [(picked, "patch-id")]


def test_shared_pr_reference_found(scenario):
    repo = scenario.repo
    git(repo, "switch", "-q", "prod")
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-m", "Add a (GH-5678)")
    original = head(repo)
    git(repo, "switch", "-q", "dev")
    (repo / "b.txt").write_text("different\n")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-m", "[3.12] Add a (GH-5678)")
    backport = head(repo)
    matches = find_equivalents(repo, original, ["dev"])
    assert [(m.commit, m.method) for m in matches] == [(backport, "reference")]
