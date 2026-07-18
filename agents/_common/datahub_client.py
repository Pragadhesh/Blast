"""DataHub client: read downstream lineage, fetch each node's SQL, count
prior incidents, write Blast's findings back as a DataHub Incident, and
maintain a compounding blast_risk_score Structured Property on the entity
(the "institutional memory" differentiator described in CLAUDE.md section 4).

Lineage reads try DataHub's MCP Server first (mcp_datahub_client.py -- the
preferred, DataHub-native integration surface), then fall back automatically to
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

from mcp_datahub_client import MCPUnavailable, get_downstream_lineage_via_mcp, search_entities_via_mcp

DEFAULT_FIXTURE = (
    Path(__file__).resolve().parent.parent.parent / "examples" / "demo_dbt_project" / "lineage_fixture.json"
)

# The persistent "institutional memory" signal (CLAUDE.md section 4.2): unlike
# the incident log, which is an append-only history, this is a single number
# recomputed fresh on every run from that same history and written back onto
# the entity itself -- so anyone browsing DataHub sees it without knowing to
# go dig through incidents. Recomputing from the timestamped incident log each
# time (rather than reading back and incrementing a stored value) means there's
# no separate decay state to track, and no risk of it drifting from reality.
_RISK_SCORE_PROPERTY_ID = "io.blast.riskScore"
_RISK_SCORE_PROPERTY_URN = f"urn:li:structuredProperty:{_RISK_SCORE_PROPERTY_ID}"
_RISK_SCORE_POINTS_PER_INCIDENT = 40.0
_RISK_SCORE_MAX = 100.0


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
        """Mock-mode only -- reads the bundled fixture's changed-dataset URN
        directly, since the demo fixture is keyed by model name. Real mode
        resolves entities via search_entity() instead (see entity_resolver.py).
        """
        fixture = json.loads(self.mock_fixture.read_text())
        return fixture["changed_dataset"]["urn"]

    def search_entity(self, name: str, platform_hint: str | None = None) -> str | None:
        """Find a DataHub entity by name (optionally narrowed by a platform
        guess), MCP-first with a GraphQL fallback -- the datahub-search-skill
        workflow entity_resolver.py builds on. Only returns a URN on an exact
        (case-insensitive) name match, optionally also matching the platform
        hint; never a fuzzy best-guess. Returns None if nothing confidently
        matches.
        """
        if self.mode in ("auto", "mcp"):
            try:
                matches = search_entities_via_mcp(name, platform_hint)
                return self._best_search_match(matches, name, platform_hint)
            except MCPUnavailable as exc:
                if self.mode == "mcp":
                    raise
                print(f"[blast] MCP search unavailable ({exc}), falling back to GraphQL")

        matches = self._search_entity_graphql(name)
        return self._best_search_match(matches, name, platform_hint)

    @staticmethod
    def _best_search_match(matches: list[dict], name: str, platform_hint: str | None) -> str | None:
        for match in matches:
            if match["name"].lower() != name.lower():
                continue
            if platform_hint and match["platform"].lower() != platform_hint.lower():
                continue
            return match["urn"]
        return None

    def _search_entity_graphql(self, name: str) -> list[dict]:
        data = self._graphql(_SEARCH_QUERY, {"query": name})
        results = ((data.get("search") or {}).get("searchResults")) or []

        matches = []
        for r in results:
            entity = r.get("entity") or {}
            platform = entity.get("platform")
            platform_name = platform.get("name") if isinstance(platform, dict) else platform
            urn = entity.get("urn")
            if not urn:
                continue
            matches.append({"urn": urn, "name": entity.get("name") or urn, "platform": platform_name or "unknown"})
        return matches

    def _recent_incident_ages_days(self, dataset_urn: str, days: int) -> list[float]:
        """Age (in days) of each Blast-raised incident on this dataset within
        the last `days` days. Shared by count_recent_incidents() (a plain
        count) and compute_and_write_risk_score() (a recency-weighted sum) so
        both read the exact same underlying history.
        """
        data = self._graphql(_INCIDENTS_QUERY, {"urn": dataset_urn})
        incidents = ((data.get("dataset") or {}).get("incidents") or {}).get("incidents") or []

        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - days * 86_400_000
        ages = []
        for incident in incidents:
            if incident.get("customType") != "BLAST_BREAKING_CHANGE":
                continue
            created_time = ((incident.get("created") or {}).get("time")) or 0
            if created_time >= cutoff_ms:
                ages.append((now_ms - created_time) / 86_400_000)
        return ages

    def count_recent_incidents(self, dataset_urn: str, days: int = 90) -> int:
        """How many Blast-raised incidents this dataset has had in the last
        `days` days -- the number behind the "flagged N predicted breaking
        changes in the last 90 days" institutional-memory line in the PR
        comment. Note this counts *flagged* PRs (written when blast-scan runs,
        pre-merge), not confirmed shipped breakage -- see docs/architecture.md.
        """
        if self.mock:
            fixture = json.loads(self.mock_fixture.read_text())
            return int(fixture["changed_dataset"].get("prior_incident_count", 0))

        return len(self._recent_incident_ages_days(dataset_urn, days))

    def compute_and_write_risk_score(self, dataset_urn: str, days: int = 90) -> float:
        """Recomputes blast_risk_score from scratch each run (recency-weighted
        sum over recent incidents, not a stored value that gets incremented --
        see the module-level comment above _RISK_SCORE_PROPERTY_ID) and writes
        it back onto the entity as a DataHub Structured Property, so it's
        visible on the entity itself, not just inside Blast's own PR comments.
        Returns the score (0-100) either way.
        """
        if self.mock:
            fixture = json.loads(self.mock_fixture.read_text())
            prior = int(fixture["changed_dataset"].get("prior_incident_count", 0))
            return min(_RISK_SCORE_MAX, (prior + 1) * _RISK_SCORE_POINTS_PER_INCIDENT)

        ages = self._recent_incident_ages_days(dataset_urn, days)
        score = min(
            _RISK_SCORE_MAX,
            sum(max(0.0, 1 - age / days) * _RISK_SCORE_POINTS_PER_INCIDENT for age in ages),
        )

        self._ensure_risk_score_property_exists()
        self._graphql(
            _UPSERT_RISK_SCORE_MUTATION,
            {
                "input": {
                    "assetUrn": dataset_urn,
                    "structuredPropertyInputParams": [
                        {"structuredPropertyUrn": _RISK_SCORE_PROPERTY_URN, "values": [{"numberValue": score}]}
                    ],
                }
            },
        )
        return score

    def _ensure_risk_score_property_exists(self) -> None:
        data = self._graphql(_STRUCTURED_PROPERTY_EXISTS_QUERY, {"urn": _RISK_SCORE_PROPERTY_URN})
        if ((data.get("structuredProperty") or {}).get("definition") or {}).get("displayName"):
            return

        try:
            self._graphql(
                _CREATE_RISK_SCORE_PROPERTY_MUTATION,
                {
                    "input": {
                        "id": _RISK_SCORE_PROPERTY_ID,
                        "qualifiedName": _RISK_SCORE_PROPERTY_ID,
                        "displayName": "Blast Risk Score",
                        "description": (
                            "Blast's compounding risk score for this dataset (0-100), recomputed from "
                            "recent flagged breaking-change PRs. Higher = more/more-recent flags."
                        ),
                        "valueType": "urn:li:dataType:datahub.number",
                        "cardinality": "SINGLE",
                        "entityTypes": ["urn:li:entityType:datahub.dataset"],
                    }
                },
            )
        except RuntimeError as exc:
            if "already exists" not in str(exc):
                raise

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

_SEARCH_QUERY = """
query blastSearchEntity($query: String!) {
  search(input: {type: DATASET, query: $query, start: 0, count: 10}) {
    searchResults {
      entity {
        urn
        ... on Dataset {
          name
          platform { name }
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

_STRUCTURED_PROPERTY_EXISTS_QUERY = """
query blastRiskScorePropertyExists($urn: String!) {
  structuredProperty(urn: $urn) {
    definition {
      displayName
    }
  }
}
"""

_CREATE_RISK_SCORE_PROPERTY_MUTATION = """
mutation blastCreateRiskScoreProperty($input: CreateStructuredPropertyInput!) {
  createStructuredProperty(input: $input) {
    urn
  }
}
"""

_UPSERT_RISK_SCORE_MUTATION = """
mutation blastUpsertRiskScore($input: UpsertStructuredPropertiesInput!) {
  upsertStructuredProperties(input: $input) {
    properties {
      structuredProperty {
        urn
      }
    }
  }
}
"""
