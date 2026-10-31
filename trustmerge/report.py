"""Conflict analysis report: origin commits, branch reachability, evidence, and recommendations."""

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Optional, Tuple

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
import typer

from trustmerge.conflicts import ConflictState, find_conflicts
from trustmerge.git import rev_parse, run_git
from trustmerge.levels import CI_PASSED, PROD, UAT, UNKNOWN, UNVERIFIED, rank
from trustmerge.llm import ConflictBriefing, LLMClient, get_llm_client
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
    weakest_level: str = ""
    strongest_level: str = ""
    best_level: str = ""

    def __post_init__(self):
        w = self.weakest_level or self.best_level or UNKNOWN
        s = self.strongest_level or self.best_level or w
        b = w
        object.__setattr__(self, "weakest_level", w)
        object.__setattr__(self, "strongest_level", s)
        object.__setattr__(self, "best_level", b)



@dataclass(frozen=True)
class HunkAnalysis:
    """Analysis and recommendation for a single conflict hunk."""

    file_path: str
    ours: SideAnalysis
    theirs: SideAnalysis
    recommendation: str
    briefing: Optional[ConflictBriefing] = None


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


def weakest_evidence_level(commits: Tuple[CommitAnalysis, ...]) -> str:
    """Return the weakest evidence level across a collection of commits (max rank)."""
    if not commits:
        return UNKNOWN
    return max(commits, key=lambda c: rank(c.level)).level


def best_evidence_level(commits: Tuple[CommitAnalysis, ...]) -> str:
    """Return the strongest evidence level across a collection of commits (min rank)."""
    if not commits:
        return UNKNOWN
    return min(commits, key=lambda c: rank(c.level)).level


def generate_recommendation(ours: SideAnalysis, theirs: SideAnalysis) -> str:
    """Generate a deterministic rule-based recommendation based on evidence."""
    ours_shas = {c.sha for c in ours.commits}
    theirs_shas = {c.sha for c in theirs.commits}

    same_logical = False
    for c in ours.commits:
        if any(any(t[:8] in rel for t in theirs_shas) for rel in c.related):
            same_logical = True
    for c in theirs.commits:
        if any(any(o[:8] in rel for o in ours_shas) for rel in c.related):
            same_logical = True

    r_ours = rank(ours.weakest_level)
    r_theirs = rank(theirs.weakest_level)

    if same_logical:
        if r_ours < r_theirs:
            return (
                f"Both sides represent the same logical change. Recommend OURS: "
                f"{ours.weakest_level} evidence vs {theirs.weakest_level} on THEIRS."
            )
        elif r_theirs < r_ours:
            return (
                f"Both sides represent the same logical change. Recommend THEIRS: "
                f"{theirs.weakest_level} evidence vs {ours.weakest_level} on OURS."
            )
        else:
            return (
                f"Both sides represent the same logical change with equal evidence ({ours.weakest_level}). "
                "Verify whether hand edits are needed."
            )

    if r_ours < r_theirs:
        return (
            f"Recommend OURS: {ours.weakest_level} evidence vs {theirs.weakest_level} on THEIRS. "
            "Never accept unverified changes over tested ones without evidence."
        )
    elif r_theirs < r_ours:
        return (
            f"Recommend THEIRS: {theirs.weakest_level} evidence vs {ours.weakest_level} on OURS. "
            "Never accept unverified changes over tested ones without evidence."
        )
    elif ours.weakest_level in (PROD, UAT, CI_PASSED):
        return (
            f"Both sides have {ours.weakest_level} evidence. "
            "Manual review required to combine or choose changes."
        )
    elif ours.weakest_level == theirs.weakest_level:
        return (
            f"Neither side has recorded testing evidence (both {ours.weakest_level}). "
            "Verification required before committing."
        )
    else:
        return (
            f"Neither side has recorded testing evidence ({ours.weakest_level} vs {theirs.weakest_level}). "
            "Verification required before committing."
        )


