"""Tests for trustmerge.report: conflict analysis, rich display, recommendations, and LLM briefings."""

import json
from pathlib import Path
import pytest
from rich.console import Console
from typer.testing import CliRunner

from fixtures import git, start_conflict
from trustmerge.cli import app
from trustmerge.conflicts import find_conflicts
from trustmerge.levels import CI_PASSED, PROD, UAT, UNKNOWN, UNVERIFIED
from trustmerge.llm import ConflictBriefing, FakeLLMClient
from trustmerge.report import (
    CommitAnalysis,
    ConflictReport,
    HunkAnalysis,
    SideAnalysis,
    analyze_commit,
    best_evidence_level,
    build_conflict_report,
    generate_recommendation,
    render_report,
    report_to_dict,
)

runner = CliRunner()


def test_analyze_commit(scenario):
    repo = scenario.repo
    c_hotfix = analyze_commit(repo, scenario.hotfix)
    assert c_hotfix.sha == scenario.hotfix
    assert c_hotfix.level == PROD
    assert "prod" in c_hotfix.reached
    assert "Fix slow upstream timeout" in c_hotfix.subject

    c_dev = analyze_commit(repo, scenario.dev_change)
    assert c_dev.level == UNVERIFIED
    assert c_dev.reached == ("dev",)


def test_best_evidence_level():
    def make_c(lvl: str) -> CommitAnalysis:
        return CommitAnalysis(
            sha="1" * 40,
            short_sha="11111111",
            subject="Sub",
            level=lvl,
            source="inferred",
            detail="det",
            reached=(),
            related=(),
        )

    assert best_evidence_level(()) == UNKNOWN
    assert best_evidence_level((make_c(UNVERIFIED), make_c(UAT))) == UAT
    assert best_evidence_level((make_c(UAT), make_c(PROD))) == PROD


def test_regression_a4_weakest_evidence_level():
    from trustmerge.report import weakest_evidence_level

    def make_c(lvl: str) -> CommitAnalysis:
        return CommitAnalysis(
            sha="1" * 40,
            short_sha="11111111",
            subject="Sub",
            level=lvl,
            source="inferred",
            detail="det",
            reached=(),
            related=(),
        )

    assert weakest_evidence_level(()) == UNKNOWN
    # Side with any UNVERIFIED commit must be UNVERIFIED
    assert weakest_evidence_level((make_c(PROD), make_c(UNVERIFIED))) == UNVERIFIED
    # Side with any UNKNOWN commit must be UNKNOWN
    assert weakest_evidence_level((make_c(UAT), make_c(UNKNOWN))) == UNKNOWN
    # Only if all commits are PROD is it PROD
    assert weakest_evidence_level((make_c(PROD), make_c(PROD))) == PROD


def test_regression_a4_recommendation_when_unknown():
    def make_side(level: str) -> SideAnalysis:
        c = CommitAnalysis(
            sha="a" * 40,
            short_sha="aaaaaaaa",
            subject="test",
            level=level,
            source="inferred",
            detail="detail",
            reached=(),
            related=(),
        )
        return SideAnalysis(
            label="side",
            lines=("x = 1",),
            commits=(c,),
            weakest_level=level,
            strongest_level=level,
        )

    rec = generate_recommendation(make_side(UNKNOWN), make_side(UNKNOWN))
    assert "both UNKNOWN" in rec
    assert "both UNVERIFIED" not in rec



def test_recommendation_rules():
    def make_side(level: str, shas: tuple[str, ...] = ("a" * 40,), rel: tuple[str, ...] = ()) -> SideAnalysis:
        c = CommitAnalysis(
            sha=shas[0],
            short_sha=shas[0][:8],
            subject="test",
            level=level,
            source="inferred",
            detail="detail",
            reached=(),
            related=rel,
        )
        return SideAnalysis(label="side", lines=("x = 1",), commits=(c,), best_level=level)

    # Ours UAT vs Theirs UNVERIFIED
    r1 = generate_recommendation(make_side(UAT), make_side(UNVERIFIED))
    assert "Recommend OURS" in r1
    assert "UAT" in r1

    # Ours UNVERIFIED vs Theirs PROD
    r2 = generate_recommendation(make_side(UNVERIFIED), make_side(PROD))
    assert "Recommend THEIRS" in r2
    assert "PROD" in r2

    # Both PROD
    r3 = generate_recommendation(make_side(PROD), make_side(PROD))
    assert "Both sides have PROD evidence" in r3

    # Both UNVERIFIED
    r4 = generate_recommendation(make_side(UNVERIFIED), make_side(UNVERIFIED))
    assert "Neither side has recorded testing evidence" in r4

    # Same logical change
    s_ours = make_side(UAT, shas=("1" * 40,), rel=("same logical change as 22222222",))
    s_theirs = make_side(UNVERIFIED, shas=("2" * 40,))
    r5 = generate_recommendation(s_ours, s_theirs)
    assert "Both sides represent the same logical change" in r5
    assert "Recommend OURS" in r5


