"""Conflict detection, hunk parsing, and side-origin finding via git blame."""

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Optional, Tuple

from trustmerge.git import GitError, rev_parse, run_git


@dataclass(frozen=True)
class ConflictSide:
    """One side of a conflict hunk."""

    label: str  # Label from conflict marker or branch/ref name
    lines: Tuple[str, ...]  # Content lines on this side
    commits: Tuple[str, ...]  # Commit SHAs that introduced these lines (via git blame)
    start_line: int  # 1-indexed start line in corresponding stage file
    end_line: int  # 1-indexed end line in corresponding stage file


@dataclass(frozen=True)
class ConflictHunk:
    """A single conflict block within a file."""

    file_path: str
    ours: ConflictSide
    theirs: ConflictSide
    base_lines: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ConflictedFile:
    """A file in conflicted merge state with all its parsed hunks."""

    path: str
    hunks: Tuple[ConflictHunk, ...]
    ours_head: str
    theirs_head: Optional[str]
    stage1_content: Optional[str] = None
    stage2_content: str = ""
    stage3_content: str = ""


@dataclass(frozen=True)
class ConflictState:
    """Overall conflict state of the repository."""

    operation: str  # "merge", "cherry-pick", "revert", or "conflict"
    files: Tuple[ConflictedFile, ...]
    ours_head: str
    theirs_head: Optional[str]


# ── detection helpers ─────────────────────────────────────────────────


def get_theirs_head(repo: Path) -> Optional[str]:
    """Return the incoming commit SHA for merge, cherry-pick, or revert."""
    for ref in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        out = run_git(repo, "rev-parse", "--verify", "--quiet", ref, check=False)
        if out:
            return out
    return None


def get_conflict_type(repo: Path) -> str:
    """Return the operation type causing the conflict."""
    if run_git(repo, "rev-parse", "--verify", "--quiet", "MERGE_HEAD", check=False):
        return "merge"
    if run_git(repo, "rev-parse", "--verify", "--quiet", "CHERRY_PICK_HEAD", check=False):
        return "cherry-pick"
    if run_git(repo, "rev-parse", "--verify", "--quiet", "REVERT_HEAD", check=False):
        return "revert"
    return "conflict"


def list_conflicted_paths(repo: Path) -> list[str]:
    """Return a list of file paths that currently have unmerged stages."""
    out = run_git(repo, "ls-files", "-u", check=False)
    paths: list[str] = []
    for line in out.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) >= 4:
            path = parts[3].strip()
            if path not in paths:
                paths.append(path)
    return paths


def is_conflicted(repo: Path) -> bool:
    """Return True if the repository currently has any unmerged files."""
    return bool(list_conflicted_paths(repo))


# ── git blame ─────────────────────────────────────────────────────────


def blame_range(
    repo: Path, commit: str, file_path: str, start_line: int, end_line: int
) -> list[str]:
    """Return unique commit SHAs in order that introduced lines in start_line..end_line."""
    if start_line > end_line or start_line < 1:
        return [commit] if commit else []
    try:
        out = run_git(
            repo,
            "blame",
            "-L",
            f"{start_line},{end_line}",
            "--porcelain",
            commit,
            "--",
            file_path,
        )
    except GitError:
        return [commit] if commit else []

    commits: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if parts and len(parts[0]) == 40:
            sha = parts[0]
            if sha not in commits:
                commits.append(sha)
    return commits or ([commit] if commit else [])


# ── parsing hunks ─────────────────────────────────────────────────────


