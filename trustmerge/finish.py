"""``git prov finish`` — finalize conflict resolution with standard trailers and a note."""

from pathlib import Path
from typing import Optional

import typer

from trustmerge.conflicts import get_theirs_head, is_conflicted, list_conflicted_paths
from trustmerge.git import GitError, rev_parse, run_git
from trustmerge.levels import CI_PASSED, PROD, RECORDABLE, UAT
from trustmerge.notes import NoteRecord, write_note


def finish_conflict(
    repo: Path,
    message: Optional[str] = None,
    tested_in: str = "uat",
    provenance: Optional[str] = None,
    add_all: bool = False,
) -> str:
    """Create a standard Git commit with Provenance and Tested-in trailers and a note.

    Returns the new commit hash.
    """
    if add_all:
        run_git(repo, "add", "-A")

    if is_conflicted(repo):
        unmerged = list_conflicted_paths(repo)
        raise GitError(
            f"Unmerged conflict markers remain in: {', '.join(unmerged)}. "
            "Please resolve all conflicts and stage files before finishing.",
            stderr="",
        )

    staged_output = run_git(repo, "diff", "--cached", "--name-only", check=False)
    staged_files = [f.strip() for f in staged_output.splitlines() if f.strip()]

    # Verify no unresolved conflict markers were accidentally staged
    for rel_path in staged_files:
        fpath = Path(repo) / rel_path
        if fpath.is_file():
            try:
                content = fpath.read_text(errors="replace")
                if "<<<<<<<" in content and "=======" in content and ">>>>>>>" in content:
                    raise GitError(
                        f"Unresolved conflict markers remain inside {rel_path}.", stderr=""
                    )
            except OSError:
                pass

    theirs_head = get_theirs_head(repo)
    merge_head_file = Path(repo) / ".git" / "MERGE_HEAD"
    cherry_pick_head_file = Path(repo) / ".git" / "CHERRY_PICK_HEAD"

    is_merging = merge_head_file.is_file() or cherry_pick_head_file.is_file()
    if not staged_files and not is_merging:
        raise GitError("No staged changes to commit.", stderr="")

    # Determine commit subject
    if message and message.strip():
        base_msg = message.strip()
    else:
        merge_msg_file = Path(repo) / ".git" / "MERGE_MSG"
        if merge_msg_file.is_file():
            clean_lines = [
                l for l in merge_msg_file.read_text().splitlines() if not l.startswith("#")
            ]
            base_msg = "\n".join(clean_lines).strip() or "Resolve conflict"
        elif theirs_head:
            base_msg = f"Resolve conflict with {theirs_head[:8]}"
        else:
            base_msg = "Resolve conflict"

    # Determine trailers
    ours_head = run_git(repo, "rev-parse", "HEAD", check=False)
    if not provenance:
        if theirs_head and ours_head:
            provenance = f"{ours_head[:8]} (ours), {theirs_head[:8]} (theirs)"
        elif ours_head:
            provenance = f"{ours_head[:8]}"
        else:
            provenance = "manual resolution"

    tested_trailer = tested_in.lower().strip()
    trailers = [
        f"Provenance: {provenance}",
        f"Tested-in: {tested_trailer}",
    ]
    full_message = f"{base_msg}\n\n" + "\n".join(trailers) + "\n"

    # Create the Git commit
    run_git(repo, "commit", "-m", full_message)
    new_sha = run_git(repo, "rev-parse", "HEAD")

    # Record provenance note on the new commit
    norm_level = tested_trailer.upper().replace("_", "-")
    if norm_level not in RECORDABLE:
        if "prod" in tested_trailer:
            norm_level = PROD
        elif "uat" in tested_trailer:
            norm_level = UAT
        else:
            norm_level = CI_PASSED

    record = NoteRecord(level=norm_level, evidence=f"provenance: {provenance}")
    write_note(repo, new_sha, record)

    return new_sha


def register(app: typer.Typer) -> None:
    """Add ``finish`` sub-command to *app*."""

    @app.command()
    def finish(
        message: Optional[str] = typer.Option(
            None, "--message", "-m", help="Commit message (defaults to merge message)"
        ),
        tested_in: str = typer.Option(
            "uat", "--tested-in", "-t", help="Tested environment trailer (e.g. uat, prod)"
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
                provenance=provenance,
                add_all=add_all,
            )
            typer.echo(f"Created resolution commit {new_sha[:8]} with trailers and note.")
        except GitError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1)
