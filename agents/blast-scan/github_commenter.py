"""Builds and posts Blast's risk report as a PR comment.

Uses a hidden HTML marker to find its own previous comment on a PR and edit it
in place on re-runs, instead of spamming a new comment on every push.
"""

from __future__ import annotations

from github import Github

from diff_parser import SchemaDiff
from graph_renderer import render_mermaid
from impact_simulator import VERDICT_RANK, Finding


def _marker(model_name: str) -> str:
    return f"<!-- blast-report:{model_name} -->"


def build_comment_body(
    model_name: str,
    diff: SchemaDiff,
    findings: list[Finding],
    summary: str,
    history_count: int = 0,
) -> str:
    hard = [f for f in findings if f.verdict == "hard_break"]
    risky = [f for f in findings if f.verdict == "silent_risk"]

    headline_parts = []
    if hard:
        headline_parts.append(f"{len(hard)} breaking change{'s' if len(hard) != 1 else ''}")
    if risky:
        headline_parts.append(f"{len(risky)} silent risk{'s' if len(risky) != 1 else ''}")
    headline = " and ".join(headline_parts) if headline_parts else "no downstream breakage"

    lines = [
        _marker(model_name),
        f"### 🧨 **Blast** found {headline} downstream in `{model_name}`",
        "",
        summary,
    ]

    if history_count > 1:
        lines.append(
            f"\n📜 **Institutional memory:** `{model_name}` has now triggered "
            f"{history_count} Blast incidents in the last 90 days -- this isn't a one-off."
        )

    lines += [
        "",
        "#### Downstream impact",
        "",
        "| | Model | Hops | Verdict |",
        "|---|---|---|---|",
    ]
    for f in sorted(findings, key=lambda f: (-VERDICT_RANK[f.verdict], f.asset.hops, f.asset.name)):
        lines.append(f"| {f.emoji} | `{f.asset.name}` | {f.asset.hops} | {f.verdict.replace('_', ' ')} |")

    if any(f.reasons for f in findings):
        lines += ["", "#### Details", ""]
        for f in findings:
            if not f.reasons:
                continue
            lines.append(f"- {f.emoji} **`{f.asset.name}`**")
            for r in f.reasons:
                lines.append(f"  - {r.detail}")

    lines += ["", "#### Dependency graph", "", render_mermaid(model_name, findings)]

    if hard or risky:
        lines += [
            "",
            "_Findings written back to DataHub as an incident on the changed dataset. "
            "Comment `/blast-fix` on this PR to have **Splint** propose a fix as a follow-up PR._",
        ]
    else:
        lines += ["", "_Findings written back to DataHub as an incident on the changed dataset._"]

    return "\n".join(lines)


def post_or_update_comment(repo_full_name: str, pr_number: int, model_name: str, body: str, token: str) -> str:
    gh = Github(token)
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    bot_login = gh.get_user().login
    marker = _marker(model_name)
    for comment in pr.get_issue_comments():
        if comment.user.login == bot_login and marker in comment.body:
            comment.edit(body)
            return comment.html_url

    comment = pr.create_issue_comment(body)
    return comment.html_url
