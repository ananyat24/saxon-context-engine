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

// --- Knowledge base selection -----------------------------------------------
// Which dataset (group_id) the Graph and Ask panels are scoped to. Persisted
// per-browser so switching pages/reloading doesn't silently reset it back to
// the default. The server still validates this against the tenant's own list
// on every request (see app/security.py's resolve_knowledge_base) -- this is
// just which one the UI asks for, not a trust boundary.
function getSelectedKnowledgeBase() {
  return localStorage.getItem("saxon_kb") || "";
}

function setSelectedKnowledgeBase(id) {
  localStorage.setItem("saxon_kb", id);
}

// Builds "?a=1&b=2"-style query strings without worrying about which param
// comes first needing "?" vs "&".
function buildQuery(params) {
  const kb = getSelectedKnowledgeBase();
  if (kb) params.knowledge_base = kb;
  const user = getSelectedUser();
  if (user) params.as_user = user;
  const qs = new URLSearchParams(params).toString();
  return qs ? `?${qs}` : "";
}

// Every connector (knowledge base) this tenant has, kept around so the
// document-set create form can offer them as checkboxes without a second
// fetch -- populated by loadKnowledgeBases().
let knowledgeBaseDirectory = [];

async function loadKnowledgeBases() {
  const select = document.getElementById("kbSelect");
  if (!getApiKey()) {
    select.hidden = true;
    select.innerHTML = "";
    knowledgeBaseDirectory = [];
    return;
  }
  try {
    const res = await fetch(`${API}/graph/knowledge-bases`, { headers: authHeaders() });
    if (!res.ok) {
      select.hidden = true;
      return;
    }
    const data = await res.json();
    knowledgeBaseDirectory = data.knowledge_bases;
    const current = getSelectedKnowledgeBase();
    const stillValid = data.knowledge_bases.some((kb) => kb.id === current);
    if (!stillValid) setSelectedKnowledgeBase(data.default);

    select.innerHTML = data.knowledge_bases
      .map((kb) => `<option value="${escapeXml(kb.id)}">${escapeXml(kb.label)}</option>`)
      .join("");
    select.value = getSelectedKnowledgeBase();
    select.hidden = data.knowledge_bases.length <= 1;
  } catch (err) {
    select.hidden = true;
  }
}

document.getElementById("kbSelect").addEventListener("change", async (e) => {
  setSelectedKnowledgeBase(e.target.value);
  setSelectedUser(""); // a different knowledge base has a different (or no) org chart
  await loadUsers();
  renderScopeSelect(); // "This connector only" option's label follows the new selection
  loadGraph();
  document.getElementById("queryAnswer").textContent = "";
  document.getElementById("queryFacts").innerHTML = "";
  document.getElementById("queryRawWrap").hidden = true;
  document.getElementById("seeMoreBtn").hidden = true;
});

// --- Document sets ----------------------------------------------------------
// Named bundles of one or more connectors (knowledge bases), used to scope
// the "Ask a question" panel across several of them at once instead of
// picking exactly one -- see app/graph/document_sets.py. Every set here
// belongs to the current tenant only (enforced server-side).
let documentSetDirectory = [];
// Which document set (if any) the Ask panel is scoped to right now. "" means
// "just the single connector picked in the header" -- the original behavior.
// Not persisted across reloads, same reasoning as selectedUser: this is a
// live choice about the question you're about to ask, not a standing setting.
let selectedDocumentSet = "";
// Which document set the create/edit form below is currently editing, if
// any -- "" means the form is in "new document set" mode. The form itself
// is shared between create and edit (same fields either way) rather than
// duplicating it, so editing "just" means pre-filling it and switching what
// the submit button does.
let editingDocSetId = "";

function getSelectedDocumentSet() {
  return selectedDocumentSet;
}

function renderDocSetConnectorPicker(checkedIds = []) {
  const container = document.getElementById("docSetConnectors");
  container.innerHTML = knowledgeBaseDirectory
    .map(
      (kb) => `<label>
        <input type="checkbox" value="${escapeXml(kb.id)}" ${checkedIds.includes(kb.id) ? "checked" : ""} />
        ${escapeXml(kb.label)}
      </label>`
    )
    .join("");
}

