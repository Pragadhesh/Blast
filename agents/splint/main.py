"""Splint: the /blast-fix agent.

Triggered by a `/blast-fix` comment on a PR that blast-scan has already
reported on (see .github/workflows/blast-fix.yml). Re-runs the same
change_interpreter -> entity_resolver -> lineage -> classify pipeline
blast-scan uses (via agents/_common) to get fresh findings -- not a fragile
re-parse of Blast's own past comment text -- fetches each broken downstream
model's *real* source file (not DataHub's compiled view SQL, which has no
Jinja and can't be written back as a dbt model), asks an LLM to rewrite it,
and opens a follow-up PR targeting the original PR's branch. Writes a note
back to DataHub either way.

Every finding it doesn't fix -- a needs_review verdict, a source file it
couldn't locate, a fix that turned out to be a no-op, a commit that failed
-- is tracked and reported on the PR by name and reason, not just logged.
A partial success (2 of 3 models fixed) is reported as partial, not
silently presented as if everything was handled.

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

from breakage_classifier import get_breakage_classifier  # noqa: E402
from change_interpreter import InterpretedFile, get_change_interpreter  # noqa: E402
from datahub_client import DataHubClient  # noqa: E402
from entity_resolver import resolve_urn  # noqa: E402
from github_repo_client import fetch_file, get_repo  # noqa: E402

from fix_generator import get_fix_generator  # noqa: E402
from pr_writer import find_model_path, propose_fixes  # noqa: E402

_SKIP_EXTENSIONS = {".md", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".lock", ".pdf", ".woff", ".woff2"}


def _merge_by_entity(interpreter, changed_files: list[tuple[str, str, str]]) -> dict[str, InterpretedFile]:
    merged: dict[str, InterpretedFile] = {}
    seen_per_entity: dict[str, set] = {}

    for file_path, old_content, new_content in changed_files:
        if Path(file_path).suffix.lower() in _SKIP_EXTENSIONS:
            continue
        result = interpreter.interpret(file_path, old_content, new_content)
        if result.is_empty:
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


def _format_skip_list(skipped: list[tuple[str, str]]) -> str:
    return "\n".join(f"- `{name}`: {reason}" for name, reason in skipped)


def main() -> None:
    repo_full_name = os.environ["GITHUB_REPOSITORY"]
    pr_number = int(os.environ["GITHUB_PR_NUMBER"])
    token = os.environ["GITHUB_TOKEN"]

    repo = get_repo(token, repo_full_name)
    pr = repo.get_pull(pr_number)
    original_branch = pr.head.ref

    changed_files: list[tuple[str, str, str]] = []
    for f in pr.get_files():
        if f.status not in ("modified", "renamed"):
            continue
        old_path = getattr(f, "previous_filename", None) or f.filename
        old_content = repo.get_contents(old_path, ref=pr.base.sha).decoded_content.decode()
        new_content = repo.get_contents(f.filename, ref=pr.head.sha).decoded_content.decode()
        changed_files.append((f.filename, old_content, new_content))

    interpreter = get_change_interpreter()
    merged = _merge_by_entity(interpreter, changed_files)
    if not merged:
        pr.create_issue_comment("Splint: no schema-relevant changes detected in this PR, nothing to fix.")
        return

    datahub = DataHubClient()
    classifier = get_breakage_classifier()
    fix_generator = get_fix_generator()

    fixes: dict[str, tuple[str, str]] = {}  # model_name -> (path, fixed_sql)
    skipped: list[tuple[str, str]] = []  # (model_name, reason)
    resolved_urns: dict[str, str] = {}  # entity_name -> urn, resolved once, reused for the DataHub write-back

    for interpreted in merged.values():
        urn = resolve_urn(interpreted, datahub)
        if urn is None:
            skipped.append((interpreted.entity_name, "couldn't resolve this entity in DataHub"))
            continue
        resolved_urns[interpreted.entity_name] = urn

        downstream = datahub.get_downstream_lineage(urn)
        findings = classifier.classify_all(interpreted.changes, downstream)

        for finding in findings:
            name = finding.asset.name

            if finding.verdict == "safe":
                continue

            if finding.verdict == "needs_review":
                skipped.append((name, "flagged needs_review -- not confident this actually broke, so no fix was attempted"))
                continue

            path = find_model_path(repo, ref=original_branch, model_name=name)
            if path is None:
                skipped.append((name, "couldn't locate its source file by convention"))
                continue

            original_sql = fetch_file(repo, path, ref=original_branch)
            fixed_sql = fix_generator.generate_fix(name, original_sql, interpreted.changes, finding.reasons)

            if fixed_sql.strip() == original_sql.strip():
                skipped.append((name, "generated fix made no changes"))
                continue

            fixes[name] = (path, fixed_sql)

    fix_pr_url, commit_failures = propose_fixes(repo, pr_number, original_branch, fixes)
    for name in commit_failures:
        skipped.append((name, "fix was generated but the commit failed (see Action log)"))
        fixes.pop(name, None)

    if fix_pr_url is None:
        body = (
            "Splint looked at this PR's findings but couldn't generate a fix it was confident "
            "committing automatically -- some breaks need a human call."
        )
        if skipped:
            body += "\n\n" + _format_skip_list(skipped)
        pr.create_issue_comment(body)
        return

    fixed_names = [name for name in fixes if name not in commit_failures]
    body = f"🩹 **Splint** opened a follow-up PR with proposed fixes for {len(fixed_names)} model(s): {fix_pr_url}"
    if skipped:
        body += "\n\n**Not fixed automatically:**\n" + _format_skip_list(skipped)
    pr.create_issue_comment(body)

    for entity_name, urn in resolved_urns.items():
        datahub.write_incident(
            dataset_urn=urn,
            title=f"Splint proposed a fix for {entity_name}",
            description=f"See {fix_pr_url}",
            custom_type="BLAST_FIX_PROPOSED",
        )
    print(f"Splint: opened fix PR -> {fix_pr_url} ({len(fixed_names)} fixed, {len(skipped)} skipped)")


if __name__ == "__main__":
    main()
