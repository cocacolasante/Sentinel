"""
KnowledgeGraphSkill — query and build the personal knowledge graph.

Actions:
  add      — add a node (project, repo, server, client, idea, person, tech)
  connect  — create a relationship between two nodes
  query    — natural-language query → Cypher → results
  show     — show all relationships for a named entity
  search   — full-text search across all nodes
  stats    — node + relationship counts
  visualize — return the URL to the interactive graph view
"""

from __future__ import annotations

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult

_LABEL_ALIASES = {
    "project": "Project",
    "repo": "Repo",
    "repository": "Repo",
    "server": "Server",
    "client": "Client",
    "idea": "Idea",
    "domain": "Domain",
    "person": "Person",
    "task": "Task",
    "skill": "Skill",
    "tech": "Tech",
    "technology": "Tech",
    "language": "Tech",
    "framework": "Tech",
}


def _resolve_label(raw: str) -> str:
    return _LABEL_ALIASES.get((raw or "project").lower().strip(), "Project")


class KnowledgeGraphSkill(BaseSkill):
    name = "knowledge_graph"
    description = (
        "Personal knowledge graph — maps relationships between everything you work on. "
        "Nodes: projects, repos, servers, clients, ideas, people, tech. "
        "Actions: add (upsert a node), connect (create a relationship), "
        "show (relationships for an entity), search (find nodes), "
        "stats (counts), visualize (open the interactive graph). "
        "Example: 'show relationships between all my projects' → graph query. "
        "Example: 'add project SentinelAI' → upsert node. "
        "Example: 'connect SentinelAI to GitHub repo sentinel' → add USES edge."
    )
    trigger_intents = ["knowledge_graph", "graph_query", "graph_add", "graph_show"]
    approval_category = ApprovalCategory.NONE

    def is_available(self) -> bool:
        from app.integrations.knowledge_graph import get_kg_client
        return get_kg_client().is_configured()

    async def execute(self, params: dict, original_message: str) -> SkillResult:
        from app.integrations.knowledge_graph import get_kg_client

        kg = get_kg_client()
        if not kg.is_configured():
            return SkillResult(
                context_data=(
                    "[knowledge_graph not configured — add NEO4J_URI, NEO4J_USER, "
                    "NEO4J_PASSWORD to .env and restart]"
                ),
                skill_name=self.name,
                is_error=True,
                needs_config=True,
            )

        action = (params.get("action") or "show").lower().strip()

        if action == "add":
            return await self._add(kg, params)
        if action == "connect":
            return await self._connect(kg, params)
        if action in ("show", "relationships"):
            return await self._show(kg, params, original_message)
        if action == "search":
            return await self._search(kg, params, original_message)
        if action == "stats":
            return await self._stats(kg)
        if action == "visualize":
            return await self._visualize()
        # Default: treat as a show/search based on the message
        return await self._smart_query(kg, params, original_message)

    async def _add(self, kg, params: dict) -> SkillResult:
        label = _resolve_label(params.get("label") or params.get("type") or "Project")
        name = (params.get("name") or params.get("node_name") or "").strip()
        if not name:
            return SkillResult(
                context_data="[knowledge_graph add requires a name parameter]",
                skill_name=self.name,
            )
        props = {k: v for k, v in params.items() if k not in ("action", "label", "type", "name", "node_name")}
        node = await kg.upsert_node(label, name, props or None)
        return SkillResult(
            context_data=f"[KG] Added {label} node: **{name}**\n{props or ''}",
            skill_name=self.name,
        )

    async def _connect(self, kg, params: dict) -> SkillResult:
        from_label = _resolve_label(params.get("from_label") or params.get("from_type") or "Project")
        from_name = (params.get("from") or params.get("from_name") or "").strip()
        rel_type = (params.get("relationship") or params.get("rel") or "RELATED_TO").upper().replace(" ", "_")
        to_label = _resolve_label(params.get("to_label") or params.get("to_type") or "Project")
        to_name = (params.get("to") or params.get("to_name") or "").strip()

        if not from_name or not to_name:
            return SkillResult(
                context_data="[knowledge_graph connect requires: from, relationship, to]",
                skill_name=self.name,
            )
        await kg.upsert_relationship(from_label, from_name, rel_type, to_label, to_name)
        return SkillResult(
            context_data=f"[KG] Connected: **{from_name}** -{rel_type}→ **{to_name}**",
            skill_name=self.name,
        )

    async def _show(self, kg, params: dict, original_message: str) -> SkillResult:
        name = (params.get("name") or params.get("entity") or "").strip()
        if not name:
            # No specific entity — return graph stats + viz link
            return await self._stats(kg)
        rels = await kg.get_relationships(name)
        if not rels:
            all_nodes = await kg.list_nodes(limit=50)
            node_names = [n["name"] for n in all_nodes[:20]]
            return SkillResult(
                context_data=(
                    f"[KG] No relationships found for **{name}**.\n"
                    f"Known nodes: {', '.join(node_names) or 'none yet'}\n"
                    "Use 'add' to register it or 'connect' to link it."
                ),
                skill_name=self.name,
            )
        lines = [f"[KG] Relationships for **{name}** ({len(rels)} connections):\n"]
        for r in rels:
            arrow = "→" if r["direction"] == "out" else "←"
            to_label = (r.get("to_labels") or ["Node"])[0]
            lines.append(f"  {name} {arrow}[{r['rel']}]→ **{r['to']}** _{to_label}_")
        lines.append(f"\n🔗 Full graph: https://sentinelai.cloud/api/v1/graph/viz")
        return SkillResult(context_data="\n".join(lines), skill_name=self.name)

    async def _search(self, kg, params: dict, original_message: str) -> SkillResult:
        query = (params.get("query") or params.get("q") or original_message or "").strip()
        results = await kg.search(query, limit=15)
        if not results:
            return SkillResult(
                context_data=f"[KG] No nodes matching '{query}'.",
                skill_name=self.name,
            )
        lines = [f"[KG] Search results for '{query}' ({len(results)} nodes):\n"]
        for r in results:
            props = r.get("props") or {}
            desc = props.get("description") or props.get("url") or props.get("tech") or ""
            lines.append(f"  • **{r['name']}** _{r['label']}_ {f'— {desc[:80]}' if desc else ''}")
        lines.append(f"\n🔗 Visualize: https://sentinelai.cloud/api/v1/graph/viz")
        return SkillResult(context_data="\n".join(lines), skill_name=self.name)

    async def _stats(self, kg) -> SkillResult:
        stats = await kg.stats()
        nodes = stats.get("nodes") or {}
        rels = stats.get("relationships") or 0
        if not nodes:
            return SkillResult(
                context_data=(
                    "[KG] Knowledge graph is empty — no nodes yet.\n"
                    "Projects, repos, servers and ideas get added automatically as Sentinel works.\n"
                    "Or say: 'add project MyApp' to add one manually.\n"
                    f"🔗 Visualize: https://sentinelai.cloud/api/v1/graph/viz"
                ),
                skill_name=self.name,
            )
        lines = ["[KG] Knowledge Graph snapshot:\n"]
        for label, count in sorted(nodes.items(), key=lambda x: -x[1]):
            lines.append(f"  • {label}: {count}")
        lines.append(f"\n  Total relationships: {rels}")
        lines.append(f"\n🔗 Interactive graph: https://sentinelai.cloud/api/v1/graph/viz")
        return SkillResult(context_data="\n".join(lines), skill_name=self.name)

    async def _visualize(self) -> SkillResult:
        return SkillResult(
            context_data=(
                "[KG] Interactive graph visualization:\n"
                "🔗 https://sentinelai.cloud/api/v1/graph/viz\n\n"
                "The graph shows all nodes (projects, repos, servers, clients, ideas) "
                "and their relationships. Click any node to explore its connections."
            ),
            skill_name=self.name,
        )

    async def _smart_query(self, kg, params: dict, original_message: str) -> SkillResult:
        """Infer action from natural language when no explicit action param."""
        msg = original_message.lower()
        if any(w in msg for w in ("add", "create", "register")):
            return await self._add(kg, params)
        if any(w in msg for w in ("connect", "link", "relate")):
            return await self._connect(kg, params)
        if any(w in msg for w in ("search", "find", "look")):
            return await self._search(kg, params, original_message)
        if any(w in msg for w in ("visual", "graph", "show all", "map")):
            return await self._visualize()
        return await self._stats(kg)
