"""Resolves an InterpretedFile (from change_interpreter.py) to a DataHub
URN -- the datahub-search-skill-shaped step: given a name and a platform
guess, find the trustworthy matching entity.

Resolution is entirely search-driven (datahub_client.search_entity(), which
tries DataHub's MCP Server first and falls back to GraphQL) -- there's no
per-repo convention file to maintain, so any repo Blast runs against works
the same way with zero setup. Only an exact (case-insensitive) name match
is trusted, optionally narrowed by the platform hint change_interpreter
guessed; never a fuzzy best-guess. If nothing confidently matches, the file
is skipped (logged), not misresolved.

In mock mode (BLAST_MOCK_DATAHUB=1), resolution is skipped entirely --
DataHubClient.resolve_changed_dataset_urn() already handles that from the
bundled fixture, unchanged.
"""

from __future__ import annotations

from change_interpreter import InterpretedFile


def resolve_urn(interpreted: InterpretedFile, datahub) -> str | None:
    """Returns a DataHub URN, or None if the entity can't be confidently resolved."""
    if not interpreted.entity_name:
        return None

    if datahub.mock:
        return datahub.resolve_changed_dataset_urn(interpreted.entity_name)

    urn = datahub.search_entity(interpreted.entity_name, interpreted.platform_hint)
    if urn is None:
        print(
            f"[blast] no confident DataHub match for '{interpreted.entity_name}' "
            f"(platform hint: {interpreted.platform_hint}) -- skipping"
        )
    return urn
