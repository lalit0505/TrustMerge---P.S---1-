"""Tests for trustmerge.conflicts: conflict detection, hunk parsing, and side origins."""

from pathlib import Path
import pytest

from fixtures import git, start_conflict
from trustmerge.conflicts import (
    ConflictHunk,
    ConflictSide,
    blame_range,
    find_conflicts,
    get_conflict_type,
    get_theirs_head,
    is_conflicted,
    list_conflicted_paths,
    parse_conflicted_file,
)


def test_clean_repo_not_conflicted(scenario):
    repo = scenario.repo
    assert is_conflicted(repo) is False
    assert list_conflicted_paths(repo) == []
    assert find_conflicts(repo) is None


def test_merge_conflict_detected_and_parsed(scenario):
    start_conflict(scenario)
    repo = scenario.repo

    assert is_conflicted(repo) is True
    assert list_conflicted_paths(repo) == ["settings.py"]
    assert get_conflict_type(repo) == "merge"

    state = find_conflicts(repo)
    assert state is not None
    assert state.operation == "merge"
    assert state.theirs_head == scenario.dev_change
    assert len(state.files) == 1

    file = state.files[0]
    assert file.path == "settings.py"
    assert len(file.hunks) == 1

    hunk = file.hunks[0]
    assert hunk.file_path == "settings.py"
    assert hunk.ours.lines == ("TIMEOUT = 60",)
    assert hunk.theirs.lines == ("TIMEOUT = 45",)

    # Origin attribution via git blame
    assert scenario.backport in hunk.ours.commits
    assert scenario.dev_change in hunk.theirs.commits


def test_cherry_pick_conflict_detected(scenario):
    repo = scenario.repo
    git(repo, "switch", "-q", "dev")
    res = git(repo, "cherry-pick", scenario.hotfix, check=False)
    assert res.returncode != 0

    assert is_conflicted(repo) is True
    assert get_conflict_type(repo) == "cherry-pick"
    assert get_theirs_head(repo) == scenario.hotfix

    state = find_conflicts(repo)
    assert state is not None
    assert state.operation == "cherry-pick"
    assert state.theirs_head == scenario.hotfix

    file = state.files[0]
    assert len(file.hunks) == 1
    hunk = file.hunks[0]

    # In dev, the dev change is ours (HEAD), and hotfix is theirs
    assert scenario.dev_change in hunk.ours.commits
    assert scenario.hotfix in hunk.theirs.commits


def test_blame_range_invalid_lines(scenario):
    repo = scenario.repo
    commits = blame_range(repo, "HEAD", "settings.py", 0, 0)
    assert commits == ["HEAD"]


