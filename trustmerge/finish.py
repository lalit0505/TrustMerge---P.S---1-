"""``git prov finish`` — finalize conflict resolution with standard trailers and a note."""

from pathlib import Path
import re
from typing import Optional, Tuple

import typer

from trustmerge.conflicts import get_theirs_head, is_conflicted, list_conflicted_paths
from trustmerge.git import GitError, rev_parse, run_git
from trustmerge.levels import CI_PASSED, PROD, RECORDABLE, UAT, rank
from trustmerge.notes import NoteRecord, write_note
from trustmerge.provenance import assess


def _sanitize_trailer_value(val: Optional[str]) -> Optional[str]:
    if val is None:
        return None
    return re.sub(r"[\r\n\x00-\x1f\x7f-\x9f]+", " ", val).strip()



def _merge_trailers(base_msg: str, new_trailers: list[tuple[str, str]]) -> str:
    lines = base_msg.rstrip().splitlines()

    trailer_start = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if not line:
            trailer_start = i + 1
            break
        if ":" in line and re.match(r"^[A-Za-z0-9_-]+:\s*", line):
            trailer_start = i
        else:
            trailer_start = i + 1
            break

    existing_body = lines[:trailer_start]
    existing_trailers = lines[trailer_start:]

    while existing_body and not existing_body[-1].strip():
        existing_body.pop()

    final_trailers: list[str] = []
    seen_keys: set[str] = set()
    new_dict = {k.lower(): f"{k}: {v}" for k, v in new_trailers}

    for t_line in existing_trailers:
        key = t_line.split(":", 1)[0].strip().lower()
        if key in new_dict:
            final_trailers.append(new_dict[key])
            seen_keys.add(key)
        else:
            final_trailers.append(t_line)
            seen_keys.add(key)

    for k, v in new_trailers:
        if k.lower() not in seen_keys:
            final_trailers.append(f"{k}: {v}")
            seen_keys.add(k.lower())

    # Ensure "Tested-in:" appears at most once
    tested_in_indices = [
        idx for idx, line in enumerate(final_trailers)
        if line.strip().lower().startswith("tested-in:")
    ]
    if len(tested_in_indices) > 1:
        for idx in reversed(tested_in_indices[:-1]):
            del final_trailers[idx]

    body_str = "\n".join(existing_body)
    trailers_str = "\n".join(final_trailers)

    if body_str and trailers_str:
        return f"{body_str}\n\n{trailers_str}\n"
    elif trailers_str:
        return f"{trailers_str}\n"
    else:
        return f"{body_str}\n"



def _get_kept_commits(
    repo: Path,
    ours_head: str,
    theirs_head: Optional[str],
    staged_files: list[str],
) -> list[str]:
    """Find the commits belonging to the sides that were kept in the staged resolution."""
    if not theirs_head:
        return [ours_head]

    base = run_git(repo, "merge-base", ours_head, theirs_head, check=False).strip()

    diff_ours = run_git(repo, "diff", "--cached", ours_head, check=False).strip()
    diff_theirs = run_git(repo, "diff", "--cached", theirs_head, check=False).strip()

    if not diff_ours and diff_theirs:
        # Kept ours exactly
        if base:
            c = run_git(repo, "rev-list", f"{base}..{ours_head}", check=False).splitlines()
            return [x.strip() for x in c if x.strip()] or [ours_head]
        return [ours_head]

    if not diff_theirs and diff_ours:
        # Kept theirs exactly
        if base:
            c = run_git(repo, "rev-list", f"{base}..{theirs_head}", check=False).splitlines()
            return [x.strip() for x in c if x.strip()] or [theirs_head]
        return [theirs_head]

    ours_kept = False
    theirs_kept = False

    if base:
        staged_lines = set()
        for f in staged_files:
            try:
                content = run_git(repo, "show", f":0:{f}", check=False)
                for line in content.splitlines():
                    s = line.strip()
                    if s:
                        staged_lines.add(s)
            except Exception:
                pass

        diff_theirs_added = [
            line[1:].strip()
            for line in run_git(repo, "diff", base, theirs_head, check=False).splitlines()
            if line.startswith("+") and not line.startswith("+++") and line[1:].strip()
        ]
        diff_ours_added = [
            line[1:].strip()
            for line in run_git(repo, "diff", base, ours_head, check=False).splitlines()
            if line.startswith("+") and not line.startswith("+++") and line[1:].strip()
        ]

        for line in diff_theirs_added:
            if line in staged_lines:
                theirs_kept = True
                break

        for line in diff_ours_added:
            if line in staged_lines:
                ours_kept = True
                break

    if not ours_kept and not theirs_kept:
        ours_kept = True
        theirs_kept = True

    kept_commits: list[str] = []
    if ours_kept:
        if base:
            c = run_git(repo, "rev-list", f"{base}..{ours_head}", check=False).splitlines()
            kept_commits.extend([x.strip() for x in c if x.strip()] or [ours_head])
        else:
            kept_commits.append(ours_head)
    if theirs_kept:
        if base:
            c = run_git(repo, "rev-list", f"{base}..{theirs_head}", check=False).splitlines()
            kept_commits.extend([x.strip() for x in c if x.strip()] or [theirs_head])
        else:
            kept_commits.append(theirs_head)

    return kept_commits or [ours_head]


