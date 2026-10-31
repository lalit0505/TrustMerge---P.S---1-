import os
import subprocess
from pathlib import Path
from typing import List, Optional, Union

class GitError(Exception):
    """Exception raised when a git command fails."""
    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr

def run_git(repo: Union[str, Path], *args: str, check: bool = True) -> str:
    """
    Run `git <args>` in directory `repo` with subprocess (capture_output, text, utf-8, replace).
    Always passes `-c core.quotepath=false` and sets GIT_TERMINAL_PROMPT=0.
    Return stdout with trailing whitespace stripped. If the command fails and check is True,
    raise GitError that includes stderr. If check is False, never raise and return stdout.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    stdout = result.stdout.strip()
    if result.returncode != 0 and check:
        raise GitError(f"Git command failed: {' '.join(args)}", result.stderr)
    return stdout

def list_branches(repo: Union[str, Path]) -> List[str]:
    """
    Local branch names, using `git branch --format=%(refname:short)`.
    """
    return run_git(repo, "branch", "--format=%(refname:short)").splitlines()

def rev_parse(repo: Union[str, Path], ref: str) -> str:
    """
    Full commit hash for a ref.
    """
    return run_git(repo, "rev-parse", ref)

def is_ancestor(repo: Union[str, Path], ancestor: str, descendant: str) -> bool:
    """
    Use `git merge-base --is-ancestor`; exit code 0 means True, 1 means False, anything else raises GitError.
    """
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode == 0:
        return True
    elif result.returncode == 1:
        return False
    else:
        raise GitError(f"Git command failed: git merge-base --is-ancestor {ancestor} {descendant}", result.stderr)

def patch_id(repo: Union[str, Path], sha: str) -> str:
    """
    Run `git show <sha>`, pass its output on stdin to `git patch-id --stable` via subprocess,
    and return the first token of the output.
    """
    diff = run_git(repo, "show", sha)
    if not diff.strip():
        return ""
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotepath=false", "patch-id", "--stable"],
            cwd=repo,
            input=diff,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise GitError(f"Git command failed: git patch-id --stable for {sha}", exc.stderr or "")
    tokens = result.stdout.split()
    return tokens[0] if tokens else ""

