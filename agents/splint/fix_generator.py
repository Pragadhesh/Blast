"""Generates a corrected version of a downstream model's SQL, given the
specific column change(s) that broke it. Same pluggable-provider pattern as
blast-scan/report_generator.py (mock / OpenAI), so Splint is demoable and
testable without burning API budget -- BLAST_MOCK_LLM=1 uses the mock path.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod

from change_interpreter import InterpretedChange

_PROMPT_TEMPLATE = """You are Splint, a CI bot that repairs dbt models broken by an upstream schema change.

Downstream model: {model_name}

Upstream change(s) that broke it:
{changes}

Why it's broken:
{reasons}

Current SQL:
{sql}

Rewrite the SQL so it works again against the new upstream schema. Keep the model's
existing structure, formatting, and intent -- change only what's necessary to fix the
break (e.g. update a renamed column reference, adjust a cast for a retyped column).
Do not touch dbt Jinja macros like {{{{ ref(...) }}}} or {{{{ source(...) }}}}.
Return ONLY the corrected SQL. No explanation, no markdown code fences."""


def _render_prompt(model_name: str, sql: str, changes: list[InterpretedChange], reasons: list[str]) -> str:
    changes_text = "\n".join(f"- {c}" for c in changes) or "(none)"
    reasons_text = "\n".join(f"- {r}" for r in reasons) or "(none)"
    return _PROMPT_TEMPLATE.format(model_name=model_name, sql=sql, changes=changes_text, reasons=reasons_text)


class FixGenerator(ABC):
    @abstractmethod
    def generate_fix(self, model_name: str, sql: str, changes: list[InterpretedChange], reasons: list[str]) -> str: ...


class MockFixGenerator(FixGenerator):
    """Deterministic, no-network fix: renames a renamed column's references
    to its new name. Used in tests/dev (BLAST_MOCK_LLM=1) to avoid burning
    API budget -- it can only mechanically fix a rename, not a retype or
    anything requiring real judgment, which is exactly what makes it safe
    to run with no model behind it.
    """

    def generate_fix(self, model_name: str, sql: str, changes: list[InterpretedChange], reasons: list[str]) -> str:
        fixed = sql
        for change in changes:
            if change.kind == "renamed" and change.old and change.new:
                fixed = re.sub(rf"\b{re.escape(change.old)}\b", change.new, fixed)
        return fixed


class OpenAIFixGenerator(FixGenerator):
    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None):
        from openai import OpenAI  # lazy import so the mock path doesn't need the package installed

        self._client = OpenAI(api_key=api_key or os.environ["OPENAI_API_KEY"])
        self._model = model

    def generate_fix(self, model_name: str, sql: str, changes: list[InterpretedChange], reasons: list[str]) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": _render_prompt(model_name, sql, changes, reasons)}],
            temperature=0.1,
            max_tokens=1500,
        )
        return response.choices[0].message.content.strip()


def get_fix_generator() -> FixGenerator:
    if os.environ.get("BLAST_MOCK_LLM", "0") == "1":
        return MockFixGenerator()
    return OpenAIFixGenerator()
