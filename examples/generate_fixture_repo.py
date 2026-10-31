#!/usr/bin/env python3
"""Generate a simulated git repository with prod, uat, and dev branches in a conflicted merge state.

Usage:
    python examples/generate_fixture_repo.py [target_directory]
"""

from pathlib import Path
import subprocess
import sys

SETTINGS_V0 = (
    'DEBUG = False\n'
    'TIMEOUT = 30\n'
    'RETRIES = 3\n'
    'ALLOWED_HOSTS = ["example.com"]\n'
)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, capture_output=True, text=True
    )


def commit_file(repo: Path, filename: str, content: str, message: str) -> str:
    (repo / filename).write_text(content)
    git(repo, "add", filename)
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def build_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "prod")
    git(path, "config", "user.name", "TrustMerge Demo")
    git(path, "config", "user.email", "demo@trustmerge.local")
    git(path, "config", "commit.gpgsign", "false")

    # Initial baseline commit on prod
    commit_file(path, "settings.py", SETTINGS_V0, "Initial settings baseline")
    git(path, "branch", "uat")
    git(path, "branch", "dev")

    # PROD: a hotfix that is running in production
    hotfix_sha = commit_file(
        path,
        "settings.py",
        SETTINGS_V0.replace("TIMEOUT = 30", "TIMEOUT = 60"),
        "Fix slow upstream timeout (hotfix #812)",
    )

    # UAT: backport with hand edit so patch-id differs, but trailer references hotfix
    git(path, "switch", "-q", "uat")
    git(path, "cherry-pick", "-n", hotfix_sha)
    with open(path / "settings.py", "a") as f:
        f.write("# backported from prod hotfix #812\n")
    git(path, "add", "settings.py")
    git(
        path,
        "commit",
        "-m",
        f"Fix slow upstream timeout (hotfix #812)\n\n(cherry picked from commit {hotfix_sha})\n",
    )
    backport_sha = git(path, "rev-parse", "HEAD").stdout.strip()

    # DEV: a different, untested change to the same line
    git(path, "switch", "-q", "dev")
    dev_sha = commit_file(
        path,
        "settings.py",
        SETTINGS_V0.replace("TIMEOUT = 30", "TIMEOUT = 45"),
        "Tune timeout (PR #845)",
    )

    # Trigger a real conflict by merging dev into uat
    git(path, "switch", "-q", "uat")
    git(path, "merge", "dev", "--no-edit", check=False)

    print(f"✅ Demo repository created at: {path.resolve()}")
    print("  Branches: prod, uat (active conflict), dev")
    print(f"  PROD hotfix commit: {hotfix_sha[:8]}")
    print(f"  UAT backport commit: {backport_sha[:8]}")
    print(f"  DEV untested commit: {dev_sha[:8]}")
    print("\nTo inspect with TrustMerge, run:")
    print(f"  git-prov analyze --repo {path.resolve()}")
    return path


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("demo_repo")
    build_repo(target)
