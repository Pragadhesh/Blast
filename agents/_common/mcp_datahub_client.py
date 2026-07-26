"""Real DataHub MCP Server client: lineage reads and entity search.

This is the "reads DataHub through the MCP Server" integration -- the
preferred, DataHub-native path instead of hand-rolled GraphQL. It's wired up
against the real `mcp` Python SDK (StdioServerParameters / stdio_client /
ClientSession) and the real `mcp-server-datahub` package's `search()` /
`get_lineage()` tools, argument names and result shapes confirmed against a
live DataHub instance (see docs/architecture.md's verification notes for
what was updated once real usage was possible). This module still discovers
tools by name pattern rather than hard-assuming exact tool names, and still
raises MCPUnavailable on anything it can't confidently parse rather than
returning wrong data -- that defensiveness was correct going in blind and
stays correct now, since a future server upgrade could still change shapes
again. Callers (datahub_client.py, entity_resolver.py) catch that and fall
back to the GraphQL path.

Configure which MCP server to spawn via:
  DATAHUB_MCP_COMMAND  (default "uvx")
  DATAHUB_MCP_ARGS     (default "mcp-server-datahub")
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from typing import Any


class MCPUnavailable(Exception):
    """Raised whenever the MCP path can't be used -- caller should fall back."""


def get_downstream_lineage_via_mcp(dataset_urn: str) -> list[dict[str, Any]]:
    """Sync entrypoint -- everything below this is async because the mcp SDK is."""
    return _run(_get_downstream_lineage_async(dataset_urn))


def search_entities_via_mcp(query: str, platform_hint: str | None = None) -> list[dict[str, Any]]:
    """Sync entrypoint for entity search -- the datahub-search-skill-shaped
    workflow: find the entity a name/identifier most likely refers to.
    """
    return _run(_search_entities_async(query, platform_hint))


def _run(coro):
    try:
        return asyncio.run(coro)
    except MCPUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 -- any failure here means "fall back", not "crash the pipeline"
        raise MCPUnavailable(str(exc)) from exc


async def _mcp_session():
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise MCPUnavailable("the `mcp` package isn't installed") from exc

    command = os.environ.get("DATAHUB_MCP_COMMAND", "uvx")
    args = shlex.split(os.environ.get("DATAHUB_MCP_ARGS", "mcp-server-datahub"))
    server_env = {
        # mcp-server-datahub logs its own internal DEBUG/INFO chatter
        # (query counts, token-budget accounting, etc.) via loguru to
        # stderr, which otherwise spills straight into the Action log.
        # Loguru reads LOGURU_LEVEL to configure its default sink before
        # the app starts, so setting it here quiets that noise without
        # touching the actual MCP protocol (which runs over stdout). An
        # explicit LOGURU_LEVEL already in the calling environment still
        # wins, since **os.environ is applied after this default.
        "LOGURU_LEVEL": "WARNING",
        **os.environ,
        "DATAHUB_GMS_URL": os.environ.get("DATAHUB_SERVER", ""),
        "DATAHUB_GMS_TOKEN": os.environ.get("DATAHUB_TOKEN", ""),
    }
    server_params = StdioServerParameters(command=command, args=args, env=server_env)
    return stdio_client(server_params), ClientSession


async def _get_downstream_lineage_async(dataset_urn: str) -> list[dict[str, Any]]:
    client_cm, ClientSession = await _mcp_session()
    async with client_cm as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            lineage_tool = _find_tool(tools.tools, "lineage")
            if lineage_tool is None:
                raise MCPUnavailable("no lineage-shaped tool exposed by the configured MCP server")

            result = await session.call_tool(
                lineage_tool.name,
                # mcp-server-datahub's get_lineage() takes `upstream: bool`,
                # not a `direction` string -- downstream is upstream=False.
                # max_hops=3 ("equivalent to unlimited" per its docstring)
                # to match the GraphQL fallback's un-hop-limited query.
                arguments={"urn": dataset_urn, "upstream": False, "max_hops": 3, "max_results": 50},
            )
            return _parse_lineage_result(result)


async def _search_entities_async(query: str, platform_hint: str | None) -> list[dict[str, Any]]:
    client_cm, ClientSession = await _mcp_session()
    async with client_cm as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            search_tool = _find_tool(tools.tools, "search")
            if search_tool is None:
                raise MCPUnavailable("no search-shaped tool exposed by the configured MCP server")

            # mcp-server-datahub's search() has no `platform` argument --
            # platform_hint is applied client-side by
            # datahub_client.py's _best_search_match() instead, same as it
            # already is for the GraphQL fallback path.
            result = await session.call_tool(search_tool.name, arguments={"query": query})
            return _parse_search_result(result)