function renderDocSetsTable() {
  const body = document.getElementById("docSetsBody");
  const empty = document.getElementById("docSetsEmpty");
  if (documentSetDirectory.length === 0) {
    body.innerHTML = "";
    empty.style.display = "block";
    return;
  }
  empty.style.display = "none";
  body.innerHTML = documentSetDirectory
    .map((ds) => {
      const connectorLabels = ds.connectors.map((c) => escapeXml(c.label)).join(", ");
      const publicBadge = ds.is_public
        ? `<span class="badge badge-ok">Public</span>`
        : `<span class="badge badge-muted">Private</span>`;
      return `<tr data-id="${escapeXml(ds.id)}">
        <td>${escapeXml(ds.name)}</td>
        <td>${connectorLabels}</td>
        <td><span class="badge badge-ok">${escapeXml(ds.status)}</span></td>
        <td>${publicBadge}</td>
        <td>
          <button class="delete-btn" type="button" data-edit-id="${escapeXml(ds.id)}">Edit</button>
          <button class="delete-btn" type="button" data-delete-id="${escapeXml(ds.id)}">Delete</button>
        </td>
      </tr>`;
    })
    .join("");
  body.querySelectorAll("[data-edit-id]").forEach((btn) => {
    btn.addEventListener("click", () => startEditDocSet(btn.dataset.editId));
  });
  body.querySelectorAll("[data-delete-id]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const ds = documentSetDirectory.find((d) => d.id === btn.dataset.deleteId);
      const label = ds ? `"${ds.name}"` : "this document set";
      if (window.confirm(`Delete ${label}? This can't be undone.`)) {
        deleteDocumentSet(btn.dataset.deleteId);
      }
    });
  });
}

function renderScopeSelect() {
  const select = document.getElementById("scopeSelect");
  if (documentSetDirectory.length === 0) {
    select.hidden = true;
    select.innerHTML = "";
    return;
  }
  const currentKb = knowledgeBaseDirectory.find((kb) => kb.id === getSelectedKnowledgeBase());
  const singleOption = `<option value="">This connector only${currentKb ? ` (${escapeXml(currentKb.label)})` : ""}</option>`;
  const setOptions = documentSetDirectory
    .map((ds) => `<option value="${escapeXml(ds.id)}">${escapeXml(ds.name)} (${ds.connectors.length} connectors)</option>`)
    .join("");
  select.innerHTML = singleOption + setOptions;
  select.value = documentSetDirectory.some((ds) => ds.id === selectedDocumentSet) ? selectedDocumentSet : "";
  selectedDocumentSet = select.value;
  select.hidden = false;
}

async function loadDocumentSets() {
  const card = document.getElementById("docSetsCard");
  if (!getApiKey()) {
    card.hidden = true;
    documentSetDirectory = [];
    renderScopeSelect();
    return;
  }
  try {
    const res = await fetch(`${API}/document-sets`, { headers: authHeaders() });
    if (!res.ok) {
      card.hidden = true;
      return;
    }
    documentSetDirectory = await res.json();
    card.hidden = false;
    renderDocSetConnectorPicker();
    renderDocSetsTable();
    renderScopeSelect();
  } catch (err) {
    card.hidden = true;
  }
}

document.getElementById("scopeSelect").addEventListener("change", async (e) => {
  selectedDocumentSet = e.target.value;
  // Re-fetches the header knowledge base's own nodes too (loadGraph), since
  // refreshSuggestedQuestionsForScope needs a fallback list when the scope
  // switches back to "this connector only" -- simplest way to keep that
  // fallback correct without caching header nodes separately.
  await loadGraph();
});

function resetDocSetForm() {
  editingDocSetId = "";
  document.getElementById("docSetFormSummary").textContent = "New document set";
  document.getElementById("createDocSetBtn").textContent = "Create document set";
  document.getElementById("cancelDocSetEditBtn").hidden = true;
  document.getElementById("docSetName").value = "";
  document.getElementById("docSetPublic").checked = true;
  renderDocSetConnectorPicker();
  document.getElementById("docSetStatus").textContent = "";
}

