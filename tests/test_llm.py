"""Tests for trustmerge.llm: FakeLLMClient, OllamaClient, and fuzzy-match judge."""

import os
from pathlib import Path
import pytest
from pydantic import ValidationError

from fixtures import git
from trustmerge.identity import Match, find_equivalents
from trustmerge.llm import (
    FakeLLMClient,
    OllamaClient,
    SameChangeVerdict,
    get_llm_client,
)


def test_same_change_verdict_validation():
    v = SameChangeVerdict(same_change=True, confidence=0.95, reasoning="Same timeout change")
    assert v.same_change is True
    assert v.confidence == 0.95
    assert v.reasoning == "Same timeout change"

    # Confidence must be between 0.0 and 1.0
    with pytest.raises(ValidationError):
        SameChangeVerdict(same_change=True, confidence=1.5, reasoning="Bad")

    with pytest.raises(ValidationError):
        SameChangeVerdict(same_change=True, confidence=-0.1, reasoning="Bad")


def test_fake_llm_client_matching():
    client = FakeLLMClient()
    patch_a = "@@ -1,2 +1,2 @@\n-TIMEOUT = 30\n+TIMEOUT = 60\n"
    patch_b = "@@ -1,2 +1,2 @@\n-TIMEOUT = 30\n+TIMEOUT = 60\n+# hotfix\n"
    verdict = client.judge_same_change(patch_a, patch_b, "Fix timeout #812", "Backport timeout #812")
    assert verdict.same_change is True
    assert verdict.confidence >= 0.7
    assert len(client.calls) == 1


def test_fake_llm_client_non_matching():
    client = FakeLLMClient()
    patch_a = "@@ -1 +1 @@\n-A = 1\n+A = 2\n"
    patch_b = "@@ -1 +1 @@\n-Z = 9\n+Z = 8\n"
    verdict = client.judge_same_change(patch_a, patch_b, "Change A", "Refactor Z")
    assert verdict.same_change is False
    assert len(client.calls) == 1


def test_fake_llm_client_custom_verdict():
    custom = SameChangeVerdict(same_change=True, confidence=0.99, reasoning="Custom verdict")
    client = FakeLLMClient(default_verdict=custom)
    verdict = client.judge_same_change("patch a", "patch b")
    assert verdict == custom


def test_get_llm_client_selects_fake_via_env(monkeypatch):
    monkeypatch.setenv("TRUSTMERGE_LLM", "fake")
    client = get_llm_client()
    assert isinstance(client, FakeLLMClient)

    monkeypatch.delenv("TRUSTMERGE_LLM", raising=False)
    monkeypatch.setenv("GITPROV_LLM", "fake")
    client = get_llm_client()
    assert isinstance(client, FakeLLMClient)


def test_find_equivalents_with_llm_judge(scenario):
    repo = scenario.repo
    # Create an edited change on dev without any trailer or reference to hotfix
    git(repo, "switch", "-q", "dev")
    git(repo, "switch", "-c", "fuzzy-branch")
    (repo / "settings.py").write_text("DEBUG = False\nTIMEOUT = 60\n# manual edit without trailer\nRETRIES = 3\nALLOWED_HOSTS = [\"example.com\"]\n")
    git(repo, "add", "settings.py")
    git(repo, "commit", "-m", "Manual bump of timeout to 60")
    cand = git(repo, "rev-parse", "HEAD").stdout.strip()

    # Without LLM: patch-id differs and no references exist
    no_llm_matches = find_equivalents(repo, scenario.hotfix, ["fuzzy-branch"], use_llm=False)
    assert no_llm_matches == []

    # With FakeLLMClient: LLM judge identifies the logical equivalence based on diff
    fake_client = FakeLLMClient()
    llm_matches = find_equivalents(
        repo, scenario.hotfix, ["fuzzy-branch"], use_llm=True, client=fake_client
    )
    assert len(llm_matches) == 1
    assert llm_matches[0].commit == cand
    assert llm_matches[0].method == "llm-judge"


def test_ollama_client_real_if_available():
    import httpx
    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=1.0)
        if r.status_code != 200:
            pytest.skip("Ollama not running locally")
    except Exception:
        pytest.skip("Ollama not reachable")

    client = OllamaClient(timeout=10.0)
    patch_a = "@@ -1,2 +1,2 @@\n-TIMEOUT = 30\n+TIMEOUT = 60\n"
    patch_b = "@@ -1,2 +1,2 @@\n-TIMEOUT = 30\n+TIMEOUT = 60\n+# comment\n"
    verdict = client.judge_same_change(
        patch_a, patch_b, "Increase timeout to 60", "Adjust timeout with note"
    )
    assert isinstance(verdict, SameChangeVerdict)
    assert verdict.same_change is True
    assert verdict.confidence > 0.5
    assert len(verdict.reasoning) > 0
