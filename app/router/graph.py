"""
Knowledge Graph REST API + interactive visualization.

GET  /api/v1/graph/viz            — vis.js HTML graph explorer
GET  /api/v1/graph/nodes          — all nodes (optional ?label=Project)
GET  /api/v1/graph/edges          — all relationships
GET  /api/v1/graph/search?q=      — full-text node search
GET  /api/v1/graph/node/{name}    — relationships for a specific node
GET  /api/v1/graph/stats          — node/rel counts
POST /api/v1/graph/node           — upsert a node
POST /api/v1/graph/edge           — upsert a relationship
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/graph", tags=["knowledge-graph"])

# ── Node colour map for vis.js ─────────────────────────────────────────────────
_COLORS = {
    "Project":  "#4f8ef7",
    "Repo":     "#f7a24f",
    "Server":   "#4ff79e",
    "Client":   "#f74f4f",
    "Idea":     "#c24ff7",
    "Domain":   "#f7f04f",
    "Person":   "#4ff7f0",
    "Task":     "#f74fc2",
    "Tech":     "#a0a0ff",
    "Skill":    "#ff9f40",
}
_DEFAULT_COLOR = "#aaaaaa"


def _kg():
    from app.integrations.knowledge_graph import get_kg_client
    return get_kg_client()


# ── REST endpoints ─────────────────────────────────────────────────────────────

@router.get("/stats")
async def graph_stats():
    kg = _kg()
    if not kg.is_configured():
        return {"error": "Neo4j not configured"}
    return await kg.stats()


@router.get("/nodes")
async def list_nodes(label: str = Query(default=""), limit: int = Query(default=200)):
    kg = _kg()
    if not kg.is_configured():
        return {"nodes": [], "error": "Neo4j not configured"}
    nodes = await kg.list_nodes(label or None, limit)
    return {"nodes": nodes}


@router.get("/edges")
async def list_edges(limit: int = Query(default=500)):
    kg = _kg()
    if not kg.is_configured():
        return {"edges": [], "error": "Neo4j not configured"}
    data = await kg.get_all(limit)
    return {"edges": data["edges"]}


@router.get("/search")
async def search_nodes(q: str = Query(default=""), limit: int = Query(default=20)):
    kg = _kg()
    if not kg.is_configured():
        return {"results": []}
    results = await kg.search(q, limit)
    return {"results": results}


@router.get("/node/{name}")
async def node_relationships(name: str):
    kg = _kg()
    if not kg.is_configured():
        return {"relationships": []}
    rels = await kg.get_relationships(name)
    return {"name": name, "relationships": rels}


@router.post("/node")
async def upsert_node(body: dict):
    kg = _kg()
    if not kg.is_configured():
        return {"error": "Neo4j not configured"}
    node = await kg.upsert_node(
        body.get("label", "Project"),
        body.get("name", ""),
        body.get("properties"),
    )
    return {"node": node}


@router.post("/edge")
async def upsert_edge(body: dict):
    kg = _kg()
    if not kg.is_configured():
        return {"error": "Neo4j not configured"}
    result = await kg.upsert_relationship(
        body.get("from_label", "Project"),
        body.get("from_name", ""),
        body.get("relationship", "RELATED_TO"),
        body.get("to_label", "Project"),
        body.get("to_name", ""),
        body.get("properties"),
    )
    return {"edge": result}


# ── Interactive vis.js visualization ──────────────────────────────────────────

@router.get("/viz", response_class=HTMLResponse)
async def graph_viz():
    """Interactive knowledge graph powered by vis.js Network."""
    kg = _kg()
    configured = kg.is_configured()

    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sentinel Knowledge Graph</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis/4.21.0/vis.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/vis/4.21.0/vis.min.css">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0f1117; color: #e0e0e0; height: 100vh; display: flex;
         flex-direction: column; }
  header { padding: 12px 20px; background: #1a1d27; border-bottom: 1px solid #2d3142;
           display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  header h1 { font-size: 18px; font-weight: 600; color: #fff; }
  header span { font-size: 13px; color: #888; }
  #controls { display: flex; gap: 8px; align-items: center; flex: 1; flex-wrap: wrap; }
  #search { background: #2d3142; border: 1px solid #3d4262; color: #e0e0e0;
            padding: 6px 12px; border-radius: 6px; font-size: 13px; width: 200px; }
  #search:focus { outline: none; border-color: #4f8ef7; }
  .btn { background: #2d3142; border: 1px solid #3d4262; color: #ccc; padding: 6px 12px;
         border-radius: 6px; font-size: 13px; cursor: pointer; }
  .btn:hover { background: #3d4262; color: #fff; }
  .btn.active { background: #4f8ef7; border-color: #4f8ef7; color: #fff; }
  #main { display: flex; flex: 1; min-height: 0; }
  #network { flex: 1; background: #0f1117; }
  #sidebar { width: 280px; background: #1a1d27; border-left: 1px solid #2d3142;
             padding: 16px; overflow-y: auto; font-size: 13px; display: none; }
  #sidebar.open { display: block; }
  #sidebar h3 { font-size: 15px; font-weight: 600; color: #fff; margin-bottom: 12px; }
  .rel-item { padding: 6px 0; border-bottom: 1px solid #2d3142; }
  .rel-dir { color: #4f8ef7; font-weight: 600; }
  .rel-type { color: #f7a24f; font-size: 11px; background: #2d3142;
              padding: 2px 6px; border-radius: 4px; margin: 0 4px; }
  .legend { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px 20px;
            background: #1a1d27; border-top: 1px solid #2d3142; }
  .legend-item { display: flex; align-items: center; gap: 5px; font-size: 11px; color: #aaa; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; }
  #status { padding: 4px 20px; font-size: 12px; color: #888;
            background: #1a1d27; border-top: 1px solid #2d3142; }
  .empty-state { display: flex; flex-direction: column; align-items: center;
                 justify-content: center; height: 100%; color: #555; }
  .empty-state h2 { font-size: 20px; margin-bottom: 8px; }
  .empty-state p { font-size: 14px; max-width: 360px; text-align: center; line-height: 1.5; }
</style>
</head>
<body>
<header>
  <h1>⬡ Knowledge Graph</h1>
  <div id="controls">
    <input id="search" type="text" placeholder="Search nodes..." oninput="filterNodes(this.value)">
    <button class="btn active" onclick="loadAll()">All</button>
    <button class="btn" onclick="filterByLabel('Project')">Projects</button>
    <button class="btn" onclick="filterByLabel('Repo')">Repos</button>
    <button class="btn" onclick="filterByLabel('Server')">Servers</button>
    <button class="btn" onclick="filterByLabel('Client')">Clients</button>
  </div>
  <span id="node-count">Loading...</span>
</header>
<div id="main">
  <div id="network"></div>
  <div id="sidebar" id="sidebar">
    <h3 id="sidebar-title">—</h3>
    <div id="sidebar-content"></div>
  </div>
</div>
<div class="legend" id="legend"></div>
<div id="status">Ready</div>

<script>
const COLORS = """ + str(_COLORS).replace("'", '"') + """;
const DEFAULT_COLOR = "#aaaaaa";
const BASE = window.location.origin;

let allNodes = [], allEdges = [], network, nodeDataset, edgeDataset;

function getColor(label) { return COLORS[label] || DEFAULT_COLOR; }

function buildLegend() {
  const leg = document.getElementById('legend');
  Object.entries(COLORS).forEach(([label, color]) => {
    leg.innerHTML += `<div class="legend-item">
      <div class="legend-dot" style="background:${color}"></div>${label}
    </div>`;
  });
}

async function loadAll() {
  document.getElementById('status').textContent = 'Loading graph...';
  document.querySelectorAll('.btn').forEach(b => b.classList.remove('active'));
  event && event.target && event.target.classList.add('active');
  try {
    const [nr, er] = await Promise.all([
      fetch(BASE + '/api/v1/graph/nodes?limit=500').then(r => r.json()),
      fetch(BASE + '/api/v1/graph/edges?limit=1000').then(r => r.json()),
    ]);
    allNodes = (nr.nodes || []);
    allEdges = (er.edges || []);
    renderGraph(allNodes, allEdges);
  } catch(e) {
    document.getElementById('status').textContent = 'Error loading graph: ' + e;
  }
}

function filterByLabel(label) {
  document.querySelectorAll('.btn').forEach(b => b.classList.remove('active'));
  event && event.target && event.target.classList.add('active');
  const filtered = allNodes.filter(n => n.label === label);
  const ids = new Set(filtered.map(n => n.id));
  const filteredEdges = allEdges.filter(e => ids.has(e.from) && ids.has(e.to));
  renderGraph(filtered, filteredEdges);
}

function filterNodes(q) {
  if (!q) { renderGraph(allNodes, allEdges); return; }
  const low = q.toLowerCase();
  const filtered = allNodes.filter(n => (n.name || '').toLowerCase().includes(low));
  const ids = new Set(filtered.map(n => n.id));
  const filteredEdges = allEdges.filter(e => ids.has(e.from) || ids.has(e.to));
  renderGraph(filtered, filteredEdges);
}

function renderGraph(nodes, edges) {
  const container = document.getElementById('network');
  if (!nodes.length) {
    container.innerHTML = `<div class="empty-state">
      <h2>No nodes yet</h2>
      <p>Sentinel will auto-populate this graph as you work — creating projects,
         deploying servers, and adding repos. Or tell Sentinel to "add project MyApp".</p>
    </div>`;
    document.getElementById('node-count').textContent = '0 nodes';
    return;
  }

  const visNodes = nodes.map(n => ({
    id: n.id,
    label: n.name || String(n.id),
    title: `<b>${n.name}</b><br>${n.label}<br>${JSON.stringify(n.props || {}, null, 2)}`,
    color: { background: getColor(n.label), border: '#fff', highlight: { background: '#fff' } },
    font: { color: '#fff', size: 13 },
    shape: 'dot',
    size: 16,
    _label: n.label,
    _name: n.name,
  }));

  const visEdges = edges.map(e => ({
    from: e.from,
    to: e.to,
    label: e.label,
    color: { color: '#3d4262', highlight: '#4f8ef7' },
    font: { color: '#888', size: 10, align: 'middle' },
    arrows: 'to',
    smooth: { type: 'curvedCW', roundness: 0.1 },
  }));

  nodeDataset = new vis.DataSet(visNodes);
  edgeDataset = new vis.DataSet(visEdges);

  const options = {
    physics: {
      stabilization: { iterations: 100 },
      barnesHut: { gravitationalConstant: -4000, springLength: 120 },
    },
    interaction: { hover: true, tooltipDelay: 200 },
    layout: { improvedLayout: true },
  };

  if (network) network.destroy();
  network = new vis.Network(container, { nodes: nodeDataset, edges: edgeDataset }, options);

  network.on('click', async (params) => {
    if (!params.nodes.length) { closeSidebar(); return; }
    const nodeId = params.nodes[0];
    const node = visNodes.find(n => n.id === nodeId);
    if (!node) return;
    openSidebar(node._name, node._label);
  });

  document.getElementById('node-count').textContent =
    `${nodes.length} nodes · ${edges.length} edges`;
  document.getElementById('status').textContent =
    `Loaded ${nodes.length} nodes and ${edges.length} relationships`;
}

async function openSidebar(name, label) {
  const sidebar = document.getElementById('sidebar');
  sidebar.classList.add('open');
  document.getElementById('sidebar-title').textContent = name;
  document.getElementById('sidebar-content').innerHTML = '<div style="color:#888">Loading...</div>';
  try {
    const data = await fetch(BASE + '/api/v1/graph/node/' + encodeURIComponent(name)).then(r => r.json());
    const rels = data.relationships || [];
    if (!rels.length) {
      document.getElementById('sidebar-content').innerHTML =
        `<div style="color:#888">No relationships yet.</div>`;
      return;
    }
    let html = `<div style="color:#888;margin-bottom:8px">${label} · ${rels.length} connections</div>`;
    rels.forEach(r => {
      const arrow = r.direction === 'out' ? '→' : '←';
      html += `<div class="rel-item">
        <span class="rel-dir">${arrow}</span>
        <span class="rel-type">${r.rel}</span>
        <b>${r.to}</b>
      </div>`;
    });
    document.getElementById('sidebar-content').innerHTML = html;
  } catch(e) {
    document.getElementById('sidebar-content').innerHTML = `<div style="color:#f74f4f">${e}</div>`;
  }
}

function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
}

buildLegend();
loadAll();
</script>
</body>
</html>"""
    return HTMLResponse(content=html)
