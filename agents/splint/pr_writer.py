"""Turns a set of generated fixes into a follow-up PR targeting the
original PR's branch, using the shared github_repo_client helpers.
"""

from __future__ import annotations

import os

from github import GithubException

from github_repo_client import commit_file_change, open_branch, open_pull_request

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
    fixes: dict[str, tuple[str, str]],
) -> tuple[str | None, list[str]]:
    """fixes: model_name -> (path, fixed_sql), already resolved by the
    caller (no re-lookup here -- avoids a redundant API call and a second
    place the same lookup could diverge from the first).

    Commits each fix on a new branch off the original PR's branch and opens
    a follow-up PR targeting it. Returns (new PR's URL, or None if nothing
    was committed; the list of model names whose commit failed despite
    having a generated fix, so the caller can report them).
    """
    if not fixes:
        return None, []

    fix_branch = f"blast-fix/{original_branch}-{pr_number}"
    open_branch(repo, base_branch=original_branch, new_branch=fix_branch)

    committed = []
    commit_failures = []
    for model_name, (path, fixed_sql) in fixes.items():
        try:
            commit_file_change(
                repo,
                branch=fix_branch,
                path=path,
                new_content=fixed_sql,
                message=f"Splint: fix {model_name} for the upstream schema change in #{pr_number}",
            )
            committed.append(model_name)
        except GithubException as exc:
            print(f"[blast] failed to commit fix for {model_name}: {exc}")
            commit_failures.append(model_name)

    if not committed:
        return None, commit_failures

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
    return pr.html_url, commit_failures