def _git_path(repo: Path, name: str) -> Path:
    """Return worktree-safe path for a git metadata file."""
    rel = run_git(repo, "rev-parse", "--git-path", name, check=False).strip()
    if not rel:
        return Path(repo) / ".git" / name
    p = Path(rel)
    return p if p.is_absolute() else Path(repo) / p


MARKER_SEQUENCE_RE = re.compile(
    r"^<<<<<<< [^\r\n]*\r?\n.*?\r?\n^=======\r?\n.*?\r?\n^>>>>>>> [^\r\n]*",
    re.MULTILINE | re.DOTALL,
)


def finish_conflict(
    repo: Path,
    message: Optional[str] = None,
    tested_in: Optional[str] = None,
    evidence: Optional[str] = None,
    provenance: Optional[str] = None,
    add_all: bool = False,
    force_claim: bool = False,
) -> str:
    """Create a standard Git commit with Provenance and Tested-in trailers and a note.

    Returns the new commit hash.
    """
    if add_all:
        pre = set(
            f.strip()
            for f in run_git(repo, "diff", "--cached", "--name-only", check=False).splitlines()
            if f.strip()
        )
        run_git(repo, "add", "-A")
        post = set(
            f.strip()
            for f in run_git(repo, "diff", "--cached", "--name-only", check=False).splitlines()
            if f.strip()
        )
        newly_staged = sorted(post - pre)
        files_to_report = newly_staged if newly_staged else sorted(post)
        if files_to_report:
            typer.echo(f"Staged {len(files_to_report)} file(s): {', '.join(files_to_report)}")

    if is_conflicted(repo):
        unmerged = list_conflicted_paths(repo)
        raise GitError(
            f"Unmerged conflict markers remain in: {', '.join(unmerged)}. "
            "Please resolve all conflicts and stage files before finishing.",
            stderr="",
        )

    staged_output = run_git(repo, "diff", "--cached", "--name-only", check=False)
    staged_files = [f.strip() for f in staged_output.splitlines() if f.strip()]

    # Verify no unresolved conflict markers were accidentally staged (real marker sequence at line start)
    for rel_path in staged_files:
        fpath = Path(repo) / rel_path
        if fpath.is_file():
            try:
                content = fpath.read_text(errors="replace")
                if MARKER_SEQUENCE_RE.search(content):
                    raise GitError(
                        f"Unresolved conflict markers remain inside {rel_path}.", stderr=""
                    )
            except OSError:
                pass

    theirs_head = get_theirs_head(repo)
    merge_head_file = _git_path(repo, "MERGE_HEAD")
    cherry_pick_head_file = _git_path(repo, "CHERRY_PICK_HEAD")
    rebase_head_file = _git_path(repo, "REBASE_HEAD")

    is_merging = merge_head_file.is_file() or cherry_pick_head_file.is_file() or rebase_head_file.is_file()
    if not staged_files and not is_merging:
        raise GitError("No staged changes to commit.", stderr="")

    ours_head = run_git(repo, "rev-parse", "HEAD", check=False)

    if provenance:
        provenance = _sanitize_trailer_value(provenance)

    norm_level: Optional[str] = None
    if tested_in is not None:
        tested_in = _sanitize_trailer_value(tested_in)
        if not evidence or not evidence.strip():
            raise GitError(
                "--evidence is required when --tested-in is specified.",
                stderr="",
            )
        candidate_level = tested_in.strip().upper().replace("_", "-")
        if candidate_level not in RECORDABLE:
            raise GitError(
                f"Invalid level '{tested_in}'. Allowed levels: {', '.join(sorted(RECORDABLE))}.",
                stderr="",
            )
        norm_level = candidate_level

        # Cap the claim
        kept_commits = _get_kept_commits(repo, ours_head, theirs_head, staged_files)
        weakest_commit = max(kept_commits, key=lambda c: rank(assess(repo, c).level))
        weakest_level = assess(repo, weakest_commit).level

        if rank(norm_level) < rank(weakest_level):
            if not force_claim:
                raise GitError(
                    f"Claimed level '{norm_level}' is stronger than the weakest level among "
                    f"commits of the kept sides ('{weakest_level}' on {weakest_commit[:8]}). "
                    "Use --force-claim to override.",
                    stderr="",
                )

    # Determine commit subject
    if message and message.strip():
        base_msg = message.strip()
    else:
        merge_msg_file = _git_path(repo, "MERGE_MSG")
        if merge_msg_file.is_file():
            clean_lines = [
                l for l in merge_msg_file.read_text(encoding="utf-8", errors="replace").splitlines()
                if not l.startswith("#")
            ]
            base_msg = "\n".join(clean_lines).strip() or "Resolve conflict"
        elif theirs_head:
            base_msg = f"Resolve conflict with {theirs_head[:8]}"
        else:
            base_msg = "Resolve conflict"


    # Determine trailers
    if not provenance:
        if theirs_head and ours_head:
            provenance = f"{ours_head[:8]} (ours), {theirs_head[:8]} (theirs)"
        elif ours_head:
            provenance = f"{ours_head[:8]}"
        else:
            provenance = "manual resolution"

    trailers_to_add: list[tuple[str, str]] = [("Provenance", provenance)]
    if norm_level is not None:
        trailers_to_add.append(("Tested-in", norm_level.lower()))

    full_message = _merge_trailers(base_msg, trailers_to_add)

    # Create the Git commit
    run_git(repo, "commit", "-m", full_message)
    new_sha = run_git(repo, "rev-parse", "HEAD")

    # Record provenance note on the new commit if tested_in was provided
    if norm_level is not None and evidence is not None:
        record = NoteRecord(level=norm_level, evidence=evidence.strip())
        write_note(repo, new_sha, record)

    return new_sha


