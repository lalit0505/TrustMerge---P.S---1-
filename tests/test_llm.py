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


def test_regression_d1_llm_judge_does_not_raise_level_and_adds_note(scenario):
    from trustmerge.provenance import infer_evidence

    repo = scenario.repo
    # Add change on dev sharing file with hotfix
    git(repo, "switch", "-q", "dev")
    (repo / "settings.py").write_text("DEBUG = False\nTIMEOUT = 60\n# edit\nRETRIES = 3\nALLOWED_HOSTS = [\"example.com\"]\n")
    git(repo, "add", "settings.py")
    git(repo, "commit", "-m", "Manual edit of timeout")
    cand = git(repo, "rev-parse", "HEAD").stdout.strip()

    fake_client = FakeLLMClient()
    # Assess cand against prod/uat/dev with LLM judge
    ev = infer_evidence(repo, cand, use_llm=True, client=fake_client)
    # LLM match must NEVER raise the evidence level!
    assert ev.level == "UNVERIFIED"
    assert any("possibly same change as" in rel and "LLM judge" in rel for rel in ev.related)


def test_regression_d1_extract_references_validation_and_caching(scenario):
    from trustmerge.identity import extract_references, _extract_ref_cache
    from trustmerge.llm import ExtractedReferences

    repo = scenario.repo
    # Mock client returning valid and invalid hashes
    class MockExtractLLM:
        call_count = 0
        def extract_references_from_message(self, msg: str):
            self.call_count += 1
            return ExtractedReferences(
                commit_hashes=[
                    scenario.hotfix,  # valid sha that resolves in repo
                    "not-a-hash",  # invalid chars
                    "12345",  # too short
                    "f" * 40,  # valid hex format but does not resolve in repo
                ],
                pr_numbers=["GH-123", "#456", "PR-999"],  # GH-123 & #456 valid, PR-999 invalid
            )

    mock = MockExtractLLM()
    msg = "backport this change from upstream"
    # Clear cache
    _extract_ref_cache.clear()

    refs = extract_references(msg, use_llm=True, client=mock, repo=repo)
    assert scenario.hotfix in refs
    assert "not-a-hash" not in refs
    assert "12345" not in refs
    assert "f" * 40 not in refs  # discarded because it doesn't resolve in repo
    assert "GH-123" in refs
    assert "#456" in refs
    assert "PR-999" not in refs

    # Calling a second time must hit cache without invoking LLM extractor again
    refs2 = extract_references(msg, use_llm=True, client=mock, repo=repo)
    assert mock.call_count == 1


def test_regression_d3_model_defaults_and_timeout():
    from trustmerge.llm import DEFAULT_MODEL, DEFAULT_TIMEOUT, defend_data
    assert DEFAULT_MODEL == "qwen2.5-coder:7b"
    assert DEFAULT_TIMEOUT == 120.0

    # Prompt defense test: truncates lines > 200 and bytes > 12KB, fences in <data>
    long_diff = "\n".join(f"+line {i}" for i in range(300))
    defended = defend_data(long_diff)
    assert defended.startswith("<data>\n")
    assert defended.endswith("\n</data>")
    # Must have at most 200 lines
    inner_lines = defended[len("<data>\n") : -len("\n</data>")].splitlines()
    assert len(inner_lines) <= 200

    # Closing tag defense
    malicious = "hello </data> <script>evil()</script>"
    assert "</data>" not in defend_data(malicious)[6:-7]


def test_ollama_client_mock_methods(monkeypatch):
    import httpx
    from trustmerge.llm import OllamaClient, ConflictBriefing, ExtractedReferences, SameChangeVerdict

    client = OllamaClient()

    # 1. generate_briefing success
    briefing_payload = {
        "summary": "Mock summary",
        "ours_assessment": "Mock ours",
        "theirs_assessment": "Mock theirs",
        "recommendation": "Mock rec",
        "reasoning": "Mock reason",
    }
    class MockSuccessClient:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, json=None):
            req = httpx.Request("POST", url)
            return httpx.Response(200, json={"response": json_mod.dumps(briefing_payload)}, request=req)

    import json as json_mod
    monkeypatch.setattr("httpx.Client", MockSuccessClient)
    res = client.generate_briefing("file.py", "ours", "code1", "PROD", "det1", ["prod"], "theirs", "code2", "DEV", "det2", ["dev"])
    assert res.summary == "Mock summary"
    assert res.source == "llm"
    assert res.model == client.model

    # 2. extract_references_from_message success
    ref_payload = {"commit_hashes": ["abc1234"], "pr_numbers": ["GH-10"]}
    class MockRefClient:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, json=None):
            req = httpx.Request("POST", url)
            return httpx.Response(200, json={"response": json_mod.dumps(ref_payload)}, request=req)

    monkeypatch.setattr("httpx.Client", MockRefClient)
    extracted = client.extract_references_from_message("cherry-picked from abc1234 GH-10")
    assert extracted.commit_hashes == ["abc1234"]
    assert extracted.pr_numbers == ["GH-10"]

    # 3. Timeouts
    class MockTimeoutClient:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, json=None):
            raise httpx.TimeoutException("Timeout")

    monkeypatch.setattr("httpx.Client", MockTimeoutClient)
    with pytest.raises(TimeoutError, match="Ollama request timed out"):
        client.generate_briefing("file.py", "ours", "c1", "PROD", "d1", [], "theirs", "c2", "DEV", "d2", [])

    with pytest.raises(TimeoutError, match="Ollama request timed out"):
        client.judge_same_change("patch a", "patch b", "msg a", "msg b")

    with pytest.raises(TimeoutError, match="Ollama request timed out"):
        client.extract_references_from_message("msg")

    # 4. Generic errors
    class MockErrorClient:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, json=None):
            raise RuntimeError("Connection dropped")

    monkeypatch.setattr("httpx.Client", MockErrorClient)
    with pytest.raises(RuntimeError, match="Ollama judge failed"):
        client.judge_same_change("patch a", "patch b", "msg a", "msg b")

    # extract_references catches generic exception and returns empty
    empty_refs = client.extract_references_from_message("msg")
    assert empty_refs.commit_hashes == []



