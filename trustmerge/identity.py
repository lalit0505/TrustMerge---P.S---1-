"""Find commits that are the same logical change on different branches."""
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from functools import lru_cache
from typing import Optional

from trustmerge.git import GitError, patch_id, rev_parse, run_git

_HASH_PATTERNS = [
    re.compile(r"cherry[ -]picked from commit ([0-9a-f]{7,40})", re.IGNORECASE),
    re.compile(r"cherry-picked from:?\s+([0-9a-f]{7,40})", re.IGNORECASE),
    re.compile(r"Backport(?:ed)? of (?:commit )?([0-9a-f]{7,40})", re.IGNORECASE),
    re.compile(r"\[\s*upstream commit ([0-9a-f]{7,40})\s*\]", re.IGNORECASE),
    re.compile(r"commit ([0-9a-f]{7,40}) upstream", re.IGNORECASE),
    re.compile(r"(?:origin|upstream):\s+([0-9a-f]{7,40})", re.IGNORECASE),
]
_PR_PATTERN = re.compile(r"\bGH-\d+\b")
_HASH_VALID_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)
_PR_VALID_RE = re.compile(r"^(GH-|#)\d+$")
BACKPORT_KEYWORDS = ("backport", "cherry", "picked", "upstream", "sync", "ported", "origin")

_llm_judge_calls: int = 0
_extract_ref_cache: dict[str, list[str]] = {}


def get_llm_judge_calls() -> int:
    """Return count of LLM judge calls made."""
    return _llm_judge_calls


def reset_llm_judge_calls() -> None:
    """Reset LLM judge calls counter."""
    global _llm_judge_calls
    _llm_judge_calls = 0


@dataclass(frozen=True)
class Match:
    commit: str
    branch: str
    method: str  # "patch-id", "reference", or "llm-judge"
    confidence: float = 0.0


def extract_references(
    message: str,
    use_llm: bool = False,
    client: Optional["LLMClient"] = None,
    repo: Optional[Path] = None,
) -> list[str]:
    """Return commit hashes and GH-<number> references in order of first appearance."""
    found: list[tuple[int, str]] = []
    for pattern in _HASH_PATTERNS:
        found += [(m.start(1), m.group(1)) for m in pattern.finditer(message)]
    found += [(m.start(), m.group(0)) for m in _PR_PATTERN.finditer(message)]
    refs: list[str] = []
    for _, ref in sorted(found):
        if ref not in refs:
            refs.append(ref)

    if not refs and use_llm:
        if message in _extract_ref_cache:
            return list(_extract_ref_cache[message])

        if any(kw in message.lower() for kw in BACKPORT_KEYWORDS):
            from trustmerge.llm import get_llm_client

            llm = client or get_llm_client()
            extracted = llm.extract_references_from_message(message)
            for h in extracted.commit_hashes:
                h_clean = h.strip()
                if not _HASH_VALID_RE.match(h_clean):
                    continue
                if repo is not None:
                    resolved = run_git(
                        repo, "rev-parse", "--verify", "--quiet", f"{h_clean}^{{commit}}", check=False
                    )
                    if not resolved:
                        continue
                if h_clean not in refs:
                    refs.append(h_clean)

            for pr in extracted.pr_numbers:
                pr_clean = pr.strip()
                if not _PR_VALID_RE.match(pr_clean):
                    continue
                if pr_clean not in refs:
                    refs.append(pr_clean)

            _extract_ref_cache[message] = list(refs)

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


@lru_cache(maxsize=32)
def _patch_ids_cached(repo_str: str, branch_head_sha: str, limit: int) -> dict[str, str]:
    """Map commit hash -> stable patch-id for the newest commits on a branch, keyed by head sha."""
    try:
        log = subprocess.run(
            ["git", "-c", "core.quotepath=false", "log", "-p", "--no-color", "--no-ext-diff", "-n", str(limit), branch_head_sha, "--"],
            cwd=repo_str, capture_output=True, text=True, errors="replace", check=True,
        )
        out = subprocess.run(
            ["git", "-c", "core.quotepath=false", "patch-id", "--stable"],
            cwd=repo_str, input=log.stdout, capture_output=True, text=True,
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


def _patch_ids(repo: Path, branch: str, limit: int) -> dict[str, str]:
    """Map commit hash -> stable patch-id for the newest commits on a branch, keyed by head sha."""
    head_sha = run_git(repo, "rev-parse", "--verify", "--quiet", branch, check=False).strip()
    if not head_sha:
        return {}
    return _patch_ids_cached(str(Path(repo).resolve()), head_sha, limit)


def find_equivalents(
    repo: Path,
    sha: str,
    branches: list[str],
    max_commits: int = 500,
    use_llm: bool = False,
    max_llm_candidates: int = 5,
    client: Optional["LLMClient"] = None,
) -> list[Match]:
    """Find commits on the given branches that are the same change as sha."""
    from trustmerge.git import is_ancestor
    from trustmerge.llm import get_llm_client, LLMClient

    sha = rev_parse(repo, sha)
    sha_refs = extract_references(
        run_git(repo, "log", "-1", "--format=%B", sha), use_llm=use_llm, client=client, repo=repo
    )
    try:
        sha_pid = patch_id(repo, sha)
    except (GitError, IndexError):
        sha_pid = None

    sha_files = (
        {
            f.strip()
            for f in run_git(repo, "show", "--name-only", "--format=", sha, check=False).splitlines()
            if f.strip()
        }
        if use_llm
        else set()
    )

    matches: list[Match] = []
    for branch in branches:
        # Skip patch-id scan entirely when commit is already an ancestor of the target branch
        already_ancestor = is_ancestor(repo, sha, branch)
        pids = {} if already_ancestor else _patch_ids(repo, branch, max_commits)
        branch_commits = _commits(repo, branch, max_commits)

        matched_candidates: set[str] = set()
        for cand, message in branch_commits:
            if cand == sha:
                continue
            cand_refs = extract_references(message, use_llm=False, repo=repo)
            if sha_pid is not None and pids.get(cand) == sha_pid:
                matches.append(Match(cand, branch, "patch-id"))
                matched_candidates.add(cand)
            elif _related(sha, sha_refs, cand, cand_refs):
                matches.append(Match(cand, branch, "reference"))
                matched_candidates.add(cand)

        if use_llm and sha_files:
            llm_candidates_tested = 0
            for cand, message in branch_commits:
                if cand == sha or cand in matched_candidates:
                    continue
                cand_files = {
                    f.strip()
                    for f in run_git(repo, "show", "--name-only", "--format=", cand, check=False).splitlines()
                    if f.strip()
                }
                if sha_files.intersection(cand_files):
                    llm_candidates_tested += 1
                    global _llm_judge_calls
                    _llm_judge_calls += 1

                    llm = client or get_llm_client()
                    patch_a = run_git(repo, "show", sha, check=False)
                    patch_b = run_git(repo, "show", cand, check=False)
                    msg_a = run_git(repo, "log", "-1", "--format=%B", sha, check=False)
                    try:
                        verdict = llm.judge_same_change(
                            patch_a=patch_a,
                            patch_b=patch_b,
                            message_a=msg_a,
                            message_b=message,
                        )
                        if verdict.same_change and verdict.confidence >= 0.70:
                            matches.append(Match(cand, branch, "llm-judge", confidence=verdict.confidence))
                    except Exception:
                        pass

                    if llm_candidates_tested >= max_llm_candidates:
                        break

    return matches
