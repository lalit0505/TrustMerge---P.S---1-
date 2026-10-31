"""Builds a simulated prod/uat/dev repo for tests and demos."""
import subprocess
from dataclasses import dataclass
from pathlib import Path

SETTINGS_V0 = 'DEBUG = False\nTIMEOUT = 30\nRETRIES = 3\nALLOWED_HOSTS = ["example.com"]\n'


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, capture_output=True, text=True
    )


def patch_id(repo, sha):
    diff = git(repo, "show", sha).stdout
    out = subprocess.run(
        ["git", "patch-id", "--stable"],
        cwd=repo, input=diff, capture_output=True, text=True, check=True,
    ).stdout
    return out.split()[0]


@dataclass
class Scenario:
    repo: Path
    initial: str
    hotfix: str       # on prod, tested in production
    backport: str     # on uat, cherry-picked with a trailer, edited by hand
    dev_change: str   # on dev, never tested


def _commit_file(repo, content, message):
    (repo / "settings.py").write_text(content)
    git(repo, "add", "settings.py")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def build_scenario(path: Path) -> Scenario:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "prod")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "commit.gpgsign", "false")

    initial = _commit_file(path, SETTINGS_V0, "Initial settings")
    git(path, "branch", "uat")
    git(path, "branch", "dev")

    # PROD: the hotfix that is running in production
    hotfix = _commit_file(
        path, SETTINGS_V0.replace("TIMEOUT = 30", "TIMEOUT = 60"),
        "Fix slow upstream timeout (hotfix #812)",
    )

    # UAT: backport with a hand edit, so the patch-id differs from the original
    git(path, "switch", "-q", "uat")
    git(path, "cherry-pick", "-n", hotfix)
    with open(path / "settings.py", "a") as f:
        f.write("# backported from prod hotfix #812\n")
    git(path, "add", "settings.py")
    git(
        path, "commit", "-m",
        f"Fix slow upstream timeout (hotfix #812)\n\n(cherry picked from commit {hotfix})",
    )
    backport = git(path, "rev-parse", "HEAD").stdout.strip()

    # DEV: a different, untested change to the same line
    git(path, "switch", "-q", "dev")
    dev_change = _commit_file(
        path, SETTINGS_V0.replace("TIMEOUT = 30", "TIMEOUT = 45"),
        "Tune timeout (PR #845)",
    )

    git(path, "switch", "-q", "prod")
    return Scenario(path, initial, hotfix, backport, dev_change)


def start_conflict(s: Scenario):
    """Promote dev into uat. This leaves the repo in a conflicted merge."""
    git(s.repo, "switch", "-q", "uat")
    return git(s.repo, "merge", "dev", "--no-edit", check=False)
