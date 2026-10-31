"""Tests for the ``scan`` command and CLI integration."""

import json

import typer
from typer.testing import CliRunner

from trustmerge.scan import register, scan_branch

app = typer.Typer()
register(app)
runner = CliRunner()


def test_scan_dev_lists_only_commits_not_on_prod(scenario):
    rows = scan_branch(scenario.repo, "dev")
    shas = [r.sha for r in rows]
    assert scenario.dev_change in shas
    assert rows[0].evidence.level == "UNVERIFIED"
    assert rows[0].evidence.reached == ("dev",)


def test_scan_uat_reports_uat_with_related(scenario):
    rows = scan_branch(scenario.repo, "uat")
    shas = [r.sha for r in rows]
    assert scenario.backport in shas
    assert rows[0].evidence.level == "UAT"
    assert rows[0].evidence.related


def test_scan_respects_limit(scenario):
    rows = scan_branch(scenario.repo, "prod", limit=1, base="uat")
    assert len(rows) == 1


def test_format_rows_empty():
    from trustmerge.scan import format_rows
    assert format_rows([]) == "no commits to scan"


def test_cli_scan_text(scenario):
    result = runner.invoke(app, ["dev", "--repo", str(scenario.repo)])
    assert result.exit_code == 0
    assert "UNVERIFIED" in result.output
    assert "reached: dev" in result.output
    assert scenario.dev_change[:8] in result.output


def test_cli_scan_json(scenario):
    result = runner.invoke(app, ["uat", "--repo", str(scenario.repo), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["sha"] == scenario.backport
    assert data[0]["level"] == "UAT"
    assert data[0]["reached"] == ["uat"]
    assert data[0]["related"]


def test_cli_scan_bad_repo(tmp_path):
    result = runner.invoke(app, ["dev", "--repo", str(tmp_path)])
    assert result.exit_code == 1
