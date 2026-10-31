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


class ConflictBriefing(BaseModel):
    """Plain-English briefing explaining the conflict based strictly on collected evidence."""

    summary: str = Field(..., description="High-level plain English summary of the conflict")
    ours_assessment: str = Field(..., description="Explanation of ours origin and evidence")
    theirs_assessment: str = Field(..., description="Explanation of theirs origin and evidence")
    recommendation: str = Field(..., description="Clear recommendation on how to resolve the conflict")
    reasoning: str = Field(..., description="Detailed rationale based strictly on the provided evidence")


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

    def generate_briefing(
        self,
        file_path: str,
        ours_label: str,
        ours_code: str,
        ours_evidence_level: str,
        ours_evidence_detail: str,
        ours_reached: list[str],
        theirs_label: str,
        theirs_code: str,
        theirs_evidence_level: str,
        theirs_evidence_detail: str,
        theirs_reached: list[str],
        related_notes: list[str] = [],
    ) -> ConflictBriefing:
        """Generate a plain-English briefing of the conflict based strictly on evidence."""
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
            return SameChangeVerdict(
                same_change=False,
                confidence=0.0,
                reasoning=f"LLM evaluation unavailable: {exc}",
            )

    def generate_briefing(
        self,
        file_path: str,
        ours_label: str,
        ours_code: str,
        ours_evidence_level: str,
        ours_evidence_detail: str,
        ours_reached: list[str],
        theirs_label: str,
        theirs_code: str,
        theirs_evidence_level: str,
        theirs_evidence_detail: str,
        theirs_reached: list[str],
        related_notes: list[str] = [],
    ) -> ConflictBriefing:
        notes_str = "\n".join(f"- {n}" for n in related_notes) if related_notes else "none"
        prompt = (
            "You are a Git provenance conflict briefing assistant. Explain the conflict in plain English based ONLY on the evidence collected below.\n"
            "CRITICAL RULE: Never claim something is tested without evidence, and always show where the evidence comes from. "
            "If the evidence doesn't say it's tested, say it's unverified. Never default to 'safe'.\n\n"
            f"File: {file_path}\n"
            f"OURS ({ours_label}):\n"
            f"- Code:\n{ours_code[:2000]}\n"
            f"- Evidence level: {ours_evidence_level}\n"
            f"- Detail: {ours_evidence_detail}\n"
            f"- Branches reached: {', '.join(ours_reached) or 'none'}\n\n"
            f"THEIRS ({theirs_label}):\n"
            f"- Code:\n{theirs_code[:2000]}\n"
            f"- Evidence level: {theirs_evidence_level}\n"
            f"- Detail: {theirs_evidence_detail}\n"
            f"- Branches reached: {', '.join(theirs_reached) or 'none'}\n\n"
            f"Related Notes:\n{notes_str}\n\n"
            "Return ONLY a valid JSON object matching this schema:\n"
            "{\n"
            '  "summary": "plain English summary",\n'
            '  "ours_assessment": "assessment of ours code and evidence",\n'
            '  "theirs_assessment": "assessment of theirs code and evidence",\n'
            '  "recommendation": "concrete recommendation",\n'
            '  "reasoning": "rationale based strictly on evidence"\n'
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
                return ConflictBriefing.model_validate_json(raw_text)
        except Exception as exc:
            return ConflictBriefing(
                summary=f"Conflict in {file_path} between {ours_label} ({ours_evidence_level}) and {theirs_label} ({theirs_evidence_level}).",
                ours_assessment=f"Ours ({ours_label}) has {ours_evidence_level} evidence ({ours_evidence_detail}).",
                theirs_assessment=f"Theirs ({theirs_label}) has {theirs_evidence_level} evidence ({theirs_evidence_detail}).",
                recommendation=f"Prefer {ours_label} if tested ({ours_evidence_level}), otherwise verify before merging.",
                reasoning=f"LLM briefing fallback due to: {exc}",
            )


class FakeLLMClient:
    """Deterministic fake client for CI and tests where Ollama is not running."""

    def __init__(
        self,
        default_verdict: Optional[SameChangeVerdict] = None,
        default_briefing: Optional[ConflictBriefing] = None,
    ):
        self.default_verdict = default_verdict
        self.default_briefing = default_briefing
        self.calls: list[dict] = []
        self.briefing_calls: list[dict] = []

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

    def generate_briefing(
        self,
        file_path: str,
        ours_label: str,
        ours_code: str,
        ours_evidence_level: str,
        ours_evidence_detail: str,
        ours_reached: list[str],
        theirs_label: str,
        theirs_code: str,
        theirs_evidence_level: str,
        theirs_evidence_detail: str,
        theirs_reached: list[str],
        related_notes: list[str] = [],
    ) -> ConflictBriefing:
        self.briefing_calls.append(
            {
                "file_path": file_path,
                "ours_label": ours_label,
                "theirs_label": theirs_label,
            }
        )
        if self.default_briefing is not None:
            return self.default_briefing

        return ConflictBriefing(
            summary=f"Conflict in {file_path}: {ours_label} ({ours_evidence_level}) vs {theirs_label} ({theirs_evidence_level}).",
            ours_assessment=f"{ours_label} is {ours_evidence_level} ({ours_evidence_detail}), reached: {', '.join(ours_reached) or 'none'}.",
            theirs_assessment=f"{theirs_label} is {theirs_evidence_level} ({theirs_evidence_detail}), reached: {', '.join(theirs_reached) or 'none'}.",
            recommendation=(
                f"Accept {ours_label} over unverified changes."
                if ours_evidence_level in ("PROD", "UAT", "CI-PASSED") and theirs_evidence_level == "UNVERIFIED"
                else "Resolve based on verified evidence."
            ),
            reasoning=(
                "Never claim code is tested without recorded evidence. "
                f"Ours has {ours_evidence_level} evidence while theirs is {theirs_evidence_level}."
            ),
        )


def get_llm_client() -> LLMClient:
    """Return configured LLM client: FakeLLMClient in CI, OllamaClient locally."""
    mode = os.environ.get("TRUSTMERGE_LLM") or os.environ.get("GITPROV_LLM")
    if mode == "fake":
        return FakeLLMClient()
    return OllamaClient()
