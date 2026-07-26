"""Renders a color-coded Mermaid dependency graph from Blast's Findings.
Mermaid renders natively in GitHub markdown, so the PR comment needs no image
hosting (see CLAUDE.md section 6)."""

from __future__ import annotations

import hashlib
import re

from breakage_classifier import Finding

_STYLE = {
    "hard_break": "fill:#ffdce0,stroke:#d1242f,stroke-width:2px,color:#1f2328",
    "silent_risk": "fill:#fff8c5,stroke:#9a6700,stroke-width:2px,color:#1f2328",
    "safe": "fill:#dafbe1,stroke:#1a7f37,stroke-width:2px,color:#1f2328",
    "needs_review": "fill:#ffe5b4,stroke:#bc4c00,stroke-width:2px,color:#1f2328",
    "changed": "fill:#d0d7ff,stroke:#4c2889,stroke-width:2px,color:#1f2328",
}

_LABEL = {"hard_break": "hard break", "silent_risk": "silent risk", "safe": "safe", "needs_review": "needs review"}


def _node_id(name: str) -> str:
    """Mermaid node IDs must be bare identifiers -- unlike the quoted label,
    they can't contain spaces, '&', or most other punctuation. Real DataHub
    display names routinely do (dashboards, Tableau workbooks, etc., e.g.
    "Revenue by segment & country"), which broke Mermaid's parser outright
    when used as-is. The human-readable name still renders correctly via
    the separate quoted label -- only the invisible internal ID changes
    here. A short hash suffix guarantees two different names that sanitize
    to the same prefix (e.g. differing only in punctuation) still get
    distinct IDs.
    """
    safe = re.sub(r"[^0-9a-zA-Z_]", "_", name).strip("_") or "node"
    if safe[0].isdigit():
        safe = f"n_{safe}"
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    return f"{safe}_{digest}"


def _escape_label(name: str) -> str:
    """A display name containing a literal '"' would otherwise close the
    quoted Mermaid label early -- same class of bug as _node_id, just for
    the label instead of the ID.
    """
    return name.replace('"', "&quot;")


def render_mermaid(changed_model: str, findings: list[Finding]) -> str:
    by_name = {f.asset.name: f for f in findings}

    lines = [
        "```mermaid",
        "graph LR",
        f'    {_node_id(changed_model)}["{_escape_label(changed_model)}"]:::changed',
    ]

    edges: set[tuple[str, str, str]] = set()
    for f in findings:
        source = f.asset.parent if f.asset.parent in by_name else changed_model
        edges.add((source, f.asset.name, f.verdict))
        lines.append(f'    {_node_id(f.asset.name)}["{_escape_label(f.asset.name)}"]:::{f.verdict}')

    for source, target, verdict in sorted(edges):
        lines.append(f"    {_node_id(source)} -->|{_LABEL[verdict]}| {_node_id(target)}")

    for kind, style in _STYLE.items():
        lines.append(f"    classDef {kind} {style}")

    lines.append("```")
    return "\n".join(lines)
