"""Conflict analysis report: origin commits, branch reachability, evidence, and recommendations."""

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
import typer

from trustmerge.conflicts import ConflictState, find_conflicts
from trustmerge.git import rev_parse, run_git
from trustmerge.levels import CI_PASSED, PROD, UAT, UNKNOWN, UNVERIFIED, rank
from trustmerge.provenance import Config, assess, short


@dataclass(frozen=True)
class CommitAnalysis:
    """Provenance and branch details for a single origin commit."""

    sha: str
    short_sha: str
    subject: str
    level: str
    source: str
    detail: str
    reached: Tuple[str, ...]
    related: Tuple[str, ...]


@dataclass(frozen=True)
class SideAnalysis:
    """Analysis for one side (ours or theirs) of a conflict hunk."""

    label: str
    lines: Tuple[str, ...]
    commits: Tuple[CommitAnalysis, ...]
    best_level: str


@dataclass(frozen=True)
class HunkAnalysis:
    """Analysis and recommendation for a single conflict hunk."""

    file_path: str
    ours: SideAnalysis
    theirs: SideAnalysis
    recommendation: str


@dataclass(frozen=True)
class ConflictReport:
    """Full conflict analysis report across all conflicted files."""

    operation: str
    ours_head: str
    theirs_head: Optional[str]
    hunks: Tuple[HunkAnalysis, ...]


# ── analysis logic ────────────────────────────────────────────────────


def analyze_commit(repo: Path, sha: str, config: Config = Config()) -> CommitAnalysis:
    """Assess testing provenance and branch reachability for *sha*."""
    ev = assess(repo, sha, config)
    subject = run_git(repo, "log", "-1", "--format=%s", sha, check=False) or "Commit"
    return CommitAnalysis(
        sha=sha,
        short_sha=short(sha),
        subject=subject,
        level=ev.level,
        source=ev.source,
        detail=ev.detail,
        reached=ev.reached,
        related=ev.related,
    )


def best_evidence_level(commits: Tuple[CommitAnalysis, ...]) -> str:
    """Return the strongest evidence level across a collection of commits."""
    if not commits:
        return UNKNOWN
    return min(commits, key=lambda c: rank(c.level)).level


def generate_recommendation(ours: SideAnalysis, theirs: SideAnalysis) -> str:
    """Generate a deterministic rule-based recommendation based on evidence."""
    # Check if either side references the other as the same logical change
    ours_shas = {c.sha for c in ours.commits}
    theirs_shas = {c.sha for c in theirs.commits}

    same_logical = False
    for c in ours.commits:
        if any(any(t[:8] in rel for t in theirs_shas) for rel in c.related):
            same_logical = True
    for c in theirs.commits:
        if any(any(o[:8] in rel for o in ours_shas) for rel in c.related):
            same_logical = True

    r_ours = rank(ours.best_level)
    r_theirs = rank(theirs.best_level)

    if same_logical:
        if r_ours < r_theirs:
            return (
                f"Both sides represent the same logical change. Recommend OURS: "
                f"{ours.best_level} evidence vs {theirs.best_level} on THEIRS."
            )
        elif r_theirs < r_ours:
            return (
                f"Both sides represent the same logical change. Recommend THEIRS: "
                f"{theirs.best_level} evidence vs {ours.best_level} on OURS."
            )
        else:
            return (
                f"Both sides represent the same logical change with equal evidence ({ours.best_level}). "
                "Verify whether hand edits are needed."
            )

    if r_ours < r_theirs:
        return (
            f"Recommend OURS: {ours.best_level} evidence vs {theirs.best_level} on THEIRS. "
            "Never accept unverified changes over tested ones without evidence."
        )
    elif r_theirs < r_ours:
        return (
            f"Recommend THEIRS: {theirs.best_level} evidence vs {ours.best_level} on OURS. "
            "Never accept unverified changes over tested ones without evidence."
        )
    elif ours.best_level in (PROD, UAT, CI_PASSED):
        return (
            f"Both sides have {ours.best_level} evidence. "
            "Manual review required to combine or choose changes."
        )
    else:
        return (
            "Neither side has recorded testing evidence (both UNVERIFIED). "
            "Verification required before committing."
        )