function startEditDocSet(id) {
  const ds = documentSetDirectory.find((d) => d.id === id);
  if (!ds) return;
  editingDocSetId = id;
  document.getElementById("docSetFormWrap").open = true;
  document.getElementById("docSetFormSummary").textContent = `Editing "${ds.name}"`;
  document.getElementById("createDocSetBtn").textContent = "Save changes";
  document.getElementById("cancelDocSetEditBtn").hidden = false;
  document.getElementById("docSetName").value = ds.name;
  document.getElementById("docSetPublic").checked = ds.is_public;
  renderDocSetConnectorPicker(ds.connectors.map((c) => c.id));
  document.getElementById("docSetStatus").textContent = "";
  document.getElementById("docSetFormWrap").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

document.getElementById("cancelDocSetEditBtn").addEventListener("click", resetDocSetForm);

document.getElementById("createDocSetBtn").addEventListener("click", async () => {
  const statusEl = document.getElementById("docSetStatus");
  const name = document.getElementById("docSetName").value.trim();
  const connectorIds = Array.from(
    document.querySelectorAll("#docSetConnectors input:checked")
  ).map((el) => el.value);
  const isPublic = document.getElementById("docSetPublic").checked;
  const isEditing = !!editingDocSetId;

  if (!name) {
    statusEl.textContent = "Give this document set a name.";
    statusEl.className = "status-line bad";
    return;
  }
  if (connectorIds.length === 0) {
    statusEl.textContent = "Pick at least one connector.";
    statusEl.className = "status-line bad";
    return;
  }

  statusEl.textContent = isEditing ? "Saving…" : "Creating…";
  statusEl.className = "status-line";
  try {
    const res = await fetch(
      isEditing ? `${API}/document-sets/${encodeURIComponent(editingDocSetId)}` : `${API}/document-sets`,
      {
        method: isEditing ? "PUT" : "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ name, connector_ids: connectorIds, is_public: isPublic }),
      }
    );
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      statusEl.textContent = body.detail || `Could not ${isEditing ? "save" : "create"} that document set.`;
      statusEl.className = "status-line bad";
      return;
    }
    resetDocSetForm();
    statusEl.textContent = isEditing ? "Saved." : "Created.";
    statusEl.className = "status-line ok";
    await loadDocumentSets();
  } catch (err) {
    statusEl.textContent = `Error: ${err.message}`;
    statusEl.className = "status-line bad";
  }
});

async function deleteDocumentSet(id) {
  try {
    const res = await fetch(`${API}/document-sets/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: authHeaders(),
    });
    if (!res.ok && res.status !== 204) return;
    if (selectedDocumentSet === id) selectedDocumentSet = "";
    if (editingDocSetId === id) resetDocSetForm();
    await loadDocumentSets();
  } catch (err) {
    // Silently leave the row in place -- the delete button itself is the
    // retry affordance, no need for a dedicated error banner here.
  }
}

// --- Source connectors -------------------------------------------------------
// Pulls content in from an external source (a web page today) into one of
// the tenant's knowledge bases -- see app/graph/connectors.py and
// app/api/connectors.py. Distinct from "connector" as used in the Document
// Sets section above (there it means one of the tenant's existing knowledge
// bases); here it means the thing that gets data INTO one in the first
// place. Every connector here belongs to the current tenant only (enforced
// server-side).
let connectorDirectory = [];

function renderConnectorKbSelect() {
  const select = document.getElementById("connectorKbSelect");
  select.innerHTML = knowledgeBaseDirectory
    .map((kb) => `<option value="${escapeXml(kb.id)}">${escapeXml(kb.label)}</option>`)
    .join("");
}

function formatSyncStatus(c) {
  const labels = {
    never_synced: "Never synced",
    synced: "Synced",
    unchanged: "Up to date",
    error: "Error",
  };
  const label = labels[c.status] || c.status;
  const cls = c.status === "error" ? "badge-bad" : c.status === "never_synced" ? "badge-muted" : "badge-ok";
  const title = c.status === "error" && c.last_error ? ` title="${escapeXml(c.last_error)}"` : "";
  return `<span class="badge ${cls}"${title}>${escapeXml(label)}</span>`;
}

const CONNECTOR_TYPE_LABELS = {
  web: "Web page",
  google_drive: "Google Drive",
  database: "Database / CRM",
  documents: "Documents",
  email: "Email inbox",
};

function renderConnectorsTable() {
  const body = document.getElementById("connectorsBody");
  const empty = document.getElementById("connectorsEmpty");
  if (connectorDirectory.length === 0) {
    body.innerHTML = "";
    empty.style.display = "block";
    return;
  }
  empty.style.display = "none";
  const kbLabel = (id) => knowledgeBaseDirectory.find((kb) => kb.id === id)?.label || id;
  body.innerHTML = connectorDirectory
    .map((c) => {
      const lastSynced = c.last_synced_at ? new Date(c.last_synced_at).toLocaleString() : "—";
      const typeLabel = CONNECTOR_TYPE_LABELS[c.type] || c.type;
      return `<tr data-id="${escapeXml(c.id)}">
        <td><button type="button" class="connector-name-link" data-preview-id="${escapeXml(c.id)}">${escapeXml(c.name)}</button></td>
        <td>
          <span class="badge badge-muted">${escapeXml(typeLabel)}</span><br />
          <span class="muted" style="font-size:0.8rem">${escapeXml(c.url)}</span>
        </td>
        <td>${escapeXml(kbLabel(c.group_id))}</td>
        <td>${formatSyncStatus(c)}</td>
        <td><span class="muted" style="font-size:0.8rem">${escapeXml(lastSynced)}</span></td>
        <td>
          <button class="sync-btn" type="button" data-sync-id="${escapeXml(c.id)}">Sync now</button>
          <button class="delete-btn" type="button" data-delete-connector-id="${escapeXml(c.id)}">Delete</button>
        </td>
      </tr>`;
    })
    .join("");
  body.querySelectorAll("[data-preview-id]").forEach((btn) => {
    btn.addEventListener("click", () => openConnectorPreview(btn.dataset.previewId));
  });
  body.querySelectorAll("[data-sync-id]").forEach((btn) => {
    btn.addEventListener("click", () => syncConnector(btn.dataset.syncId));
  });
  body.querySelectorAll("[data-delete-connector-id]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const c = connectorDirectory.find((x) => x.id === btn.dataset.deleteConnectorId);
      const label = c ? `"${c.name}"` : "this connector";
      if (window.confirm(`Remove ${label}? This won't remove anything already added to the graph.`)) {
        deleteConnector(btn.dataset.deleteConnectorId);
      }
    });
  });
}

