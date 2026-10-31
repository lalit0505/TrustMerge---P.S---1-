"""Infer testing evidence for a commit from branch reachability and identity matching."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from trustmerge.git import GitError, is_ancestor, rev_parse, run_git
from trustmerge.identity import find_equivalents
from trustmerge.levels import CI_PASSED, LEVEL_ORDER, PROD, UAT, UNKNOWN, UNVERIFIED, rank
from trustmerge.notes import get_recorded_evidence


@dataclass(frozen=True)
class Config:
    """Branch names that map to environments."""

    prod: str = "prod"
    uat: str = "uat"
    dev: str = "dev"
    max_commits: int = 300


@dataclass(frozen=True)
class Evidence:
    """What the tool knows about a commit's testing status."""

    sha: str
    level: str
    source: str
    detail: str
    related: Tuple[str, ...] = ()
    reached: Tuple[str, ...] = ()


def short(sha: str) -> str:
    """First eight characters of a commit hash."""
    return sha[:8]


def resolve_ref(repo: Path, name: str) -> Optional[str]:
    """Return a full refname for *name* (local first, then remote), or ``None``."""
    for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
        out = run_git(repo, "rev-parse", "--verify", "--quiet", ref, check=False)
        if out:
            return ref
    return None


def infer_evidence(repo: Path, sha: str, config: Config = Config()) -> Evidence:
    """Determine the evidence level for *sha* using only Git data (no notes)."""
    sha = rev_parse(repo, sha)
    environments = [(PROD, config.prod), (UAT, config.uat), (None, config.dev)]
    chosen = None
    reached: list[str] = []
    related: list[str] = []

    for level, name in environments:
        ref = resolve_ref(repo, name)
        if ref is None:
            continue

        detail = None
        if is_ancestor(repo, sha, ref):
            detail = f"reachable from {name}"

        for match in find_equivalents(repo, sha, [ref], max_commits=config.max_commits):
            if match.method == "patch-id" and detail is None:
                detail = f"identical change {short(match.commit)} is on {name}"
            elif match.method == "reference" and level is not None:
                related.append(
                    f"same logical change as {short(match.commit)} on {name}"
                    " (code may differ, verify separately)"
                )

        if detail is None:
            continue
        reached.append(name)
        if level is not None and chosen is None:
            chosen = (level, detail)

    if chosen is not None:
        return Evidence(
            sha, chosen[0], "inferred", chosen[1],
            tuple(related), tuple(reached),
        )
    if config.dev in [r for r in reached]:
        return Evidence(
            sha, UNVERIFIED, "inferred",
            f"only on {config.dev}; no evidence it was tested",
            tuple(related), tuple(reached),
        )
    return Evidence(
        sha, UNKNOWN, "inferred",
        f"not found on {config.prod}, {config.uat} or {config.dev}",
        tuple(related), (),
    )


def assess(repo: Path, sha: str, config: Config = Config()) -> Evidence:
    """Public entry-point: return evidence for *sha*.

    Checks recorded git notes and evidence files first.
    Falls back to Git inference when no recorded evidence is present.
    """
    sha = rev_parse(repo, sha)
    inferred = infer_evidence(repo, sha, config)
    recorded = get_recorded_evidence(repo, sha)
    if not recorded:
        return inferred

    # Pick the strongest recorded evidence
    best = min(
        recorded,
        key=lambda r: rank(r.level.upper()) if r.level.upper() in LEVEL_ORDER else 999,
    )
    rec_level = best.level.upper()

    if rank(rec_level) <= rank(inferred.level):
        return Evidence(
            sha=sha,
            level=rec_level,
            source="recorded",
            detail=best.evidence,
            related=inferred.related,
            reached=inferred.reached,
        )
    else:
        # Inferred evidence is stronger (e.g. already in PROD)
        related_extra = (f"recorded {rec_level}: {best.evidence}",)
        return Evidence(
            sha=sha,
            level=inferred.level,
            source=inferred.source,
            detail=inferred.detail,
            related=related_extra + inferred.related,
            reached=inferred.reached,
        )
