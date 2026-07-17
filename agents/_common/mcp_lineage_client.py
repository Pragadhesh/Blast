"""Real DataHub MCP Server client for the lineage read path.

This is the "reads DataHub through the MCP Server" integration the
hackathon explicitly rewards, instead of hand-rolled GraphQL. It's wired up
against the real `mcp` Python SDK (StdioServerParameters / stdio_client /
ClientSession) -- but the exact tool name(s) and result shape DataHub's MCP
server exposes have NOT been verified against a live server in this
environment (see docs/architecture.md's verification notes). So this module
is written defensively: it discovers a lineage-shaped tool by name pattern
rather than assuming one exact name, and raises MCPUnavailable on anything
it can't confidently parse rather than returning wrong data. datahub_client.py
catches that and falls back to the proven GraphQL path.

Configure which MCP server to spawn via:
  DATAHUB_MCP_COMMAND  (default "uvx")
  DATAHUB_MCP_ARGS     (default "acryl-datahub-mcp-server")
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from typing import Any


class MCPUnavailable(Exception):
    """Raised whenever the MCP path can't be used -- caller should fall back to GraphQL."""


def get_downstream_lineage_via_mcp(dataset_urn: str) -> list[dict[str, Any]]:
    """Sync entrypoint -- everything below this is async because the mcp SDK is."""
    try:
        return asyncio.run(_get_downstream_lineage_async(dataset_urn))
    except MCPUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 -- any failure here means "fall back", not "crash the pipeline"
        raise MCPUnavailable(str(exc)) from exc


async def _get_downstream_lineage_async(dataset_urn: str) -> list[dict[str, Any]]:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise MCPUnavailable("the `mcp` package isn't installed") from exc

    command = os.environ.get("DATAHUB_MCP_COMMAND", "uvx")
    args = shlex.split(os.environ.get("DATAHUB_MCP_ARGS", "acryl-datahub-mcp-server"))
    server_env = {
        **os.environ,
        "DATAHUB_GMS_URL": os.environ.get("DATAHUB_SERVER", ""),
        "DATAHUB_GMS_TOKEN": os.environ.get("DATAHUB_TOKEN", ""),
    }
    server_params = StdioServerParameters(command=command, args=args, env=server_env)

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            lineage_tool = _find_lineage_tool(tools.tools)
            if lineage_tool is None:
                raise MCPUnavailable("no lineage-shaped tool exposed by the configured MCP server")

            result = await session.call_tool(
                lineage_tool.name,
                arguments={"urn": dataset_urn, "direction": "DOWNSTREAM"},
            )
            return _parse_lineage_result(result)


def _find_lineage_tool(tools: list[Any]) -> Any | None:
    for tool in tools:
        if "lineage" in getattr(tool, "name", "").lower():
            return tool
    return None


def _parse_lineage_result(result: Any) -> list[dict[str, Any]]:
    """Best-effort parse of an MCP tool result into the plain-dict shape
    datahub_client.py expects (matching DownstreamAsset's fields). Only
    trusts shapes it can confidently recognize -- anything else raises
    MCPUnavailable rather than silently returning incomplete/wrong data.
    """
    raw_items: list[Any] = []
    for content in getattr(result, "content", []) or []:
        text = getattr(content, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, list):
            raw_items.extend(parsed)
        elif isinstance(parsed, dict):
            raw_items.extend(parsed.get("downstream") or parsed.get("relationships") or parsed.get("results") or [])

    if not raw_items:
        raise MCPUnavailable("MCP lineage tool returned no parseable JSON content")

    assets = []
    for item in raw_items:
        entity = item.get("entity", item) if isinstance(item, dict) else {}
        urn = item.get("urn") or entity.get("urn")
        if not urn:
            continue

        platform = entity.get("platform")
        platform_name = platform.get("name") if isinstance(platform, dict) else platform

        view_props = entity.get("viewProperties")
        view_logic = (view_props.get("logic") if isinstance(view_props, dict) else None) or entity.get("view_logic")

        assets.append(
            {
                "urn": urn,
                "name": entity.get("name") or urn,
                "hops": item.get("hops") or item.get("degree") or 1,
                "platform": platform_name or "unknown",
                "owners": entity.get("owners") or [],
                "view_logic": view_logic,
                "parent": item.get("parent"),
            }
        )

    if not assets:
        raise MCPUnavailable("MCP lineage tool result didn't contain any recognizable dataset entries")

    return assets