def test_build_conflict_report_clean_repo(scenario):
    assert build_conflict_report(scenario.repo) is None


def test_build_conflict_report_with_fake_llm_briefing(scenario):
    start_conflict(scenario)
    fake_client = FakeLLMClient()
    report = build_conflict_report(scenario.repo, use_llm=True, client=fake_client)

    assert report is not None
    assert report.operation == "merge"
    assert report.theirs_head == scenario.dev_change
    assert len(report.hunks) == 1

    hunk = report.hunks[0]
    assert hunk.file_path == "settings.py"
    assert hunk.ours.best_level == UAT
    assert hunk.theirs.best_level == UNVERIFIED
    assert hunk.briefing is not None
    assert "Conflict in settings.py" in hunk.briefing.summary
    assert "Never claim code is tested without recorded evidence" in hunk.briefing.reasoning

    # Dictionary serializable
    d = report_to_dict(report)
    assert d["operation"] == "merge"
    assert d["hunks"][0]["briefing"]["summary"] == hunk.briefing.summary


def test_build_conflict_report_no_llm(scenario):
    start_conflict(scenario)
    report = build_conflict_report(scenario.repo, use_llm=False)
    assert report is not None
    assert report.hunks[0].briefing is None
    assert "Recommend OURS" in report.hunks[0].recommendation


def test_cli_analyze_clean_repo_fails(scenario):
    res = runner.invoke(app, ["analyze", "--repo", str(scenario.repo)])
    assert res.exit_code == 1
    assert "No merge or cherry-pick conflict detected" in res.output


def test_cli_analyze_human_readable_output(scenario, monkeypatch):
    monkeypatch.setenv("TRUSTMERGE_LLM", "fake")
    start_conflict(scenario)
    res = runner.invoke(app, ["analyze", "--repo", str(scenario.repo)])
    assert res.exit_code == 0
    assert "TrustMerge Conflict Analysis" in res.output
    assert "settings.py" in res.output
    assert "UAT" in res.output
    assert "UNVERIFIED" in res.output
    assert "Rule-based briefing (fake LLM)" in res.output


def test_cli_analyze_no_llm_flag(scenario):
    start_conflict(scenario)
    res = runner.invoke(app, ["analyze", "--repo", str(scenario.repo), "--no-llm"])
    assert res.exit_code == 0
    assert "TrustMerge Conflict Analysis" in res.output
    assert "Rule-based briefing (fake LLM)" not in res.output
    assert "Recommendation" in res.output