def test_parse_conflicted_file_with_diff3(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    diff3_content = (
        "header\n"
        "<<<<<<< HEAD\n"
        "ours line\n"
        "||||||| base\n"
        "base line\n"
        "=======\n"
        "theirs line\n"
        ">>>>>>> other\n"
        "footer\n"
    )
    (repo / "foo.txt").write_text(diff3_content)

    cf = parse_conflicted_file(repo, "foo.txt", ours_head="", theirs_head="")
    assert len(cf.hunks) == 1
    hunk = cf.hunks[0]
    assert hunk.ours.lines == ("ours line",)
    assert hunk.base_lines == ("base line",)
    assert hunk.theirs.lines == ("theirs line",)


def test_parse_conflicted_file_empty_side_deletion(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    content = (
        "<<<<<<< HEAD\n"
        "=======\n"
        "theirs line\n"
        ">>>>>>> other\n"
    )
    (repo / "deleted.txt").write_text(content)

    cf = parse_conflicted_file(repo, "deleted.txt", ours_head="1111111111111111111111111111111111111111", theirs_head="2222222222222222222222222222222222222222")
    assert len(cf.hunks) == 1
    hunk = cf.hunks[0]
    assert hunk.ours.lines == ()
    assert hunk.theirs.lines == ("theirs line",)
    assert hunk.ours.commits == ("1111111111111111111111111111111111111111",)


def test_parse_conflicted_file_missing_on_disk(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    # file not created on disk (modify/delete conflict)
    cf = parse_conflicted_file(repo, "nonexistent.txt", ours_head="abc", theirs_head="def")
    assert len(cf.hunks) == 1
    assert cf.hunks[0].ours.commits == ("abc",)
    assert cf.hunks[0].theirs.commits == ("def",)


def test_regression_b1_auto_merge_above_conflict_blames_correct_ours_commit(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    run("init", "-b", "main")
    run("config", "user.name", "Test")
    run("config", "user.email", "test@example.com")
    run("config", "commit.gpgsign", "false")

    code_base = (
        "line 1\nline 2\nline 3\nTARGET_VAR = \"base\"\n"
        "line 5\nline 6\nline 7\nline 8\nline 9\nline 10\n"
    )
    (repo / "code.py").write_text(code_base)
    run("add", "code.py")
    run("commit", "-m", "Base commit")
    base_sha = run("rev-parse", "HEAD").stdout.strip()

    run("checkout", "-b", "feature-ours")
    code_ours = (
        "line 1\nline 2\nline 3\nTARGET_VAR = \"ours_value\"\n"
        "line 5\nline 6\nline 7\nline 8\nline 9\nline 10\n"
    )
    (repo / "code.py").write_text(code_ours)
    run("add", "code.py")
    run("commit", "-m", "Ours edit")
    ours_sha = run("rev-parse", "HEAD").stdout.strip()

    run("checkout", "main")
    run("checkout", "-b", "feature-theirs")
    # 3 lines added above the conflict (auto-merges cleanly)
    code_theirs = (
        "theirs auto 1\ntheirs auto 2\ntheirs auto 3\n"
        "line 1\nline 2\nline 3\nTARGET_VAR = \"theirs_value\"\n"
        "line 5\nline 6\nline 7\nline 8\nline 9\nline 10\n"
    )
    (repo / "code.py").write_text(code_theirs)
    run("add", "code.py")
    run("commit", "-m", "Theirs edit")
    theirs_sha = run("rev-parse", "HEAD").stdout.strip()

    run("checkout", "feature-ours")
    subprocess.run(["git", "merge", "feature-theirs"], cwd=repo, capture_output=True)

    state = find_conflicts(repo)
    assert state is not None
    hunk = state.files[0].hunks[0]

    # Target line was line 4 in stage-2 (ours)
    assert hunk.ours.start_line == 4
    assert hunk.ours.end_line == 4
    # Crucially: ours commits must contain ours_sha and NOT base_sha
    assert ours_sha in hunk.ours.commits
    assert base_sha not in hunk.ours.commits


def test_regression_b2_blame_parsing_ignores_tabbed_lines_with_hex():
    from trustmerge.conflicts import blame_range
    # When output contains lines starting with \t that have 40-char hex
    porcelain_sample = (
        "a" * 40 + " 1 1 1\n"
        "author Test\n"
        "\t" + "b" * 40 + " some fake code\n"
    )
    # Testing that 'b'*40 is not parsed as a commit
    import unittest.mock as mock
    with mock.patch("trustmerge.conflicts.run_git", return_value=porcelain_sample):
        commits = blame_range(Path("."), "HEAD", "file.py", 1, 1)
        assert commits == ["a" * 40]
        assert "b" * 40 not in commits


def test_regression_b3_marker_parsing_state_transitions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    # Markdown file with ======= heading and lookalikes outside conflict
    content = (
        "Document Title\n"
        "==============\n"
        "Some intro text.\n"
        "Heading 2\n"
        "=======\n"
        "More text.\n"
        "<<<<<<< HEAD\n"
        "ours setting\n"
        "=======\n"
        "theirs setting\n"
        ">>>>>>> incoming\n"
        "Section Title\n"
        "=======\n"
        "Body text\n"
        ">>>>>>> not a marker\n"
    )
    (repo / "doc.md").write_text(content)
    cf = parse_conflicted_file(repo, "doc.md", ours_head="1" * 40, theirs_head="2" * 40)
    # Exactly ONE conflict hunk, not two
    assert len(cf.hunks) == 1
    hunk = cf.hunks[0]
    assert hunk.ours.lines == ("ours setting",)
    assert hunk.theirs.lines == ("theirs setting",)


