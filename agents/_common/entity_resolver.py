"""Resolves an InterpretedFile (from change_interpreter.py) to a DataHub
URN -- the datahub-search-skill-shaped step: given a name and a platform
guess, find the trustworthy matching entity.

Two paths, tried in order:
1. Convention config (.blast/entities.yml in the *consumer* repo) -- an
   explicit, human-reviewed platform -> URN-template mapping. This is the
   trust boundary a production tool needs: deterministic, no guessing, and
   whoever owns the repo controls it.
2. DataHub search via MCP (mcp_datahub_client.search_entities_via_mcp) --
   used only when no convention entry matches. Requires the top match's
   name to equal the interpreted entity name (case-insensitive) before
   trusting it; never picks a fuzzy best-guess silently.

In mock mode (BLAST_MOCK_DATAHUB=1), resolution is skipped entirely --
DataHubClient.resolve_changed_dataset_urn() already handles that from the
bundled fixture, unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from change_interpreter import InterpretedFile
from mcp_datahub_client import MCPUnavailable, search_entities_via_mcp

DEFAULT_CONFIG_PATH = ".blast/entities.yml"


def _load_convention_config() -> dict:
    path = Path(os.environ.get("BLAST_ENTITIES_CONFIG", DEFAULT_CONFIG_PATH))
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def resolve_urn(interpreted: InterpretedFile, datahub) -> str | None:
    """Returns a DataHub URN, or None if the entity can't be confidently resolved."""
    if not interpreted.entity_name:
        return None

    if datahub.mock:
        return datahub.resolve_changed_dataset_urn(interpreted.entity_name)

    config = _load_convention_config()
    platforms = config.get("platforms") or {}
    template = platforms.get(interpreted.platform_hint) if interpreted.platform_hint else None
    if template:
        return template.format(
            name=interpreted.entity_name,
            schema=interpreted.schema_hint or "public",
        )

    try:
        matches = search_entities_via_mcp(interpreted.entity_name, interpreted.platform_hint)
    except MCPUnavailable as exc:
        print(f"[blast] entity search unavailable for '{interpreted.entity_name}' ({exc}), skipping")
        return None

    for match in matches:
        if match["name"].lower() == interpreted.entity_name.lower():
            if interpreted.platform_hint and match["platform"].lower() != interpreted.platform_hint.lower():
                continue
            return match["urn"]

    print(
        f"[blast] no confident DataHub match for '{interpreted.entity_name}' "
        f"(platform hint: {interpreted.platform_hint}) -- add it to {DEFAULT_CONFIG_PATH} to resolve deterministically"
    )
    return None