def register(app: typer.Typer) -> None:
    """Add ``finish`` sub-command to *app*."""

    @app.command()
    def finish(
        message: Optional[str] = typer.Option(
            None, "--message", "-m", help="Commit message (defaults to merge message)"
        ),
        tested_in: Optional[str] = typer.Option(
            None, "--tested-in", "-t", help="Tested environment trailer (e.g. uat, prod)"
        ),
        evidence: Optional[str] = typer.Option(
            None, "--evidence", "-e", help="Evidence text supporting the tested-in claim"
        ),
        force_claim: bool = typer.Option(
            False,
            "--force-claim",
            help="Allow claiming an evidence level stronger than the commits of the kept sides",
        ),
        provenance: Optional[str] = typer.Option(
            None, "--provenance", "-p", help="Provenance description/trailer content"
        ),
        add_all: bool = typer.Option(
            False, "--all", "-a", help="Stage all modified files before finishing"
        ),
        repo: Path = typer.Option(Path("."), "--repo", help="Path to git repository"),
    ) -> None:
        """Create final resolution commit with standard trailers and provenance note."""
        try:
            new_sha = finish_conflict(
                repo=repo,
                message=message,
                tested_in=tested_in,
                evidence=evidence,
                provenance=provenance,
                add_all=add_all,
                force_claim=force_claim,
            )
            typer.echo(f"Created resolution commit {new_sha[:8]} with trailers and note.")
        except GitError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1)

