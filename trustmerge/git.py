import subprocess
from typing import List, Optional

class GitError(Exception):
    """Exception raised when a git command fails."""
    def __init__(self, message: str, stderr: str):
        super().__init__(message)
        self.stderr = stderr

def run_git(repo: str, *args: str, check: bool = True) -> str:
    """
    Run `git <args>` in directory `repo` with subprocess (capture_output, text).
    Return stdout with trailing whitespace stripped. If the command fails and check is True,
    raise GitError that includes stderr. If check is False, never raise and return stdout.
    """
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True
    )
    stdout = result.stdout.strip()
    if result.returncode != 0 and check:
        raise GitError(f"Git command failed: {' '.join(args)}", result.stderr)
    return stdout

def list_branches(repo: str) -> List[str]:
    """
    Local branch names, using `git branch --format=%(refname:short)`.
    """
    return run_git(repo, "branch", "--format=%(refname:short)").splitlines()

def rev_parse(repo: str, ref: str) -> str:
    """
    Full commit hash for a ref.
    """
    return run_git(repo, "rev-parse", ref)

def is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    """
    Use `git merge-base --is-ancestor`; exit code 0 means True, 1 means False, anything else raises GitError.
    """
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=repo, capture_output=True, text=True
    )
    if result.returncode == 0:
        return True
    elif result.returncode == 1:
        return False
    else:
        raise GitError(f"Git command failed: git merge-base --is-ancestor {ancestor} {descendant}", result.stderr)

def patch_id(repo: str, sha: str) -> str:
    """
    Run `git show <sha>`, pass its output on stdin to `git patch-id --stable` via subprocess,
    and return the first token of the output.
    """
    diff = run_git(repo, "show", sha)
    result = subprocess.run(
        ["git", "patch-id", "--stable"], input=diff, capture_output=True, text=True, check=True
    )
    return result.stdout.split()[0]
