// Demo UI logic: no framework, no build step -- fetch the API's own endpoints
// and render what comes back, so this only ever shows what the backend
// actually has, never mock data.

const API = "/api/v1";

// --- API key handling ---------------------------------------------------
// Kept in this browser's localStorage only, for demo convenience across
// reloads. Never sent anywhere except this API's own routes via the
// X-API-Key header.
function getApiKey() {
  return localStorage.getItem("saxon_api_key") || "";
}

function authHeaders() {
  const key = getApiKey();
  return key ? { "X-API-Key": key } : {};
}

function updateKeyDot() {
  const hasKey = !!getApiKey();
  document.getElementById("keyDot").classList.toggle("set", hasKey);
  document.getElementById("keyBtnLabel").textContent = hasKey ? "API key set" : "API key";
}

function openKeyModal() {
  document.getElementById("apiKey").value = getApiKey();
  document.getElementById("authStatus").textContent = "";
  document.getElementById("keyOverlay").hidden = false;
  document.getElementById("apiKey").focus();
}

function closeKeyModal() {
  document.getElementById("keyOverlay").hidden = true;
}

document.getElementById("keyBtn").addEventListener("click", openKeyModal);
document.getElementById("closeKeyBtn").addEventListener("click", closeKeyModal);
// Click on the dimmed backdrop (not the modal card itself) also closes it.
document.getElementById("keyOverlay").addEventListener("click", (e) => {
  if (e.target.id === "keyOverlay") closeKeyModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !document.getElementById("keyOverlay").hidden) closeKeyModal();
});

document.getElementById("saveKeyBtn").addEventListener("click", () => {
  const key = document.getElementById("apiKey").value.trim();
  localStorage.setItem("saxon_api_key", key);
  updateKeyDot();
  document.getElementById("authStatus").textContent = key ? "Key saved." : "Key cleared.";
  document.getElementById("authStatus").className = "status-line ok";
  loadTenantData();
  if (key) setTimeout(closeKeyModal, 500);
});

document.getElementById("apiKey").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("saveKeyBtn").click();
});

updateKeyDot();

// --- Health -------------------------------------------------------------
async function loadHealth() {
  const badge = document.getElementById("healthBadge");
  try {
    const res = await fetch(`${API}/health`);
    const data = await res.json();
    badge.textContent = data.database_connected ? "database connected" : "database unreachable";
    badge.className = "badge " + (data.database_connected ? "badge-ok" : "badge-bad");
  } catch (err) {
    badge.textContent = "API unreachable";
    badge.className = "badge badge-bad";
  }
}

// --- Ontology -------------------------------------------------------------
async function loadOntology() {
  try {
    const res = await fetch(`${API}/entities`);
    const data = await res.json();
    document.getElementById("ontologySummary").innerHTML = `
      <div class="stat"><span class="num">${data.entity_types.length}</span><span class="label">Entity types</span></div>
      <div class="stat"><span class="num">${data.relationship_types.length}</span><span class="label">Relationship types</span></div>
    `;
    document.getElementById("ontologyInsight").textContent =
      `Extraction only recognizes ${data.entity_types.length} kinds of things and ` +
      `${data.relationship_types.length} kinds of connections between them, so the graph ` +
      `stays focused on your domain instead of filling up with categories nobody reviewed.`;
    document.getElementById("entityTypes").innerHTML = data.entity_types
      .map((t) => `<span class="pill">${t}</span>`)
      .join("");
    document.getElementById("relTypes").innerHTML = data.relationship_types
      .map((t) => `<span class="pill">${t}</span>`)
      .join("");
  } catch (err) {
    document.getElementById("ontologySummary").textContent = "Could not load ontology.";
  }
}

