"""Performance tests for scale and caching (D4)."""

import time
import subprocess
from pathlib import Path
from typer.testing import CliRunner

from fixtures import git
from trustmerge.cli import app
from trustmerge.scan import scan_branch

runner = CliRunner()


def test_regression_d4_perf_200_commits(tmp_path):
    repo = tmp_path / "perf_repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Perf Test")
    git(repo, "config", "user.email", "perf@example.com")

    lines = []
    mark = 1
    for i in range(200):
        lines.append("commit refs/heads/main")
        lines.append(f"mark :{mark}")
        lines.append("committer Perf <perf@example.com> 1000000000 +0000")
        lines.append(f"data {len(f'commit {i}')}")
        lines.append(f"commit {i}")
        if mark > 1:
            lines.append(f"from :{mark-1}")
        lines.append(f"M 644 inline file_{i % 5}.txt")
        lines.append(f"data {len(f'content {i}')}")
        lines.append(f"content {i}")
        mark += 1

    main_tip_mark = mark - 1
    for i in range(20):
        lines.append("commit refs/heads/dev")
        lines.append(f"mark :{mark}")
        lines.append("committer Perf <perf@example.com> 1000000000 +0000")
        lines.append(f"data {len(f'dev commit {i}')}")
        lines.append(f"dev commit {i}")
        if i == 0:
            lines.append(f"from :{main_tip_mark}")
        else:
            lines.append(f"from :{mark-1}")
        lines.append("M 644 inline dev_file.txt")
        lines.append(f"data {len(f'dev {i}')}")
        lines.append(f"dev {i}")
        mark += 1

    subprocess.run(
        ["git", "fast-import", "--quiet"],
        cwd=repo,
        input="\n".join(lines) + "\n",
        text=True,
        check=True,
    )
    git(repo, "checkout", "dev")

    # Time scan --limit 20
    start = time.perf_counter()
    res = runner.invoke(app, ["scan", "dev", "--limit", "20", "--no-llm", "--repo", str(repo)])
    elapsed = time.perf_counter() - start

    assert res.exit_code == 0
    # Must complete in under 10 seconds without re-hashing
    assert elapsed < 10.0, f"Scan took {elapsed:.2f}s, expected < 10.0s"
