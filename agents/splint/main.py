"""Splint: the /blast-fix agent.

Triggered by a `/blast-fix` comment on a PR that blast-scan has already
reported on (see .github/workflows/blast-fix.yml). Re-runs the same
diff -> lineage -> classify pipeline blast-scan uses (via agents/_common) to
get fresh findings -- not a fragile re-parse of Blast's own past comment
text -- fetches each broken downstream model's *real* source file (not
DataHub's compiled view SQL, which has no Jinja and can't be written back
as a dbt model), asks an LLM to rewrite it, and opens a follow-up PR
targeting the original PR's branch. Writes a note back to DataHub either way.

Run mode:
  python agents/splint/main.py   # reads GITHUB_REPOSITORY / GITHUB_PR_NUMBER /
                                  # GITHUB_TOKEN from the environment, as set by
                                  # .github/workflows/blast-fix.yml
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "agents" / "_common"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from datahub_client import DataHubClient  # noqa: E402
from diff_parser import diff_dbt_file, merge_diffs  # noqa: E402
from github_repo_client import fetch_file, get_repo  # noqa: E402
from impact_simulator import simulate  # noqa: E402

from fix_generator import get_fix_generator  # noqa: E402
from pr_writer import find_model_path, propose_fixes  # noqa: E402

DBT_MODELS_PATH = os.environ.get("BLAST_DBT_MODELS_PATH", "models/")


def main() -> None:
    repo_full_name = os.environ["GITHUB_REPOSITORY"]
    pr_number = int(os.environ["GITHUB_PR_NUMBER"])
    token = os.environ["GITHUB_TOKEN"]

    repo = get_repo(token, repo_full_name)
    pr = repo.get_pull(pr_number)
    original_branch = pr.head.ref

    diffs = []
    for f in pr.get_files():
        if f.status not in ("modified", "renamed"):
            continue
        if not f.filename.startswith(DBT_MODELS_PATH) or not f.filename.endswith((".sql", ".yml", ".yaml")):
            continue
        old_path = getattr(f, "previous_filename", None) or f.filename
        old_content = repo.get_contents(old_path, ref=pr.base.sha).decoded_content.decode()
        new_content = repo.get_contents(f.filename, ref=pr.head.sha).decoded_content.decode()
        diffs.extend(diff_dbt_file(f.filename, old_content, new_content))

    merged = merge_diffs(diffs)
    if not merged:
        pr.create_issue_comment("Splint: no dbt schema/model changes detected in this PR, nothing to fix.")
        return

    datahub = DataHubClient()
    fix_generator = get_fix_generator()
    fixes: dict[str, str] = {}

    for diff in merged:
        urn = datahub.resolve_changed_dataset_urn(diff.model_name)
        downstream = datahub.get_downstream_lineage(urn)
        findings = simulate(diff, downstream)

        for finding in findings:
            if finding.verdict == "safe":
                continue

            path = find_model_path(repo, ref=original_branch, model_name=finding.asset.name)
            if path is None:
                print(f"Splint: couldn't locate a source file for {finding.asset.name} by convention, skipping")
                continue

            original_sql = fetch_file(repo, path, ref=original_branch)
            changes = [r.change for r in finding.reasons]
            reasons = [r.detail for r in finding.reasons]
            fixed_sql = fix_generator.generate_fix(finding.asset.name, original_sql, changes, reasons)

            if fixed_sql.strip() == original_sql.strip():
                print(f"Splint: generated fix for {finding.asset.name} was a no-op, skipping")
                continue

            fixes[finding.asset.name] = fixed_sql

    fix_pr_url = propose_fixes(repo, pr_number, original_branch, fixes)

    if fix_pr_url is None:
        pr.create_issue_comment(
            "Splint looked at this PR's findings but couldn't generate a fix it was confident "
            "committing automatically -- some breaks need a human call."
        )
        return

    pr.create_issue_comment(f"🩹 **Splint** opened a follow-up PR with proposed fixes: {fix_pr_url}")

    for diff in merged:
        urn = datahub.resolve_changed_dataset_urn(diff.model_name)
        datahub.write_incident(
            dataset_urn=urn,
            title=f"Splint proposed a fix for {diff.model_name}",
            description=f"See {fix_pr_url}",
            custom_type="BLAST_FIX_PROPOSED",
        )
    print(f"Splint: opened fix PR -> {fix_pr_url}")


if __name__ == "__main__":
    main()
