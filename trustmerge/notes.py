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


def _parse_all_json_objects(raw: str) -> list:
    """Parse one or more JSON objects or lists from raw string."""
    decoder = json.JSONDecoder()
    idx = 0
    raw_len = len(raw)
    items = []
    while idx < raw_len:
        while idx < raw_len and raw[idx].isspace():
            idx += 1
        if idx >= raw_len:
            break
        try:
            obj, end_idx = decoder.raw_decode(raw, idx)
            items.append(obj)
            idx = end_idx
        except json.JSONDecodeError:
            next_start = -1
            for i in range(idx + 1, raw_len):
                if raw[i] in ('[', '{'):
                    next_start = i
                    break
            if next_start != -1:
                idx = next_start
            else:
                break
    return items


def read_note(repo: Path, sha: str) -> Optional[list[NoteRecord]]:
    """Return recorded evidence for *sha*, or ``None`` if no note exists."""
    all_records: list[NoteRecord] = []
    seen: set[tuple[str, str]] = set()

    for ref in (NOTES_REF, ALT_NOTES_REF):
        raw = run_git(repo, "notes", "--ref", ref, "show", sha, check=False)
        if raw:
            items = _parse_all_json_objects(raw)
            for item in items:
                entries = item if isinstance(item, list) else [item]
                for entry in entries:
                    rec = normalize_record(entry)
                    if rec is not None and (rec.level, rec.evidence) not in seen:
                        seen.add((rec.level, rec.evidence))
                        all_records.append(rec)

    return all_records if all_records else None


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


def _get_remote_ref_sha(repo: Path, remote: str, ref: str) -> Optional[str]:
    """Return commit sha of ref on remote, or None if remote does not have it.
    Raises GitError on communication failure.
    """
    out = run_git(repo, "ls-remote", remote, ref, check=True).strip()
    if not out:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == ref:
            return parts[0]
    return out.split()[0] if out else None


def _get_local_ref_sha(repo: Path, ref: str) -> Optional[str]:
    """Return commit sha of local ref, or None if it doesn't exist."""
    out = run_git(repo, "rev-parse", "--verify", "--quiet", ref, check=False).strip()
    return out if out else None


def sync_notes_ref(repo: Path, remote: str, ref: str) -> bool:
    """Sync a single notes ref (NOTES_REF or ALT_NOTES_REF) with remote.
    Returns True if notes were pushed, fetched, or merged.
    """
    local_sha = _get_local_ref_sha(repo, ref)
    remote_sha = _get_remote_ref_sha(repo, remote, ref)

    if not local_sha and not remote_sha:
        return False

    ref_short = "trustmerge" if ref == NOTES_REF else "gitprov"
    temp_ref = f"refs/notes/{ref_short}-remote"

    if local_sha and not remote_sha:
        # local only -> push
        run_git(repo, "push", remote, f"{ref}:{ref}", check=True)
        return True

    if remote_sha and not local_sha:
        # remote only -> fetch
        run_git(repo, "fetch", remote, f"{ref}:{ref}", check=True)
        return True

    # Both exist
    if local_sha == remote_sha:
        return False

    # Fetch remote into temp ref so remote objects exist locally
    run_git(repo, "fetch", remote, f"+{ref}:{temp_ref}", check=True)
    temp_sha = run_git(repo, "rev-parse", "--verify", "--quiet", temp_ref, check=False).strip()
    if not temp_sha or temp_sha == local_sha:
        run_git(repo, "update-ref", "-d", temp_ref, check=False)
        return False

    from trustmerge.git import is_ancestor
    try:
        if is_ancestor(repo, temp_sha, local_sha):
            # Local is strictly ahead -> push
            run_git(repo, "push", remote, f"{ref}:{ref}", check=True)
            return True
        elif is_ancestor(repo, local_sha, temp_sha):
            # Remote is strictly ahead -> fast-forward update local ref
            run_git(repo, "update-ref", ref, temp_sha, check=True)
            return True
        else:
            # Diverged notes -> merge with cat_sort_uniq, and push
            run_git(
                repo,
                "notes",
                f"--ref={ref_short}",
                "merge",
                "-s",
                "cat_sort_uniq",
                temp_ref,
                check=True,
            )
            run_git(repo, "push", remote, f"{ref}:{ref}", check=True)
            return True
    finally:
        run_git(repo, "update-ref", "-d", temp_ref, check=False)


def sync_notes(repo: Path, remote: str = "origin") -> bool:
    """Sync both NOTES_REF and ALT_NOTES_REF if present.
    Returns True if any notes were synced, False if nothing to sync.
    """
    synced_any = False
    for ref in (NOTES_REF, ALT_NOTES_REF):
        if sync_notes_ref(repo, remote, ref):
            synced_any = True
    return synced_any


def sync_push(repo: Path, remote: str = "origin") -> str:
    """Push notes to *remote*."""
    res = []
    if _get_local_ref_sha(repo, NOTES_REF):
        res.append(run_git(repo, "push", remote, NOTES_REF))
    if _get_local_ref_sha(repo, ALT_NOTES_REF):
        res.append(run_git(repo, "push", remote, ALT_NOTES_REF))
    return "\n".join(res)


def sync_fetch(repo: Path, remote: str = "origin") -> str:
    """Fetch notes from *remote*."""
    res = []
    for ref in (NOTES_REF, ALT_NOTES_REF):
        if _get_remote_ref_sha(repo, remote, ref):
            res.append(run_git(repo, "fetch", remote, f"{ref}:{ref}"))
    return "\n".join(res)


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
            synced = sync_notes(repo, remote=remote)
            if not synced:
                typer.echo("nothing to sync")
            else:
                typer.echo(f"Synchronized notes with remote '{remote}'.")
        except GitError as exc:
            typer.echo(f"Sync error: {exc}", err=True)
            raise typer.Exit(1)