// Fetches nodes/relationships for one specific knowledge base (group_id),
// independent of whatever the header's kbSelect is currently set to -- used
// by the connector data preview and the doc-set-aware suggested questions
// below, both of which need a specific scope rather than "whatever's
// selected right now".
async function fetchGraphSliceFor(groupId, { nodesLimit = 30, relsLimit = 40 } = {}) {
  try {
    const qs = (extra) => `?knowledge_base=${encodeURIComponent(groupId)}${extra}`;
    const [nodesRes, relsRes] = await Promise.all([
      fetch(`${API}/graph/nodes${qs(`&limit=${nodesLimit}`)}`, { headers: authHeaders() }),
      fetch(`${API}/graph/relationships${qs(`&limit=${relsLimit}`)}`, { headers: authHeaders() }),
    ]);
    return {
      nodes: nodesRes.ok ? await nodesRes.json() : [],
      rels: relsRes.ok ? await relsRes.json() : [],
    };
  } catch (err) {
    return { nodes: [], rels: [] };
  }
}

async function openConnectorPreview(connectorId) {
  const connector = connectorDirectory.find((c) => c.id === connectorId);
  if (!connector) return;
  const overlay = document.getElementById("connectorPreviewOverlay");
  const title = document.getElementById("connectorPreviewTitle");
  const subtitle = document.getElementById("connectorPreviewSubtitle");
  const bodyEl = document.getElementById("connectorPreviewBody");

  title.textContent = connector.name;
  subtitle.textContent = "Loading what's been pulled into the graph from this connector…";
  bodyEl.innerHTML = "";
  overlay.hidden = false;

  const { nodes, rels } = await fetchGraphSliceFor(connector.group_id);

  if (connector.status === "never_synced") {
    subtitle.textContent = "This connector hasn't been synced yet, so there's nothing to show here.";
    return;
  }
  subtitle.textContent = `${nodes.length} thing${nodes.length === 1 ? "" : "s"} and ${rels.length} fact${rels.length === 1 ? "" : "s"} found from this source so far.`;

  const entitiesHtml = nodes.length
    ? `<ul class="preview-list">${nodes.map((n) => `<li>${escapeXml(n.name)}</li>`).join("")}</ul>`
    : `<p class="preview-empty">Nothing found yet.</p>`;
  const factsHtml = rels.length
    ? `<ul class="preview-list">${rels.map((r) => `<li>${escapeXml(r.fact || `${r.source} → ${r.type} → ${r.target}`)}</li>`).join("")}</ul>`
    : `<p class="preview-empty">Nothing found yet.</p>`;

  bodyEl.innerHTML = `
    <div class="preview-section"><h3>Entities</h3>${entitiesHtml}</div>
    <div class="preview-section"><h3>Facts</h3>${factsHtml}</div>
  `;
}

function closeConnectorPreview() {
  document.getElementById("connectorPreviewOverlay").hidden = true;
}
document.getElementById("closeConnectorPreviewBtn").addEventListener("click", closeConnectorPreview);
document.getElementById("connectorPreviewOverlay").addEventListener("click", (e) => {
  if (e.target.id === "connectorPreviewOverlay") closeConnectorPreview();
});

async function loadConnectors() {
  const card = document.getElementById("connectorsCard");
  if (!getApiKey()) {
    card.hidden = true;
    connectorDirectory = [];
    return;
  }
  try {
    const res = await fetch(`${API}/connectors`, { headers: authHeaders() });
    if (!res.ok) {
      card.hidden = true;
      return;
    }
    connectorDirectory = await res.json();
    card.hidden = false;
    renderConnectorKbSelect();
    renderConnectorsTable();
  } catch (err) {
    card.hidden = true;
  }
}