def test_cli_analyze_json_output(scenario, monkeypatch):
    monkeypatch.setenv("TRUSTMERGE_LLM", "fake")
    start_conflict(scenario)
    res = runner.invoke(app, ["analyze", "--repo", str(scenario.repo), "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["operation"] == "merge"
    assert len(data["hunks"]) == 1
    hunk = data["hunks"][0]
    assert hunk["file_path"] == "settings.py"
    assert hunk["ours"]["best_level"] == "UAT"
    assert hunk["theirs"]["best_level"] == "UNVERIFIED"
    assert hunk["briefing"] is not None
    assert "source" in hunk["briefing"]
    assert "model" in hunk["briefing"]


def test_render_report_to_console():
    console = Console(record=True, color_system=None)
    c1 = CommitAnalysis("1"*40, "11111111", "Commit 1", "UAT", "inferred", "reachable from uat", ("uat",), ())
    c2 = CommitAnalysis("2"*40, "22222222", "Commit 2", "UNVERIFIED", "inferred", "only on dev", ("dev",), ())
    briefing = ConflictBriefing(
        summary="Test summary",
        ours_assessment="Ours tested",
        theirs_assessment="Theirs untested",
        recommendation="Keep ours",
        reasoning="Test reasoning",
        source="llm",
        model="qwen2.5-coder:7b",
    )
    hunk = HunkAnalysis(
        file_path="app.py",
        ours=SideAnalysis("HEAD", ("print('ours')",), (c1,), "UAT"),
        theirs=SideAnalysis("incoming", ("print('theirs')",), (c2,), "UNVERIFIED"),
        recommendation="Keep ours",
        briefing=briefing,
    )
    rep = ConflictReport("merge", "1"*40, "2"*40, (hunk,))
    render_report(rep, console=console)
    output = console.export_text()
    assert "TrustMerge Conflict Analysis" in output
    assert "AI Conflict Briefing" in output
    assert "Test summary" in output
    assert "Keep ours" in output


def test_regression_d2_validate_briefing_rejects_false_claims_and_contradictions():
    from trustmerge.report import validate_briefing
    c_uat = CommitAnalysis("1"*40, "11111111", "c1", "UAT", "recorded", "recorded", (), ())
    c_unv = CommitAnalysis("2"*40, "22222222", "c2", "UNVERIFIED", "inferred", "inferred", (), ())

    ours = SideAnalysis("HEAD", ("a",), (c_uat,), weakest_level="UAT")
    theirs = SideAnalysis("incoming", ("b",), (c_unv,), weakest_level="UNVERIFIED")
    rule_rec = "Recommend OURS: UAT evidence vs UNVERIFIED on THEIRS."

    # 1. Briefing that falsely claims theirs is tested
    bad_briefing_1 = ConflictBriefing(
        summary="Summary",
        ours_assessment="Ours is UAT",
        theirs_assessment="Theirs is tested and verified on staging",
        recommendation="Recommend ours",
        reasoning="Reasoning",
        source="llm",
        model="test",
    )
    validated_1 = validate_briefing(bad_briefing_1, ours, theirs, rule_rec)
    assert validated_1.source == "rules (LLM output rejected)"
    assert validated_1.recommendation == rule_rec

    # 2. Briefing that contradicts rules (recommends theirs over UAT ours)
    bad_briefing_2 = ConflictBriefing(
        summary="Summary",
        ours_assessment="Ours",
        theirs_assessment="Theirs",
        recommendation="Prefer theirs changes",
        reasoning="Reasoning",
        source="llm",
        model="test",
    )
    validated_2 = validate_briefing(bad_briefing_2, ours, theirs, rule_rec)
    assert validated_2.source == "rules (LLM output rejected)"
    assert validated_2.recommendation == rule_rec


def test_regression_d2_ollama_failure_caught_and_displays_unavailable():
    class FailingLLM:
        def generate_briefing(self, **kwargs):
            raise ConnectionError("Ollama daemon down")

    console = Console(record=True, color_system=None)
    c1 = CommitAnalysis("1"*40, "11111111", "Commit 1", "UAT", "inferred", "reachable from uat", ("uat",), ())
    c2 = CommitAnalysis("2"*40, "22222222", "Commit 2", "UNVERIFIED", "inferred", "only on dev", ("dev",), ())
    briefing = ConflictBriefing(
        summary="Conflict summary",
        ours_assessment="Ours UAT",
        theirs_assessment="Theirs UNVERIFIED",
        recommendation="Recommend OURS",
        reasoning="Reasoning",
        source="rules",
        model="LLM unavailable: ConnectionError",
    )
    hunk = HunkAnalysis(
        file_path="app.py",
        ours=SideAnalysis("HEAD", ("print('ours')",), (c1,), "UAT"),
        theirs=SideAnalysis("incoming", ("print('theirs')",), (c2,), "UNVERIFIED"),
        recommendation="Recommend OURS",
        briefing=briefing,
    )
    rep = ConflictReport("merge", "1"*40, "2"*40, (hunk,))
    render_report(rep, console=console)
    output = console.export_text()
    assert "Rule-based briefing (LLM unavailable:" in output

