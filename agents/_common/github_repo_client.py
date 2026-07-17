"""Shared GitHub repo-write helpers: fetch a file at a ref, create a branch,
commit a file change, open a PR.

blast-scan only needs read-plus-comment, which github_commenter.py already
does directly via PyGithub. This module is the write-a-branch-and-PR
surface that Splint (agents/splint/) needs and blast-scan doesn't, kept
here so the two agents don't each hand-roll their own copy.
"""

from __future__ import annotations

from github import Github
from github.PullRequest import PullRequest
from github.Repository import Repository


def get_repo(token: str, repo_full_name: str) -> Repository:
    return Github(token).get_repo(repo_full_name)


def fetch_file(repo: Repository, path: str, ref: str) -> str:
    return repo.get_contents(path, ref=ref).decoded_content.decode()


def open_branch(repo: Repository, base_branch: str, new_branch: str) -> None:
    base_ref = repo.get_git_ref(f"heads/{base_branch}")
    repo.create_git_ref(ref=f"refs/heads/{new_branch}", sha=base_ref.object.sha)


def commit_file_change(repo: Repository, branch: str, path: str, new_content: str, message: str) -> None:
    existing = repo.get_contents(path, ref=branch)
    repo.update_file(path=path, message=message, content=new_content, sha=existing.sha, branch=branch)


def open_pull_request(repo: Repository, title: str, body: str, head: str, base: str) -> PullRequest:
    return repo.create_pull(title=title, body=body, head=head, base=base)
