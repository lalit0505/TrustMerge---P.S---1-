# TrustMerge (`git-prov`)

> **Git conflict resolution with testing provenance:** where each side came from, which branches it reached, and whether there is evidence it was tested.

[![CI](https://github.com/lalit0505/TrustMerge---P.S---1-/actions/workflows/ci.yml/badge.svg)](https://github.com/lalit0505/TrustMerge---P.S---1-/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](pyproject.toml)

When merge or cherry-pick conflicts happen, standard Git only shows you the conflicting lines of code. It cannot answer the questions that actually matter during resolution:
- **Where did this code come from?** Which commit originally wrote it?
- **Which environments has it reached?** (`prod`, `uat`, `dev`)
- **Is there proof it was tested?** Or is it untested development work?
- **Which side should be kept?**

TrustMerge solves this with one core rule:
> **Never claim something is tested without evidence, and always show where the evidence comes from. Never default to "safe".**

---

## Architecture & Technical Design

### 1. Identifying the Same Change Across Branches
When a patch is backported, cherry-picked, or edited during conflict resolution, its commit hash changes. TrustMerge identifies equivalent changes through a multi-stage deterministic pipeline backed by an open-source LLM judge:

```
[Candidate Commits]
        │
        ▼
1. Exact Stable Patch-ID match (`git patch-id --stable`)
        │ (no match)
        ▼
2. Message Trailers & Backport References:
   - `(cherry picked from commit <hash>)` from `git cherry-pick -x`
   - Django's `Backport of <hash> from main.`
   - CPython's `(GH-1234)` pull request references
   - Linux kernel's `[ Upstream commit <hash> ]`
        │ (no match)
        ▼
3. Overlapping Files & Modified Hunks
        │ (ambiguous candidates)
        ▼
4. Open-Source AI Judge (qwen2.5-coder:7b via Ollama)
   - Temperature 0, 16k context
   - Validated JSON verdict: `SameChangeVerdict(same_change=bool, confidence=float, reasoning=str)`
```

### 2. Storing and Transferring Testing Provenance
- Testing evidence is stored as **Git Notes** under `refs/notes/trustmerge` (with fallback reading from `refs/notes/gitprov`).
- Notes attach directly to commit objects without altering commit hashes or tree objects.
- `git prov init` configures the bidirectional fetch refspec (`+refs/notes/trustmerge:refs/notes/trustmerge`).
- `git prov sync` pushes and fetches provenance notes across remotes.
- TrustMerge also supports local file-based evidence via `.trustmerge/evidence.json` or `.gitprov/evidence.json`.

### 3. Graceful Fallback for Clones Without Notes
If a repository is cloned without fetching notes, TrustMerge **never fails and never defaults to "safe"**. It automatically falls back to Git history inference:
- Analyzes branch reachability via `git merge-base --is-ancestor`
- Detects patch-id equivalence against production and UAT branches
- Clearly tags the evidence source as **`inferred`** rather than **`recorded`**

### 4. Vanilla Git Compatibility
TrustMerge finishes conflict resolution with standard Git tools:
- The resulting merge commit is an ordinary Git commit with standard commit trailers:
  ```git
  Resolve timeout conflict

  Provenance: 34a438a6 (uat), 330c642a (dev)
  Tested-in: uat
  ```
- Anyone inspecting the repository with vanilla Git tools (`git log`, `git blame`, GitHub UI) sees an ordinary commit with structured trailers.

### 5. Evidence Level Hierarchy
TrustMerge assigns strict evidence levels in decreasing order of confidence:

| Level | Meaning | When Assigned |
| :--- | :--- | :--- |
| **`PROD`** | Running in production | Reachable from `prod` branch, identical patch-id on `prod`, or recorded note |
| **`UAT`** | Tested in staging / UAT | Reachable from `uat` branch, identical patch-id on `uat`, or recorded note |
| **`CI-PASSED`** | Verified by CI test suite | Explicitly recorded via `git prov mark` or CI notes |
| **`UNVERIFIED`** | Untested code | Only reachable from `dev`; no recorded testing evidence found |
| **`UNKNOWN`** | No evidence found | Commit is not reachable on any tracked environment branch |

---

## Open-Source AI Integration

TrustMerge uses **qwen2.5-coder:7b** running locally via Ollama (`http://localhost:11434`):
1. **Fuzzy Same-Change Judge**: Evaluates ambiguous code hunks across branches where patch-IDs and trailers differ.
2. **Messy Message Reference Extractor**: Turns conversational or unstructured commit messages into structured commit hashes and PR numbers when regexes fail.
3. **Evidence-Bounded Conflict Briefing**: Generates a plain-English briefing for the developer. The prompt strictly instructs the model: *"Never claim something is tested without evidence. If the evidence doesn't say it's tested, say it's unverified."*
4. **Offline / CI Determinism**: In CI and test environments, `TRUSTMERGE_LLM=fake` activates `FakeLLMClient`, ensuring 100% deterministic, offline test execution without requiring an active Ollama daemon.

---

## Installation

### Prerequisites
- Python 3.10+
- Git 2.30+
- (Optional) [Ollama](https://ollama.ai) with `qwen2.5-coder:7b` for live AI briefings.

### Install from source
```bash
git clone https://github.com/lalit0505/TrustMerge---P.S---1-.git
cd TrustMerge---P.S---1-
pip install -e ".[dev]"
```

Because the package exposes `git-prov` and `git-trustmerge` as console scripts, Git will automatically recognise them as sub-commands:
```bash
git prov --help
# or
git trustmerge --help
```

---

## Quickstart & Demo

You can run the included demo fixture generator to create a simulated repository with real merge conflicts:

```bash
# 1. Generate a demo repository with a conflict between tested UAT code and untested dev code
python examples/generate_fixture_repo.py demo_repo

# 2. Enter the demo repository
cd demo_repo

# 3. Run TrustMerge conflict analysis
git prov analyze
```

### Sample Terminal Output
```
╭──────────────────── TrustMerge Conflict Analysis (MERGE) ────────────────────╮
│ Conflict between OURS (34a438a6) and THEIRS (330c642a)                       │
│ Conflicted hunks: 1                                                          │
╰──────────────────────────────────────────────────────────────────────────────╯
                           File: settings.py (Hunk 1)                           
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ OURS: HEAD                            ┃ THEIRS: dev                          ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Level: UAT                            │ Level: UNVERIFIED                    │
│                                       │                                      │
│ Code:                                 │ Code:                                │
│   TIMEOUT = 60                        │   TIMEOUT = 45                       │
│                                       │                                      │
│ Origin Commits:                       │ Origin Commits:                      │
│   • 34a438a6 Fix slow upstream        │   • 330c642a Tune timeout (PR #845)  │
│ timeout (hotfix #812)                 │     inferred: only on dev; no        │
│     inferred: reachable from uat      │ evidence it was tested               │
│     reached: uat                      │     reached: dev                     │
│     note: same logical change as      │                                      │
│ 663880a4 on prod (code may differ,    │                                      │
│ verify separately)                    │                                      │
└───────────────────────────────────────┴──────────────────────────────────────┘
╭────────────────────────── 🤖 AI Conflict Briefing ───────────────────────────╮
│ Summary:                                                                     │
│ There is a conflict in settings.py regarding the TIMEOUT variable. The       │
│ current code in HEAD (OURS) has a TIMEOUT of 60 verified through UAT         │
│ testing. The incoming code on dev (THEIRS) has a TIMEOUT of 45 with no       │
│ evidence of testing.                                                         │
│                                                                              │
│ Ours (HEAD):                                                                 │
│ The TIMEOUT of 60 is verified and tested in UAT.                             │
│                                                                              │
│ Theirs (dev):                                                                │
│ The TIMEOUT of 45 is UNVERIFIED; no testing evidence exists.                 │
│                                                                              │
│ Recommendation:                                                              │
│ Retain the HEAD version of settings.py with TIMEOUT = 60, as it is verified  │
│ through UAT testing.                                                         │
│                                                                              │
│ Reasoning:                                                                   │
│ The HEAD version has recorded evidence of being tested in UAT, while the dev │
│ version lacks testing evidence. Unverified code should not overwrite tested  │
│ production changes.                                                          │
╰──────────────────────────────────────────────────────────────────────────────╯
```

---

## Command Reference

### `git prov analyze`
Run during a conflict state. Displays origin commits, environment reachability, evidence levels, and AI briefing.
```bash
git prov analyze
git prov analyze --json        # Machine-readable JSON output
git prov analyze --no-llm      # Deterministic rule-based output without AI
```

### `git prov scan [branch]`
Lists commits on a branch that have not reached production, displaying their current evidence level.
```bash
git prov scan dev
git prov scan uat --limit 10
git prov scan dev --json
```

### `git prov mark <commit> --tested <level> --evidence <ref>`
Records evidence for a commit directly into Git notes.
```bash
git prov mark 4b2578c1 --tested uat --evidence "Tested on staging-cluster-4"
git prov mark HEAD --tested ci-passed --evidence "GitHub Actions Run #1042"
```

### `git prov finish`
Creates the final resolution commit with standard `Provenance:` and `Tested-in:` trailers and records a note.
```bash
# After resolving conflict markers and staging
git prov finish -m "Resolve timeout conflict" --tested-in uat

# Automatically stage working tree changes and finish
git prov finish -a --tested-in prod
```

### `git prov init`
Configures the Git fetch refspec so notes are fetched automatically.
```bash
git prov init
git prov init --remote upstream
```

### `git prov sync`
Pushes and fetches provenance notes to/from remote repositories.
```bash
git prov sync
git prov sync --remote origin
```

---

## Running Tests

TrustMerge comes with a test suite covering all modules:

```bash
# Run full test suite
pytest -q

# Run with verbose output
pytest -v
```

In CI or headless environments, set `TRUSTMERGE_LLM=fake` (configured automatically in `.github/workflows/ci.yml`).

---

## Limitations

1. **Squashed or Rewritten Commits without Metadata**: If a commit was squashed without preserving trailers or modified beyond recognition, TrustMerge defaults to `UNVERIFIED` rather than guessing.
2. **Untracked Branches**: TrustMerge inspects standard branch names (`prod`, `uat`, `dev` by default). Custom branch names can be configured via `trustmerge.provenance.Config`.
3. **Local LLM Availability**: When Ollama is offline or uninstalled, TrustMerge transparently falls back to deterministic rule-based recommendations without failing. LLM output is never authoritative and is subject to deterministic validation.
4. **Recorded Evidence is Unsigned and Trust-on-Push**: Recorded evidence stored in `refs/notes/trustmerge` and local evidence files is unsigned and trust-on-push: anyone with push access to the repository can attach or edit notes under `refs/notes/trustmerge`. For cryptographic auditability, verify notes in CI against signed build provenance.
5. **Reachability vs. Tested Verification**: Reaching a branch only verifies integration into that branch's history, not that tests passed, unless explicit recorded evidence exists. If any origin commit on a side is `UNVERIFIED` or `UNKNOWN`, that side's effective level is capped at the weakest level.

---

## Architecture & Wired AI Features

TrustMerge pairs deterministic Git provenance analysis with local LLM assistance:

- **Deterministic Rule Engine (Always Authoritative)**:
  - Line-by-line stage-2/stage-3 blame mapping using `difflib.SequenceMatcher`.
  - Level ranking: `PROD` > `UAT` > `CI-PASSED` > `UNVERIFIED` > `UNKNOWN`.
  - Weakest-commit capping: sides are ranked by their weakest commit to prevent unverified code laundering.
  - Briefing validator (`validate_briefing`): validates LLM briefings against deterministic rules and rejects hallucinations or inverted recommendations.

- **Wired Local AI Features (`qwen2.5-coder:7b` via Ollama)**:
  - **AI Conflict Briefing**: Generates plain-English summaries and rationales guarded by strict prompt defense and deterministic validation.
  - **LLM Judge (`find_equivalents` with `--llm`)**: Identifies cherry-picked or backported equivalents when deterministic patch-ids and trailers find nothing. LLM matches add informative notes (`note: possibly same change as ... (LLM judge, confidence 0.xx, verify)`) and never elevate evidence levels.
  - **LLM Reference Extractor (`extract_references`)**: Extracts backported commit hashes and GitHub PR references with strict regex validation and memoized caching.

- **Test Coverage**:
  - `trustmerge.finish`: **92%** test coverage
  - `trustmerge.llm`: **97%** test coverage
  - Fully deterministic test suite with `TRUSTMERGE_LLM=fake` (no running Ollama required for CI).


---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
