"""Generic, format-agnostic change interpretation: given a file's old and
new content, ask the LLM what changed and what DataHub entity this file
represents.

This replaces a per-file-format detector (a SQL parser, an HCL parser, a
Tableau-config parser, ...) with one interpreter that works for anything an
LLM can read. Hand-writing a detector per format doesn't scale to "works
across an entire org's varied tech stack" -- the next team's Airflow DAG or
Snowflake script would need yet another detector. An LLM already
understands SQL, HCL, YAML, XML, Python, etc. natively; asking it to
describe a diff generalizes for free where a parser wouldn't.

Same pluggable-provider pattern as report_generator.py / fix_generator.py:
BLAST_MOCK_LLM=1 uses a deterministic, network-free mock -- but the mock is
scoped honestly: it reuses the old sqlglot/YAML-based diff_parser.py logic,
which only understands dbt-model-shaped SQL and schema.yml. That's enough
to reproduce the bundled demo offline; it is NOT a general parser and isn't
meant to be one. The real (OpenAI) backend is the actual format-agnostic
path -- see docs/architecture.md.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class InterpretedChange:
    kind: str  # "renamed" | "dropped" | "added" | "type_changed" | "resource_renamed" | "resource_deleted"
    old: str | None = None
    new: str | None = None

    def __str__(self) -> str:
        if self.kind == "renamed":
            return f"renamed `{self.old}` -> `{self.new}`"
        if self.kind == "resource_renamed":
            return f"resource renamed `{self.old}` -> `{self.new}`"
        if self.kind == "dropped":
            return f"dropped `{self.old}`"
        if self.kind == "resource_deleted":
            return f"resource deleted: `{self.old}`"
        if self.kind == "added":
            return f"added `{self.new}`"
        if self.kind == "type_changed":
            return f"changed `{self.old}` -> `{self.new}`"
        return f"{self.kind}: {self.old} -> {self.new}"


@dataclass
class InterpretedFile:
    file_path: str
    entity_name: str | None
    platform_hint: str | None = None
    schema_hint: str | None = None
    changes: list[InterpretedChange] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.entity_name or not self.changes


_PROMPT_TEMPLATE = """You are analyzing a file change in a data platform repository to determine
whether it changes a data asset's schema or identity -- a table, a storage
bucket, a streaming topic, a BI resource, anything a data catalog like
DataHub might track lineage for.

File path: {file_path}

--- OLD VERSION ---
{old_content}

--- NEW VERSION ---
{new_content}

Respond with ONLY a JSON object, no other text, in this exact shape:
{{
  "entity_name": "<the name of the data asset this file defines/configures, or null if this file isn't a data asset definition at all>",
  "platform_hint": "<your best guess of the underlying platform, e.g. postgres, s3, kafka, mongodb, metabase, dbt, or null>",
  "schema_hint": "<your best guess of the database schema/namespace this asset lives in, e.g. raw, public, analytics -- or null if not applicable/unknown>",
  "changes": [
    {{"kind": "renamed", "old": "<old identifier>", "new": "<new identifier>"}},
    {{"kind": "dropped", "old": "<identifier>"}},
    {{"kind": "added", "new": "<identifier>"}},
    {{"kind": "type_changed", "old": "<old type/identifier>", "new": "<new type/identifier>"}},
    {{"kind": "resource_renamed", "old": "<old resource identifier, e.g. a bucket or topic name>", "new": "<new resource identifier>"}},
    {{"kind": "resource_deleted", "old": "<identifier>"}}
  ]
}}
Use renamed/dropped/added/type_changed for changes to a *column or field*
within an asset. Use resource_renamed/resource_deleted for changes to the
*identity of the whole asset* (e.g. an S3 bucket or Kafka topic renamed).
If nothing schema-relevant changed (formatting, comments, unrelated code),
return an empty "changes" list."""


class ChangeInterpreter(ABC):
    @abstractmethod
    def interpret(self, file_path: str, old_content: str, new_content: str) -> InterpretedFile: ...


def _to_interpreted_change(c) -> InterpretedChange:
    if c.kind == "renamed":
        return InterpretedChange(kind="renamed", old=c.column, new=c.renamed_to)
    if c.kind == "dropped":
        return InterpretedChange(kind="dropped", old=c.column)
    if c.kind == "added":
        return InterpretedChange(kind="added", new=c.column)
    if c.kind == "type_changed":
        return InterpretedChange(
            kind="type_changed",
            old=f"{c.column} {c.old_type}" if c.old_type else c.column,
            new=f"{c.column} {c.new_type}" if c.new_type else c.column,
        )
    return InterpretedChange(kind=c.kind, old=c.column)


class MockChangeInterpreter(ChangeInterpreter):
    """Deterministic, no-network interpreter for BLAST_MOCK_LLM=1. Only
    understands dbt-model-shaped .sql files and dbt-style schema.yml --
    reuses the proven diff_parser.py logic rather than duplicating it.
    Enough to replay the bundled demo offline; not a general parser.
    """

    def interpret(self, file_path: str, old_content: str, new_content: str) -> InterpretedFile:
        from diff_parser import diff_dbt_file, model_name_from_path

        try:
            diffs = diff_dbt_file(file_path, old_content, new_content)
        except ValueError:
            return InterpretedFile(file_path=file_path, entity_name=None)

        if not diffs:
            return InterpretedFile(file_path=file_path, entity_name=None)

        # schema.yml can describe multiple models; .sql is always one. This
        # mock only needs to handle the bundled demo, which changes one
        # model at a time, so collapse to the first non-empty diff.
        diff = next((d for d in diffs if not d.is_empty), diffs[0])
        changes = [_to_interpreted_change(c) for c in diff.changes]
        entity_name = diff.model_name or model_name_from_path(file_path)
        return InterpretedFile(
            file_path=file_path, entity_name=entity_name, platform_hint="dbt", schema_hint="public", changes=changes
        )


class OpenAIChangeInterpreter(ChangeInterpreter):
    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None):
        from openai import OpenAI  # lazy import so the mock path doesn't need the package installed

        self._client = OpenAI(api_key=api_key or os.environ["OPENAI_API_KEY"])
        self._model = model

    def interpret(self, file_path: str, old_content: str, new_content: str) -> InterpretedFile:
        prompt = _PROMPT_TEMPLATE.format(file_path=file_path, old_content=old_content, new_content=new_content)
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=800,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return InterpretedFile(file_path=file_path, entity_name=None)

        changes = [
            InterpretedChange(kind=c.get("kind", "unknown"), old=c.get("old"), new=c.get("new"))
            for c in (parsed.get("changes") or [])
            if c.get("kind")
        ]
        return InterpretedFile(
            file_path=file_path,
            entity_name=parsed.get("entity_name"),
            platform_hint=parsed.get("platform_hint"),
            schema_hint=parsed.get("schema_hint"),
            changes=changes,
        )


def get_change_interpreter() -> ChangeInterpreter:
    if os.environ.get("BLAST_MOCK_LLM", "0") == "1":
        return MockChangeInterpreter()
    return OpenAIChangeInterpreter()