def build_conflict_report(
    repo: Path,
    config: Config = Config(),
    use_llm: bool = True,
    client: Optional[LLMClient] = None,
) -> Optional[ConflictReport]:
    """Inspect conflicted repository and produce a structured analysis report."""
    state = find_conflicts(repo)
    if not state:
        return None

    llm = (client or get_llm_client()) if use_llm else None

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
                weakest_level=weakest_evidence_level(ours_commits),
                strongest_level=best_evidence_level(ours_commits),
            )
            theirs_side = SideAnalysis(
                label=hunk.theirs.label,
                lines=hunk.theirs.lines,
                commits=theirs_commits,
                weakest_level=weakest_evidence_level(theirs_commits),
                strongest_level=best_evidence_level(theirs_commits),
            )

            rule_rec = generate_recommendation(ours_side, theirs_side)
            briefing = None

            if llm is not None:
                try:
                    related_all = [rel for c in (ours_commits + theirs_commits) for rel in c.related]
                    briefing = llm.generate_briefing(
                        file_path=hunk.file_path,
                        ours_label=ours_side.label,
                        ours_code="\n".join(ours_side.lines),
                        ours_evidence_level=ours_side.weakest_level,
                        ours_evidence_detail="; ".join(c.detail for c in ours_side.commits) or "none",
                        ours_reached=list({r for c in ours_side.commits for r in c.reached}),
                        theirs_label=theirs_side.label,
                        theirs_code="\n".join(theirs_side.lines),
                        theirs_evidence_level=theirs_side.weakest_level,
                        theirs_evidence_detail="; ".join(c.detail for c in theirs_side.commits) or "none",
                        theirs_reached=list({r for c in theirs_side.commits for r in c.reached}),
                        related_notes=related_all,
                    )
                except Exception:
                    briefing = None


            final_rec = briefing.recommendation if briefing is not None else rule_rec

            hunk_analyses.append(
                HunkAnalysis(
                    file_path=hunk.file_path,
                    ours=ours_side,
                    theirs=theirs_side,
                    recommendation=final_rec,
                    briefing=briefing,
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
    data = asdict(report)
    for hunk in data.get("hunks", []):
        b = hunk.get("briefing")
        if b is not None and hasattr(b, "model_dump"):
            hunk["briefing"] = b.model_dump()
    return data


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
    w_lvl = escape(side.weakest_level)
    s_lvl = escape(side.strongest_level)
    color = _LEVEL_COLORS.get(side.weakest_level, 'white')
    if side.strongest_level and side.strongest_level != side.weakest_level:
        lines.append(
            f"Level: [{color}]{w_lvl}[/] "
            f"(strongest: {s_lvl})"
        )
    else:
        lines.append(f"Level: [{color}]{w_lvl}[/]")
    lines.append("")

    lines.append("[bold]Code:[/]")
    if side.lines:
        for l in side.lines:
            lines.append(f"  {escape(l)}")
    else:
        lines.append("  (deleted)")

    lines.append("")
    lines.append("[bold]Origin Commits:[/]")
    if side.commits:
        for c in side.commits:
            lines.append(f"  • {escape(c.short_sha)} {escape(c.subject)}")
            lines.append(f"    {escape(c.source)}: {escape(c.detail)}")
            reached_str = escape(', '.join(c.reached) or 'none')
            lines.append(f"    reached: {reached_str}")
            for rel in c.related:
                lines.append(f"    note: {escape(rel)}")
    else:
        lines.append("  (no origin commits found)")
    return "\n".join(lines)


def render_report(report: ConflictReport, console: Optional[Console] = None) -> None:
    """Render the conflict report to the terminal using rich."""
    if console is None:
        console = Console()

    console.print()
    header_title = f"TrustMerge Conflict Analysis ({escape(report.operation.upper())})"
    rebase_note = ""
    if report.operation == "rebase":
        rebase_note = "\n[yellow]Note: During rebase, OURS is upstream/target and THEIRS is your rebased commit (swapped by Git).[/]"

    console.print(
        Panel(
            f"Conflict between OURS ([cyan]{escape(short(report.ours_head))}[/]) and "
            f"THEIRS ([magenta]{escape(short(report.theirs_head or 'unknown'))}[/])\n"
            f"Conflicted hunks: {len(report.hunks)}"
            f"{rebase_note}",
            title=f"[bold]{header_title}[/]",
            border_style="bold blue",
        )
    )

    for idx, hunk in enumerate(report.hunks, 1):
        table = Table(
            title=f"File: {escape(hunk.file_path)} (Hunk {idx})",
            expand=True,
            show_header=True,
            header_style="bold",
        )
        table.add_column(f"OURS: {escape(hunk.ours.label)}", style="cyan", ratio=1)
        table.add_column(f"THEIRS: {escape(hunk.theirs.label)}", style="magenta", ratio=1)

        table.add_row(
            _format_side_text(hunk.ours),
            _format_side_text(hunk.theirs),
        )
        console.print(table)

        if hunk.briefing:
            briefing_text = (
                f"[bold]Summary:[/]\n{escape(hunk.briefing.summary)}\n\n"
                f"[bold]Ours ({escape(hunk.ours.label)}):[/]\n{escape(hunk.briefing.ours_assessment)}\n\n"
                f"[bold]Theirs ({escape(hunk.theirs.label)}):[/]\n{escape(hunk.briefing.theirs_assessment)}\n\n"
                f"[bold]Recommendation:[/]\n{escape(hunk.briefing.recommendation)}\n\n"
                f"[bold]Reasoning:[/]\n{escape(hunk.briefing.reasoning)}"
            )
            briefing_panel = Panel(
                briefing_text,
                title="[bold green]🤖 AI Conflict Briefing[/]",
                border_style="green",
            )
            console.print(briefing_panel)
        else:
            rec_panel = Panel(
                escape(hunk.recommendation),
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
        use_llm: bool = typer.Option(
            True, "--llm/--no-llm", help="Generate AI conflict briefing with Ollama"
        ),
    ) -> None:
        """Analyze conflict: origin commits, testing evidence, and recommendation."""
        report = build_conflict_report(repo, use_llm=use_llm)
        if not report:
            typer.echo("No merge or cherry-pick conflict detected.", err=True)
            raise typer.Exit(1)

        if as_json:
            typer.echo(json.dumps(report_to_dict(report), indent=2))
        else:
            render_report(report)
