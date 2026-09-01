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
  renderScopeSelect(); // "This connector only" option's label follows the new selection, and doc sets that don't touch this KB drop out
  // Source connectors and document sets are fetched once per page load
  // (they don't change when switching knowledge bases), but which of them
  // are actually *shown* does -- re-render both against the new selection
  // rather than refetching, since nothing about the underlying data changed.
  renderConnectorsTable();
  renderConnectorKbSelect(); // "New source connector" form defaults to the newly-selected KB
  renderDocSetsTable();
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

// A document set can legitimately span several knowledge bases at once --
// that's its whole point (see app/graph/document_sets.py) -- so "belongs to
// the current knowledge base" means "touches it at all", not "is entirely
// contained in it": a set spanning Solandra and Northwind should still show
// up on either tab, but a Northwind-only set has no business appearing
// while viewing Solandra. Same reasoning as connectorsForCurrentScope above.
function docSetsForCurrentScope() {
  const kb = getSelectedKnowledgeBase();
  return kb ? documentSetDirectory.filter((ds) => ds.connectors.some((c) => c.id === kb)) : documentSetDirectory;
}

function renderDocSetsTable() {
  const body = document.getElementById("docSetsBody");
  const empty = document.getElementById("docSetsEmpty");
  const visible = docSetsForCurrentScope();
  if (visible.length === 0) {
    body.innerHTML = "";
    empty.textContent =
      documentSetDirectory.length === 0
        ? "No document sets yet -- create one below."
        : "No document sets touch this knowledge base yet -- create one below.";
    empty.style.display = "block";
    return;
  }
  empty.style.display = "none";
  body.innerHTML = visible
    .map((ds) => {
      const connectorLabels = ds.connectors.map((c) => escapeXml(c.label)).join(", ");
      const publicBadge = ds.is_public
        ? `<span class="badge badge-ok">Public</span>`
        : `<span class="badge badge-neutral">Private</span>`;
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
  const visible = docSetsForCurrentScope();
  if (visible.length === 0) {
    select.hidden = true;
    select.innerHTML = "";
    selectedDocumentSet = "";
    return;
  }
  const currentKb = knowledgeBaseDirectory.find((kb) => kb.id === getSelectedKnowledgeBase());
  const singleOption = `<option value="">This connector only${currentKb ? ` (${escapeXml(currentKb.label)})` : ""}</option>`;
  const setOptions = visible
    .map((ds) => `<option value="${escapeXml(ds.id)}">${escapeXml(ds.name)} (${ds.connectors.length} connectors)</option>`)
    .join("");
  select.innerHTML = singleOption + setOptions;
  select.value = visible.some((ds) => ds.id === selectedDocumentSet) ? selectedDocumentSet : "";
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
  ["connectorKbSelect", "oauthConnectKbSelect"].forEach((id) => {
    const select = document.getElementById(id);
    if (!select) return;
    select.innerHTML = knowledgeBaseDirectory
      .map((kb) => `<option value="${escapeXml(kb.id)}">${escapeXml(kb.label)}</option>`)
      .join("");
    // Default a new connector to whichever knowledge base is currently
    // selected, rather than always defaulting to the first option -- still
    // changeable, just a sensible starting point for "add one below" right
    // after seeing this knowledge base has none.
    const current = getSelectedKnowledgeBase();
    if (current) select.value = current;
  });
}

function formatSyncStatus(c) {
  // "stale" (see app/api/connectors.py's _connector_health) takes priority
  // over the raw status -- a connector can be "synced" from its own last
  // attempt's point of view and still be stale if nothing's synced it
  // again in a long time, which is exactly the case worth flagging: the
  // background scheduler may have stopped running for it.
  if (c.status === "queued") {
    return `<span class="badge badge-neutral" title="Accepted and waiting for a worker to pick it up -- see app/graph/ingestion_queue.py.">Queued…</span>`;
  }
  if (c.health === "authorized_needs_files") {
    return `<span class="badge badge-warn" title="Signed in, but no files were picked yet -- click Finish connecting.">Needs files</span>`;
  }
  if (c.health === "stale") {
    return `<span class="badge badge-warn" title="Hasn't synced successfully in a while -- check that background syncing is still running for this connector.">Stale</span>`;
  }
  const labels = {
    never_synced: "Never synced",
    synced: "Synced",
    unchanged: "Up to date",
    error: "Error",
  };
  const label = labels[c.status] || c.status;
  const cls = c.status === "error" ? "badge-bad" : c.status === "never_synced" ? "badge-neutral" : "badge-ok";
  // An error badge is a real button, not just a span with a hover title --
  // the hover title stays too (still useful on desktop without a click),
  // but a click is the discoverable, works-on-touch way to actually read
  // why a sync failed, rather than a message that only ever showed up if
  // you happened to hover exactly the right badge.
  if (c.status === "error" && c.last_error) {
    return `<button type="button" class="badge badge-bad badge-btn" title="${escapeXml(c.last_error)}" data-sync-error-id="${escapeXml(c.id)}">${escapeXml(label)}</button>`;
  }
  return `<span class="badge ${cls}">${escapeXml(label)}</span>`;
}

const CONNECTOR_TYPE_LABELS = {
  web: "Web page",
  google_drive: "Google Drive",
  google_drive_oauth: "Google Drive (connected)",
  sharepoint: "SharePoint",
  gmail: "Gmail",
  outlook_mail: "Outlook mail",
  database: "Database / CRM (mock)",
  documents: "Documents",
  email: "Email inbox (mock)",
};

// connectorDirectory itself stays the full, tenant-wide list (needed for
// by-id lookups from sync/delete/preview, and it's fetched once regardless
// of which knowledge base is selected) -- but a client with several
// knowledge bases under one tenant/API key (e.g. a shared demo key spanning
// Northwind and a client's own data) shouldn't see every OTHER knowledge
// base's connectors just because it's viewing this one. Every place that
// actually *displays* connectors filters through this first, and re-renders
// whenever the header's knowledge base selection changes (see kbSelect's
// change handler below).
function connectorsForCurrentScope() {
  const kb = getSelectedKnowledgeBase();
  return kb ? connectorDirectory.filter((c) => c.group_id === kb) : connectorDirectory;
}

function renderConnectorsHealthSummary() {
  const summaryEl = document.getElementById("connectorsHealthSummary");
  const visible = connectorsForCurrentScope();
  if (visible.length === 0) {
    summaryEl.textContent = "";
    return;
  }
  const needsAttention = visible.filter((c) => c.health === "error" || c.health === "stale").length;
  summaryEl.textContent =
    needsAttention === 0
      ? `All ${visible.length} connector${visible.length === 1 ? "" : "s"} syncing normally.`
      : `${needsAttention} of ${visible.length} connector${visible.length === 1 ? "" : "s"} need${needsAttention === 1 ? "s" : ""} attention.`;
}

function renderConnectorsTable() {
  const body = document.getElementById("connectorsBody");
  const empty = document.getElementById("connectorsEmpty");
  renderConnectorsHealthSummary();
  const visible = connectorsForCurrentScope();
  if (visible.length === 0) {
    body.innerHTML = "";
    empty.textContent =
      connectorDirectory.length === 0
        ? "No source connectors yet -- add one below."
        : "No source connectors for this knowledge base yet -- add one below.";
    empty.style.display = "block";
    return;
  }
  empty.style.display = "none";
  const kbLabel = (id) => knowledgeBaseDirectory.find((kb) => kb.id === id)?.label || id;
  body.innerHTML = visible
    .map((c) => {
      const lastSynced = c.last_synced_at ? new Date(c.last_synced_at).toLocaleString() : "—";
      const typeLabel = CONNECTOR_TYPE_LABELS[c.type] || c.type;
      return `<tr data-id="${escapeXml(c.id)}">
        <td><button type="button" class="connector-name-link" data-preview-id="${escapeXml(c.id)}">${escapeXml(c.name)}</button></td>
        <td>
          <span class="badge badge-neutral">${escapeXml(typeLabel)}</span>
          ${c.push_enabled ? `<span class="badge badge-ok" title="Syncs instantly on new mail, not just on the usual interval">Real-time</span>` : ""}
          ${c.source_authority > 0 ? `<span class="badge badge-neutral" title="Wins a tie against a lower-authority source's disagreeing fact">Authority ${c.source_authority}</span>` : ""}<br />
          <span class="muted" style="font-size:0.8rem">${escapeXml(c.url)}</span>
        </td>
        <td>${escapeXml(kbLabel(c.group_id))}</td>
        <td>${formatSyncStatus(c)}</td>
        <td><span class="muted" style="font-size:0.8rem">${escapeXml(lastSynced)}</span></td>
        <td>
          ${
            c.health === "authorized_needs_files"
              ? `<button class="sync-btn" type="button" data-finish-drive-connect-id="${escapeXml(c.id)}">Finish connecting</button>`
              : `<button class="sync-btn" type="button" data-sync-id="${escapeXml(c.id)}">Sync now</button>`
          }
          <button class="delete-btn" type="button" data-delete-connector-id="${escapeXml(c.id)}">Delete</button>
          ${
            c.type === "database"
              ? `<br /><label class="upload-csv-label">
                   Upload CSV
                   <input type="file" accept=".csv" hidden data-upload-csv-id="${escapeXml(c.id)}" />
                 </label>`
              : ""
          }
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
  body.querySelectorAll("[data-finish-drive-connect-id]").forEach((btn) => {
    btn.addEventListener("click", () => resumeGoogleDrivePicker(btn.dataset.finishDriveConnectId));
  });
  body.querySelectorAll("[data-sync-error-id]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const c = connectorDirectory.find((x) => x.id === btn.dataset.syncErrorId);
      window.alert(c?.last_error || "This connector's last sync failed, but no explanation was recorded.");
    });
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
  // A dropped-in file is uploaded right away (see uploadConnectorCsv below);
  // it isn't ingested until "Sync now" runs, same as any other connector
  // type -- uploading just makes the file available for the next sync.
  body.querySelectorAll("[data-upload-csv-id]").forEach((input) => {
    input.addEventListener("change", () => {
      const file = input.files?.[0];
      if (file) uploadConnectorCsv(input.dataset.uploadCsvId, file);
      input.value = ""; // lets the same filename be picked again later
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
    loadOAuthProviders(); // independent of the connector list itself; doesn't block rendering it
    pollWhileConnectorsAreBusy();
  } catch (err) {
    card.hidden = true;
  }
}

// A sync accepted onto the background ingestion queue (see
// app/graph/ingestion_queue.py) really does finish on its own -- usually
// within a minute, since it's running real extraction/embedding calls --
// but nothing was re-checking this list after the initial "queued"
// response, so the row just sat on "Queued…" until something else
// happened to reload it (switching knowledge bases, clicking another
// button). Not stuck, just never re-checked. This polls every few seconds
// for as long as anything here is still "queued", and stops itself once
// nothing is -- so idle viewing costs nothing.
let connectorsPollTimer = null;

function pollWhileConnectorsAreBusy() {
  const stillBusy = connectorDirectory.some((c) => c.status === "queued");
  if (stillBusy && !connectorsPollTimer) {
    connectorsPollTimer = setInterval(loadConnectors, 4000);
  } else if (!stillBusy && connectorsPollTimer) {
    clearInterval(connectorsPollTimer);
    connectorsPollTimer = null;
  }
}

// --- Google Drive one-click connect -----------------------------------------
// See app/api/connectors.py's oauth/exchange + oauth/files routes and
// app/ingestion/google_drive_source.py's GoogleDriveOAuthConnector. Three
// steps, feeling like one click: (1) Google's own consent popup (Identity
// Services), (2) trade the resulting code for tokens server-side, get back a
// short-lived access token, (3) open the Google Picker with that token so the
// user picks exactly which files to share -- no folder-sharing step, no admin.
let googleOAuthClientId = null;
let googlePickerLoading = null;

async function loadOAuthProviders() {
  try {
    const res = await fetch(`${API}/connectors/oauth/providers`, { headers: authHeaders() });
    if (!res.ok) return;
    const body = await res.json();
    const drive = body.google_drive || {};
    googleOAuthClientId = drive.available ? drive.client_id : null;
    document.getElementById("oauthConnectRow").hidden = !drive.available;
    document.getElementById("oauthConnectHint").hidden = !drive.available;
  } catch (err) {
    // Leave the connect row hidden -- same as "not configured".
  }
}

function ensureGooglePickerLoaded() {
  if (googlePickerLoading) return googlePickerLoading;
  googlePickerLoading = new Promise((resolve, reject) => {
    if (typeof gapi === "undefined") {
      reject(new Error("Google's Picker script hasn't loaded yet -- check your connection and try again."));
      return;
    }
    gapi.load("picker", { callback: resolve, onerror: () => reject(new Error("Could not load Google Picker.")) });
  });
  return googlePickerLoading;
}

function setOAuthConnectStatus(text, cls) {
  const el = document.getElementById("oauthConnectStatus");
  el.textContent = text;
  el.className = `status-line${cls ? " " + cls : ""}`;
}

// Opens the Picker restricted to individual file selection (no folders --
// see google_drive_source.py's module docstring for why: the drive.file
// scope this app requests doesn't grant access to a folder's contents, only
// to files the user explicitly opens/picks one at a time).
function openGoogleDrivePicker(accessToken) {
  return new Promise((resolve) => {
    const view = new google.picker.DocsView()
      .setIncludeFolders(false)
      .setSelectFolderEnabled(false)
      .setMode(google.picker.DocsViewMode.LIST);
    const picker = new google.picker.PickerBuilder()
      .addView(view)
      .setOAuthToken(accessToken)
      .enableFeature(google.picker.Feature.MULTISELECT_ENABLED)
      .setCallback((data) => {
        if (data.action === google.picker.Action.PICKED) {
          resolve({
            picked: true,
            fileIds: data.docs.map((d) => d.id),
            fileNames: data.docs.map((d) => d.name),
          });
        } else if (data.action === google.picker.Action.CANCEL) {
          resolve({ picked: false });
        }
      })
      .build();
    picker.setVisible(true);
  });
}

async function finishGoogleDriveFileSelection(connectorId, accessToken) {
  let result;
  try {
    await ensureGooglePickerLoaded();
    result = await openGoogleDrivePicker(accessToken);
  } catch (err) {
    setOAuthConnectStatus(`Error: ${err.message}`, "bad");
    return;
  }
  if (!result.picked) {
    // No files chosen -- don't leave a half-connected, unused Google grant
    // sitting around (holding a live OAuth grant nobody's using is exactly
    // the kind of thing worth cleaning up automatically, not leaving for
    // someone to notice later). This also revokes it at Google's end (see
    // the DELETE route).
    setOAuthConnectStatus("Cancelled -- disconnecting…", "");
    await deleteConnector(connectorId);
    setOAuthConnectStatus("", "");
    return;
  }
  setOAuthConnectStatus("Finishing…", "");
  try {
    const res = await fetch(`${API}/connectors/${encodeURIComponent(connectorId)}/oauth/files`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ file_ids: result.fileIds, file_names: result.fileNames }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      setOAuthConnectStatus(body.detail || "Could not finish connecting.", "bad");
      return;
    }
    setOAuthConnectStatus(`Connected -- pulling in ${result.fileIds.length} file(s)…`, "ok");
  } catch (err) {
    setOAuthConnectStatus(`Error: ${err.message}`, "bad");
  }
  await loadConnectors();
}

// Resumes a connector left in "authorized_needs_files" (the consent step
// finished, but the file picker never did -- e.g. the tab was closed) --
// mints a fresh access token from the already-stored refresh token instead
// of asking the user to sign into Google again.
async function resumeGoogleDrivePicker(connectorId) {
  setOAuthConnectStatus("Reopening file picker…", "");
  try {
    const res = await fetch(`${API}/connectors/${encodeURIComponent(connectorId)}/oauth/access-token`, {
      headers: authHeaders(),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      setOAuthConnectStatus(body.detail || "Could not resume this connection.", "bad");
      return;
    }
    const { access_token } = await res.json();
    await finishGoogleDriveFileSelection(connectorId, access_token);
  } catch (err) {
    setOAuthConnectStatus(`Error: ${err.message}`, "bad");
  }
}

document.getElementById("connectGoogleDriveBtn").addEventListener("click", () => {
  if (!googleOAuthClientId || typeof google === "undefined" || !google.accounts?.oauth2) {
    setOAuthConnectStatus("Google sign-in hasn't finished loading yet -- try again in a moment.", "bad");
    return;
  }
  const groupId = document.getElementById("oauthConnectKbSelect").value;
  const kbLabel = knowledgeBaseDirectory.find((kb) => kb.id === groupId)?.label || groupId;
  const btn = document.getElementById("connectGoogleDriveBtn");
  btn.disabled = true;
  setOAuthConnectStatus("Opening Google sign-in…", "");

  const client = google.accounts.oauth2.initCodeClient({
    client_id: googleOAuthClientId,
    scope: "https://www.googleapis.com/auth/drive.file",
    ux_mode: "popup",
    callback: async (response) => {
      btn.disabled = false;
      if (!response.code) {
        setOAuthConnectStatus("Sign-in was cancelled.", "");
        return;
      }
      setOAuthConnectStatus("Connecting…", "");
      try {
        const res = await fetch(`${API}/connectors/google/oauth/exchange`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...authHeaders() },
          body: JSON.stringify({ name: `Google Drive (${kbLabel})`, group_id: groupId, code: response.code }),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          setOAuthConnectStatus(body.detail || "Could not connect Google Drive.", "bad");
          return;
        }
        const created = await res.json();
        await loadConnectors();
        setOAuthConnectStatus("Signed in -- pick the files to bring in…", "");
        await finishGoogleDriveFileSelection(created.id, created.access_token);
      } catch (err) {
        setOAuthConnectStatus(`Error: ${err.message}`, "bad");
      }
    },
  });
  client.requestCode();
});

// Types that need a tenant-supplied address -- kept in sync with
// app/api/connectors.py's own _TYPES_REQUIRING_URL. The demo data types
// (database/documents/email) read a fixed bundled sample server-side, so
// their URL field stays hidden rather than asking for an input that's ignored.
const CONNECTOR_TYPES_REQUIRING_URL = new Set(["web", "google_drive", "sharepoint", "gmail", "outlook_mail"]);
const CONNECTOR_URL_PLACEHOLDERS = {
  web: "https://example.com/page-to-pull-in",
  google_drive: "Drive folder link or id (share the folder with the service account first)",
  sharepoint: "https://yourtenant.sharepoint.com/sites/YourSite",
  gmail: "mailbox@yourdomain.com (needs Workspace domain-wide delegation)",
  outlook_mail: "mailbox@yourdomain.com (needs the Mail.Read Graph permission)",
};

function updateConnectorUrlVisibility() {
  const type = document.getElementById("connectorType").value;
  document.getElementById("connectorUrlRow").hidden = !CONNECTOR_TYPES_REQUIRING_URL.has(type);
  document.getElementById("connectorUrl").placeholder = CONNECTOR_URL_PLACEHOLDERS[type] || "";
  // The CSV picker only makes sense for a "database"-type connector -- see
  // app/api/connectors.py's upload_connector_file, which rejects any other
  // type. Previously this option was only discoverable *after* creating a
  // database connector (the upload control lives on its table row) -- easy
  // to miss if you're looking for "upload a CSV" up front in the form.
  const showCsv = type === "database";
  document.getElementById("connectorCsvRow").hidden = !showCsv;
  document.getElementById("connectorCsvHint").hidden = !showCsv;
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
  const sourceAuthority = Number(document.getElementById("connectorAuthority").value) || 0;

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
      body: JSON.stringify({
        name,
        type,
        group_id: groupId,
        url: needsUrl ? url : undefined,
        source_authority: sourceAuthority,
      }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      statusEl.textContent = body.detail || "Could not add that connector.";
      statusEl.className = "status-line bad";
      return;
    }
    const created = await res.json();
    document.getElementById("connectorName").value = "";
    document.getElementById("connectorUrl").value = "";

    const csvInput = document.getElementById("connectorCsvFile");
    const csvFiles = type === "database" ? Array.from(csvInput.files || []) : [];
    if (csvFiles.length > 0) {
      statusEl.textContent = `Added. Uploading ${csvFiles.length} file(s)…`;
      statusEl.className = "status-line";
      await loadConnectors(); // the row has to exist before uploadConnectorCsv can find it
      for (const file of csvFiles) {
        await uploadConnectorCsv(created.id, file);
      }
      csvInput.value = "";
      statusEl.textContent = 'Added and uploaded. Click "Sync now" on it below to pull its content in.';
      statusEl.className = "status-line ok";
    } else {
      statusEl.textContent = "Added. Click \"Sync now\" on it below to pull its content in.";
      statusEl.className = "status-line ok";
    }
    await loadConnectors();
  } catch (err) {
    statusEl.textContent = `Error: ${err.message}`;
    statusEl.className = "status-line bad";
  }
});

// Sync now just accepts the job onto the background ingestion queue and
// returns immediately (see app/graph/ingestion_queue.py) -- it no longer
// waits for fetch+extraction to finish, so there's no longer a synchronous
// result to read from the response itself. This best-effort poll is purely
// a UI convenience so a person watching the table sees it land instead of
// having to manually refresh; it's not how the app tracks the real
// outcome -- the connector's own status (visible to any client, any time,
// via GET /connectors) is that source of truth regardless of whether
// anyone's still watching this poll.
const SYNC_POLL_INTERVAL_MS = 2000;
const SYNC_POLL_MAX_ATTEMPTS = 15; // ~30s -- covers a typical demo-sized sync

async function syncConnector(id) {
  const row = document.querySelector(`#connectorsBody tr[data-id="${CSS.escape(id)}"]`);
  const btn = row?.querySelector("[data-sync-id]");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Queuing…";
  }
  try {
    await fetch(`${API}/connectors/${encodeURIComponent(id)}/sync`, {
      method: "POST",
      headers: authHeaders(),
    });
  } catch (err) {
    // Network failure before a response came back at all -- the reload
    // below still runs so the row doesn't stay stuck on "Queuing...".
  }
  await loadConnectors();
  pollUntilSyncSettles(id);
}

async function pollUntilSyncSettles(id, attempt = 0) {
  const current = connectorDirectory.find((c) => c.id === id);
  if (!current || current.status !== "queued" || attempt >= SYNC_POLL_MAX_ATTEMPTS) {
    // A settled sync may have just run reconciliation server-side (see
    // app/ingestion/connector_sync.py) -- refresh the review queue so a new
    // proposal shows up without a manual page reload.
    await loadReconciliation();
    return;
  }
  await new Promise((resolve) => setTimeout(resolve, SYNC_POLL_INTERVAL_MS));
  await loadConnectors();
  pollUntilSyncSettles(id, attempt + 1);
}

// --- Reconciliation review queue ---------------------------------------------
// The Reconcile stage's uncertain (fuzzy-name) cross-connector matches -- see
// app/graph/reconciliation.py. Confident matches (exact/normalized name)
// merge automatically and never show up here; this panel is only for the
// ones a human has to decide.
function connectorLabelFor(groupId) {
  return knowledgeBaseDirectory.find((kb) => kb.id === groupId)?.label || groupId;
}

function renderReconciliationTable(proposals) {
  const body = document.getElementById("reconciliationBody");
  const empty = document.getElementById("reconciliationEmpty");
  if (proposals.length === 0) {
    body.innerHTML = "";
    empty.style.display = "block";
    return;
  }
  empty.style.display = "none";
  body.innerHTML = proposals
    .map(
      (p) => `<tr data-id="${escapeXml(p.id)}">
        <td>${escapeXml(p.entity_a_name)} <span class="muted" style="font-size:0.8rem">(${escapeXml(connectorLabelFor(p.entity_a_group_id))})</span></td>
        <td>${escapeXml(p.entity_b_name)} <span class="muted" style="font-size:0.8rem">(${escapeXml(connectorLabelFor(p.entity_b_group_id))})</span></td>
        <td>${Math.round(p.similarity * 100)}%</td>
        <td>
          <button class="delete-btn" type="button" data-approve-id="${escapeXml(p.id)}">Approve</button>
          <button class="delete-btn" type="button" data-reject-id="${escapeXml(p.id)}">Reject</button>
        </td>
      </tr>`
    )
    .join("");
  body.querySelectorAll("[data-approve-id]").forEach((btn) => {
    btn.addEventListener("click", () => decideReconciliationProposal(btn.dataset.approveId, "approve"));
  });
  body.querySelectorAll("[data-reject-id]").forEach((btn) => {
    btn.addEventListener("click", () => decideReconciliationProposal(btn.dataset.rejectId, "reject"));
  });
}

async function loadReconciliation() {
  const card = document.getElementById("reconciliationCard");
  if (!getApiKey()) {
    card.hidden = true;
    return;
  }
  try {
    const res = await fetch(`${API}/reconciliation?status_filter=pending`, { headers: authHeaders() });
    if (!res.ok) {
      card.hidden = true;
      return;
    }
    const proposals = await res.json();
    // Only worth showing at all once there's something to decide -- an
    // empty review queue isn't news the way an empty connectors/document
    // sets table still is (those are things you'd set up).
    card.hidden = proposals.length === 0;
    renderReconciliationTable(proposals);
  } catch (err) {
    card.hidden = true;
  }
}

async function decideReconciliationProposal(id, action) {
  const row = document.querySelector(`#reconciliationBody tr[data-id="${CSS.escape(id)}"]`);
  row?.querySelectorAll("button").forEach((btn) => (btn.disabled = true));
  try {
    await fetch(`${API}/reconciliation/${encodeURIComponent(id)}/${action}`, {
      method: "POST",
      headers: authHeaders(),
    });
  } catch (err) {
    // Falls through to the reload below either way -- loadReconciliation
    // re-fetching the real pending list is the source of truth, not this
    // request's own success/failure.
  }
  await loadReconciliation();
}

// "Easily droppable CSV": a Database/CRM connector accepts a CSV upload
// directly, landing it in that connector's own folder server-side (see
// app/api/connectors.py's POST /connectors/{id}/files) -- one file per
// record type is the expected shape, matching how the bundled sample
// datasets are laid out. The upload alone doesn't ingest anything; "Sync
// now" (unchanged) picks up whatever's been uploaded, the same as any other
// connector type.
async function uploadConnectorCsv(id, file) {
  const row = document.querySelector(`#connectorsBody tr[data-id="${CSS.escape(id)}"]`);
  const form = new FormData();
  form.append("file", file);
  try {
    const res = await fetch(`${API}/connectors/${encodeURIComponent(id)}/files`, {
      method: "POST",
      headers: authHeaders(), // deliberately no Content-Type -- the browser sets the multipart boundary itself
      body: form,
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      window.alert(body.detail || `Could not upload "${file.name}".`);
      return;
    }
  } catch (err) {
    window.alert(`Error uploading "${file.name}": ${err.message}`);
    return;
  }
  // Not persisted -- just a quick confirmation the drop landed, so someone
  // dropping in several files in a row can see each one register before
  // clicking "Sync now" once at the end.
  if (row) {
    const link = row.querySelector(".connector-name-link");
    if (link) {
      const original = link.textContent;
      link.textContent = `${original} (${file.name} uploaded)`;
      setTimeout(() => {
        link.textContent = original;
      }, 3000);
    }
  }
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
  const statsEl = document.getElementById("queryStats");
  // A prior "Explain why + recommend" answer has to be cleared here too --
  // it's a separate result block (see runCausalQuery below) that only ever
  // gets touched by the causal button, so without this a stale
  // recommendation from a previous question stayed on screen underneath a
  // brand new plain-Ask answer, looking like it was still the answer to the
  // new question.
  const causalEl = document.getElementById("causalRecommendation");
  const query = document.getElementById("queryInput").value.trim();
  if (!query) return;
  if (!getApiKey()) {
    answerEl.textContent = 'Click "Access key" in the top right first.';
    factsEl.innerHTML = "";
    rawWrap.hidden = true;
    seeMoreBtn.hidden = true;
    statsEl.hidden = true;
    causalEl.hidden = true;
    causalEl.innerHTML = "";
    return;
  }

  answerEl.textContent = "Thinking…";
  factsEl.innerHTML = "";
  rawWrap.hidden = true;
  seeMoreBtn.hidden = true;
  statsEl.hidden = true;
  causalEl.hidden = true;
  causalEl.innerHTML = "";
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
    renderQueryStats(statsEl, data.metadata);

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

// "Explain why + recommend" -- the causal-reasoning mode (POST
// /api/v1/context/query/causal, see app/context/orchestrator.py's
// get_causal_context_packet), deliberately a separate button/call from
// "Ask" above rather than a mode toggle on it: that endpoint is allowed to
// infer cause/impact/recommendation from a chain of facts, which the plain
// Ask path never does, and keeping them as visibly separate UI actions
// mirrors that separation all the way through the stack.
async function runCausalQuery() {
  const recEl = document.getElementById("causalRecommendation");
  const query = document.getElementById("queryInput").value.trim();
  if (!query) return;
  if (!getApiKey()) {
    recEl.hidden = false;
    recEl.textContent = 'Click "Access key" in the top right first.';
    return;
  }
  recEl.hidden = false;
  recEl.innerHTML = `<p class="muted">Tracing the causal chain…</p>`;
  try {
    const res = await fetch(`${API}/context/query/causal`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({
        query,
        knowledge_base: getSelectedKnowledgeBase() || undefined,
        as_user: getSelectedUser() || undefined,
      }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      recEl.textContent = body.detail || "Could not trace a causal chain for that.";
      return;
    }
    const data = await res.json();
    const rec = data.metadata?.recommendation;
    if (!rec) {
      // No real causal chain -- either nothing at all to go on
      // (retrieval_path "none"/"causal_chain_empty"), or a fact-only
      // fallback with real evidence behind it: either a single entity's own
      // directly-known facts ("causal_fallback_direct_facts") or the actual
      // connecting path between two named entities that wasn't entirely
      // causal-typed ("causal_path_between_entities") -- see
      // get_causal_context_packet. Both fallback shapes used to render
      // identically to a real causal answer -- same muted paragraph, no
      // distinguishing label, no evidence list at all -- which made it
      // look like the causal engine had actually explained something (and,
      // when the plain "Ask" answer happened to draw on the same facts,
      // made the two panels look like an outright bug/duplicate). Checking
      // for actual facts rather than one specific retrieval_path string
      // covers both shapes today and any similar one added later.
      const summary = data.metadata?.summary || "No causal chain found for that.";
      const facts = data.metadata?.facts || [];
      if (facts.length > 0) {
        const disclaimer =
          data.metadata?.retrieval_path === "causal_path_between_entities"
            ? "No single causal chain explains this -- here's the actual connection between them instead (not an inference, not a recommendation):"
            : "No causal chain connects this to anything else -- here's the most directly relevant fact(s) instead (not an inference, not a recommendation):";
        const factsHost = document.createElement("div");
        renderFacts(factsHost, facts);
        // Deliberately never shows metadata.summary here (unlike the API
        // response, which keeps it -- see the MCP tool's documented
        // "summary" field). With a handful of facts, a synthesized sentence
        // stitched from them reads as a near-restatement of the same list
        // right below it -- real information density is low on a dataset
        // this size, so the paragraph consistently added noise rather than
        // insight. The evidence list (each line now carrying its own real
        // source document, not just bare text) already says everything a
        // person asking "why" actually needs from a fact-only answer.
        recEl.innerHTML = `<p class="fact-list-label">${disclaimer}</p>`;
        recEl.appendChild(factsHost);
      } else {
        recEl.innerHTML = `<p class="muted">${escapeXml(summary)}</p>`;
      }
      return;
    }
    // Deliberately styled/labeled distinctly from the plain-facts answer
    // above -- this is a generated suggestion, not a restated fact, and it
    // should never read as one. See app/context/orchestrator.py's docstring
    // on why "recommendation" and "summary" are never blended.
    const decisionNote = data.metadata?.decision_id
      ? `<p class="muted" style="font-size:0.8rem">Logged as an auditable recommendation (id: ${escapeXml(data.metadata.decision_id)}). Saxon has not acted on this -- it's a suggestion only.</p>`
      : "";
    recEl.innerHTML = `
      <p class="fact-list-label">Generated recommendation -- not a stated fact, an inference from the chain below:</p>
      <p><strong>What happened:</strong> ${escapeXml(rec.what_happened)}</p>
      <p><strong>Why:</strong> ${escapeXml(rec.why)}</p>
      <p><strong>Impact:</strong> ${escapeXml(rec.impact)}</p>
      <p><strong>Recommendation:</strong> ${escapeXml(rec.recommendation)}</p>
      ${decisionNote}
      <details class="raw-details"><summary>Chain of facts this was based on</summary>
        <pre class="result-block">${escapeXml(data.metadata?.summary || "")}</pre>
      </details>`;
  } catch (err) {
    recEl.textContent = `Error: ${err.message}`;
  }
}
document.getElementById("causalBtn").addEventListener("click", runCausalQuery);

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
      // Real provenance, not a guess -- group_id is the same field every
      // query in this app is already scoped by (see graph_repository.py's
      // _entity_own_facts/search_graphiti_facts). Only shown when it maps
      // to a knowledge base this tenant can see, and only when a document
      // set (multiple possible sources) is actually the scope in play --
      // pointless noise on a single-connector query where it's always the
      // same source.
      const kbLabel = f.group_id ? knowledgeBaseDirectory.find((kb) => kb.id === f.group_id)?.label : null;
      const kbTag =
        kbLabel && getSelectedDocumentSet() ? `<span class="fact-source">${escapeXml(kbLabel)}</span>` : "";
      // The actual document/row this fact was extracted from (e.g.
      // "orders.csv (Order)") -- resolved server-side from Graphiti's own
      // edge.episodes property (see graph_repository.py's
      // _resolve_episode_sources), never a guess. This is the piece that
      // makes "where this answer comes from" mean something beyond
      // restating the fact text: it names the actual source document(s),
      // not just which knowledge base it's in.
      const docSources = Array.isArray(f.sources) ? f.sources.filter(Boolean) : [];
      const docTag = docSources.length
        ? `<span class="fact-source" title="Extracted from this source document/record">${escapeXml(docSources.join(", "))}</span>`
        : "";
      return `<li class="${current ? "" : "fact-superseded"}">${escapeXml(f.fact || "")}${kbTag}${docTag}${badge}</li>`;
    })
    .join("");
  container.innerHTML =
    `<p class="fact-list-label">Where this answer comes from:</p><ul class="fact-bullets">${items}</ul>` + note;
}

const RETRIEVAL_PATH_LABELS = {
  entity_resolution: "matched directly to a known entity",
  semantic_search: "found via broader search",
  none: "no match found",
};

// Small, quiet observability line (v4): how this specific answer was
// produced -- see app/context/orchestrator.py's retrieval_path and
// app/context/query_service.py's cache_hit. Not meant to be the focus of
// the page, just visible proof of the retrieval-efficiency story (skip
// semantic search when a named entity already answers it, skip the whole
// retrieval+synthesis call on a cache hit) for anyone who wants it.
//
// Deliberately does NOT include cost_usd -- what this app spends on LLM
// calls is this operator's own internal cost, not something every tenant's
// end user browsing the page needs to see next to their answer. It's still
// in the raw API response for anyone building their own tooling against
// it; the running total for whoever operates this deployment is behind
// ADMIN_API_KEY (see the footer link, and GET /api/v1/admin/spend).
function renderQueryStats(el, metadata) {
  if (!metadata) {
    el.hidden = true;
    return;
  }
  const parts = [];
  const pathLabel = RETRIEVAL_PATH_LABELS[metadata.retrieval_path];
  if (pathLabel) parts.push(pathLabel);
  if (metadata.cache_hit) parts.push("served from cache (no new retrieval or LLM call)");
  if (!parts.length) {
    el.hidden = true;
    return;
  }
  el.textContent = parts.join(" · ");
  el.hidden = false;
}

document.getElementById("queryInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("askBtn").click();
});

// --- Connect an AI agent (MCP) -----------------------------------------------
// Purely local: the endpoint is always this same origin's /mcp (see
// app/mcp/server.py) and the header is whatever access key is already saved
// here -- no separate fetch needed, unlike the sections above.
function renderMcpCard() {
  const card = document.getElementById("mcpCard");
  const key = getApiKey();
  if (!key) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  document.getElementById("mcpConfig").textContent =
    `Endpoint:  ${location.origin}/mcp\n` +
    `Header:    X-API-Key: ${key}\n\n` +
    `Streamable HTTP transport -- add this URL and header in your MCP client's server settings.`;
}

// --- Admin: running spend (footer link) -------------------------------------
// Deliberately separate from the tenant access key above -- this reads
// GET /api/v1/admin/spend, gated by the operator-only ADMIN_API_KEY (see
// app/security.py's require_admin), never a tenant's own key. Not meant to
// be a real admin panel -- just enough to keep the running total out of the
// per-query line every tenant's user sees, without losing visibility for
// whoever actually operates this deployment. Kept in localStorage under a
// different key than saxon_api_key so entering one never substitutes for
// the other.
function getAdminKey() {
  return localStorage.getItem("saxon_admin_key") || "";
}

document.getElementById("adminSpendBtn").addEventListener("click", async () => {
  let key = getAdminKey();
  if (!key) {
    key = window.prompt("Admin key (ADMIN_API_KEY):") || "";
    if (!key) return;
    localStorage.setItem("saxon_admin_key", key);
  }
  try {
    const res = await fetch(`${API}/admin/spend`, { headers: { "X-Admin-Key": key } });
    if (res.status === 401) {
      localStorage.removeItem("saxon_admin_key"); // a stored key that no longer works shouldn't keep silently failing
      window.alert("That admin key isn't valid.");
      return;
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      window.alert(body.detail || "Could not load spend totals.");
      return;
    }
    const data = await res.json();
    const line = (label, bucket) =>
      `${label}: $${bucket.spent_usd.toFixed(4)} of $${bucket.budget_usd.toFixed(2)} budget`;
    window.alert(`${line("Query spend", data.query)}\n${line("Ingestion spend", data.ingestion)}`);
  } catch (err) {
    window.alert(`Error: ${err.message}`);
  }
});

// --- Init -------------------------------------------------------------------
async function loadTenantData() {
  loadOntology();
  await loadKnowledgeBases();
  await loadUsers();
  await loadConnectors();
  await loadReconciliation();
  await loadDocumentSets();
  renderMcpCard();
  loadGraph();
}

loadHealth();
loadTenantData();
