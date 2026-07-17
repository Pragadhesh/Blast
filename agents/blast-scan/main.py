"""Blast-scan: the schema-change risk agent.

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

from datahub_client import DataHubClient  # noqa: E402
from diff_parser import SchemaDiff, diff_dbt_file, merge_diffs  # noqa: E402
from github_commenter import build_comment_body, post_or_update_comment  # noqa: E402
from impact_simulator import Finding, simulate  # noqa: E402
from report_generator import get_summarizer  # noqa: E402

DEMO_BASE = REPO_ROOT / "examples" / "demo_dbt_project"
DEMO_PR = REPO_ROOT / "examples" / "demo_pr_change"
DEMO_CHANGED_FILES = [
    "models/staging/stg_orders.sql",
    "models/staging/schema.yml",
]

DBT_MODELS_PATH = os.environ.get("BLAST_DBT_MODELS_PATH", "models/")

PipelineResult = tuple[SchemaDiff, list[Finding], str, int]


def run_pipeline(model_diffs: list[SchemaDiff], datahub: DataHubClient) -> list[PipelineResult]:
    summarizer = get_summarizer()
    results: list[PipelineResult] = []

    for diff in model_diffs:
        if diff.is_empty:
            continue

        urn = datahub.resolve_changed_dataset_urn(diff.model_name)
        downstream = datahub.get_downstream_lineage(urn)
        findings = simulate(diff, downstream)
        summary = summarizer.summarize(diff.model_name, diff, findings)

        history_count = 0
        if any(f.verdict != "safe" for f in findings):
            hard = sum(1 for f in findings if f.verdict == "hard_break")
            risky = sum(1 for f in findings if f.verdict == "silent_risk")
            prior = datahub.count_recent_incidents(urn)
            history_count = prior + 1
            datahub.write_incident(
                dataset_urn=urn,
                title=f"Blast: {diff.model_name} change broke {hard} and risked {risky} downstream model(s)",
                description=(
                    f"{summary}\n\nThis table has now triggered {history_count} Blast "
                    f"incident(s) in the last 90 days."
                ),
            )

        results.append((diff, findings, summary, history_count))

    return results


def run_demo() -> None:
    os.environ.setdefault("BLAST_MOCK_DATAHUB", "1")
    os.environ.setdefault("BLAST_MOCK_LLM", "1")

    diffs: list[SchemaDiff] = []
    for rel_path in DEMO_CHANGED_FILES:
        old_content = (DEMO_BASE / rel_path).read_text()
        new_content = (DEMO_PR / rel_path).read_text()
        diffs.extend(diff_dbt_file(rel_path, old_content, new_content))
    merged = merge_diffs(diffs)

    datahub = DataHubClient()
    results = run_pipeline(merged, datahub)

    if not results:
        print("No dbt schema changes detected in the demo scenario.")
        return

    for diff, findings, summary, history_count in results:
        print(build_comment_body(diff.model_name, diff, findings, summary, history_count))
        print("\n" + "=" * 80 + "\n")


def run_real() -> None:
    repo_full_name = os.environ["GITHUB_REPOSITORY"]
    pr_number = int(os.environ["GITHUB_PR_NUMBER"])
    token = os.environ["GITHUB_TOKEN"]

    from github import Github

    gh = Github(token)
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    diffs: list[SchemaDiff] = []
    for f in pr.get_files():
        if f.status not in ("modified", "renamed"):
            continue
        if not f.filename.startswith(DBT_MODELS_PATH):
            continue
        if not f.filename.endswith((".sql", ".yml", ".yaml")):
            continue

        old_path = getattr(f, "previous_filename", None) or f.filename
        old_content = repo.get_contents(old_path, ref=pr.base.sha).decoded_content.decode()
        new_content = repo.get_contents(f.filename, ref=pr.head.sha).decoded_content.decode()
        diffs.extend(diff_dbt_file(f.filename, old_content, new_content))

    merged = merge_diffs(diffs)
    if not merged:
        print("Blast: no dbt schema/model changes detected in this PR.")
        return

    datahub = DataHubClient()
    results = run_pipeline(merged, datahub)

    for diff, findings, summary, history_count in results:
        body = build_comment_body(diff.model_name, diff, findings, summary, history_count)
        url = post_or_update_comment(repo_full_name, pr_number, diff.model_name, body, token)
        print(f"Posted Blast report for `{diff.model_name}` -> {url}")


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
