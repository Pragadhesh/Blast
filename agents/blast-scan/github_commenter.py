"""Builds and posts Blast's risk report as a PR comment.

Uses a hidden HTML marker to find its own previous comment on a PR and edit it
in place on re-runs, instead of spamming a new comment on every push.
"""

from __future__ import annotations

from github import Auth, Github

from breakage_classifier import VERDICT_RANK, Finding
from graph_renderer import render_mermaid


def _marker(model_name: str) -> str:
    return f"<!-- blast-report:{model_name} -->"


def _owner_display(owner_urn: str) -> str:
    # e.g. "urn:li:corpuser:data-eng" -> "data-eng". Displayed as plain
    # text, not a GitHub @mention -- a DataHub owner identifier isn't
    # guaranteed to be a valid GitHub handle, and pinging one live could
    # notify the wrong person.
    return owner_urn.rsplit(":", 1)[-1]


def build_comment_body(
    model_name: str,
    findings: list[Finding],
    summary: str,
    history_count: int = 0,
) -> str:
    hard = [f for f in findings if f.verdict == "hard_break"]
    risky = [f for f in findings if f.verdict == "silent_risk"]
    review = [f for f in findings if f.verdict == "needs_review"]

    headline_parts = []
    if hard:
        headline_parts.append(f"{len(hard)} breaking change{'s' if len(hard) != 1 else ''}")
    if risky:
        headline_parts.append(f"{len(risky)} silent risk{'s' if len(risky) != 1 else ''}")
    if review:
        headline_parts.append(f"{len(review)} needing review")
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

    owned_findings = [f for f in findings if f.verdict != "safe" and f.asset.owners]
    if owned_findings:
        owner_lines = ", ".join(
            f"`{f.asset.name}` ({', '.join(_owner_display(o) for o in f.asset.owners)})" for f in owned_findings
        )
        lines.append(f"\n👤 **Owners to loop in:** {owner_lines}")

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
            for reason in f.reasons:
                lines.append(f"  - {reason}")

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
    gh = Github(auth=Auth.Token(token))
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    # Matching on the marker alone, not also the comment author: GITHUB_TOKEN
    # (the token GitHub Actions provides automatically) can't call GET /user
    # to look up "who am I" -- that endpoint is restricted to real user/PAT
    # sessions and returns a 403 for Actions/App tokens. The hidden marker is
    # already a strong enough signature; a random other comment containing
    # this exact HTML comment is effectively impossible.
    marker = _marker(model_name)
    for comment in pr.get_issue_comments():
        if marker in comment.body:
            comment.edit(body)
            return comment.html_url

    comment = pr.create_issue_comment(body)
    return comment.html_url