// --- Graph ----------------------------------------------------------------
async function loadGraph() {
  const summaryEl = document.getElementById("graphSummary");
  const emptyEl = document.getElementById("graphEmpty");
  const svg = document.getElementById("graphViz");

  const insightEl = document.getElementById("graphInsight");

  if (!getApiKey()) {
    summaryEl.innerHTML = "";
    svg.innerHTML = "";
    insightEl.textContent = "";
    emptyEl.style.display = "block";
    emptyEl.textContent = 'Click "API key" in the top right to see this tenant\'s graph.';
    return;
  }

  try {
    const [summaryRes, nodesRes, relsRes] = await Promise.all([
      fetch(`${API}/graph/summary`, { headers: authHeaders() }),
      fetch(`${API}/graph/nodes?limit=15`, { headers: authHeaders() }),
      fetch(`${API}/graph/relationships?limit=25`, { headers: authHeaders() }),
    ]);

    if (summaryRes.status === 401) {
      emptyEl.style.display = "block";
      emptyEl.textContent = "That key isn't recognized for this API. Double-check it, or ask whoever set up your tenant for a fresh one.";
      summaryEl.innerHTML = "";
      svg.innerHTML = "";
      insightEl.textContent = "";
      return;
    }
    if (!summaryRes.ok || !nodesRes.ok || !relsRes.ok) {
      emptyEl.style.display = "block";
      emptyEl.textContent = "The graph database isn't reachable right now. Check that Neo4j is running, then reload the page.";
      summaryEl.innerHTML = "";
      svg.innerHTML = "";
      insightEl.textContent = "";
      return;
    }

    const summary = await summaryRes.json();
    const nodes = await nodesRes.json();
    const rels = await relsRes.json();

    summaryEl.innerHTML = `
      <div class="stat"><span class="num">${summary.node_count}</span><span class="label">Nodes</span></div>
      <div class="stat"><span class="num">${summary.relationship_count}</span><span class="label">Relationships</span></div>
    `;

    if (nodes.length === 0) {
      emptyEl.style.display = "block";
      svg.innerHTML = "";
      insightEl.textContent = "";
      document.getElementById("suggestedQuestions").innerHTML = "";
    } else {
      emptyEl.style.display = "none";
      renderGraph(svg, nodes, rels);
      insightEl.textContent = describeGraph(summary, nodes);
      renderSuggestedQuestions(nodes);
    }
  } catch (err) {
    summaryEl.textContent = "Could not load graph.";
  }
}

// Turns the raw counts into a sentence a stakeholder can act on, rather than
// making them infer what "4 nodes, 6 relationships" means on their own.
function describeGraph(summary, nodes) {
  const { node_count, relationship_count, entity_types_present } = summary;
  const density = node_count > 0 ? (relationship_count / node_count).toFixed(1) : 0;
  const types = entity_types_present && entity_types_present.length
    ? entity_types_present.join(", ")
    : "no recognized types yet";
  return `This tenant is currently tracking ${node_count} entities (${types}) connected by ` +
    `${relationship_count} relationships, or about ${density} connections per entity on average. ` +
    `That's a demo-sized dataset for now; ingesting more source data will grow it.`;
}

// Turns real node names from this tenant's own graph into example questions,
// so the "Ask" box isn't a blank prompt asking a stakeholder to guess syntax.
function renderSuggestedQuestions(nodes) {
  const container = document.getElementById("suggestedQuestions");
  const names = nodes.map((n) => n.name).filter(Boolean).slice(0, 3);
  if (names.length === 0) {
    container.innerHTML = "";
    return;
  }
  const questions = [
    `What do we know about ${names[0]}?`,
    names[1] ? `How is ${names[0]} connected to ${names[1]}?` : null,
    names[2] ? `What's changed recently about ${names[2]}?` : null,
  ].filter(Boolean);
  container.innerHTML = questions
    .map((q) => `<button class="chip chip-suggest" type="button">${escapeXml(q)}</button>`)
    .join("");
  container.querySelectorAll(".chip-suggest").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.getElementById("queryInput").value = btn.textContent;
      document.getElementById("askBtn").click();
    });
  });
}