def parse_conflicted_file(
    repo: Path, file_path: str, ours_head: str, theirs_head: Optional[str]
) -> ConflictedFile:
    """Parse conflict markers in *file_path* and attribute each side via git blame."""
    stage1 = run_git(repo, "show", f":1:{file_path}", check=False) or None
    stage2 = run_git(repo, "show", f":2:{file_path}", check=False)
    stage3 = run_git(repo, "show", f":3:{file_path}", check=False)

    disk_file = Path(repo) / file_path
    if not disk_file.is_file():
        # Modify/delete conflict
        ours_commits = [ours_head] if ours_head else []
        theirs_commits = [theirs_head] if theirs_head else []
        hunk = ConflictHunk(
            file_path=file_path,
            ours=ConflictSide("ours", (), tuple(ours_commits), 0, 0),
            theirs=ConflictSide("theirs", (), tuple(theirs_commits), 0, 0),
            base_lines=(),
        )
        return ConflictedFile(
            path=file_path,
            hunks=(hunk,),
            ours_head=ours_head,
            theirs_head=theirs_head,
            stage1_content=stage1,
            stage2_content=stage2,
            stage3_content=stage3,
        )

    try:
        raw_lines = disk_file.read_text(errors="replace").splitlines()
    except OSError:
        raw_lines = []

    hunks: list[ConflictHunk] = []
    i2, i3 = 0, 0
    in_ours, in_base, in_theirs = False, False, False
    ours_lines: list[str] = []
    theirs_lines: list[str] = []
    base_lines: list[str] = []
    ours_start, ours_label = 1, "HEAD"
    theirs_start, theirs_label = 1, (theirs_head or "theirs")

    for line in raw_lines:
        if line.startswith("<<<<<<<"):
            in_ours = True
            in_base = False
            in_theirs = False
            ours_lines = []
            theirs_lines = []
            base_lines = []
            parts = line.split(maxsplit=1)
            ours_label = parts[1].strip() if len(parts) > 1 else "HEAD"
            ours_start = i2 + 1
        elif line.startswith("|||||||"):
            in_ours = False
            in_base = True
            in_theirs = False
            base_lines = []
        elif line.startswith("======="):
            in_ours = False
            in_base = False
            in_theirs = True
            theirs_lines = []
            theirs_start = i3 + 1
        elif line.startswith(">>>>>>>"):
            in_ours = False
            in_base = False
            in_theirs = False
            parts = line.split(maxsplit=1)
            theirs_label = parts[1].strip() if len(parts) > 1 else (theirs_head or "theirs")

            ours_end = i2
            theirs_end = i3

            # Attribute commits via git blame
            if ours_lines and ours_head:
                ours_commits = blame_range(repo, ours_head, file_path, ours_start, ours_end)
            elif ours_head:
                ours_commits = [ours_head]
            else:
                ours_commits = []

            if theirs_lines and theirs_head:
                theirs_commits = blame_range(repo, theirs_head, file_path, theirs_start, theirs_end)
            elif theirs_head:
                theirs_commits = [theirs_head]
            else:
                theirs_commits = []

            hunks.append(
                ConflictHunk(
                    file_path=file_path,
                    ours=ConflictSide(
                        label=ours_label,
                        lines=tuple(ours_lines),
                        commits=tuple(ours_commits),
                        start_line=ours_start,
                        end_line=ours_end,
                    ),
                    theirs=ConflictSide(
                        label=theirs_label,
                        lines=tuple(theirs_lines),
                        commits=tuple(theirs_commits),
                        start_line=theirs_start,
                        end_line=theirs_end,
                    ),
                    base_lines=tuple(base_lines),
                )
            )
        elif in_ours:
            ours_lines.append(line)
            i2 += 1
        elif in_base:
            base_lines.append(line)
        elif in_theirs:
            theirs_lines.append(line)
            i3 += 1
        else:
            i2 += 1
            i3 += 1

    return ConflictedFile(
        path=file_path,
        hunks=tuple(hunks),
        ours_head=ours_head,
        theirs_head=theirs_head,
        stage1_content=stage1,
        stage2_content=stage2,
        stage3_content=stage3,
    )


def find_conflicts(repo: Path) -> Optional[ConflictState]:
    """Inspect repository and return complete ConflictState if conflicted, else None."""
    paths = list_conflicted_paths(repo)
    if not paths:
        return None

    ours_head = run_git(repo, "rev-parse", "HEAD", check=False)
    theirs_head = get_theirs_head(repo)
    operation = get_conflict_type(repo)

    files = [
        parse_conflicted_file(repo, p, ours_head=ours_head, theirs_head=theirs_head)
        for p in paths
    ]

    return ConflictState(
        operation=operation,
        files=tuple(files),
        ours_head=ours_head,
        theirs_head=theirs_head,
    )
