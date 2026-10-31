"""Tests for trustmerge.finish: creating ordinary Git commits with trailers and notes."""

from pathlib import Path
import pytest
from typer.testing import CliRunner

from fixtures import git, start_conflict
from trustmerge.cli import app
from trustmerge.finish import finish_conflict
from trustmerge.git import GitError
from trustmerge.notes import read_note
from trustmerge.provenance import assess

runner = CliRunner()


def test_finish_fails_if_unmerged_conflicts_remain(scenario):
    start_conflict(scenario)
    with pytest.raises(GitError, match="Unmerged conflict markers remain"):
        finish_conflict(scenario.repo)


def test_finish_fails_if_nothing_staged(scenario):
    # Repo is clean on prod
    with pytest.raises(GitError, match="No staged changes to commit"):
        finish_conflict(scenario.repo)


def test_finish_creates_commit_with_trailers_and_note(scenario):
    start_conflict(scenario)
    repo = scenario.repo

    # Resolve conflict in settings.py
    (repo / "settings.py").write_text(
        "DEBUG = False\nTIMEOUT = 60\nRETRIES = 3\nALLOWED_HOSTS = [\"example.com\"]\n"
    )
    git(repo, "add", "settings.py")

    new_sha = finish_conflict(
        repo=repo,
        message="Resolve timeout conflict",
        tested_in="uat",
        evidence=f"provenance: {scenario.backport[:8]} (uat)",
        provenance=f"{scenario.backport[:8]} (uat)",
    )

    # 1. Plain git log shows an ordinary commit
    log_msg = git(repo, "log", "-1", "--format=%B").stdout
    assert "Resolve timeout conflict" in log_msg
    assert f"Provenance: {scenario.backport[:8]} (uat)" in log_msg
    assert "Tested-in: uat" in log_msg

    # 2. Commit is a 2-parent merge commit
    parents = git(repo, "log", "-1", "--format=%P").stdout.strip().split()
    assert len(parents) == 2
    assert scenario.backport in parents
    assert scenario.dev_change in parents

    # 3. Note is recorded under refs/notes/trustmerge
    notes = read_note(repo, new_sha)
    assert notes is not None
    assert len(notes) == 1
    assert notes[0].level == "UAT"
    assert f"provenance: {scenario.backport[:8]} (uat)" in notes[0].evidence

    # 4. Assess detects recorded provenance
    ev = assess(repo, new_sha)
    assert ev.level == "UAT"
    assert ev.source == "recorded"


def test_cli_finish_fails_on_unresolved_conflict(scenario):
    start_conflict(scenario)
    res = runner.invoke(app, ["finish", "--repo", str(scenario.repo)])
    assert res.exit_code == 1
    assert "Unmerged conflict markers remain" in res.output


def test_cli_finish_success(scenario):
    start_conflict(scenario)
    repo = scenario.repo

    # Resolve conflict
    (repo / "settings.py").write_text(
        "DEBUG = False\nTIMEOUT = 60\nRETRIES = 3\nALLOWED_HOSTS = [\"example.com\"]\n"
    )
    git(repo, "add", "settings.py")

    res = runner.invoke(
        app,
        [
            "finish",
            "--message",
            "Keep UAT timeout",
            "--tested-in",
            "prod",
            "--evidence",
            "prod manual run verified",
            "--force-claim",
            "--repo",
            str(repo),
        ],
    )
    assert res.exit_code == 0
    assert "Created resolution commit" in res.output

    head_sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    log_body = git(repo, "log", "-1", "--format=%B").stdout
    assert "Keep UAT timeout" in log_body
    assert "Tested-in: prod" in log_body

    notes = read_note(repo, head_sha)
    assert notes[0].level == "PROD"


def test_cli_finish_with_all_flag(scenario):
    start_conflict(scenario)
    repo = scenario.repo

    # Resolve conflict in working tree without git add
    (repo / "settings.py").write_text(
        "DEBUG = False\nTIMEOUT = 60\nRETRIES = 3\nALLOWED_HOSTS = [\"example.com\"]\n"
    )

    res = runner.invoke(app, ["finish", "--all", "--repo", str(repo)])
    assert res.exit_code == 0
    assert "Created resolution commit" in res.output


