"""Generates the plain-English risk summary for the PR comment.

gpt-4o-mini is the default backend (CLAUDE.md section 6: cheap, fast, sufficient
for summarization -- don't upgrade the model here). Set BLAST_MOCK_LLM=1 during
development/tests to skip the API call entirely and conserve budget; an Ollama
backend is available as a local fallback (BLAST_LLM_PROVIDER=ollama) if OpenAI
quota ever becomes a risk before the demo recording.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod

from breakage_classifier import Finding
from change_interpreter import InterpretedChange

_PROMPT_TEMPLATE = """You are Blast, a CI bot that reviews dbt schema changes for breaking downstream impact.

Changed model: {model_name}
Schema changes:
{changes}

Downstream simulation results:
{findings}

Write a concise (3-5 sentence) plain-English summary for a PR comment aimed at the engineer who opened the PR.
Call out hard breaks and silent risks by name, and suggest one concrete fix (e.g. add a compatibility alias,
update the downstream model, or coordinate with the owning team) if there is at least one hard break or silent risk.
Do not use markdown headers. Keep it tight."""


def _render_prompt(model_name: str, changes: list[InterpretedChange], findings: list[Finding]) -> str:
    changes_text = "\n".join(f"- {c}" for c in changes) or "(none)"
    findings_text = (
        "\n".join(f"- {f.emoji} {f.asset.name} ({f.verdict}): " + "; ".join(f.reasons) for f in findings) or "(none)"
    )
    return _PROMPT_TEMPLATE.format(model_name=model_name, changes=changes_text, findings=findings_text)


class Summarizer(ABC):
    @abstractmethod
    def summarize(self, model_name: str, changes: list[InterpretedChange], findings: list[Finding]) -> str: ...


class MockSummarizer(Summarizer):
    """Deterministic, no-network summary -- used in tests/dev to avoid burning API budget."""

    def summarize(self, model_name: str, changes: list[InterpretedChange], findings: list[Finding]) -> str:
        hard = [f for f in findings if f.verdict == "hard_break"]
        risky = [f for f in findings if f.verdict == "silent_risk"]

        change_desc = ", ".join(str(c) for c in changes) or "no schema changes"
        lines = [f"Changing `{model_name}` ({change_desc}) affects {len(findings)} downstream model(s)."]
        if hard:
            lines.append(f"{len(hard)} will break outright: " + ", ".join(f.asset.name for f in hard) + ".")
        if risky:
            lines.append(f"{len(risky)} carry silent risk: " + ", ".join(f.asset.name for f in risky) + ".")
        if not hard and not risky:
            lines.append("No downstream breakage detected.")
        return " ".join(lines)


class OpenAISummarizer(Summarizer):
    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None):
        from openai import OpenAI  # lazy import so mock/CI paths don't need the package installed

        self._client = OpenAI(api_key=api_key or os.environ["OPENAI_API_KEY"])
        self._model = model

    def summarize(self, model_name: str, changes: list[InterpretedChange], findings: list[Finding]) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": _render_prompt(model_name, changes, findings)}],
            temperature=0.2,
            max_tokens=300,
        )
        return response.choices[0].message.content.strip()


class OllamaSummarizer(Summarizer):
    """Local fallback backend -- no per-call cost, useful if OpenAI quota runs out."""

    def __init__(self, model: str = "llama3", host: str | None = None):
        self._model = model
        self._host = (host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")

    def summarize(self, model_name: str, changes: list[InterpretedChange], findings: list[Finding]) -> str:
        import requests

        resp = requests.post(
            f"{self._host}/api/generate",
            json={"model": self._model, "prompt": _render_prompt(model_name, changes, findings), "stream": False},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["response"].strip()


def get_summarizer() -> Summarizer:
    if os.environ.get("BLAST_MOCK_LLM", "0") == "1":
        return MockSummarizer()
    if os.environ.get("BLAST_LLM_PROVIDER") == "ollama":
        return OllamaSummarizer()
    return OpenAISummarizer()
