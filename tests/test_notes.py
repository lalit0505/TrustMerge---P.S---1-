"""Tests for trustmerge.notes and recorded evidence provenance."""

import json
from pathlib import Path
import pytest
from typer.testing import CliRunner

from fixtures import git
from trustmerge.cli import app
from trustmerge.levels import CI_PASSED, PROD, UAT, UNVERIFIED
from trustmerge.notes import (
    ALT_NOTES_REF,
    NOTES_REF,
    NoteRecord,
    get_recorded_evidence,
    has_notes_ref,
    init_refspec,
    read_evidence_file,
    read_note,
    write_note,
)
from trustmerge.provenance import assess

runner = CliRunner()


def test_note_record_dict_roundtrip():
    record = NoteRecord(level="UAT", evidence="run-123")
    d = record.to_dict()
    assert d == {"level": "UAT", "evidence": "run-123"}
    assert NoteRecord.from_dict(d) == record


def test_write_and_read_note(scenario):
    repo = scenario.repo
    sha = scenario.dev_change
    assert read_note(repo, sha) is None
    assert has_notes_ref(repo) is False

    record = NoteRecord(level="UAT", evidence="staging-run-42")
    write_note(repo, sha, record)

    assert has_notes_ref(repo) is True
    records = read_note(repo, sha)
    assert records == [record]


def test_write_note_append_and_dedup(scenario):
    repo = scenario.repo
    sha = scenario.dev_change

    r1 = NoteRecord(level="CI-PASSED", evidence="job-1")
    r2 = NoteRecord(level="UAT", evidence="manual-signoff")
    write_note(repo, sha, r1)
    write_note(repo, sha, r2)
    # duplicate r1 should not be appended again
    write_note(repo, sha, r1)

    records = read_note(repo, sha)
    assert records == [r1, r2]


def test_write_note_invalid_level(scenario):
    repo = scenario.repo
    with pytest.raises(ValueError, match="level must be one of"):
        write_note(repo, scenario.dev_change, NoteRecord(level="INVALID", evidence="foo"))


def test_read_alt_notes_ref(scenario):
    repo = scenario.repo
    sha = scenario.dev_change
    record = NoteRecord(level="UAT", evidence="gitprov-legacy")
    blob = json.dumps([record.to_dict()])
    git(repo, "notes", "--ref", ALT_NOTES_REF, "add", "-m", blob, sha)

    assert read_note(repo, sha) == [record]


def test_evidence_file_json_dict_format(scenario):
    repo = scenario.repo
    sha = scenario.dev_change
    dot_prov = repo / ".gitprov"
    dot_prov.mkdir(parents=True, exist_ok=True)
    (dot_prov / "evidence.json").write_text(
        json.dumps({sha[:8]: {"level": "UAT", "evidence": "from-file-dict"}})
    )

    records = read_evidence_file(repo, sha)
    assert records == [NoteRecord(level="UAT", evidence="from-file-dict")]
    assert get_recorded_evidence(repo, sha) == records


def test_evidence_file_json_list_format(scenario):
    repo = scenario.repo
    sha = scenario.dev_change
    dot_tm = repo / ".trustmerge"
    dot_tm.mkdir(parents=True, exist_ok=True)
    (dot_tm / "evidence.json").write_text(
        json.dumps([{"sha": sha, "level": "CI-PASSED", "evidence": "action-99"}])
    )

    records = read_evidence_file(repo, sha)
    assert records == [NoteRecord(level="CI-PASSED", evidence="action-99")]


def test_assess_with_notes_upgrades_unverified_to_recorded(scenario):
    repo = scenario.repo
    sha = scenario.dev_change

    # Initially unverified
    ev_before = assess(repo, sha)
    assert ev_before.level == UNVERIFIED
    assert ev_before.source == "inferred"

    # After recording a note
    write_note(repo, sha, NoteRecord(level="UAT", evidence="qa-signoff"))
    ev_after = assess(repo, sha)
    assert ev_after.level == UAT
    assert ev_after.source == "recorded"
    assert ev_after.detail == "qa-signoff"
    assert ev_after.reached == ("dev",)


