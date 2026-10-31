"""Find commits that are the same logical change on different branches."""
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from typing import Optional

from trustmerge.git import GitError, patch_id, rev_parse, run_git

_HASH_PATTERNS = [
    re.compile(r"cherry picked from commit ([0-9a-f]{7,40})"),
    re.compile(r"Backport of ([0-9a-f]{7,40})"),
]
_PR_PATTERN = re.compile(r"\bGH-\d+\b")


@dataclass(frozen=True)
class Match:
    commit: str
    branch: str
    method: str  # "patch-id", "reference", or "llm-judge"


def extract_references(message: str) -> list[str]:
    """Return commit hashes and GH-<number> references in order of first appearance."""
    found: list[tuple[int, str]] = []
    for pattern in _HASH_PATTERNS:
        found += [(m.start(1), m.group(1)) for m in pattern.finditer(message)]
    found += [(m.start(), m.group(0)) for m in _PR_PATTERN.finditer(message)]
    refs: list[str] = []
    for _, ref in sorted(found):
        if ref not in refs:
            refs.append(ref)
    return refs


def _is_pr(ref: str) -> bool:
    return ref.startswith("GH-")


def _related(sha: str, sha_refs: list[str], cand: str, cand_refs: list[str]) -> bool:
    """True if the messages show the two commits are the same change."""
    if any(not _is_pr(r) and sha.startswith(r) for r in cand_refs):
        return True  # candidate was cherry-picked from sha
    if any(not _is_pr(r) and cand.startswith(r) for r in sha_refs):
        return True  # sha was cherry-picked from candidate
    for a in sha_refs:
        for b in cand_refs:
            if _is_pr(a) or _is_pr(b):
                if a == b:
                    return True  # same PR number
            elif a.startswith(b) or b.startswith(a):
                return True  # both cite the same original commit
    return False


def _commits(repo: Path, branch: str, limit: int) -> list[tuple[str, str]]:
    out = run_git(repo, "log", "-n", str(limit), "--format=%H%x1f%B%x1e", branch, "--")
    commits = []
    for chunk in out.split("\x1e"):
        chunk = chunk.strip()
        if chunk:
            commit, _, body = chunk.partition("\x1f")
            commits.append((commit.strip(), body))
    return commits


def _patch_ids(repo: Path, branch: str, limit: int) -> dict[str, str]:
    """Map commit hash -> stable patch-id for the newest commits on a branch."""
    try:
        log = subprocess.run(
            ["git", "log", "-p", "--no-color", "--no-ext-diff", "-n", str(limit), branch, "--"],
            cwd=repo, capture_output=True, text=True, errors="replace", check=True,
        )
        out = subprocess.run(
            ["git", "patch-id", "--stable"],
            cwd=repo, input=log.stdout, capture_output=True, text=True,
            errors="replace", check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise GitError(exc.stderr) from exc
    result: dict[str, str] = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            result[parts[1]] = parts[0]
    return result


def find_equivalents(
    repo: Path,
    sha: str,
    branches: list[str],
    max_commits: int = 500,
    use_llm: bool = False,
    client: Optional["LLMClient"] = None,
) -> list[Match]:
    """Find commits on the given branches that are the same change as sha."""
    from trustmerge.llm import get_llm_client, LLMClient

    sha = rev_parse(repo, sha)
    sha_refs = extract_references(run_git(repo, "log", "-1", "--format=%B", sha))
    try:
        sha_pid = patch_id(repo, sha)
    except (GitError, IndexError):
        sha_pid = None

    sha_files = (
        set(run_git(repo, "show", "--name-only", "--format=", sha, check=False).splitlines())
        if use_llm
        else set()
    )

    matches: list[Match] = []
    for branch in branches:
        pids = _patch_ids(repo, branch, max_commits)
        for cand, message in _commits(repo, branch, max_commits):
            if cand == sha:
                continue
            if sha_pid is not None and pids.get(cand) == sha_pid:
                method = "patch-id"
            elif _related(sha, sha_refs, cand, extract_references(message)):
                method = "reference"
            elif use_llm:
                cand_files = set(
                    run_git(repo, "show", "--name-only", "--format=", cand, check=False).splitlines()
                )
                if sha_files.intersection(cand_files):
                    llm = client or get_llm_client()
                    patch_a = run_git(repo, "show", sha, check=False)
                    patch_b = run_git(repo, "show", cand, check=False)
                    msg_a = run_git(repo, "log", "-1", "--format=%B", sha, check=False)
                    verdict = llm.judge_same_change(
                        patch_a=patch_a,
                        patch_b=patch_b,
                        message_a=msg_a,
                        message_b=message,
                    )
                    if verdict.same_change and verdict.confidence >= 0.70:
                        method = "llm-judge"
                    else:
                        continue
                else:
                    continue
            else:
                continue
            matches.append(Match(cand, branch, method))
    return matches
