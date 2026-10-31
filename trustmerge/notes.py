"""Git notes store for recorded testing evidence.

Notes live under ``refs/notes/trustmerge`` (and fallback to ``refs/notes/gitprov``)
so they don't change commit hashes and can be pushed/fetched independently.
Also supports reading recorded evidence from ``.gitprov/evidence.json`` or
``.trustmerge/evidence.json``.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import typer

from trustmerge.git import GitError, rev_parse, run_git
from trustmerge.levels import RECORDABLE

NOTES_REF = "refs/notes/trustmerge"
ALT_NOTES_REF = "refs/notes/gitprov"


@dataclass(frozen=True)
class NoteRecord:
    """One piece of recorded evidence stored in a git note or evidence file."""

    level: str
    evidence: str  # free-form reference (CI URL, ticket, test run ID, etc.)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "NoteRecord":
        return cls(level=d["level"], evidence=d["evidence"])


def normalize_record(raw: object) -> Optional[NoteRecord]:
    """Validate and normalize a raw record into NoteRecord, or None if invalid."""
    if not isinstance(raw, dict):
        return None
    lvl = raw.get("level")
    if not isinstance(lvl, str):
        return None
    norm_lvl = lvl.strip().upper().replace("_", "-")
    if norm_lvl not in RECORDABLE:
        return None
    ev = raw.get("evidence")
    if not isinstance(ev, str) or not ev.strip():
        return None
    return NoteRecord(level=norm_lvl, evidence=ev.strip())


# ── read / write notes ────────────────────────────────────────────────


def read_note(repo: Path, sha: str) -> Optional[list[NoteRecord]]:
    """Return recorded evidence for *sha*, or ``None`` if no note exists."""
    for ref in (NOTES_REF, ALT_NOTES_REF):
        raw = run_git(repo, "notes", "--ref", ref, "show", sha, check=False)
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # Free-text notes must NOT become CI-PASSED; ignore them
                continue
            if isinstance(data, dict):
                data = [data]
            elif not isinstance(data, list):
                continue
            records = [r for item in data if (r := normalize_record(item)) is not None]
            if records:
                return records
    return None


def write_note(repo: Path, sha: str, record: NoteRecord) -> None:
    """Append *record* to the note on *sha* (creates the note if needed)."""
    norm_level = record.level.upper().replace("_", "-")
    if norm_level not in RECORDABLE:
        raise ValueError(f"level must be one of {RECORDABLE}, got {record.level!r}")
    if not isinstance(record.evidence, str) or not record.evidence.strip():
        raise ValueError("evidence must be a non-empty string")
    norm_rec = NoteRecord(level=norm_level, evidence=record.evidence.strip())

    sha = rev_parse(repo, sha)
    existing = read_note(repo, sha) or []
    if norm_rec in existing:
        return
    entries = [r.to_dict() for r in existing] + [norm_rec.to_dict()]
    blob = json.dumps(entries, indent=2)
    run_git(
        repo, "notes", "--ref", NOTES_REF, "add", "-f", "-m", blob, sha,
    )


# ── evidence file fallback ────────────────────────────────────────────


def read_evidence_file(repo: Path, sha: str) -> Optional[list[NoteRecord]]:
    """Read recorded evidence from .trustmerge/evidence.json or .gitprov/evidence.json."""
    sha = rev_parse(repo, sha).lower()
    for subpath in (".trustmerge/evidence.json", ".gitprov/evidence.json"):
        path = Path(repo) / subpath
        if not path.is_file():
            continue
        try:
            content = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        records: list[NoteRecord] = []
        if isinstance(content, dict):
            for k, val in content.items():
                if not isinstance(k, str) or len(k.strip()) < 7:
                    continue
                k_clean = k.strip().lower()
                # match with sha.startswith(key) only
                if sha.startswith(k_clean):
                    rec = normalize_record(val)
                    if rec is not None:
                        records.append(rec)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    item_sha = item.get("sha")
                    if not isinstance(item_sha, str) or len(item_sha.strip()) < 7:
                        continue
                    k_clean = item_sha.strip().lower()
                    # match with sha.startswith(key) only
                    if sha.startswith(k_clean):
                        rec = normalize_record(item)
                        if rec is not None:
                            records.append(rec)
        if records:
            return records
    return None



def get_recorded_evidence(repo: Path, sha: str) -> list[NoteRecord]:
    """Return all recorded evidence for *sha* from notes or evidence files."""
    notes = read_note(repo, sha)
    if notes:
        return notes
    from_file = read_evidence_file(repo, sha)
    if from_file:
        return from_file
    return []


# ── init / sync ──────────────────────────────────────────────────────


def init_refspec(repo: Path, remote: str = "origin") -> bool:
    """Add the notes fetch refspecs to *remote* if not already present."""
    added_any = False
    for ref in (NOTES_REF, ALT_NOTES_REF):
        line = f"+{ref}:{ref}"
        existing = run_git(
            repo, "config", "--get-all", f"remote.{remote}.fetch", check=False,
        )
        if line not in existing.splitlines():
            run_git(repo, "config", "--add", f"remote.{remote}.fetch", line)
            added_any = True
    return added_any


def sync_push(repo: Path, remote: str = "origin") -> str:
    """Push notes to *remote*."""
    return run_git(repo, "push", remote, NOTES_REF)


def sync_fetch(repo: Path, remote: str = "origin") -> str:
    """Fetch notes from *remote*."""
    return run_git(repo, "fetch", remote, f"{NOTES_REF}:{NOTES_REF}", check=False)


def has_notes_ref(repo: Path) -> bool:
    """Return True if local notes ref exists."""
    out = run_git(repo, "rev-parse", "--verify", "--quiet", NOTES_REF, check=False)
    if out:
        return True
    alt_out = run_git(repo, "rev-parse", "--verify", "--quiet", ALT_NOTES_REF, check=False)
    return bool(alt_out)


# ── CLI registration ─────────────────────────────────────────────────


def register(app: typer.Typer) -> None:
    """Add ``init``, ``mark``, and ``sync`` sub-commands to *app*."""

    @app.command()
    def init(
        remote: str = typer.Option("origin", "--remote", help="Remote name"),
        repo: Path = typer.Option(Path("."), "--repo", help="Path to git repository"),
    ) -> None:
        """Set up notes fetch refspec so git fetch gets provenance notes."""
        added = init_refspec(repo, remote=remote)
        if added:
            typer.echo(f"Configured notes fetch refspec for remote '{remote}'.")
        else:
            typer.echo(f"Notes fetch refspec already configured for remote '{remote}'.")

    @app.command()
    def mark(
        commit: str = typer.Argument(..., help="Commit hash or ref to mark"),
        tested: str = typer.Option(
            ..., "--tested", "-t", help="Testing level: PROD, UAT, or CI-PASSED"
        ),
        evidence: str = typer.Option(
            ..., "--evidence", "-e", help="Evidence reference (CI URL, ticket, run ID)"
        ),
        repo: Path = typer.Option(Path("."), "--repo", help="Path to git repository"),
    ) -> None:
        """Record testing evidence on a commit as a git note."""
        norm_level = tested.upper().replace("_", "-")
        if norm_level not in RECORDABLE:
            typer.echo(
                f"Error: Invalid level '{tested}'. Allowed levels: {', '.join(sorted(RECORDABLE))}",
                err=True,
            )
            raise typer.Exit(1)
        try:
            sha = rev_parse(repo, commit)
        except GitError as exc:
            typer.echo(f"Error resolving commit '{commit}': {exc}", err=True)
            raise typer.Exit(1)
        record = NoteRecord(level=norm_level, evidence=evidence)
        write_note(repo, sha, record)
        typer.echo(f"Marked {sha[:8]} as {norm_level} ({evidence})")

    @app.command()
    def sync(
        remote: str = typer.Option("origin", "--remote", help="Remote name"),
        repo: Path = typer.Option(Path("."), "--repo", help="Path to git repository"),
    ) -> None:
        """Push and fetch notes to/from remote."""
        remotes = run_git(repo, "remote", check=False).splitlines()
        if remote not in remotes:
            typer.echo(f"Remote '{remote}' not found.", err=True)
            raise typer.Exit(1)
        try:
            sync_fetch(repo, remote=remote)
            sync_push(repo, remote=remote)
            typer.echo(f"Synchronized notes with remote '{remote}'.")
        except GitError as exc:
            typer.echo(f"Sync error: {exc}", err=True)
            raise typer.Exit(1)