// Minimal dependency-free graph render: places nodes evenly around a circle
// and draws a line for each relationship between them. No physics/force
// layout -- for the handful of nodes a demo tenant has, a circle is legible
// and there's no library to load.
function renderGraph(svg, nodes, rels) {
  const w = 480, h = 380, cx = w / 2, cy = h / 2, r = Math.min(w, h) / 2 - 60;
  const nodeByName = {};
  const positions = {};

  nodes.forEach((n, i) => {
    const angle = (2 * Math.PI * i) / nodes.length - Math.PI / 2;
    positions[n.name] = { x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) };
    nodeByName[n.name] = n;
  });

  let svgContent = "";

  rels.forEach((rel) => {
    const a = positions[rel.source];
    const b = positions[rel.target];
    if (!a || !b) return; // relationship points at a node outside the fetched limit
    const midX = (a.x + b.x) / 2, midY = (a.y + b.y) / 2;
    svgContent += `<line class="gv-edge" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}">
      <title>${escapeXml(rel.fact || "")}</title>
    </line>`;
    svgContent += `<text class="gv-edge-label" x="${midX}" y="${midY}" text-anchor="middle">${escapeXml(rel.type)}</text>`;
  });

  nodes.forEach((n) => {
    const p = positions[n.name];
    svgContent += `<circle class="gv-node" cx="${p.x}" cy="${p.y}" r="7">
      <title>${escapeXml(n.summary || n.name)}</title>
    </circle>`;
    svgContent += `<text class="gv-label" x="${p.x}" y="${p.y - 12}" text-anchor="middle">${escapeXml(truncate(n.name, 18))}</text>`;
  });

  svg.innerHTML = svgContent;
}

function truncate(s, n) {
  return s && s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function escapeXml(s) {
  return String(s || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;",
  }[c]));
}

// --- Ask the context engine -------------------------------------------------
document.getElementById("askBtn").addEventListener("click", async () => {
  const answerEl = document.getElementById("queryAnswer");
  const factsEl = document.getElementById("queryFacts");
  const rawWrap = document.getElementById("queryRawWrap");
  const rawEl = document.getElementById("queryRaw");
  const query = document.getElementById("queryInput").value.trim();
  if (!query) return;
  if (!getApiKey()) {
    answerEl.textContent = 'Click "API key" in the top right first.';
    factsEl.innerHTML = "";
    rawWrap.hidden = true;
    return;
  }

  answerEl.textContent = "Asking…";
  factsEl.innerHTML = "";
  rawWrap.hidden = true;
  try {
    const res = await fetch(`${API}/context/query`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ query }),
    });
    if (res.status === 401) {
      answerEl.textContent = "That key isn't recognized for this API. Double-check it, or ask whoever set up your tenant for a fresh one.";
      return;
    }
    if (!res.ok) {
      answerEl.textContent = "The context engine hit an error answering that. Check that Neo4j is running, then try again.";
      return;
    }
    const data = await res.json();
    const summary = data.metadata?.summary;
    const facts = data.metadata?.facts || [];

    answerEl.textContent = summary && summary !== "No matching graph context found."
      ? summary
      : "Nothing in this tenant's graph matches that yet. Try a broader question, or ingest more data first.";

    renderFacts(factsEl, facts);

    rawEl.textContent = JSON.stringify(data, null, 2);
    rawWrap.hidden = false;
  } catch (err) {
    answerEl.textContent = `Error: ${err.message}`;
  }
});

// Every fact carries whether it's still true or was superseded -- surfacing
// that plainly is the actual proof this is a temporal graph, not a lookup table.
function renderFacts(container, facts) {
  if (!facts.length) {
    container.innerHTML = "";
    return;
  }
  container.innerHTML = `<p class="fact-list-label">Facts this answer draws on:</p>` + facts
    .map((f) => {
      const current = f.is_valid !== false;
      const badge = current
        ? `<span class="fact-badge fact-badge-current">current</span>`
        : `<span class="fact-badge fact-badge-superseded">superseded</span>`;
      return `<div class="fact-card ${current ? "" : "fact-card-superseded"}">
        <span class="fact-text">${escapeXml(f.fact || "")}</span>
        ${badge}
      </div>`;
    })
    .join("");
}

document.getElementById("queryInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("askBtn").click();
});

// --- Init -------------------------------------------------------------------
function loadTenantData() {
  loadOntology();
  loadGraph();
}

loadHealth();
loadTenantData();
