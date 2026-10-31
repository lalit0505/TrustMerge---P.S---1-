"""``git prov scan`` — show which changes reached which branches."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import typer

from trustmerge.git import GitError, run_git
from trustmerge.provenance import Config, Evidence, assess, resolve_ref, short


@dataclass(frozen=True)
class ScanRow:
    """One row of scan output: a commit with its evidence."""

    sha: str
    subject: str
    evidence: Evidence


def scan_branch(
    repo: Path,
    branch: str = "dev",
    config: Config = Config(),
    limit: int = 20,
    base: Optional[str] = None,
) -> List[ScanRow]:
    """Return evidence rows for commits on *branch* that are not on *base*.

    Defaults to listing commits on ``dev`` that haven't reached ``prod``.
    """
    ref = resolve_ref(repo, branch) or branch
    base_ref = resolve_ref(repo, base if base is not None else config.prod)

    args = ["log", "-n", str(limit), "--format=%H%x1f%s", ref]
    if base_ref is not None and base_ref != ref:
        args.extend(["--not", base_ref])
    args.append("--")

    try:
        output = run_git(repo, *args)
    except GitError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc

    rows: List[ScanRow] = []
    for line in output.splitlines():
        if "\x1f" not in line:
            continue
        sha, subject = line.split("\x1f", 1)
        if sha:
            rows.append(ScanRow(sha, subject, assess(repo, sha, config)))
    return rows


# ── output formatting ────────────────────────────────────────────────


def row_to_dict(row: ScanRow) -> dict:
    """Serialise a row for ``--json`` output."""
    return {
        "sha": row.sha,
        "subject": row.subject,
        "level": row.evidence.level,
        "source": row.evidence.source,
        "detail": row.evidence.detail,
        "reached": list(row.evidence.reached),
        "related": list(row.evidence.related),
    }


def format_rows(rows: List[ScanRow]) -> str:
    """Human-readable multi-line summary."""
    if not rows:
        return "no commits to scan"
    lines: list[str] = []
    for row in rows:
        lines.append(f"{short(row.sha)}  {row.evidence.level:<10}  {row.subject}")
        lines.append(f"          {row.evidence.source}: {row.evidence.detail}")
        lines.append(
            f"          reached: {', '.join(row.evidence.reached) or 'none'}"
        )
        for note in row.evidence.related:
            lines.append(f"          note: {note}")
    return "\n".join(lines)


# ── CLI registration ─────────────────────────────────────────────────


def register(app: typer.Typer) -> None:
    """Add the ``scan`` sub-command to *app*."""

    @app.command()
    def scan(
        branch: str = typer.Argument("dev"),
        repo: Path = typer.Option(".", "--repo"),
        limit: int = typer.Option(20, "--limit"),
        base: Optional[str] = typer.Option(None, "--base"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show which changes reached which branches and their evidence level."""
        try:
            rows = scan_branch(repo, branch, limit=limit, base=base)
        except typer.Exit:
            raise
        if as_json:
            typer.echo(json.dumps([row_to_dict(r) for r in rows], indent=2))
        else:
            typer.echo(format_rows(rows))