// Types that need a tenant-supplied address -- kept in sync with
// app/api/connectors.py's own _TYPES_REQUIRING_URL. The demo data types
// (database/documents/email) read a fixed bundled sample server-side, so
// their URL field stays hidden rather than asking for an input that's ignored.
const CONNECTOR_TYPES_REQUIRING_URL = new Set(["web", "google_drive"]);
const CONNECTOR_URL_PLACEHOLDERS = {
  web: "https://example.com/page-to-pull-in",
  google_drive: "Drive folder link or id (share the folder with the service account first)",
};

function updateConnectorUrlVisibility() {
  const type = document.getElementById("connectorType").value;
  document.getElementById("connectorUrlRow").hidden = !CONNECTOR_TYPES_REQUIRING_URL.has(type);
  document.getElementById("connectorUrl").placeholder = CONNECTOR_URL_PLACEHOLDERS[type] || "";
}
document.getElementById("connectorType").addEventListener("change", updateConnectorUrlVisibility);
updateConnectorUrlVisibility();

document.getElementById("createConnectorBtn").addEventListener("click", async () => {
  const statusEl = document.getElementById("connectorStatus");
  const name = document.getElementById("connectorName").value.trim();
  const type = document.getElementById("connectorType").value;
  const needsUrl = CONNECTOR_TYPES_REQUIRING_URL.has(type);
  const url = document.getElementById("connectorUrl").value.trim();
  const groupId = document.getElementById("connectorKbSelect").value;

  if (!name || (needsUrl && !url)) {
    statusEl.textContent = needsUrl ? "Give the connector a name and a URL." : "Give the connector a name.";
    statusEl.className = "status-line bad";
    return;
  }

  statusEl.textContent = "Adding…";
  statusEl.className = "status-line";
  try {
    const res = await fetch(`${API}/connectors`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ name, type, group_id: groupId, url: needsUrl ? url : undefined }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      statusEl.textContent = body.detail || "Could not add that connector.";
      statusEl.className = "status-line bad";
      return;
    }
    document.getElementById("connectorName").value = "";
    document.getElementById("connectorUrl").value = "";
    statusEl.textContent = "Added. Click \"Sync now\" on it below to pull its content in.";
    statusEl.className = "status-line ok";
    await loadConnectors();
  } catch (err) {
    statusEl.textContent = `Error: ${err.message}`;
    statusEl.className = "status-line bad";
  }
});

async function syncConnector(id) {
  const row = document.querySelector(`#connectorsBody tr[data-id="${CSS.escape(id)}"]`);
  const btn = row?.querySelector("[data-sync-id]");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Syncing…";
  }
  try {
    const res = await fetch(`${API}/connectors/${encodeURIComponent(id)}/sync`, {
      method: "POST",
      headers: authHeaders(),
    });
    // A 402 (spend cap hit) or any other non-2xx still means the connector's
    // own status was recorded server-side (see app/api/connectors.py) --
    // reloading the list picks that up either way, so there's no separate
    // error branch needed here beyond letting the reload happen.
    await res.json().catch(() => ({}));
  } catch (err) {
    // Network failure before a response came back at all -- the connector's
    // last known status just stays what it was; reload still runs below so
    // the row doesn't stay stuck on "Syncing...".
  }
  await loadConnectors();
}

