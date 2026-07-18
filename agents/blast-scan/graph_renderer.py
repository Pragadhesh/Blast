"""Renders a color-coded Mermaid dependency graph from Blast's Findings.
Mermaid renders natively in GitHub markdown, so the PR comment needs no image
hosting (see CLAUDE.md section 6)."""

from __future__ import annotations

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
    return name.replace(".", "_").replace("-", "_")


def render_mermaid(changed_model: str, findings: list[Finding]) -> str:
    by_name = {f.asset.name: f for f in findings}

    lines = ["```mermaid", "graph LR", f'    {_node_id(changed_model)}["{changed_model}"]:::changed']

    edges: set[tuple[str, str, str]] = set()
    for f in findings:
        source = f.asset.parent if f.asset.parent in by_name else changed_model
        edges.add((source, f.asset.name, f.verdict))
        lines.append(f'    {_node_id(f.asset.name)}["{f.asset.name}"]:::{f.verdict}')

    for source, target, verdict in sorted(edges):
        lines.append(f"    {_node_id(source)} -->|{_LABEL[verdict]}| {_node_id(target)}")

    for kind, style in _STYLE.items():
        lines.append(f"    classDef {kind} {style}")

    lines.append("```")
    return "\n".join(lines)
