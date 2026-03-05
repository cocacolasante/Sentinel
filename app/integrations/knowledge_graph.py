"""
Personal Knowledge Graph — Neo4j integration.

Manages a graph of everything Sentinel works on:
  Nodes:    Project, Repo, Server, Client, Idea, Domain, Person, Task
  Edges:    USES, DEPLOYED_ON, FOR, RELATED_TO, HOSTED_AT, RUNS,
            IMPLEMENTS, CREATED_BY, DEPENDS_ON, PART_OF

All write operations are idempotent (MERGE not CREATE).
Designed to be called from skills, milestone hooks, and project_builder.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Valid node labels and relationship types
NODE_LABELS = frozenset(
    {"Project", "Repo", "Server", "Client", "Idea", "Domain", "Person", "Task", "Skill", "Tech"}
)
REL_TYPES = frozenset(
    {
        "USES", "DEPLOYED_ON", "FOR", "RELATED_TO", "HOSTED_AT",
        "RUNS", "IMPLEMENTS", "CREATED_BY", "DEPENDS_ON", "PART_OF",
        "CONTRIBUTED_TO", "MANAGES", "LINKED_TO",
    }
)


def _safe_label(label: str) -> str:
    label = label.strip().title().replace(" ", "")
    if label not in NODE_LABELS:
        return "Project"  # safe default
    return label


def _safe_rel(rel: str) -> str:
    rel = rel.strip().upper().replace(" ", "_")
    return rel if rel in REL_TYPES else "RELATED_TO"


class KnowledgeGraphClient:
    """
    Async-compatible wrapper around the Neo4j Python driver.
    All heavy I/O runs via asyncio.to_thread so it doesn't block the event loop.
    """

    def __init__(self) -> None:
        self._driver = None

    def is_configured(self) -> bool:
        from app.config import get_settings
        s = get_settings()
        return bool(s.neo4j_uri and s.neo4j_user and s.neo4j_password)

    def _get_driver(self):
        if self._driver is None:
            from neo4j import GraphDatabase
            from app.config import get_settings
            s = get_settings()
            self._driver = GraphDatabase.driver(
                s.neo4j_uri,
                auth=(s.neo4j_user, s.neo4j_password),
            )
        return self._driver

    def _run_sync(self, query: str, params: dict | None = None) -> list[dict]:
        driver = self._get_driver()
        with driver.session() as session:
            result = session.run(query, params or {})
            return [dict(r) for r in result]

    async def _run(self, query: str, params: dict | None = None) -> list[dict]:
        import asyncio
        return await asyncio.to_thread(self._run_sync, query, params)

    # ── Schema bootstrap ───────────────────────────────────────────────────────

    async def init_schema(self) -> None:
        """Create indexes and constraints (idempotent)."""
        stmts = [
            "CREATE CONSTRAINT node_name IF NOT EXISTS FOR (n:Project) REQUIRE n.name IS UNIQUE",
            "CREATE CONSTRAINT repo_name IF NOT EXISTS FOR (n:Repo) REQUIRE n.name IS UNIQUE",
            "CREATE CONSTRAINT server_name IF NOT EXISTS FOR (n:Server) REQUIRE n.name IS UNIQUE",
            "CREATE CONSTRAINT client_name IF NOT EXISTS FOR (n:Client) REQUIRE n.name IS UNIQUE",
            "CREATE CONSTRAINT person_name IF NOT EXISTS FOR (n:Person) REQUIRE n.name IS UNIQUE",
        ]
        for stmt in stmts:
            try:
                await self._run(stmt)
            except Exception as exc:
                logger.debug("Schema stmt skipped (already exists?): %s", exc)

    # ── Node operations ────────────────────────────────────────────────────────

    async def upsert_node(
        self,
        label: str,
        name: str,
        properties: dict | None = None,
    ) -> dict:
        """Create or update a node. Returns the node properties."""
        label = _safe_label(label)
        props = {k: v for k, v in (properties or {}).items() if v is not None}
        props["name"] = name

        # Build SET clause for extra properties
        set_clause = ", ".join(f"n.{k} = ${k}" for k in props if k != "name")
        query = f"""
        MERGE (n:{label} {{name: $name}})
        {("SET " + set_clause) if set_clause else ""}
        RETURN n
        """
        rows = await self._run(query, props)
        return dict(rows[0]["n"]) if rows else {"name": name, "label": label}

    async def delete_node(self, label: str, name: str) -> dict:
        label = _safe_label(label)
        await self._run(
            f"MATCH (n:{label} {{name: $name}}) DETACH DELETE n",
            {"name": name},
        )
        return {"deleted": name}

    # ── Relationship operations ────────────────────────────────────────────────

    async def upsert_relationship(
        self,
        from_label: str,
        from_name: str,
        rel_type: str,
        to_label: str,
        to_name: str,
        properties: dict | None = None,
    ) -> dict:
        """Create both nodes (if missing) and the relationship between them."""
        fl = _safe_label(from_label)
        tl = _safe_label(to_label)
        rt = _safe_rel(rel_type)
        props = properties or {}

        set_clause = ", ".join(f"r.{k} = $prop_{k}" for k in props)
        prop_params = {f"prop_{k}": v for k, v in props.items()}

        query = f"""
        MERGE (a:{fl} {{name: $from_name}})
        MERGE (b:{tl} {{name: $to_name}})
        MERGE (a)-[r:{rt}]->(b)
        {("SET " + set_clause) if set_clause else ""}
        RETURN a.name AS from_name, type(r) AS rel, b.name AS to_name
        """
        rows = await self._run(
            query,
            {"from_name": from_name, "to_name": to_name, **prop_params},
        )
        return rows[0] if rows else {}

    # ── Query operations ───────────────────────────────────────────────────────

    async def get_all(self, limit: int = 500) -> dict:
        """Return all nodes and relationships for visualization."""
        nodes_rows = await self._run(
            "MATCH (n) RETURN id(n) AS id, labels(n) AS labels, n.name AS name, "
            "properties(n) AS props LIMIT $limit",
            {"limit": limit},
        )
        edges_rows = await self._run(
            "MATCH (a)-[r]->(b) RETURN id(a) AS from_id, id(b) AS to_id, "
            "type(r) AS rel, properties(r) AS props LIMIT $limit",
            {"limit": limit},
        )
        return {
            "nodes": [
                {
                    "id": r["id"],
                    "label": r["labels"][0] if r["labels"] else "Node",
                    "name": r["name"],
                    "props": r["props"],
                }
                for r in nodes_rows
            ],
            "edges": [
                {
                    "from": r["from_id"],
                    "to": r["to_id"],
                    "label": r["rel"],
                    "props": r["props"],
                }
                for r in edges_rows
            ],
        }

    async def get_neighborhood(self, name: str, depth: int = 2) -> dict:
        """Return nodes and edges within `depth` hops of a named node."""
        nodes_rows = await self._run(
            """
            MATCH (start {name: $name})
            CALL apoc.path.subgraphAll(start, {maxLevel: $depth}) YIELD nodes, relationships
            UNWIND nodes AS n
            RETURN id(n) AS id, labels(n) AS labels, n.name AS name, properties(n) AS props
            """,
            {"name": name, "depth": depth},
        )
        edges_rows = await self._run(
            """
            MATCH (start {name: $name})
            CALL apoc.path.subgraphAll(start, {maxLevel: $depth}) YIELD relationships
            UNWIND relationships AS r
            RETURN id(startNode(r)) AS from_id, id(endNode(r)) AS to_id,
                   type(r) AS rel, properties(r) AS props
            """,
            {"name": name, "depth": depth},
        )
        return {
            "center": name,
            "nodes": [
                {
                    "id": r["id"],
                    "label": r["labels"][0] if r["labels"] else "Node",
                    "name": r["name"],
                    "props": r["props"],
                }
                for r in nodes_rows
            ],
            "edges": [
                {"from": r["from_id"], "to": r["to_id"], "label": r["rel"]}
                for r in edges_rows
            ],
        }

    async def get_neighborhood_simple(self, name: str, depth: int = 2) -> dict:
        """Neighborhood without APOC — uses plain Cypher variable-length paths."""
        nodes_rows = await self._run(
            f"""
            MATCH (start {{name: $name}})
            MATCH path = (start)-[*0..{depth}]-(neighbor)
            WITH collect(DISTINCT neighbor) + [start] AS all_nodes
            UNWIND all_nodes AS n
            RETURN id(n) AS id, labels(n) AS labels, n.name AS name, properties(n) AS props
            """,
            {"name": name},
        )
        edges_rows = await self._run(
            f"""
            MATCH (start {{name: $name}})
            MATCH (start)-[*0..{depth}]-(neighbor)
            MATCH (a)-[r]-(b)
            WHERE (a.name = start.name OR a)-[*0..{depth}]-(start)
              AND (b.name = start.name OR b)-[*0..{depth}]-(start)
            RETURN DISTINCT id(a) AS from_id, id(b) AS to_id,
                   type(r) AS rel, properties(r) AS props
            """,
            {"name": name},
        )
        return {
            "center": name,
            "nodes": [
                {"id": r["id"], "label": r["labels"][0] if r["labels"] else "Node",
                 "name": r["name"], "props": r["props"]}
                for r in nodes_rows
            ],
            "edges": [
                {"from": r["from_id"], "to": r["to_id"], "label": r["rel"]}
                for r in edges_rows
            ],
        }

    async def search(self, query: str, limit: int = 20) -> list[dict]:
        """Case-insensitive name/description search across all nodes."""
        rows = await self._run(
            """
            MATCH (n)
            WHERE toLower(n.name) CONTAINS toLower($q)
               OR toLower(coalesce(n.description, '')) CONTAINS toLower($q)
            RETURN id(n) AS id, labels(n) AS labels, n.name AS name,
                   properties(n) AS props
            LIMIT $limit
            """,
            {"q": query, "limit": limit},
        )
        return [
            {"id": r["id"], "label": r["labels"][0] if r["labels"] else "Node",
             "name": r["name"], "props": r["props"]}
            for r in rows
        ]

    async def list_nodes(self, label: str | None = None, limit: int = 100) -> list[dict]:
        if label:
            label = _safe_label(label)
            rows = await self._run(
                f"MATCH (n:{label}) RETURN id(n) AS id, labels(n) AS labels, "
                f"n.name AS name, properties(n) AS props LIMIT $limit",
                {"limit": limit},
            )
        else:
            rows = await self._run(
                "MATCH (n) RETURN id(n) AS id, labels(n) AS labels, "
                "n.name AS name, properties(n) AS props LIMIT $limit",
                {"limit": limit},
            )
        return [
            {"id": r["id"], "label": r["labels"][0] if r["labels"] else "Node",
             "name": r["name"], "props": r["props"]}
            for r in rows
        ]

    async def get_relationships(self, name: str) -> list[dict]:
        """Get all direct relationships for a node."""
        rows = await self._run(
            """
            MATCH (n {name: $name})-[r]-(m)
            RETURN n.name AS from, type(r) AS rel, m.name AS to,
                   labels(m) AS to_labels,
                   CASE WHEN startNode(r) = n THEN 'out' ELSE 'in' END AS direction
            """,
            {"name": name},
        )
        return [dict(r) for r in rows]

    async def stats(self) -> dict:
        """Return counts of nodes and relationships."""
        rows = await self._run(
            """
            MATCH (n) WITH labels(n)[0] AS label, count(*) AS cnt
            RETURN label, cnt ORDER BY cnt DESC
            """
        )
        rel_rows = await self._run("MATCH ()-[r]->() RETURN count(r) AS cnt")
        return {
            "nodes": {r["label"]: r["cnt"] for r in rows},
            "relationships": rel_rows[0]["cnt"] if rel_rows else 0,
        }


# Module-level singleton
_client: KnowledgeGraphClient | None = None


def get_kg_client() -> KnowledgeGraphClient:
    global _client
    if _client is None:
        _client = KnowledgeGraphClient()
    return _client


async def auto_register_project(
    name: str,
    repo_url: str = "",
    tech: str = "",
    description: str = "",
    client_name: str = "",
    server_ip: str = "",
) -> None:
    """
    Convenience function called by project_builder and milestone_logger
    to auto-populate the KG when a project is created or deployed.
    """
    kg = get_kg_client()
    if not kg.is_configured():
        return
    try:
        await kg.upsert_node("Project", name, {"description": description, "tech": tech})
        if repo_url:
            repo_name = repo_url.rstrip("/").split("/")[-1]
            await kg.upsert_node("Repo", repo_name, {"url": repo_url})
            await kg.upsert_relationship("Project", name, "USES", "Repo", repo_name)
        if tech:
            await kg.upsert_node("Tech", tech)
            await kg.upsert_relationship("Project", name, "USES", "Tech", tech)
        if client_name:
            await kg.upsert_node("Client", client_name)
            await kg.upsert_relationship("Project", name, "FOR", "Client", client_name)
        if server_ip:
            await kg.upsert_node("Server", server_ip, {"ip": server_ip})
            await kg.upsert_relationship("Project", name, "DEPLOYED_ON", "Server", server_ip)
    except Exception as exc:
        logger.warning("KG auto_register_project failed (non-fatal): %s", exc)
