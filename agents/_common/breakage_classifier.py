"""Generic, format-agnostic downstream breakage classification: given a
description of what changed upstream and a downstream asset's known
definition, decide whether the downstream asset breaks.

Replaces impact_simulator.py's sqlglot-AST column-reference scan as the
*production* path, for the same reason change_interpreter.py replaces the
sqlglot-based diff parser: an AST scanner only understands SQL, and
classification needs to work for any downstream asset DataHub knows about
-- SQL or not (a Tableau workbook, an Airflow DAG, anything with only a
text description). needs_review is the honest degrade when there's nothing
to reason from, instead of guessing.

Same pluggable-provider pattern as report_generator.py: BLAST_MOCK_LLM=1
uses a deterministic mock. For column-shaped changes (renamed/dropped/added/
type_changed) the mock delegates to impact_simulator.py's proven AST scan
unchanged, so the bundled demo's output doesn't drift. For resource-shaped
changes (a renamed/deleted whole asset) the mock uses a plain
string-containment heuristic. Neither is a general parser; the real
(OpenAI) backend is the actual format-agnostic path.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from change_interpreter import InterpretedChange
from datahub_client import DownstreamAsset

Verdict = str  # "hard_break" | "silent_risk" | "safe" | "needs_review"

VERDICT_RANK = {"safe": 0, "needs_review": 1, "silent_risk": 2, "hard_break": 3}
VERDICT_EMOJI = {"hard_break": "🔴", "silent_risk": "🟡", "safe": "🟢", "needs_review": "🟠"}

_COLUMN_KINDS = {"renamed", "dropped", "added", "type_changed"}


@dataclass
class Finding:
    asset: DownstreamAsset
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)

    @property
    def emoji(self) -> str:
        return VERDICT_EMOJI[self.verdict]


_PROMPT_TEMPLATE = """A data asset upstream of the one below just changed. Determine whether this
downstream asset breaks as a result.

Upstream changes:
{changes}

Downstream asset: {asset_name} (platform: {platform})
Downstream asset's known definition (SQL, config, or description -- whatever DataHub has on file):
{definition}

Respond with ONLY a JSON object, no other text:
{{
  "verdict": "<one of: hard_break, silent_risk, safe, needs_review>",
  "reason": "<one sentence explaining the verdict>"
}}
hard_break: the downstream asset references something upstream that was
removed/renamed and will now error or definitely produce wrong results.
silent_risk: the downstream asset still runs but its results may silently
change (e.g. a type change inside an aggregation, a renamed resource
referenced by an unstable pointer).
safe: the downstream asset doesn't reference anything that changed.
needs_review: you cannot confidently tell from the definition given (e.g.
it's empty, or not something you can reason about) -- do not guess."""


class BreakageClassifier(ABC):
    @abstractmethod
    def classify(self, changes: list[InterpretedChange], asset: DownstreamAsset) -> Finding: ...

    def classify_all(self, changes: list[InterpretedChange], downstream: list[DownstreamAsset]) -> list[Finding]:
        return [self.classify(changes, asset) for asset in downstream]


class MockBreakageClassifier(BreakageClassifier):
    def classify(self, changes: list[InterpretedChange], asset: DownstreamAsset) -> Finding:
        column_changes = [c for c in changes if c.kind in _COLUMN_KINDS]
        resource_changes = [c for c in changes if c.kind not in _COLUMN_KINDS]

        reasons: list[str] = []
        verdict = "safe"

        if column_changes:
            finding = self._classify_columns(column_changes, asset)
            reasons.extend(r.detail for r in finding.reasons)
            verdict = max(verdict, finding.verdict, key=lambda v: VERDICT_RANK[v])

        if resource_changes:
            r_verdict, r_reasons = self._classify_resource(resource_changes, asset)
            reasons.extend(r_reasons)
            verdict = max(verdict, r_verdict, key=lambda v: VERDICT_RANK[v])

        return Finding(asset=asset, verdict=verdict, reasons=reasons)

    def _classify_columns(self, changes: list[InterpretedChange], asset: DownstreamAsset):
        from diff_parser import ColumnChange, SchemaDiff
        from impact_simulator import classify as simulator_classify

        column_changes = []
        for c in changes:
            if c.kind == "renamed":
                column_changes.append(ColumnChange(kind="renamed", column=c.old, renamed_to=c.new))
            elif c.kind == "dropped":
                column_changes.append(ColumnChange(kind="dropped", column=c.old))
            elif c.kind == "added":
                column_changes.append(ColumnChange(kind="added", column=c.new))
            elif c.kind == "type_changed":
                old_name, _, old_type = (c.old or "").partition(" ")
                _, _, new_type = (c.new or "").partition(" ")
                column_changes.append(
                    ColumnChange(kind="type_changed", column=old_name, old_type=old_type or None, new_type=new_type or None)
                )

        diff = SchemaDiff(model_name=asset.name, changes=column_changes)
        return simulator_classify(diff, asset)

    def _classify_resource(self, changes: list[InterpretedChange], asset: DownstreamAsset) -> tuple[Verdict, list[str]]:
        if not asset.view_logic:
            return "needs_review", ["downstream of a renamed/deleted resource, no definition available to verify -- check manually."]

        reasons = []
        verdict: Verdict = "safe"
        for c in changes:
            if c.old and c.old in asset.view_logic:
                reasons.append(f"still references `{c.old}`, which was {c.kind.replace('resource_', '')} upstream.")
                verdict = "hard_break"
        return verdict, reasons


class OpenAIBreakageClassifier(BreakageClassifier):
    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None):
        from openai import OpenAI  # lazy import so the mock path doesn't need the package installed

        self._client = OpenAI(api_key=api_key or os.environ["OPENAI_API_KEY"])
        self._model = model

    def classify(self, changes: list[InterpretedChange], asset: DownstreamAsset) -> Finding:
        changes_text = "\n".join(f"- {c}" for c in changes) or "(none)"
        definition = asset.view_logic or "(no definition available -- DataHub has no queryable SQL/config for this asset)"
        prompt = _PROMPT_TEMPLATE.format(
            changes=changes_text, asset_name=asset.name, platform=asset.platform, definition=definition
        )
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=300,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return Finding(asset=asset, verdict="needs_review", reasons=["LLM response could not be parsed."])

        verdict = parsed.get("verdict")
        if verdict not in VERDICT_RANK:
            verdict = "needs_review"
        reason = parsed.get("reason")
        return Finding(asset=asset, verdict=verdict, reasons=[reason] if reason else [])


def get_breakage_classifier() -> BreakageClassifier:
    if os.environ.get("BLAST_MOCK_LLM", "0") == "1":
        return MockBreakageClassifier()
    return OpenAIBreakageClassifier()
