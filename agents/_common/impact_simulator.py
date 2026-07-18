"""Classifies whether a downstream dbt model's SQL would actually break from a
SchemaDiff -- not just "it's connected in lineage" (see CLAUDE.md section 4).

Only used as part of the mock (BLAST_MOCK_LLM=1) backend of
breakage_classifier.py's MockBreakageClassifier now, for the column-shaped
(dbt-style) case specifically -- deterministic and free, good enough to
replay the bundled demo offline. The production path
(breakage_classifier.py's OpenAIBreakageClassifier) doesn't use this
module: it asks the LLM to reason about the downstream asset's definition
directly, which generalizes to non-SQL downstream assets this AST scanner
was never meant to handle. See docs/architecture.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import sqlglot
from sqlglot import exp

from datahub_client import DownstreamAsset
from diff_parser import ColumnChange, SchemaDiff
from sql_utils import render_jinja_refs

Verdict = Literal["hard_break", "silent_risk", "safe"]

VERDICT_RANK = {"safe": 0, "silent_risk": 1, "hard_break": 2}
VERDICT_EMOJI = {"hard_break": "🔴", "silent_risk": "🟡", "safe": "🟢"}

_AGGREGATE_OR_ARITHMETIC = (exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Add, exp.Sub, exp.Mul, exp.Div)


@dataclass
class Reason:
    change: ColumnChange
    verdict: Verdict
    detail: str


@dataclass
class Finding:
    asset: DownstreamAsset
    verdict: Verdict
    reasons: list[Reason] = field(default_factory=list)

    @property
    def emoji(self) -> str:
        return VERDICT_EMOJI[self.verdict]


def _referenced_columns(sql: str) -> dict[str, list[exp.Column]]:
    parsed = sqlglot.parse_one(render_jinja_refs(sql))
    by_name: dict[str, list[exp.Column]] = {}
    for col in parsed.find_all(exp.Column):
        by_name.setdefault(col.name, []).append(col)
    return by_name


def _in_aggregate_or_arithmetic(column: exp.Column) -> bool:
    node = column.parent
    while node is not None:
        if isinstance(node, _AGGREGATE_OR_ARITHMETIC):
            return True
        node = node.parent
    return False


def classify(diff: SchemaDiff, asset: DownstreamAsset) -> Finding:
    if not asset.view_logic:
        return Finding(
            asset=asset,
            verdict="silent_risk",
            reasons=[
                Reason(
                    change=ColumnChange(kind="dropped", column="?"),
                    verdict="silent_risk",
                    detail="No SQL definition available for this asset -- could not simulate, verify manually.",
                )
            ],
        )

    columns_by_name = _referenced_columns(asset.view_logic)
    reasons: list[Reason] = []

    for change in diff.changes:
        refs = columns_by_name.get(change.column)
        if not refs:
            continue

        if change.kind in ("dropped", "renamed"):
            gone_because = f"was renamed to `{change.renamed_to}`" if change.renamed_to else "was dropped"
            reasons.append(
                Reason(
                    change=change,
                    verdict="hard_break",
                    detail=f"references `{change.column}`, which {gone_because} upstream and no longer exists.",
                )
            )
        elif change.kind == "type_changed":
            if any(_in_aggregate_or_arithmetic(r) for r in refs):
                detail = (
                    f"aggregates/computes over `{change.column}`, whose type is changing from "
                    f"{change.old_type} to {change.new_type} -- may silently change results "
                    "(e.g. precision loss) without erroring."
                )
            else:
                detail = (
                    f"selects `{change.column}`, whose type is changing from {change.old_type} to "
                    f"{change.new_type} -- downstream consumers may see different formatting/precision."
                )
            reasons.append(Reason(change=change, verdict="silent_risk", detail=detail))
        # "added" columns can't break a query that doesn't reference them yet.

    if not reasons:
        return Finding(asset=asset, verdict="safe", reasons=[])

    worst = max((r.verdict for r in reasons), key=lambda v: VERDICT_RANK[v])
    return Finding(asset=asset, verdict=worst, reasons=reasons)


def simulate(diff: SchemaDiff, downstream: list[DownstreamAsset]) -> list[Finding]:
    return [classify(diff, asset) for asset in downstream]
