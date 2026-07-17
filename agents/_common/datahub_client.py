"""DataHub client: read downstream lineage, fetch each node's SQL, count
prior incidents, and write Blast's findings back as a DataHub Incident (the
"institutional memory" differentiator described in CLAUDE.md section 4).

Lineage reads try DataHub's MCP Server first (mcp_lineage_client.py -- the
hackathon-preferred integration surface), then fall back automatically to
this module's own GraphQL client if the MCP path isn't available or its
result can't be confidently parsed. Control this with BLAST_DATAHUB_MODE:
  auto (default) -- try MCP, fall back to GraphQL
  mcp             -- MCP only, raise if unavailable (useful for verifying the MCP path works)
  graphql         -- skip MCP entirely, always use GraphQL

DataHub's GraphQL schema varies a bit across versions. If the live queries
below don't match your instance, check $DATAHUB_SERVER/api/graphiql and
adjust _LINEAGE_QUERY / _RAISE_INCIDENT_MUTATION / _INCIDENTS_QUERY
accordingly. Set BLAST_MOCK_DATAHUB=1 to skip the network entirely and read
examples/demo_dbt_project/lineage_fixture.json instead -- that's what the
demo and local dev run against.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from mcp_lineage_client import MCPUnavailable, get_downstream_lineage_via_mcp

DEFAULT_FIXTURE = (
    Path(__file__).resolve().parent.parent.parent / "examples" / "demo_dbt_project" / "lineage_fixture.json"
)


@dataclass
class DownstreamAsset:
    urn: str
    name: str
    hops: int
    platform: str = "dbt"
    owners: list[str] = field(default_factory=list)
    view_logic: str | None = None
    # Immediate upstream node name, when known. Only populated in mock mode --
    # resolving real multi-hop paths needs DataHub's lineage "paths" field,
    # which the GraphQL fallback doesn't request yet (see module docstring).
    parent: str | None = None


class DataHubClient:
    def __init__(
        self,
        server: str | None = None,
        token: str | None = None,
        mock_fixture: str | Path | None = None,
    ):
        self.server = (server or os.environ.get("DATAHUB_SERVER", "http://localhost:9002")).rstrip("/")
        self.token = token or os.environ.get("DATAHUB_TOKEN")
        self.mock = os.environ.get("BLAST_MOCK_DATAHUB", "0") == "1"
        self.mock_fixture = Path(mock_fixture or DEFAULT_FIXTURE)
        self.mode = os.environ.get("BLAST_DATAHUB_MODE", "auto")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        resp = requests.post(
            f"{self.server}/api/graphql",
            headers=self._headers(),
            json={"query": query, "variables": variables},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"DataHub GraphQL error: {payload['errors']}")
        return payload["data"]

    def get_downstream_lineage(self, dataset_urn: str) -> list[DownstreamAsset]:
        if self.mock:
            return self._mock_downstream_lineage()

        if self.mode in ("auto", "mcp"):
            try:
                raw = get_downstream_lineage_via_mcp(dataset_urn)
                return [
                    DownstreamAsset(
                        urn=r["urn"],
                        name=r["name"],
                        hops=r.get("hops", 1),
                        platform=r.get("platform", "unknown"),
                        owners=r.get("owners", []),
                        view_logic=r.get("view_logic"),
                        parent=r.get("parent"),
                    )
                    for r in raw
                ]
            except MCPUnavailable as exc:
                if self.mode == "mcp":
                    raise
                print(f"[blast] MCP lineage read unavailable ({exc}), falling back to GraphQL")

        return self._get_downstream_lineage_graphql(dataset_urn)

    def _get_downstream_lineage_graphql(self, dataset_urn: str) -> list[DownstreamAsset]:
        data = self._graphql(_LINEAGE_QUERY, {"urn": dataset_urn})
        relationships = ((data.get("dataset") or {}).get("lineage") or {}).get("relationships") or []

        assets = []
        for rel in relationships:
            entity = rel.get("entity") or {}
            if entity.get("type") != "DATASET":
                # Newer DataHub versions insert QUERY entities between
                # datasets to represent the SQL transformation itself; the
                # `... on Dataset` fragment below just no-ops on those
                # rather than excluding them, so they'd otherwise show up
                # here as bogus blank-name/unknown-platform assets.
                continue
            view_props = entity.get("viewProperties") or {}
            owners = [
                o["owner"]["urn"]
                for o in ((entity.get("ownership") or {}).get("owners") or [])
                if o.get("owner", {}).get("urn")
            ]
            assets.append(
                DownstreamAsset(
                    urn=entity.get("urn", ""),
                    name=entity.get("name", ""),
                    hops=rel.get("degree", 1),
                    platform=(entity.get("platform") or {}).get("name", "unknown"),
                    owners=owners,
                    view_logic=view_props.get("logic"),
                )
            )
        return assets

    def _mock_downstream_lineage(self) -> list[DownstreamAsset]:
        fixture = json.loads(self.mock_fixture.read_text())
        return [
            DownstreamAsset(
                urn=d["urn"],
                name=d["name"],
                hops=d.get("hops", 1),
                platform=d.get("platform", "dbt"),
                owners=d.get("owners", []),
                view_logic=d.get("view_logic"),
                parent=d.get("parent"),
            )
            for d in fixture["downstream"]
        ]

    def resolve_changed_dataset_urn(self, model_name: str) -> str:
        if self.mock:
            fixture = json.loads(self.mock_fixture.read_text())
            return fixture["changed_dataset"]["urn"]
        # dbt datasets are keyed off the compiled dbt node name; adjust the
        # platform/env segments below to match how your instance ingests dbt.
        return f"urn:li:dataset:(urn:li:dataPlatform:dbt,{model_name},PROD)"

    def count_recent_incidents(self, dataset_urn: str, days: int = 90) -> int:
        """How many Blast-raised incidents this dataset has had in the last
        `days` days -- the number behind the "this table has broken N times
        in 90 days" institutional-memory line in the PR comment.
        """
        if self.mock:
            fixture = json.loads(self.mock_fixture.read_text())
            return int(fixture["changed_dataset"].get("prior_incident_count", 0))

        data = self._graphql(_INCIDENTS_QUERY, {"urn": dataset_urn})
        incidents = ((data.get("dataset") or {}).get("incidents") or {}).get("incidents") or []

        cutoff_ms = int(time.time() * 1000) - days * 86_400_000
        count = 0
        for incident in incidents:
            if incident.get("customType") != "BLAST_BREAKING_CHANGE":
                continue
            created_time = ((incident.get("created") or {}).get("time")) or 0
            if created_time >= cutoff_ms:
                count += 1
        return count

    def write_incident(
        self,
        dataset_urn: str,
        title: str,
        description: str,
        custom_type: str = "BLAST_BREAKING_CHANGE",
    ) -> str | None:
        if self.mock:
            print(f"[mock] would raise DataHub incident on {dataset_urn}: {title}")
            return None

        data = self._graphql(
            _RAISE_INCIDENT_MUTATION,
            {
                "input": {
                    "resourceUrn": dataset_urn,
                    "type": "CUSTOM",
                    "customType": custom_type,
                    "title": title,
                    "description": description,
                }
            },
        )
        return data.get("raiseIncident")


_LINEAGE_QUERY = """
query blastDownstreamLineage($urn: String!) {
  dataset(urn: $urn) {
    urn
    name
    lineage(input: {direction: DOWNSTREAM, start: 0, count: 100}) {
      relationships {
        degree
        entity {
          urn
          type
          ... on Dataset {
            name
            platform { name }
            viewProperties { logic }
            ownership { owners { owner { ... on CorpUser { urn } } } }
          }
        }
      }
    }
  }
}
"""

_INCIDENTS_QUERY = """
query blastRecentIncidents($urn: String!) {
  dataset(urn: $urn) {
    incidents(start: 0, count: 50) {
      incidents {
        urn
        customType
        created {
          time
        }
      }
    }
  }
}
"""

_RAISE_INCIDENT_MUTATION = """
mutation blastRaiseIncident($input: RaiseIncidentInput!) {
  raiseIncident(input: $input)
}
"""
