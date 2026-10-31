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
