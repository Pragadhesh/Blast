"""Turns a set of (model_name, fixed_sql) pairs into a follow-up PR targeting
the original PR's branch, using the shared github_repo_client helpers.
"""

from __future__ import annotations

import os

from github import GithubException

from github_repo_client import commit_file_change, open_branch, open_pull_request

# Where a consumer repo's dbt models live, e.g. "dbt/models/" for
# commerce-warehouse (its dbt project isn't at the repo root). Configurable
# per-repo since this is a genuine convention DataHub's lineage doesn't
# expose -- see docs/architecture.md's known limitations.
_MODELS_ROOT = os.environ.get("BLAST_MODELS_ROOT", "models/")


def find_model_path(repo, ref: str, model_name: str, models_root: str = _MODELS_ROOT) -> str | None:
    """Locate a dbt model's source file by naming convention:
    {models_root}**/{model_name}.sql. DataHub's lineage doesn't expose
    source file paths, so Splint relies on this convention -- see
    docs/architecture.md's known limitations.
    """
    try:
        stack = list(repo.get_contents(models_root, ref=ref))
    except GithubException as exc:
        print(f"[blast] couldn't list '{models_root}' at {ref} ({exc.status}) -- check BLAST_MODELS_ROOT")
        return None

    while stack:
        item = stack.pop()
        if item.type == "dir":
            stack.extend(repo.get_contents(item.path, ref=ref))
        elif item.name == f"{model_name}.sql":
            return item.path
    return None


def propose_fixes(
    repo,
    pr_number: int,
    original_branch: str,
    fixes: dict[str, str],
) -> str | None:
    """Commits each (model_name -> fixed_sql) fix on a new branch off the
    original PR's branch and opens a follow-up PR targeting it. Returns the
    new PR's URL, or None if nothing was actually committed (e.g. every
    model's source file couldn't be located by convention).
    """
    if not fixes:
        return None

    fix_branch = f"blast-fix/{original_branch}-{pr_number}"
    open_branch(repo, base_branch=original_branch, new_branch=fix_branch)

    committed = []
    for model_name, fixed_sql in fixes.items():
        path = find_model_path(repo, ref=original_branch, model_name=model_name)
        if path is None:
            continue
        commit_file_change(
            repo,
            branch=fix_branch,
            path=path,
            new_content=fixed_sql,
            message=f"Splint: fix {model_name} for the upstream schema change in #{pr_number}",
        )
        committed.append(model_name)

    if not committed:
        return None

    body = (
        f"Generated fixes for {len(committed)} model(s) broken by #{pr_number}: "
        + ", ".join(f"`{m}`" for m in committed)
        + ".\n\nReview before merging -- these are LLM-generated corrections, not guaranteed correct."
    )
    pr = open_pull_request(
        repo,
        title=f"Splint: fix downstream breakage from #{pr_number}",
        body=body,
        head=fix_branch,
        base=original_branch,
    )
    return pr.html_url
