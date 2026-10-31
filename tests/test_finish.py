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