async function deleteConnector(id) {
  try {
    const res = await fetch(`${API}/connectors/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: authHeaders(),
    });
    if (!res.ok && res.status !== 204) return;
    await loadConnectors();
  } catch (err) {
    // Silently leave the row in place -- the delete button itself is the
    // retry affordance, no need for a dedicated error banner here.
  }
}

// --- "View as" (role-based visibility) --------------------------------------
// Which person's view of the selected knowledge base the Graph and Ask panels
// show. Not persisted across reloads like the knowledge base is -- switching
// who you're looking through is a live comparison you're making right now,
// not a standing preference. The server still validates this id belongs to
// the selected knowledge base's own org chart on every request (see
// app/graph/authorization.py's resolve_as_user) -- this is just which one the
// UI asks for, not a trust boundary.
let selectedUser = "";
// id -> {id, name, role, manager_id}, refreshed by loadUsers(). Lets
// describeGraph() say who "viewing as" actually refers to without a second
// fetch just to look up a name.
let userDirectory = {};

function getSelectedUser() {
  return selectedUser;
}

function setSelectedUser(id) {
  selectedUser = id;
}

// Orders the org chart top-down (exec, then managers, then reps) rather than
// alphabetically, so picking a name also gives a rough sense of seniority --
// without drawing the tree itself, which read as clutter in a dropdown.
function buildUserOptions(users) {
  const byId = Object.fromEntries(users.map((u) => [u.id, u]));

  function depth(u, seen = new Set()) {
    if (!u.manager_id || seen.has(u.id)) return 0; // seen guards a bad/cyclic manager_id
    const manager = byId[u.manager_id];
    if (!manager) return 0;
    return 1 + depth(manager, new Set(seen).add(u.id));
  }

  const withDepth = users.map((u) => ({ ...u, depth: depth(u) }));
  withDepth.sort((a, b) => a.depth - b.depth || a.name.localeCompare(b.name));

  const everyone = `<option value="">Everyone (no role filter)</option>`;
  const options = withDepth.map((u) => {
    const label = `${u.name} (${u.role})`;
    return `<option value="${escapeXml(u.id)}">${escapeXml(label)}</option>`;
  });
  return everyone + options.join("");
}

async function loadUsers() {
  const select = document.getElementById("userSelect");
  const kb = getSelectedKnowledgeBase();
  if (!getApiKey() || !kb) {
    select.hidden = true;
    select.innerHTML = "";
    return;
  }
  try {
    const res = await fetch(`${API}/graph/users${buildQuery({})}`, { headers: authHeaders() });
    if (!res.ok) {
      select.hidden = true;
      return;
    }
    const users = await res.json();
    userDirectory = Object.fromEntries(users.map((u) => [u.id, u]));
    if (users.length === 0) {
      select.hidden = true;
      select.innerHTML = "";
      return;
    }
    select.innerHTML = buildUserOptions(users);
    select.value = getSelectedUser();
    select.hidden = false;
  } catch (err) {
    select.hidden = true;
  }
}

document.getElementById("userSelect").addEventListener("change", (e) => {
  setSelectedUser(e.target.value);
  loadGraph();
  document.getElementById("queryAnswer").textContent = "";
  document.getElementById("queryFacts").innerHTML = "";
  document.getElementById("queryRawWrap").hidden = true;
  document.getElementById("seeMoreBtn").hidden = true;
});

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

function openHelpModal() {
  document.getElementById("helpOverlay").hidden = false;
}

function closeHelpModal() {
  document.getElementById("helpOverlay").hidden = true;
}

document.getElementById("helpBtn").addEventListener("click", openHelpModal);
document.getElementById("closeHelpBtn").addEventListener("click", closeHelpModal);
document.getElementById("helpOverlay").addEventListener("click", (e) => {
  if (e.target.id === "helpOverlay") closeHelpModal();
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!document.getElementById("keyOverlay").hidden) closeKeyModal();
  if (!document.getElementById("helpOverlay").hidden) closeHelpModal();
  if (!document.getElementById("connectorPreviewOverlay").hidden) closeConnectorPreview();
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
// Bumped on every loadGraph() call so a response that arrives after the user
// has already switched knowledge bases again can recognize it's stale and
// discard itself, instead of overwriting the panel with the wrong dataset's
// numbers. Switching the dropdown quickly (or a slow response racing a fast
// one) could otherwise land whichever fetch happens to resolve last.
let graphRequestId = 0;

async function loadGraph() {
  const requestId = ++graphRequestId;
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
      fetch(`${API}/graph/summary${buildQuery({})}`, { headers: authHeaders() }),
      fetch(`${API}/graph/nodes${buildQuery({ limit: 15 })}`, { headers: authHeaders() }),
      fetch(`${API}/graph/relationships${buildQuery({ limit: 25 })}`, { headers: authHeaders() }),
    ]);

    if (requestId !== graphRequestId) return; // a newer selection has already superseded this one

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
    if (requestId !== graphRequestId) return; // superseded again while parsing the responses

    summaryEl.innerHTML = `
      <div class="stat"><span class="num">${summary.node_count}</span><span class="label">Nodes</span></div>
      <div class="stat"><span class="num">${summary.relationship_count}</span><span class="label">Relationships</span></div>
    `;

    if (nodes.length === 0) {
      emptyEl.style.display = "block";
      svg.innerHTML = "";
      // Still explain *whose* empty view this is -- a rep with nothing
      // assigned to them yet should read as "this person can't see anything
      // here," not as a broken page.
      insightEl.textContent = getSelectedUser() ? describeGraph(summary, nodes) : "";
      await refreshSuggestedQuestionsForScope([]);
    } else {
      emptyEl.style.display = "none";
      renderGraph(svg, nodes, rels);
      insightEl.textContent = describeGraph(summary, nodes);
      await refreshSuggestedQuestionsForScope(nodes);
    }
  } catch (err) {
    summaryEl.textContent = "Could not load graph.";
  }
}

// Turns the raw counts into a sentence a stakeholder can act on, rather than
// making them infer what "4 nodes, 6 relationships" means on their own. When
// a user is selected, this is also the proof that role-based visibility is
// actually filtering, not just decorating the page with a dropdown -- it
// names whose view this is and how much of the knowledge base that leaves out.
function describeGraph(summary, nodes) {
  const { node_count, relationship_count } = summary;
  const density = node_count > 0 ? (relationship_count / node_count).toFixed(1) : 0;
  const base = `${node_count} things and ${relationship_count} connections between them` +
    (node_count > 0 ? `, or about ${density} connections per thing on average` : "");

  const userId = getSelectedUser();
  const user = userId ? userDirectory[userId] : null;
  if (user) {
    return `Viewing as ${user.name} (${user.role}): they can see ${base}. ` +
      `That's everything assigned to them plus everyone who reports to them in the org chart, ` +
      `not the whole knowledge base. Switch "Everyone" in the role dropdown to see it all.`;
  }
  return `So far it has found ${base}. This is a small starting set, and it'll grow as more ` +
    `information is added.`;
}

// Picks which set of nodes the suggested questions should be built from:
// when a document set is scoping the Ask panel (see getSelectedDocumentSet),
// suggestions should reflect everything in THAT bundle, not just whichever
// single connector happens to be selected in the header -- otherwise a
// question chip could reference something outside the dataset actually
// being searched. `headerNodes` (already fetched by loadGraph for the
// header's own knowledge base) is reused when no document set is active, to
// avoid a second fetch for the common case.
async function refreshSuggestedQuestionsForScope(headerNodes) {
  if (!getApiKey()) {
    document.getElementById("suggestedQuestions").innerHTML = "";
    return;
  }
  const docSetId = getSelectedDocumentSet();
  const ds = docSetId ? documentSetDirectory.find((d) => d.id === docSetId) : null;
  if (!ds) {
    renderSuggestedQuestions(headerNodes);
    return;
  }
  const slices = await Promise.all(ds.connectors.map((c) => fetchGraphSliceFor(c.id, { nodesLimit: 15, relsLimit: 1 })));
  const seen = new Set();
  const nodes = [];
  slices.forEach((slice) =>
    slice.nodes.forEach((n) => {
      if (n.name && !seen.has(n.name)) {
        seen.add(n.name);
        nodes.push(n);
      }
    })
  );
  renderSuggestedQuestions(nodes);
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
// layout -- a circle is legible without a library to load, as long as the
// circle itself grows with the node count instead of staying a fixed size.
function renderGraph(svg, nodes, rels) {
  const count = nodes.length;
  // A radius that worked for 4-6 nodes packs nodes (and their labels)
  // shoulder to shoulder once there are 15-20 -- the arc length between
  // neighbors shrinks as more nodes share the same circle. Growing the
  // radius (and the canvas around it) with node count keeps that spacing
  // roughly constant instead.
  const r = Math.max(140, count * 16);
  const pad = 90; // room for labels/edge curves sticking out past the circle
  const w = r * 2 + pad * 2;
  const h = w;
  const cx = w / 2, cy = h / 2;
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);

  // Denser graphs also need shorter labels and smaller text so neighboring
  // labels have a chance of not colliding even with more room between nodes.
  const labelMaxLen = count > 25 ? 9 : count > 15 ? 11 : count > 10 ? 14 : 18;
  const fontSize = count > 25 ? 7 : count > 15 ? 8 : 9;

  const positions = {};

  nodes.forEach((n, i) => {
    const angle = (2 * Math.PI * i) / count - Math.PI / 2;
    positions[n.name] = { x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) };
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
      svgContent += `<text class="gv-edge-label" style="font-size:${Math.max(6, fontSize - 2)}px" x="${labelX}" y="${labelY}" text-anchor="middle">${escapeXml(rel.type)}</text>`;
    });
  });

  nodes.forEach((n, i) => {
    const p = positions[n.name];
    // Alternate how far the label sits from its node so two labels on
    // adjacent, closely-spaced nodes don't land at the same height and
    // overlap -- only really visible once a graph has 15+ nodes on the circle.
    const labelOffset = 12 + (i % 2) * 14;
    svgContent += `<circle class="gv-node" cx="${p.x}" cy="${p.y}" r="6">
      <title>${escapeXml(n.summary || n.name)}</title>
    </circle>`;
    svgContent += `<text class="gv-label" style="font-size:${fontSize}px" x="${p.x}" y="${p.y - labelOffset}" text-anchor="middle">${escapeXml(truncate(n.name, labelMaxLen))}</text>`;
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
// Same staleness problem as loadGraph()'s graphRequestId: asking a second
// question (or switching knowledge bases) before the first answer comes back
// shouldn't let the first, slower response overwrite the newer one.
let askRequestId = 0;

// How many results the fallback semantic search asks for -- see
// app/api/context.py's result_limit. Bumped only by clicking "See more
// results" (see below), and reset back to the default on every new question,
// so a broad question doesn't silently stay expensive after the user moves on.
const DEFAULT_RESULT_LIMIT = 8;
const EXPANDED_RESULT_LIMIT = 20;

async function runAskQuery(resultLimit) {
  const requestId = ++askRequestId;
  const answerEl = document.getElementById("queryAnswer");
  const factsEl = document.getElementById("queryFacts");
  const rawWrap = document.getElementById("queryRawWrap");
  const rawEl = document.getElementById("queryRaw");
  const seeMoreBtn = document.getElementById("seeMoreBtn");
  const query = document.getElementById("queryInput").value.trim();
  if (!query) return;
  if (!getApiKey()) {
    answerEl.textContent = 'Click "Access key" in the top right first.';
    factsEl.innerHTML = "";
    rawWrap.hidden = true;
    seeMoreBtn.hidden = true;
    return;
  }

  answerEl.textContent = "Thinking…";
  factsEl.innerHTML = "";
  rawWrap.hidden = true;
  seeMoreBtn.hidden = true;
  try {
    // A document set scoped to several connectors at once takes priority over
    // the single-connector picker in the header when one's selected -- see
    // app/api/context.py, which doesn't support as_user alongside it yet.
    const docSet = getSelectedDocumentSet();
    const res = await fetch(`${API}/context/query`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(
        docSet
          ? { query, document_set: docSet, result_limit: resultLimit }
          : {
              query,
              knowledge_base: getSelectedKnowledgeBase() || undefined,
              as_user: getSelectedUser() || undefined,
              result_limit: resultLimit,
            }
      ),
    });
    if (requestId !== askRequestId) return; // a newer question has already superseded this one

    if (res.status === 401) {
      answerEl.textContent = "That key doesn't match anything on file. Double-check it, or ask whoever set this up for you for the right one.";
      return;
    }
    if (!res.ok) {
      answerEl.textContent = "Something went wrong answering that. Try again in a moment.";
      return;
    }
    const data = await res.json();
    if (requestId !== askRequestId) return;

    const summary = data.metadata?.summary;
    const facts = data.metadata?.facts || [];

    answerEl.textContent = summary && summary !== "No matching graph context found."
      ? summary
      : "Nothing on file matches that yet. Try asking a broader question, or add more information first.";

    // A single fact IS the answer verbatim in this case (no synthesis step ran
    // for just one fact -- see orchestrator.py), but it's still shown below
    // too: that's the only place the current/superseded badge appears, and
    // always showing where an answer came from is the actual point of this
    // tool -- collapsing it away for the single-fact case would quietly break
    // that promise exactly when the answer is most directly traceable to one
    // specific fact.
    renderFacts(factsEl, facts);

    // result_limit_hit means the fallback search returned exactly as many
    // results as it was capped at -- a sign there may be lower-relevance
    // ones beyond it worth surfacing on request, rather than always paying
    // for a bigger default on every question. Once already showing the
    // expanded count, there's no further "more" to offer.
    seeMoreBtn.hidden = !(data.metadata?.result_limit_hit && resultLimit < EXPANDED_RESULT_LIMIT);

    rawEl.textContent = JSON.stringify(data, null, 2);
    rawWrap.hidden = false;
  } catch (err) {
    answerEl.textContent = `Error: ${err.message}`;
  }
}

document.getElementById("askBtn").addEventListener("click", () => runAskQuery(DEFAULT_RESULT_LIMIT));
document.getElementById("seeMoreBtn").addEventListener("click", () => runAskQuery(EXPANDED_RESULT_LIMIT));

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
  const items = sorted
    .map((f) => {
      const current = f.is_valid !== false;
      const badge = current
        ? `<span class="fact-badge fact-badge-current">current</span>`
        : `<span class="fact-badge fact-badge-superseded">superseded*</span>`;
      return `<li class="${current ? "" : "fact-superseded"}">${escapeXml(f.fact || "")}${badge}</li>`;
    })
    .join("");
  container.innerHTML =
    `<p class="fact-list-label">Where this answer comes from:</p><ul class="fact-bullets">${items}</ul>` + note;
}

document.getElementById("queryInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("askBtn").click();
});

// --- Init -------------------------------------------------------------------
async function loadTenantData() {
  loadOntology();
  await loadKnowledgeBases();
  await loadUsers();
  await loadConnectors();
  await loadDocumentSets();
  loadGraph();
}

loadHealth();
loadTenantData();
