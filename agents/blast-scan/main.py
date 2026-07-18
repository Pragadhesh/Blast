"""Blast-scan: the schema-change risk agent.

Considers every changed file in a PR, not just dbt models -- SQL DDL,
Terraform, or anything else. change_interpreter.py (LLM-driven) decides
per-file whether it's schema-relevant; this module doesn't pre-judge by
file format, only by a cheap denylist of obviously-irrelevant extensions
(docs, images, lockfiles) to avoid burning LLM calls on files that can
never be a data asset definition.

Run modes:
  python agents/blast-scan/main.py --demo   # bundled demo, no GitHub/DataHub needed
  python agents/blast-scan/main.py          # real mode: reads GITHUB_REPOSITORY / GITHUB_PR_NUMBER /
                                             # GITHUB_TOKEN from the environment, as set by
                                             # .github/workflows/blast-scan.yml
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "agents" / "_common"))

from breakage_classifier import Finding, get_breakage_classifier  # noqa: E402
from change_interpreter import InterpretedChange, InterpretedFile, get_change_interpreter  # noqa: E402
from datahub_client import DataHubClient  # noqa: E402
from entity_resolver import resolve_urn  # noqa: E402
from github_commenter import build_comment_body, post_or_update_comment  # noqa: E402
from report_generator import get_summarizer  # noqa: E402

DEMO_BASE = REPO_ROOT / "examples" / "demo_dbt_project"
DEMO_PR = REPO_ROOT / "examples" / "demo_pr_change"
DEMO_CHANGED_FILES = [
    "models/staging/stg_orders.sql",
    "models/staging/schema.yml",
]

# Cost/latency guard, not a format allowlist: skip files that can never be
# a data asset definition before even asking the LLM. Anything not listed
# here (.sql, .tf, .yml, .py, .xml, unknown extensions, ...) still goes
# through change_interpreter -- it decides relevance, not this list.
_SKIP_EXTENSIONS = {".md", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".lock", ".pdf", ".woff", ".woff2"}

PipelineResult = tuple[str, list[InterpretedChange], list[Finding], str, int, float]


def _merge_by_entity(changed_files: list[tuple[str, str, str]]) -> dict[str, InterpretedFile]:
    """changed_files: list of (file_path, old_content, new_content). Runs
    change_interpreter on each, merges changes by entity_name (so a PR
    touching both a model's .sql and its schema.yml produces one entry,
    not two), and drops files with no schema-relevant change.
    """
    interpreter = get_change_interpreter()
    merged: dict[str, InterpretedFile] = {}
    seen_per_entity: dict[str, set] = {}

    for file_path, old_content, new_content in changed_files:
        if Path(file_path).suffix.lower() in _SKIP_EXTENSIONS:
            continue

        result = interpreter.interpret(file_path, old_content, new_content)
        if result.is_empty:
            print(f"Blast: no schema-relevant change detected in {file_path}, skipping")
            continue

        entity = result.entity_name
        if entity not in merged:
            merged[entity] = InterpretedFile(
                file_path=file_path,
                entity_name=entity,
                platform_hint=result.platform_hint,
                schema_hint=result.schema_hint,
            )
            seen_per_entity[entity] = set()

        seen = seen_per_entity[entity]
        for c in result.changes:
            key = (c.kind, c.old, c.new)
            if key not in seen:
                seen.add(key)
                merged[entity].changes.append(c)

    return merged


def run_pipeline(merged: dict[str, InterpretedFile], datahub: DataHubClient) -> list[PipelineResult]:
    summarizer = get_summarizer()
    classifier = get_breakage_classifier()
    results: list[PipelineResult] = []

    for entity_name, interpreted in merged.items():
        urn = resolve_urn(interpreted, datahub)
        if urn is None:
            print(f"Blast: could not resolve '{entity_name}' to a DataHub entity, skipping")
            continue

        downstream = datahub.get_downstream_lineage(urn)
        findings = classifier.classify_all(interpreted.changes, downstream)
        summary = summarizer.summarize(entity_name, interpreted.changes, findings)

        history_count = 0
        risk_score = 0.0
        if any(f.verdict != "safe" for f in findings):
            hard = sum(1 for f in findings if f.verdict == "hard_break")
            risky = sum(1 for f in findings if f.verdict == "silent_risk")
            prior = datahub.count_recent_incidents(urn)
            history_count = prior + 1
            datahub.write_incident(
                dataset_urn=urn,
                title=f"Blast: {entity_name} change predicted to break {hard} and risk {risky} downstream model(s)",
                description=(
                    f"{summary}\n\nBlast has flagged {history_count} predicted breaking change(s) on this "
                    f"table in the last 90 days. This reflects PRs Blast flagged pre-merge, not confirmed "
                    f"shipped breakage."
                ),
            )
            risk_score = datahub.compute_and_write_risk_score(urn)

        results.append((entity_name, interpreted.changes, findings, summary, history_count, risk_score))

    return results


def run_demo() -> None:
    os.environ.setdefault("BLAST_MOCK_DATAHUB", "1")
    os.environ.setdefault("BLAST_MOCK_LLM", "1")

    changed_files = [
        (rel_path, (DEMO_BASE / rel_path).read_text(), (DEMO_PR / rel_path).read_text())
        for rel_path in DEMO_CHANGED_FILES
    ]
    merged = _merge_by_entity(changed_files)

    datahub = DataHubClient()
    results = run_pipeline(merged, datahub)

    if not results:
        print("No schema-relevant changes detected in the demo scenario.")
        return

    for entity_name, _changes, findings, summary, history_count, risk_score in results:
        print(build_comment_body(entity_name, findings, summary, history_count, risk_score))
        print("\n" + "=" * 80 + "\n")


def run_real() -> None:
    repo_full_name = os.environ["GITHUB_REPOSITORY"]
    pr_number = int(os.environ["GITHUB_PR_NUMBER"])
    token = os.environ["GITHUB_TOKEN"]

    from github import Auth, Github

    gh = Github(auth=Auth.Token(token))
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    changed_files: list[tuple[str, str, str]] = []
    for f in pr.get_files():
        if f.status not in ("modified", "renamed", "added", "removed"):
            continue

        old_content = ""
        if f.status != "added":
            old_path = getattr(f, "previous_filename", None) or f.filename
            old_content = repo.get_contents(old_path, ref=pr.base.sha).decoded_content.decode()

        new_content = ""
        if f.status != "removed":
            new_content = repo.get_contents(f.filename, ref=pr.head.sha).decoded_content.decode()

        changed_files.append((f.filename, old_content, new_content))

    merged = _merge_by_entity(changed_files)
    if not merged:
        print("Blast: no schema-relevant changes detected in this PR.")
        return

    datahub = DataHubClient()
    results = run_pipeline(merged, datahub)

    for entity_name, _changes, findings, summary, history_count, risk_score in results:
        body = build_comment_body(entity_name, findings, summary, history_count, risk_score)
        url = post_or_update_comment(repo_full_name, pr_number, entity_name, body, token)
        print(f"Posted Blast report for `{entity_name}` -> {url}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Blast: know what breaks before you merge.")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run the bundled demo scenario locally and print the report instead of posting to GitHub.",
    )
    args = parser.parse_args()

    run_demo() if args.demo else run_real()


if __name__ == "__main__":
    main()
