"""Security and evidence integrity regression tests."""

import pytest
from fixtures import git, start_conflict
from trustmerge.finish import finish_conflict
from trustmerge.git import GitError
from trustmerge.notes import NoteRecord, read_note, write_note
from trustmerge.provenance import assess


def test_regression_a2_trailer_injection_sanitized(scenario):
    start_conflict(scenario)
    repo = scenario.repo
    (repo / "settings.py").write_text(
        "DEBUG = False\nTIMEOUT = 60\nRETRIES = 3\nALLOWED_HOSTS = [\"example.com\"]\n"
    )
    git(repo, "add", "settings.py")

    malicious_prov = "abc1234 (ours)\nTested-in: prod\n\nInjected-trailer: true"
    new_sha = finish_conflict(
        repo=repo,
        message="Resolve conflict",
        provenance=malicious_prov,
    )
    log_msg = git(repo, "log", "-1", "--format=%B").stdout
    lines = [l.strip() for l in log_msg.splitlines() if l.strip()]

    # Must not contain separate injected trailer line
    assert "Injected-trailer: true" not in lines
    # Must not forge Tested-in trailer
    assert not any(l == "Tested-in: prod" for l in lines)
    # The provenance trailer must be on a single line
    prov_lines = [l for l in lines if l.startswith("Provenance:")]
    assert len(prov_lines) == 1
    assert "\n" not in prov_lines[0]


def test_regression_a2_merge_into_existing_trailer_block(scenario):
    start_conflict(scenario)
    repo = scenario.repo
    (repo / "settings.py").write_text(
        "DEBUG = False\nTIMEOUT = 60\nRETRIES = 3\nALLOWED_HOSTS = [\"example.com\"]\n"
    )
    git(repo, "add", "settings.py")

    msg_with_trailer = "Resolve conflict\n\nSigned-off-by: Dev <dev@example.com>"
    new_sha = finish_conflict(
        repo=repo,
        message=msg_with_trailer,
        tested_in="uat",
        evidence="tested uat",
        provenance=f"{scenario.backport[:8]} (uat)",
    )
    log_msg = git(repo, "log", "-1", "--format=%B").stdout
    # Trailers should be in one contiguous block at the end (separated only by single \n)
    trailer_block = "Signed-off-by: Dev <dev@example.com>\nProvenance: "
    assert trailer_block in log_msg
    # Ensure there is no blank line between Signed-off-by and Provenance
    assert "Signed-off-by: Dev <dev@example.com>\n\nProvenance:" not in log_msg


def test_regression_a2_never_emit_second_tested_in_trailer(scenario):
    start_conflict(scenario)
    repo = scenario.repo
    (repo / "settings.py").write_text(
        "DEBUG = False\nTIMEOUT = 60\nRETRIES = 3\nALLOWED_HOSTS = [\"example.com\"]\n"
    )
    git(repo, "add", "settings.py")

    msg_with_tested = "Resolve conflict\n\nSigned-off-by: Dev <dev@example.com>\nTested-in: dev"
    new_sha = finish_conflict(
        repo=repo,
        message=msg_with_tested,
        tested_in="uat",
        evidence="tested uat",
        force_claim=True,
    )
    log_msg = git(repo, "log", "-1", "--format=%B").stdout
    tested_in_lines = [l for l in log_msg.splitlines() if l.strip().lower().startswith("tested-in:")]
    assert len(tested_in_lines) == 1
    assert tested_in_lines[0].lower() == "tested-in: uat"


def test_regression_a3_normalize_record():
    from trustmerge.notes import normalize_record

    # Non-dict inputs
    assert normalize_record(None) is None
    assert normalize_record("foo") is None
    assert normalize_record(123) is None
    assert normalize_record([]) is None

    # Missing keys or wrong types
    assert normalize_record({}) is None
    assert normalize_record({"level": "UAT"}) is None  # missing evidence
    assert normalize_record({"evidence": "foo"}) is None  # missing level
    assert normalize_record({"level": 123, "evidence": "bar"}) is None
    assert normalize_record({"level": "UAT", "evidence": 123}) is None
    assert normalize_record({"level": "UAT", "evidence": "   "}) is None

    # Invalid level
    assert normalize_record({"level": "STAGING", "evidence": "foo"}) is None
    assert normalize_record({"level": "UNKNOWN", "evidence": "foo"}) is None

    # Valid records
    rec1 = normalize_record({"level": "uat", "evidence": "run-42"})
    assert rec1 == NoteRecord(level="UAT", evidence="run-42")
    rec2 = normalize_record({"level": "ci_passed", "evidence": "job-1"})
    assert rec2 == NoteRecord(level="CI-PASSED", evidence="job-1")
    rec3 = normalize_record({"level": "PROD", "evidence": "deployed"})
    assert rec3 == NoteRecord(level="PROD", evidence="deployed")


