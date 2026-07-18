"""Shared GitHub repo-write helpers: fetch a file at a ref, create a branch,
commit a file change, open a PR.

blast-scan only needs read-plus-comment, which github_commenter.py already
does directly via PyGithub. This module is the write-a-branch-and-PR
surface that Splint (agents/splint/) needs and blast-scan doesn't, kept
here so the two agents don't each hand-roll their own copy.
"""

from __future__ import annotations

from github import Auth, Github, GithubException
from github.PullRequest import PullRequest
from github.Repository import Repository


def get_repo(token: str, repo_full_name: str) -> Repository:
    return Github(auth=Auth.Token(token)).get_repo(repo_full_name)


def fetch_file(repo: Repository, path: str, ref: str) -> str:
    return repo.get_contents(path, ref=ref).decoded_content.decode()


def open_branch(repo: Repository, base_branch: str, new_branch: str) -> None:
    """Creates new_branch at base_branch's current HEAD. If new_branch
    already exists -- e.g. a prior Splint run got this far (branch created,
    fixes committed) and then failed on a later step, such as the PR-creation
    permission gate documented in README.md -- resets it to base_branch's
    HEAD instead of crashing with "Reference already exists". That keeps a
    retry idempotent: the branch starts clean again rather than the retry
    failing on a leftover ref from the previous attempt.
    """
    base_ref = repo.get_git_ref(f"heads/{base_branch}")
    try:
        repo.create_git_ref(ref=f"refs/heads/{new_branch}", sha=base_ref.object.sha)
    except GithubException as exc:
        if exc.status != 422 or "already exists" not in str(exc.data.get("message", "")):
            raise
        repo.get_git_ref(f"heads/{new_branch}").edit(sha=base_ref.object.sha, force=True)


def commit_file_change(repo: Repository, branch: str, path: str, new_content: str, message: str) -> None:
    existing = repo.get_contents(path, ref=branch)
    repo.update_file(path=path, message=message, content=new_content, sha=existing.sha, branch=branch)


def open_pull_request(repo: Repository, title: str, body: str, head: str, base: str) -> PullRequest:
    return repo.create_pull(title=title, body=body, head=head, base=base)