def build_conflict_report(
    repo: Path, config: Config = Config()
) -> Optional[ConflictReport]:
    """Inspect conflicted repository and produce a structured analysis report."""
    state = find_conflicts(repo)
    if not state:
        return None

    hunk_analyses: list[HunkAnalysis] = []
    for file in state.files:
        for hunk in file.hunks:
            ours_commits = tuple(
                analyze_commit(repo, sha, config) for sha in hunk.ours.commits
            )
            theirs_commits = tuple(
                analyze_commit(repo, sha, config) for sha in hunk.theirs.commits
            )

            ours_side = SideAnalysis(
                label=hunk.ours.label,
                lines=hunk.ours.lines,
                commits=ours_commits,
                best_level=best_evidence_level(ours_commits),
            )
            theirs_side = SideAnalysis(
                label=hunk.theirs.label,
                lines=hunk.theirs.lines,
                commits=theirs_commits,
                best_level=best_evidence_level(theirs_commits),
            )

            rec = generate_recommendation(ours_side, theirs_side)
            hunk_analyses.append(
                HunkAnalysis(
                    file_path=hunk.file_path,
                    ours=ours_side,
                    theirs=theirs_side,
                    recommendation=rec,
                )
            )

    return ConflictReport(
        operation=state.operation,
        ours_head=state.ours_head,
        theirs_head=state.theirs_head,
        hunks=tuple(hunk_analyses),
    )


# ── serialization & formatting ───────────────────────────────────────


def report_to_dict(report: ConflictReport) -> dict:
    """Convert ConflictReport to a serializable dictionary."""
    return asdict(report)


_LEVEL_COLORS = {
    PROD: "bold green",
    UAT: "bold blue",
    CI_PASSED: "bold cyan",
    UNVERIFIED: "bold yellow",
    UNKNOWN: "bold red",
}


def _format_side_text(side: SideAnalysis) -> str:
    """Format the text block for one side in the conflict table."""
    lines: list[str] = []
    lines.append(f"Level: [{_LEVEL_COLORS.get(side.best_level, 'white')}]{side.best_level}[/]")
    lines.append("")
    lines.append("[bold]Code:[/]")
    if side.lines:
        for l in side.lines:
            lines.append(f"  {l}")
    else:
        lines.append("  (deleted)")

    lines.append("")
    lines.append("[bold]Origin Commits:[/]")
    if side.commits:
        for c in side.commits:
            lines.append(f"  • {c.short_sha} {c.subject}")
            lines.append(f"    {c.source}: {c.detail}")
            lines.append(f"    reached: {', '.join(c.reached) or 'none'}")
            for rel in c.related:
                lines.append(f"    note: {rel}")
    else:
        lines.append("  (no origin commits found)")
    return "\n".join(lines)


def render_report(report: ConflictReport, console: Optional[Console] = None) -> None:
    """Render the conflict report to the terminal using rich."""
    if console is None:
        console = Console()

    console.print()
    header_title = f"TrustMerge Conflict Analysis ({report.operation.upper()})"
    console.print(
        Panel(
            f"Conflict between OURS ([cyan]{short(report.ours_head)}[/]) and "
            f"THEIRS ([magenta]{short(report.theirs_head or 'unknown')}[/])\n"
            f"Conflicted hunks: {len(report.hunks)}",
            title=f"[bold]{header_title}[/]",
            border_style="bold blue",
        )
    )

    for idx, hunk in enumerate(report.hunks, 1):
        table = Table(
            title=f"File: {hunk.file_path} (Hunk {idx})",
            expand=True,
            show_header=True,
            header_style="bold",
        )
        table.add_column(f"OURS: {hunk.ours.label}", style="cyan", ratio=1)
        table.add_column(f"THEIRS: {hunk.theirs.label}", style="magenta", ratio=1)

        table.add_row(
            _format_side_text(hunk.ours),
            _format_side_text(hunk.theirs),
        )
        console.print(table)

        rec_panel = Panel(
            hunk.recommendation,
            title="[bold yellow]💡 Recommendation[/]",
            border_style="yellow",
        )
        console.print(rec_panel)
        console.print()


# ── CLI registration ─────────────────────────────────────────────────


def register(app: typer.Typer) -> None:
    """Add ``analyze`` sub-command to *app*."""

    @app.command()
    def analyze(
        repo: Path = typer.Option(Path("."), "--repo", help="Path to git repository"),
        as_json: bool = typer.Option(False, "--json", help="Output analysis as JSON"),
    ) -> None:
        """Analyze conflict: origin commits, testing evidence, and recommendation."""
        report = build_conflict_report(repo)
        if not report:
            typer.echo("No merge or cherry-pick conflict detected.", err=True)
            raise typer.Exit(1)

        if as_json:
            typer.echo(json.dumps(report_to_dict(report), indent=2))
        else:
            render_report(report)