def _entity_name(entity: dict[str, Any], urn: str) -> str:
    """DataHub's GraphQL schema nests a Dataset's display name under
    properties.name -- there's no top-level `name` field on the entity
    itself (confirmed against mcp-server-datahub's own entity_details.gql
    fragment). Falling back to the raw urn here is what silently broke
    entity resolution before this was fixed: entity_resolver.py only
    trusts an exact case-insensitive name match, and a urn never equals
    the plain model name it's supposed to match against.
    """
    props = entity.get("properties")
    if isinstance(props, dict) and props.get("name"):
        return props["name"]
    return entity.get("name") or urn


def _entity_platform(entity: dict[str, Any]) -> str:
    platform = entity.get("platform")
    if isinstance(platform, dict):
        return platform.get("name") or "unknown"
    return platform or "unknown"


def _entity_owners(entity: dict[str, Any]) -> list[str]:
    """Owners live under ownership.owners[].owner.urn, not a flat
    top-level `owners` list -- same nested shape the GraphQL fallback
    path already parses in datahub_client.py.
    """
    ownership = entity.get("ownership")
    if not isinstance(ownership, dict):
        return []
    owners = []
    for o in ownership.get("owners") or []:
        owner = (o or {}).get("owner") or {}
        if owner.get("urn"):
            owners.append(owner["urn"])
    return owners


def _find_tool(tools: list[Any], keyword: str) -> Any | None:
    for tool in tools:
        if keyword in getattr(tool, "name", "").lower():
            return tool
    return None


def _extract_json_items(result: Any, *list_keys: str) -> list[Any]:
    """Shared plumbing: MCP tool results carry their payload as text content
    blocks; pull out whatever JSON list each tool call actually returned.
    """
    items: list[Any] = []
    for content in getattr(result, "content", []) or []:
        text = getattr(content, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, list):
            items.extend(parsed)
        elif isinstance(parsed, dict):
            for key in list_keys:
                if key in parsed:
                    items.extend(parsed[key] or [])
                    break
    return items


def _extract_lineage_items(result: Any) -> list[Any]:
    """get_lineage()'s payload is nested two levels --
    {"downstreams": {"searchResults": [{"entity": {...}, "degree": N}, ...]}}
    -- not a flat list of items, so this unwraps that specific shape rather
    than reusing the generic single-level _extract_json_items.
    """
    items: list[Any] = []
    for content in getattr(result, "content", []) or []:
        text = getattr(content, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        direction = parsed.get("downstreams")
        if isinstance(direction, dict):
            items.extend(direction.get("searchResults") or [])
    return items


def _parse_lineage_result(result: Any) -> list[dict[str, Any]]:
    """Best-effort parse of an MCP lineage tool result into the plain-dict
    shape datahub_client.py expects (matching DownstreamAsset's fields).
    Only trusts shapes it can confidently recognize -- anything else raises
    MCPUnavailable rather than silently returning incomplete/wrong data.
    """
    raw_items = _extract_lineage_items(result)
    if not raw_items:
        raise MCPUnavailable("MCP lineage tool returned no parseable JSON content")

    assets = []
    for item in raw_items:
        entity = item.get("entity", item) if isinstance(item, dict) else {}
        urn = item.get("urn") or entity.get("urn")
        if not urn:
            continue

        view_props = entity.get("viewProperties")
        view_logic = (view_props.get("logic") if isinstance(view_props, dict) else None) or entity.get("view_logic")

        assets.append(
            {
                "urn": urn,
                "name": _entity_name(entity, urn),
                "hops": item.get("hops") or item.get("degree") or 1,
                "platform": _entity_platform(entity),
                "owners": _entity_owners(entity),
                "view_logic": view_logic,
                "parent": item.get("parent"),
            }
        )

    if not assets:
        raise MCPUnavailable("MCP lineage tool result didn't contain any recognizable dataset entries")

    return assets


def _parse_search_result(result: Any) -> list[dict[str, Any]]:
    """Best-effort parse of an MCP search tool result into
    {urn, name, platform} matches, most-relevant first (trusting the
    server's own result ordering).
    """
    raw_items = _extract_json_items(result, "results", "entities", "searchResults")
    if not raw_items:
        raise MCPUnavailable("MCP search tool returned no parseable JSON content")

    matches = []
    for item in raw_items:
        entity = item.get("entity", item) if isinstance(item, dict) else {}
        urn = item.get("urn") or entity.get("urn")
        if not urn:
            continue
        matches.append({"urn": urn, "name": _entity_name(entity, urn), "platform": _entity_platform(entity)})

    if not matches:
        raise MCPUnavailable("MCP search tool result didn't contain any recognizable entities")

    return matches