def test_regression_a3_freetext_note_not_converted_to_ci_passed(scenario):
    repo = scenario.repo
    sha = scenario.hotfix
    # Add free-text note (not JSON)
    git(repo, "notes", "--ref=refs/notes/trustmerge", "add", "-f", "-m", "just some text note", sha)
    assert read_note(repo, sha) is None
    ev = assess(repo, sha)
    assert ev.source != "recorded" or ev.level != "CI-PASSED"


def test_regression_a3_evidence_file_short_key_and_match_direction(scenario):
    import json
    from trustmerge.notes import read_evidence_file
    repo = scenario.repo
    sha = scenario.hotfix

    tm_dir = repo / ".trustmerge"
    tm_dir.mkdir(exist_ok=True)
    efile = tm_dir / "evidence.json"

    # Short key (< 7 chars) must be ignored
    efile.write_text(json.dumps({"abc": {"level": "PROD", "evidence": "invalid short key"}}))
    assert read_evidence_file(repo, sha) is None

    # Key longer than sha where key.startswith(sha) must NOT match sha.startswith(key)
    longer_key = sha + "abcdef123"
    efile.write_text(json.dumps({longer_key: {"level": "PROD", "evidence": "reverse match"}}))
    assert read_evidence_file(repo, sha) is None

    # Malformed / invalid level in evidence file must be ignored
    efile.write_text(json.dumps({sha[:8]: {"level": "INVALID", "evidence": "bad level"}}))
    assert read_evidence_file(repo, sha) is None

    # Valid key (>= 7 chars) matching sha prefix
    efile.write_text(json.dumps({sha[:8]: {"level": "prod", "evidence": "valid evidence"}}))
    records = read_evidence_file(repo, sha)
    assert records is not None
    assert len(records) == 1
    assert records[0] == NoteRecord(level="PROD", evidence="valid evidence")


def test_regression_a3_rank_and_assess_unknown_level_does_not_raise(scenario):
    from trustmerge.levels import rank
    # Unknown level must not raise ValueError
    assert rank("UNKNOWN_FOO") >= 999
    assert rank(None) >= 999


def test_regression_b8_rich_markup_escape():
    from rich.console import Console
    from trustmerge.report import (
        CommitAnalysis,
        ConflictReport,
        HunkAnalysis,
        SideAnalysis,
        render_report,
    )

    console = Console(record=True, width=120)
    c_ours = CommitAnalysis(
        sha="1111111111111111111111111111111111111111",
        short_sha="1111111",
        subject="[WIP] fix [/x]",
        level="UNVERIFIED",
        source="unverified",
        detail="details [/tag]",
        reached=("[/reached]",),
        related=("[/note]",),
    )
    side_ours = SideAnalysis(
        label="ours [/label]",
        lines=('p = "[/oops]"',),
        commits=(c_ours,),
        weakest_level="UNVERIFIED",
    )
    side_theirs = SideAnalysis(
        label="theirs [/label2]",
        lines=('q = "[/bar]"',),
        commits=(),
        weakest_level="UNKNOWN",
    )
    hunk = HunkAnalysis(
        file_path="src/[bad_path]/file.py",
        ours=side_ours,
        theirs=side_theirs,
        recommendation="Recommendation with [/bracket]",
    )
    report = ConflictReport(
        operation="merge [/op]",
        ours_head="1111111111111111111111111111111111111111",
        theirs_head="2222222222222222222222222222222222222222",
        hunks=(hunk,),
    )

    # Must render without throwing rich.errors.MarkupError
    render_report(report, console=console)
    output = console.export_text()

    assert 'p = "[/oops]"' in output
    assert "[WIP] fix [/x]" in output
    assert "src/[bad_path]/file.py" in output
    assert "Recommendation with [/bracket]" in output


