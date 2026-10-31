"""LLM client for judging fuzzy matches and generating briefings via Ollama (with fake fallback for CI)."""

import json
import os
from pathlib import Path
import re
from typing import Optional, Protocol

import httpx
from pydantic import BaseModel, Field


class SameChangeVerdict(BaseModel):
    """Structured verdict from LLM on whether two patches are the same logical change."""

    same_change: bool = Field(
        ..., description="Whether these two patches/commits represent the same logical change"
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0"
    )
    reasoning: str = Field(
        ..., description="Brief plain-English explanation of why they are or are not the same change"
    )


class LLMClient(Protocol):
    """Protocol for LLM interactions."""

    def judge_same_change(
        self,
        patch_a: str,
        patch_b: str,
        message_a: str = "",
        message_b: str = "",
    ) -> SameChangeVerdict:
        """Judge whether two patches represent the same logical change."""
        ...


class OllamaClient:
    """Client for local Ollama instance (http://localhost:11434)."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = (
            model
            or os.environ.get("TRUSTMERGE_MODEL")
            or os.environ.get("GITPROV_MODEL")
            or "qwen2.5-coder:7b"
        )
        self.timeout = timeout

    def judge_same_change(
        self,
        patch_a: str,
        patch_b: str,
        message_a: str = "",
        message_b: str = "",
    ) -> SameChangeVerdict:
        prompt = (
            "You are an expert Git provenance analyst. Analyze these two git changes and decide "
            "if they represent the same logical change (even if formatted, edited, or backported differently).\n\n"
            f"Commit Message A:\n{message_a}\n\n"
            f"Patch A:\n{patch_a[:4000]}\n\n"
            f"Commit Message B:\n{message_b}\n\n"
            f"Patch B:\n{patch_b[:4000]}\n\n"
            "Return ONLY a valid JSON object matching this schema:\n"
            "{\n"
            '  "same_change": true or false,\n'
            '  "confidence": float between 0.0 and 1.0,\n'
            '  "reasoning": "brief explanation"\n'
            "}"
        )
        payload = {
            "model": self.model,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "options": {
                "temperature": 0.0,
                "num_ctx": 16384,
            },
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(f"{self.base_url}/api/generate", json=payload)
                resp.raise_for_status()
                data = resp.json()
                raw_text = data.get("response", "{}")
                return SameChangeVerdict.model_validate_json(raw_text)
        except Exception as exc:
            # Fallback if Ollama is unreachable or response invalid
            return SameChangeVerdict(
                same_change=False,
                confidence=0.0,
                reasoning=f"LLM evaluation unavailable: {exc}",
            )


class FakeLLMClient:
    """Deterministic fake client for CI and tests where Ollama is not running."""

    def __init__(self, default_verdict: Optional[SameChangeVerdict] = None):
        self.default_verdict = default_verdict
        self.calls: list[dict] = []

    def judge_same_change(
        self,
        patch_a: str,
        patch_b: str,
        message_a: str = "",
        message_b: str = "",
    ) -> SameChangeVerdict:
        self.calls.append(
            {
                "patch_a": patch_a,
                "patch_b": patch_b,
                "message_a": message_a,
                "message_b": message_b,
            }
        )
        if self.default_verdict is not None:
            return self.default_verdict

        # Deterministic diff and ticket heuristic
        def extract_added(patch: str) -> list[str]:
            lines = []
            for line in patch.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    code = line[1:].strip()
                    if code and not code.startswith("#") and not code.startswith("//"):
                        lines.append(code)
            return lines

        added_a = set(extract_added(patch_a))
        added_b = set(extract_added(patch_b))
        shared_code = added_a.intersection(added_b)
        if shared_code:
            return SameChangeVerdict(
                same_change=True,
                confidence=0.92,
                reasoning=f"Both patches add identical logical code: {list(shared_code)[:2]}",
            )

        # Check for shared ticket / PR numbers
        tickets_a = set(re.findall(r"(?:#|GH-)\d+", f"{message_a}\n{patch_a}"))
        tickets_b = set(re.findall(r"(?:#|GH-)\d+", f"{message_b}\n{patch_b}"))
        shared_tickets = tickets_a.intersection(tickets_b)
        if shared_tickets:
            return SameChangeVerdict(
                same_change=True,
                confidence=0.90,
                reasoning=f"Both changes reference ticket {list(shared_tickets)[0]}",
            )

        return SameChangeVerdict(
            same_change=False,
            confidence=0.85,
            reasoning="Patches introduce different code changes without shared ticket references",
        )


def get_llm_client() -> LLMClient:
    """Return configured LLM client: FakeLLMClient in CI, OllamaClient locally."""
    mode = os.environ.get("TRUSTMERGE_LLM") or os.environ.get("GITPROV_LLM")
    if mode == "fake":
        return FakeLLMClient()
    return OllamaClient()