def test_clone_without_notes_falls_back_to_inferred(tmp_path, scenario):
    repo = scenario.repo
    sha = scenario.dev_change

    # Mark note on origin repo
    write_note(repo, sha, NoteRecord(level="UAT", evidence="tested-on-staging"))
    assert assess(repo, sha).source == "recorded"

    # Clone the repo into a fresh directory without fetching notes
    clone_path = tmp_path / "clone_without_notes"
    git(tmp_path, "clone", "-q", str(repo), str(clone_path))

    # Notes are not transferred by a default git clone
    clone_ev = assess(clone_path, sha)
    assert clone_ev.source == "inferred"
    assert clone_ev.level == UNVERIFIED
    assert "only on dev" in clone_ev.detail


def test_init_refspec(scenario):
    repo = scenario.repo
    # Add fake origin remote
    git(repo, "remote", "add", "origin", "git@example.com:test.git")

    assert init_refspec(repo, "origin") is True
    # Second call should be a no-op
    assert init_refspec(repo, "origin") is False

    fetches = git(repo, "config", "--get-all", "remote.origin.fetch").stdout.splitlines()
    assert f"+{NOTES_REF}:{NOTES_REF}" in fetches
    assert f"+{ALT_NOTES_REF}:{ALT_NOTES_REF}" in fetches


def test_sync_push_and_fetch(tmp_path):
    # Set up bare central repo
    bare = tmp_path / "bare.git"
    git(tmp_path, "init", "--bare", str(bare))

    # Repo A pushes a commit and a note
    repo_a = tmp_path / "repo_a"
    git(tmp_path, "clone", "-q", str(bare), str(repo_a))
    git(repo_a, "config", "user.name", "User A")
    git(repo_a, "config", "user.email", "a@example.com")
    (repo_a / "file.txt").write_text("hello\n")
    git(repo_a, "add", "file.txt")
    git(repo_a, "commit", "-m", "Initial commit")
    git(repo_a, "push", "origin", "HEAD:main")
    sha = git(repo_a, "rev-parse", "HEAD").stdout.strip()

    write_note(repo_a, sha, NoteRecord(level="UAT", evidence="a-testing"))
    from trustmerge.notes import sync_push, sync_fetch
    sync_push(repo_a, "origin")

    # Repo B clones and fetches notes
    repo_b = tmp_path / "repo_b"
    git(tmp_path, "clone", "-q", str(bare), str(repo_b))
    git(repo_b, "config", "user.name", "User B")
    git(repo_b, "config", "user.email", "b@example.com")
    assert read_note(repo_b, sha) is None

    sync_fetch(repo_b, "origin")
    records = read_note(repo_b, sha)
    assert records == [NoteRecord(level="UAT", evidence="a-testing")]


def test_cli_mark_and_scan(scenario):
    repo = scenario.repo
    sha = scenario.dev_change

    res_mark = runner.invoke(
        app,
        ["mark", sha, "--tested", "uat", "--evidence", "manual-test-run", "--repo", str(repo)],
    )
    assert res_mark.exit_code == 0
    assert f"Marked {sha[:8]} as UAT" in res_mark.output

    # Check that scan now shows the recorded evidence
    res_scan = runner.invoke(app, ["scan", "dev", "--repo", str(repo)])
    assert res_scan.exit_code == 0
    assert "UAT" in res_scan.output
    assert "recorded: manual-test-run" in res_scan.output


def test_cli_mark_invalid_level(scenario):
    res = runner.invoke(
        app,
        ["mark", scenario.dev_change, "--tested", "bogus", "--evidence", "test", "--repo", str(scenario.repo)],
    )
    assert res.exit_code == 1
    assert "Invalid level 'bogus'" in res.output


def test_cli_init_and_sync(tmp_path, scenario):
    repo = scenario.repo
    git(repo, "remote", "add", "origin", "https://example.com/repo.git")

    res_init = runner.invoke(app, ["init", "--repo", str(repo)])
    assert res_init.exit_code == 0
    assert "Configured notes fetch refspec" in res_init.output

    res_init_again = runner.invoke(app, ["init", "--repo", str(repo)])
    assert res_init_again.exit_code == 0
    assert "already configured" in res_init_again.output


def test_cli_sync_missing_remote(scenario):
    res = runner.invoke(app, ["sync", "--remote", "nonexistent", "--repo", str(scenario.repo)])
    assert res.exit_code == 1
    assert "Remote 'nonexistent' not found." in res.output


def test_regression_c1_read_note_cat_sort_uniq_multiline(scenario):
    repo = scenario.repo
    sha = scenario.dev_change

    # Simulate cat_sort_uniq multiline output (several JSON lists)
    raw = (
        '[{"level": "UAT", "evidence": "run-1"}]\n'
        '[{"level": "PROD", "evidence": "run-2"}]\n'
    )
    git(repo, "notes", "--ref", NOTES_REF, "add", "-f", "-m", raw, sha)

    records = read_note(repo, sha)
    assert records is not None
    assert len(records) == 2
    assert NoteRecord(level="UAT", evidence="run-1") in records
    assert NoteRecord(level="PROD", evidence="run-2") in records


