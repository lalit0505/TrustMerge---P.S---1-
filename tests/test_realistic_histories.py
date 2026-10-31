"""Tests for realistic commit histories: Django, CPython, Linux kernel styles, and LLM message parsing."""

from pathlib import Path
import pytest

from fixtures import git
from trustmerge.identity import extract_references, find_equivalents
from trustmerge.llm import FakeLLMClient, get_llm_client


def test_django_backport_styles():
    h = "a" * 40
    # Standard Django format
    msg1 = f"[4.2.x] Fixed #12345 -- Fix query crash.\n\nBackport of {h} from main.\n"
    assert extract_references(msg1) == [h]

    # Without 'from main'
    msg2 = f"Backport of {h}."
    assert extract_references(msg2) == [h]

    # Backported of commit ...
    msg3 = f"Backported of commit {h} to 4.1.x"
    assert extract_references(msg3) == [h]


def test_cpython_backport_styles():
    h = "b" * 40
    # CPython standard format with PR tag and cherry-pick trailer
    msg1 = f"[3.12] gh-10293: Fix parser crash (GH-10294)\n\n(cherry picked from commit {h})\n"
    refs1 = extract_references(msg1)
    assert h in refs1
    assert "GH-10294" in refs1

    # Only PR reference
    msg2 = "[3.11] gh-8888: Fix socket timeout (GH-8889)"
    assert extract_references(msg2) == ["GH-8889"]


def test_kernel_and_git_styles():
    h = "c" * 40
    # Linux kernel upstream commit
    msg_kernel1 = f"[ Upstream commit {h} ]\n\nFix null pointer in net driver."
    assert extract_references(msg_kernel1) == [h]

    msg_kernel2 = f"commit {h} upstream.\n\nFix buffer overflow."
    assert extract_references(msg_kernel2) == [h]

    # Hyphenated cherry-picked
    msg_cherry1 = f"cherry-picked from commit {h}"
    assert extract_references(msg_cherry1) == [h]

    msg_cherry2 = f"Cherry-picked from: {h}"
    assert extract_references(msg_cherry2) == [h]

    # Origin / upstream header
    msg_origin = f"Origin: {h}\nBug: 1234"
    assert extract_references(msg_origin) == [h]


def test_messy_commit_message_llm_extraction():
    h = "d" * 40
    messy_msg = (
        "Hey everyone, this is an emergency sync with upstream commit "
        f"{h} to address a production regression."
    )

    # Standard regex doesn't match conversational format
    assert extract_references(messy_msg, use_llm=False) == []

    # LLM extraction parses the commit hash from unstructured prose
    fake_client = FakeLLMClient()
    refs = extract_references(messy_msg, use_llm=True, client=fake_client)
    assert refs == [h]


def test_realistic_django_history_end_to_end(scenario):
    repo = scenario.repo
    # Create main branch commit
    git(repo, "switch", "-q", "-c", "django-main", "prod")
    (repo / "views.py").write_text("def view(): return 200\n")
    git(repo, "add", "views.py")
    git(repo, "commit", "-m", "Fixed #45678 -- Handle empty body in views")
    main_sha = git(repo, "rev-parse", "HEAD").stdout.strip()

    # Backport to stable branch with edited code & Django-style message
    git(repo, "switch", "-q", "-c", "django-stable", "prod")
    (repo / "views.py").write_text("def view():\n    # backport comment\n    return 200\n")
    git(repo, "add", "views.py")
    git(
        repo,
        "commit",
        "-m",
        f"[4.2.x] Fixed #45678 -- Handle empty body in views.\n\nBackport of {main_sha} from main.\n",
    )
    backport_sha = git(repo, "rev-parse", "HEAD").stdout.strip()

    # Verify identity matching connects them across branches
    matches = find_equivalents(repo, main_sha, ["django-stable"])
    assert len(matches) == 1
    assert matches[0].commit == backport_sha
    assert matches[0].method == "reference"


def test_realistic_cpython_history_end_to_end(scenario):
    repo = scenario.repo
    # Original PR commit on main
    git(repo, "switch", "-q", "-c", "cpython-main", "prod")
    (repo / "parser.c").write_text("// original cpython fix\n")
    git(repo, "add", "parser.c")
    git(repo, "commit", "-m", "gh-54321: Fix buffer over-read (GH-54322)")
    main_sha = git(repo, "rev-parse", "HEAD").stdout.strip()

    # Backport commit on 3.12 branch
    git(repo, "switch", "-q", "-c", "cpython-3.12", "prod")
    (repo / "parser.c").write_text("// backported fix with minor change\n")
    git(repo, "add", "parser.c")
    git(
        repo,
        "commit",
        "-m",
        f"[3.12] gh-54321: Fix buffer over-read (GH-54322)\n\n(cherry picked from commit {main_sha})\n",
    )
    backport_sha = git(repo, "rev-parse", "HEAD").stdout.strip()

    matches = find_equivalents(repo, main_sha, ["cpython-3.12"])
    assert len(matches) == 1
    assert matches[0].commit == backport_sha
    assert matches[0].method == "reference"