def test_regression_a1_finish_without_tested_in_emits_no_note_and_no_tested_trailer(scenario):
    start_conflict(scenario)
    repo = scenario.repo
    (repo / "settings.py").write_text(
        "DEBUG = False\nTIMEOUT = 60\nRETRIES = 3\nALLOWED_HOSTS = [\"example.com\"]\n"
    )
    git(repo, "add", "settings.py")
    new_sha = finish_conflict(repo=repo, message="Resolve conflict cleanly")
    log_msg = git(repo, "log", "-1", "--format=%B").stdout
    assert "Provenance:" in log_msg
    assert "Tested-in:" not in log_msg
    assert read_note(repo, new_sha) is None


def test_regression_a1_invalid_level_rejected(scenario):
    start_conflict(scenario)
    repo = scenario.repo
    (repo / "settings.py").write_text(
        "DEBUG = False\nTIMEOUT = 60\nRETRIES = 3\nALLOWED_HOSTS = [\"example.com\"]\n"
    )
    git(repo, "add", "settings.py")
    with pytest.raises(GitError, match="Invalid level 'staging'"):
        finish_conflict(repo=repo, tested_in="staging", evidence="some run")


def test_regression_a1_requires_evidence_with_tested_in(scenario):
    start_conflict(scenario)
    repo = scenario.repo
    (repo / "settings.py").write_text(
        "DEBUG = False\nTIMEOUT = 60\nRETRIES = 3\nALLOWED_HOSTS = [\"example.com\"]\n"
    )
    git(repo, "add", "settings.py")
    with pytest.raises(GitError, match="--evidence is required"):
        finish_conflict(repo=repo, tested_in="uat", evidence=None)


def test_regression_a1_note_stores_evidence_text(scenario):
    start_conflict(scenario)
    repo = scenario.repo
    (repo / "settings.py").write_text(
        "DEBUG = False\nTIMEOUT = 60\nRETRIES = 3\nALLOWED_HOSTS = [\"example.com\"]\n"
    )
    git(repo, "add", "settings.py")
    new_sha = finish_conflict(
        repo=repo,
        tested_in="uat",
        evidence="https://ci.example.com/job/42",
        provenance=f"{scenario.backport[:8]} (uat)",
    )
    notes = read_note(repo, new_sha)
    assert notes is not None
    assert notes[0].evidence == "https://ci.example.com/job/42"


def test_regression_a1_claim_capping_and_force_claim(scenario):
    start_conflict(scenario)
    repo = scenario.repo
    # Resolve by taking DEV side (untested)
    (repo / "settings.py").write_text(
        "DEBUG = False\nTIMEOUT = 45\nRETRIES = 3\nALLOWED_HOSTS = [\"example.com\"]\n"
    )
    git(repo, "add", "settings.py")
    # Claiming UAT without force-claim must fail because dev commit is UNVERIFIED
    with pytest.raises(GitError, match="stronger than the weakest level"):
        finish_conflict(
            repo=repo,
            tested_in="uat",
            evidence="claimed UAT",
            force_claim=False,
        )
    # With force_claim=True it succeeds
    new_sha = finish_conflict(
        repo=repo,
        tested_in="uat",
        evidence="claimed UAT forced",
        force_claim=True,
    )
    notes = read_note(repo, new_sha)
    assert notes[0].level == "UAT"


def test_regression_b7_docs_containing_marker_strings_not_blocked(scenario):
    start_conflict(scenario)
    repo = scenario.repo

    # Resolve settings.py
    (repo / "settings.py").write_text("DEBUG = False\nTIMEOUT = 60\n")
    git(repo, "add", "settings.py")

    # Add a documentation file mentioning conflict markers inside comments/quotes
    doc_content = (
        "# Documentation about merge conflicts\n"
        "# Conflict markers look like:\n"
        "#   <<<<<<< HEAD\n"
        "#   =======\n"
        "#   >>>>>>> branch\n"
        "def example():\n"
        "    msg = '<<<<<<< not a real marker ======= >>>>'\n"
    )
    (repo / "doc.txt").write_text(doc_content)
    git(repo, "add", "doc.txt")

    # This must NOT raise "Unresolved conflict markers remain"
    new_sha = finish_conflict(repo=repo, message="Resolve with docs")
    assert new_sha is not None