def test_regression_c1_sync_all_cases(tmp_path):
    bare = tmp_path / "central.git"
    git(tmp_path, "init", "--bare", str(bare))

    repo_a = tmp_path / "clone_a"
    git(tmp_path, "clone", "-q", str(bare), str(repo_a))
    git(repo_a, "config", "user.name", "User A")
    git(repo_a, "config", "user.email", "a@example.com")
    (repo_a / "f1.txt").write_text("file 1\n")
    git(repo_a, "add", "f1.txt")
    git(repo_a, "commit", "-m", "Commit 1")
    sha1 = git(repo_a, "rev-parse", "HEAD").stdout.strip()
    (repo_a / "f2.txt").write_text("file 2\n")
    git(repo_a, "add", "f2.txt")
    git(repo_a, "commit", "-m", "Commit 2")
    sha2 = git(repo_a, "rev-parse", "HEAD").stdout.strip()
    git(repo_a, "push", "origin", "HEAD:main")

    repo_b = tmp_path / "clone_b"
    git(tmp_path, "clone", "-q", str(bare), str(repo_b))
    git(repo_b, "config", "user.name", "User B")
    git(repo_b, "config", "user.email", "b@example.com")

    # (a) No local notes and no remote notes -> succeed with message "nothing to sync"
    res_a = runner.invoke(app, ["sync", "--repo", str(repo_a)])
    assert res_a.exit_code == 0
    assert "nothing to sync" in res_a.output.lower()

    # (b) Local notes only -> push
    write_note(repo_a, sha1, NoteRecord(level="UAT", evidence="qa-run-1"))
    res_b = runner.invoke(app, ["sync", "--repo", str(repo_a)])
    assert res_b.exit_code == 0
    assert "synchronized notes" in res_b.output.lower()
    # Bare remote now has notes ref
    bare_notes = git(bare, "rev-parse", "--verify", "--quiet", NOTES_REF).stdout.strip()
    assert bare_notes

    # (c) Remote notes only -> fetch
    assert read_note(repo_b, sha1) is None
    res_c = runner.invoke(app, ["sync", "--repo", str(repo_b)])
    assert res_c.exit_code == 0
    assert "synchronized notes" in res_c.output.lower()
    assert read_note(repo_b, sha1) == [NoteRecord(level="UAT", evidence="qa-run-1")]

    # (d) DIVERGED notes from two people marking different commits
    # User A marks sha1 with another note and pushes
    write_note(repo_a, sha1, NoteRecord(level="PROD", evidence="prod-run-a"))
    res_a_push = runner.invoke(app, ["sync", "--repo", str(repo_a)])
    assert res_a_push.exit_code == 0

    # User B (without pulling User A's new note) marks sha2 with a note
    write_note(repo_b, sha2, NoteRecord(level="CI-PASSED", evidence="ci-run-b"))

    # User B syncs: diverged!
    res_b_sync = runner.invoke(app, ["sync", "--repo", str(repo_b)])
    assert res_b_sync.exit_code == 0
    assert "synchronized notes" in res_b_sync.output.lower()

    # User B now has both notes
    notes_b_sha1 = read_note(repo_b, sha1)
    notes_b_sha2 = read_note(repo_b, sha2)
    assert any(n.level == "PROD" for n in notes_b_sha1)
    assert any(n.level == "CI-PASSED" for n in notes_b_sha2)

    # User A syncs to get the merged result
    res_a_sync = runner.invoke(app, ["sync", "--repo", str(repo_a)])
    assert res_a_sync.exit_code == 0
    notes_a_sha2 = read_note(repo_a, sha2)
    assert any(n.level == "CI-PASSED" for n in notes_a_sha2)

    # Error handling: remote communication failure must NOT be silently ignored
    broken_repo = tmp_path / "broken"
    git(tmp_path, "clone", "-q", str(bare), str(broken_repo))
    git(broken_repo, "remote", "set-url", "origin", "file:///nonexistent/repo.git")
    res_err = runner.invoke(app, ["sync", "--repo", str(broken_repo)])
    assert res_err.exit_code == 1
    assert "sync error" in res_err.output.lower()

