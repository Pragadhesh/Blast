"""Turns a before/after pair of a dbt model (.sql) or schema doc (.yml) into a
structured SchemaDiff that impact_simulator.py can reason about.

Only used as the mock (BLAST_MOCK_LLM=1) backend of
change_interpreter.py's MockChangeInterpreter now -- deterministic and
free, good enough to replay the bundled demo offline. The production path
(change_interpreter.py's OpenAIChangeInterpreter) doesn't use this module:
it hands the raw diff to the LLM instead, which generalizes to any file
format this sqlglot-based parser was never meant to handle (Terraform,
DDL, anything else). See docs/architecture.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import sqlglot
import yaml
from sqlglot import exp

from sql_utils import render_jinja_refs

ChangeKind = Literal["dropped", "added", "renamed", "type_changed"]


@dataclass(frozen=True)
class ColumnChange:
    kind: ChangeKind
    column: str
    old_type: str | None = None
    new_type: str | None = None
    renamed_to: str | None = None

    def __str__(self) -> str:
        if self.kind == "dropped":
            return f"dropped `{self.column}`"
        if self.kind == "added":
            return f"added `{self.column}`"
        if self.kind == "renamed":
            return f"renamed `{self.column}` -> `{self.renamed_to}`"
        return f"changed `{self.column}` type: {self.old_type} -> {self.new_type}"


@dataclass
class SchemaDiff:
    model_name: str
    changes: list[ColumnChange] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.changes

    def changed_column_names(self) -> set[str]:
        names: set[str] = set()
        for c in self.changes:
            names.add(c.column)
            if c.renamed_to:
                names.add(c.renamed_to)
        return names


def _select_columns(sql: str) -> dict[str, exp.Expression]:
    """Map each output column name of a model's final SELECT to its expression."""
    parsed = sqlglot.parse_one(render_jinja_refs(sql))
    select = parsed if isinstance(parsed, exp.Select) else parsed.find(exp.Select)
    if select is None:
        return {}
    return {projection.alias_or_name: projection for projection in select.selects}


def diff_sql_model(old_sql: str, new_sql: str, model_name: str) -> SchemaDiff:
    old_cols = _select_columns(old_sql)
    new_cols = _select_columns(new_sql)

    dropped = set(old_cols) - set(new_cols)
    added = set(new_cols) - set(old_cols)

    renamed_pairs: dict[str, str] = {}
    for new_name in added:
        projection = new_cols[new_name]
        source = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(source, exp.Column) and source.name in dropped:
            renamed_pairs[source.name] = new_name

    changes: list[ColumnChange] = []
    for old_name, new_name in renamed_pairs.items():
        changes.append(ColumnChange(kind="renamed", column=old_name, renamed_to=new_name))
        dropped.discard(old_name)
        added.discard(new_name)

    for name in sorted(dropped):
        changes.append(ColumnChange(kind="dropped", column=name))
    for name in sorted(added):
        changes.append(ColumnChange(kind="added", column=name))

    return SchemaDiff(model_name=model_name, changes=changes)


def _parse_schema_yml(yaml_text: str) -> dict[str, dict[str, str | None]]:
    data = yaml.safe_load(yaml_text) or {}
    result: dict[str, dict[str, str | None]] = {}
    for model in data.get("models") or []:
        name = model.get("name")
        if not name:
            continue
        result[name] = {
            col["name"]: col.get("data_type")
            for col in (model.get("columns") or [])
            if col.get("name")
        }
    return result


def diff_schema_yml(old_yaml_text: str, new_yaml_text: str) -> list[SchemaDiff]:
    old_models = _parse_schema_yml(old_yaml_text)
    new_models = _parse_schema_yml(new_yaml_text)

    diffs: list[SchemaDiff] = []
    for model_name in sorted(set(old_models) | set(new_models)):
        old_cols = old_models.get(model_name, {})
        new_cols = new_models.get(model_name, {})

        dropped = set(old_cols) - set(new_cols)
        added = set(new_cols) - set(old_cols)
        common = set(old_cols) & set(new_cols)

        # heuristic: a dropped/added pair with an identical declared type is
        # treated as a rename rather than a drop+add, since schema.yml has no
        # explicit "renamed_from" field to say so directly.
        renamed_pairs: dict[str, str] = {}
        for old_name in list(dropped):
            match = next(
                (
                    new_name
                    for new_name in added
                    if new_name not in renamed_pairs.values() and new_cols[new_name] == old_cols[old_name]
                ),
                None,
            )
            if match:
                renamed_pairs[old_name] = match

        changes: list[ColumnChange] = []
        for old_name, new_name in renamed_pairs.items():
            changes.append(
                ColumnChange(
                    kind="renamed",
                    column=old_name,
                    renamed_to=new_name,
                    old_type=old_cols[old_name],
                    new_type=new_cols[new_name],
                )
            )
            dropped.discard(old_name)
            added.discard(new_name)

        for name in sorted(dropped):
            changes.append(ColumnChange(kind="dropped", column=name, old_type=old_cols[name]))
        for name in sorted(added):
            changes.append(ColumnChange(kind="added", column=name, new_type=new_cols[name]))
        for name in sorted(common):
            if old_cols[name] != new_cols[name]:
                changes.append(
                    ColumnChange(
                        kind="type_changed",
                        column=name,
                        old_type=old_cols[name],
                        new_type=new_cols[name],
                    )
                )

        if changes:
            diffs.append(SchemaDiff(model_name=model_name, changes=changes))

    return diffs


def model_name_from_path(file_path: str) -> str:
    stem = file_path.rsplit("/", 1)[-1]
    return stem[:-4] if stem.endswith(".sql") else stem


def diff_dbt_file(file_path: str, old_content: str, new_content: str) -> list[SchemaDiff]:
    if file_path.endswith((".yml", ".yaml")):
        return diff_schema_yml(old_content, new_content)
    if file_path.endswith(".sql"):
        return [diff_sql_model(old_content, new_content, model_name_from_path(file_path))]
    raise ValueError(f"Unsupported dbt file type: {file_path}")


def merge_diffs(diffs: list[SchemaDiff]) -> list[SchemaDiff]:
    """Combine diffs for the same model coming from multiple changed files in one
    PR (e.g. a model's .sql body and its schema.yml both change)."""
    merged: dict[str, SchemaDiff] = {}
    for d in diffs:
        if d.is_empty:
            continue
        existing = merged.setdefault(d.model_name, SchemaDiff(model_name=d.model_name))
        seen = {(c.kind, c.column, c.renamed_to) for c in existing.changes}
        for c in d.changes:
            key = (c.kind, c.column, c.renamed_to)
            if key not in seen:
                existing.changes.append(c)
                seen.add(key)
    return list(merged.values())