def test_regression_b7_all_flag_prints_staged_files(scenario):
    start_conflict(scenario)
    repo = scenario.repo

    (repo / "settings.py").write_text("DEBUG = False\nTIMEOUT = 60\n")

    res = runner.invoke(app, ["finish", "--all", "--repo", str(repo)])
    assert res.exit_code == 0
    # Must list staged files
    assert "settings.py" in res.output
    assert "Staged" in res.output or "staged" in res.output


def test_finish_trailer_merge_edge_cases():
    from trustmerge.finish import _merge_trailers, _sanitize_trailer_value
    assert _sanitize_trailer_value(None) is None

    # Only trailers, no body
    merged = _merge_trailers("", [("Provenance", "abc")])
    assert merged.strip() == "Provenance: abc"

    # Existing trailers updated and duplicates removed
    msg = (
        "Subject\n\n"
        "Tested-in: dev\n"
        "Tested-in: uat\n"
        "Signed-off-by: dev@example.com"
    )
    res = _merge_trailers(msg, [("Tested-in", "prod"), ("Provenance", "xyz")])
    assert res.count("Tested-in:") == 1
    assert "Tested-in: prod" in res
    assert "Provenance: xyz" in res
    assert "Signed-off-by: dev@example.com" in res


def test_finish_marker_sequence_staged_error(scenario):
    start_conflict(scenario)
    repo = scenario.repo

    # Stage a file with real unresolved marker sequence
    bad_content = "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> dev\n"
    (repo / "settings.py").write_text(bad_content)
    git(repo, "add", "settings.py")

    with pytest.raises(GitError, match="Unresolved conflict markers remain inside settings.py"):
        finish_conflict(repo=repo)


def test_finish_kept_commits_edge_cases(scenario):
    from trustmerge.finish import _get_kept_commits
    repo = scenario.repo

    # theirs_head is None
    res = _get_kept_commits(repo, scenario.dev_change, None, [])
    assert res == [scenario.dev_change]

    # Staged resolution keeping ours exactly
    start_conflict(scenario)
    git(repo, "checkout", "--ours", "settings.py")
    git(repo, "add", "settings.py")
    res_ours = _get_kept_commits(repo, scenario.backport, scenario.dev_change, ["settings.py"])
    assert scenario.backport in res_ours

    # Staged resolution keeping theirs exactly
    git(repo, "checkout", scenario.dev_change, "--", "settings.py")
    git(repo, "add", "settings.py")
    res_theirs = _get_kept_commits(repo, scenario.backport, scenario.dev_change, ["settings.py"])
    assert scenario.dev_change in res_theirs

    # Custom rewrite
    (repo / "settings.py").write_text("DEBUG = True\nCUSTOM = 1\n")
    git(repo, "add", "settings.py")
    res_custom = _get_kept_commits(repo, scenario.backport, scenario.dev_change, ["settings.py"])
    assert len(res_custom) >= 1


def test_finish_message_and_provenance_defaults(scenario):
    start_conflict(scenario)
    repo = scenario.repo

    (repo / "settings.py").write_text("DEBUG = False\nTIMEOUT = 60\n")
    git(repo, "add", "settings.py")

    # Remove MERGE_MSG to exercise fallback
    merge_msg = repo / ".git" / "MERGE_MSG"
    if merge_msg.exists():
        merge_msg.unlink()

    new_sha = finish_conflict(repo=repo)
    log_msg = git(repo, "log", "-1", "--format=%B").stdout
    assert "Resolve conflict with" in log_msg
    assert "Provenance:" in log_msg



