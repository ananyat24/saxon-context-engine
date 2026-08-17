// Demo UI logic: no framework, no build step -- fetch the API's own endpoints
// and render what comes back, so this only ever shows what the backend
// actually has, never mock data.

const API = "/api/v1";

// --- Access key handling ---------------------------------------------------
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
  document.getElementById("keyBtnLabel").textContent = hasKey ? "Key set" : "Access key";
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
    badge.textContent = data.database_connected ? "connected" : "not connected";
    badge.className = "badge " + (data.database_connected ? "badge-ok" : "badge-bad");
  } catch (err) {
    badge.textContent = "not reachable";
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
      `Right now it's watching for ${data.entity_types.length} kinds of things and ` +
      `${data.relationship_types.length} kinds of relationships between them, so it stays ` +
      `focused on what matters to your business instead of picking up anything and everything.`;
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
    emptyEl.textContent = 'Click "Access key" in the top right to see your information.';
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
      emptyEl.textContent = "That key doesn't match anything on file. Double-check it, or ask whoever set this up for you for the right one.";
      summaryEl.innerHTML = "";
      svg.innerHTML = "";
      insightEl.textContent = "";
      return;
    }
    if (!summaryRes.ok || !nodesRes.ok || !relsRes.ok) {
      emptyEl.style.display = "block";
      emptyEl.textContent = "Can't reach the system right now. Try reloading the page in a moment.";
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
  const { node_count, relationship_count } = summary;
  const density = node_count > 0 ? (relationship_count / node_count).toFixed(1) : 0;
  return `So far it has found ${node_count} things and ${relationship_count} connections ` +
    `between them, or about ${density} connections per thing on average. This is a small ` +
    `starting set, and it'll grow as more information is added.`;
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

  // Two things can be connected more than once (e.g. an old and a new answer
  // to "who manages this account"), sometimes in opposite directions (A
  // manages B vs. B manages A). Grouping by the pair and spreading each one
  // out to its own curve keeps their labels from landing on top of each
  // other. The perpendicular used to space them out has to be computed once
  // per pair rather than per relationship -- otherwise a reversed edge flips
  // its own offset's sign and lands right back on top of a different edge in
  // the same group instead of getting its own spot.
  const groups = {};
  rels.forEach((rel) => {
    if (!positions[rel.source] || !positions[rel.target]) return;
    const key = [rel.source, rel.target].sort().join("|");
    (groups[key] = groups[key] || []).push(rel);
  });

  Object.keys(groups).forEach((key) => {
    const group = groups[key];
    const [nameA, nameB] = key.split("|");
    const a = positions[nameA], b = positions[nameB];
    const midX = (a.x + b.x) / 2, midY = (a.y + b.y) / 2;
    const dx = b.x - a.x, dy = b.y - a.y;
    const len = Math.hypot(dx, dy) || 1;
    const nx = -dy / len, ny = dx / len;

    group.forEach((rel, i) => {
      const spread = (i - (group.length - 1) / 2) * 24;
      const ctrlX = midX + nx * spread * 2, ctrlY = midY + ny * spread * 2;
      const labelX = midX + nx * spread, labelY = midY + ny * spread;

      svgContent += `<path class="gv-edge" d="M ${a.x} ${a.y} Q ${ctrlX} ${ctrlY} ${b.x} ${b.y}" fill="none">
        <title>${escapeXml(rel.fact || "")}</title>
      </path>`;
      svgContent += `<text class="gv-edge-label" x="${labelX}" y="${labelY}" text-anchor="middle">${escapeXml(rel.type)}</text>`;
    });
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

// --- Ask a question -------------------------------------------------
document.getElementById("askBtn").addEventListener("click", async () => {
  const answerEl = document.getElementById("queryAnswer");
  const factsEl = document.getElementById("queryFacts");
  const rawWrap = document.getElementById("queryRawWrap");
  const rawEl = document.getElementById("queryRaw");
  const query = document.getElementById("queryInput").value.trim();
  if (!query) return;
  if (!getApiKey()) {
    answerEl.textContent = 'Click "Access key" in the top right first.';
    factsEl.innerHTML = "";
    rawWrap.hidden = true;
    return;
  }

  answerEl.textContent = "Thinking…";
  factsEl.innerHTML = "";
  rawWrap.hidden = true;
  try {
    const res = await fetch(`${API}/context/query`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ query }),
    });
    if (res.status === 401) {
      answerEl.textContent = "That key doesn't match anything on file. Double-check it, or ask whoever set this up for you for the right one.";
      return;
    }
    if (!res.ok) {
      answerEl.textContent = "Something went wrong answering that. Try again in a moment.";
      return;
    }
    const data = await res.json();
    const summary = data.metadata?.summary;
    const facts = data.metadata?.facts || [];

    answerEl.textContent = summary && summary !== "No matching graph context found."
      ? summary
      : "Nothing on file matches that yet. Try asking a broader question, or add more information first.";

    renderFacts(factsEl, facts);

    rawEl.textContent = JSON.stringify(data, null, 2);
    rawWrap.hidden = false;
  } catch (err) {
    answerEl.textContent = `Error: ${err.message}`;
  }
});

// Every fact carries whether it's still true or was superseded by something
// newer -- surfacing that plainly is the actual proof this system tracks
// history instead of just overwriting old information. Current facts are
// shown first since they're what most people reading this actually want to
// see; the superseded ones are still here for anyone who wants the history.
function renderFacts(container, facts) {
  if (!facts.length) {
    container.innerHTML = "";
    return;
  }
  const sorted = [...facts].sort((a, b) => (a.is_valid === false ? 1 : 0) - (b.is_valid === false ? 1 : 0));
  const hasSuperseded = sorted.some((f) => f.is_valid === false);
  const note = hasSuperseded
    ? `<p class="fact-note">* "Superseded" means this used to be true, but something more recent has replaced it. It's kept here so nothing gets lost.</p>`
    : "";
  container.innerHTML = `<p class="fact-list-label">Where this answer comes from:</p>` + sorted
    .map((f) => {
      const current = f.is_valid !== false;
      const badge = current
        ? `<span class="fact-badge fact-badge-current">current</span>`
        : `<span class="fact-badge fact-badge-superseded">superseded*</span>`;
      return `<div class="fact-card ${current ? "" : "fact-card-superseded"}">
        <span class="fact-text">${escapeXml(f.fact || "")}</span>
        ${badge}
      </div>`;
    })
    .join("") + note;
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
