// static/app.js con
// static/app.js
const API = "/api";

function readCookie(name) {
  const prefix = name + "=";
  const part = document.cookie.split("; ").find(item => item.startsWith(prefix));
  return part ? decodeURIComponent(part.slice(prefix.length)) : "";
}
async function apiFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  const method = (options.method || "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = readCookie("procureai_csrf");
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }
  return window.fetch(url, { ...options, headers, credentials: "same-origin", cache: "no-store" });
}
function setFormSubmitLabel(form, label) {
  const button = form?.querySelector('button[type="submit"], button:not([type])');
  if (button) button.textContent = label;
}
function showLogin(message = "") {
  document.getElementById("loginScreen").hidden = false;
  document.getElementById("appShell").hidden = true;
  document.getElementById("loginError").textContent = message;
}
function showApp() {
  document.getElementById("loginScreen").hidden = true;
  document.getElementById("appShell").hidden = false;
}
let currentPage = "overview";
let dashboardData = {};
let quoteRecords = [];
let parsedQuoteLineItems = [];
let projectRecords = [];
let activeProjectId = null;
let invoiceRecords = [];
// --- Navigation ---
document.querySelectorAll(".nav-item").forEach(btn => {
  btn.addEventListener("click", (e) => {
    document.querySelectorAll(".nav-item").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    const page = btn.dataset.page;
    document.getElementById(`page-${page}`).classList.add("active");
    document.getElementById("crumbTitle").textContent = btn.textContent.trim().split(" ")[0];
    if (page === "overview") loadProjectTrends();
    if (page === "settings") loadDatabaseBackups();
    if (page === "invoices") loadInvoices();
    if (page === "projects") {
      activeProjectId = null;
      document.getElementById("projectWorkspaceView").hidden = true;
      document.getElementById("projectsListView").hidden = false;
      loadProjects();
    }
    if (window.innerWidth < 800) document.getElementById("sidebar").classList.remove("open");
  });
});
document.querySelectorAll("[data-goto]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelector(`.nav-item[data-page="${btn.dataset.goto}"]`).click();
  });
});
document.getElementById("mobileMenu").addEventListener("click", () => {
  document.getElementById("sidebar").classList.toggle("open");
});
document.getElementById("projectStatusFilter").addEventListener("change", renderProjectCards);
document.getElementById("projectCards").addEventListener("click", async event => {
  const create = event.target.closest('[data-modal="projectModal"]');
  if (create) { document.querySelector('.project-page-heading [data-modal="projectModal"]')?.click(); return; }
  const retry = event.target.closest("#retryProjects");
  if (retry) return loadProjects();
  const remove = event.target.closest("[data-delete-project]");
  if (remove) {
    const projectId = remove.dataset.deleteProject;
    const project = projectRecords.find(item => item.project_id === projectId);
    if (!project || String(project.status).toLowerCase() !== "completed") {
      showToast("Only completed projects can be deleted.", true);
      return;
    }
    const confirmed = window.confirm(
      `Remove "${project.title}" from the project workspace? Its procurement, invoice, job report and audit records will be retained for historical reference.`
    );
    if (!confirmed) return;
    remove.disabled = true;
    try {
      const response = await apiFetch(`${API}/projects/${encodeURIComponent(projectId)}`, {method:"DELETE"});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not delete completed project");
      projectRecords = projectRecords.filter(item => item.project_id !== projectId);
      renderProjectCards();
      await loadProjectTrends();
      showToast("Completed project removed. Historical records were retained.");
    } catch(error) {
      showToast(error.message, true);
      remove.disabled = false;
    }
    return;
  }
  const open = event.target.closest("[data-open-project]");
  if (open) openProjectWorkspace(open.dataset.openProject);
});
document.getElementById("backToProjects").addEventListener("click", () => {
  activeProjectId = null;
  document.getElementById("projectWorkspaceView").hidden = true;
  document.getElementById("projectsListView").hidden = false;
  loadProjects();
});
document.getElementById("recalculateProject").addEventListener("click", async () => {
  if (!activeProjectId) return;
  const button = document.getElementById("recalculateProject");
  button.disabled = true;
  try {
    const response = await apiFetch(`${API}/projects/${encodeURIComponent(activeProjectId)}/recalculate`, {method:"POST"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not recalculate progress");
    renderProjectWorkspace(data.project);
    showToast("Project progress recalculated");
  } catch(error) { showToast(error.message,true); }
  finally { button.disabled = false; }
});
document.getElementById("projectWorkspaceContent").addEventListener("click", async event => {
  const closeButton = event.target.closest("[data-close-project]");
  if (closeButton && activeProjectId) {
    if (!window.confirm("Close this project as completed? It will remain available for historical records and monthly trends.")) return;
    closeButton.disabled = true;
    try {
      const response = await apiFetch(`${API}/projects/${encodeURIComponent(activeProjectId)}/complete`, {
        method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({completion_note:"Marked complete by project administrator."})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not close project");
      renderProjectWorkspace(data.project);
      await loadProjects();
      await loadProjectTrends();
      showToast("Project closed and added to completion trends");
    } catch(error) { showToast(error.message,true); }
    finally { if (closeButton.isConnected) closeButton.disabled = false; }
    return;
  }
  const terminateButton = event.target.closest("[data-terminate-project]");
  if (terminateButton && activeProjectId) {
    const note = window.prompt("Optional: record why this project is being terminated. Existing records will be retained.");
    if (note === null) return;
    if (!window.confirm("Terminate and archive this project? New work will be blocked, but all existing requests, quotations, purchase orders, invoices, reports and audit history will be retained.")) return;
    terminateButton.disabled = true;
    try {
      const response = await apiFetch(`${API}/projects/${encodeURIComponent(activeProjectId)}/terminate`, {
        method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({termination_note:note})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not terminate project");
      renderProjectWorkspace(data.project);
      await loadProjects();
      showToast("Project terminated and archived. Existing records were retained.");
    } catch(error) { showToast(error.message,true); }
    finally { if (terminateButton.isConnected) terminateButton.disabled = false; }
    return;
  }
  const button = event.target.closest("[data-add-project-request]");
  if (!button || !activeProjectId) return;
  const form = document.getElementById("requestForm");
  form.reset();
  await populateRequestProjects(activeProjectId);
  form.elements.project_id.value = activeProjectId;
  document.querySelector("#requestModal .modal-head h2").textContent = "Add purchase request to project";
  document.getElementById("requestModal").classList.add("open");
});
document.getElementById("projectWorkspaceContent").addEventListener("submit", async event => {
  if (event.target.id === "projectLinkQuotationForm") {
    event.preventDefault();
    const form = event.target;
    const quoteId = form.elements.quote_id.value;
    if (!quoteId || !activeProjectId) return;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      const response = await apiFetch(`${API}/projects/${encodeURIComponent(activeProjectId)}/quotations/${quoteId}`, {method:"POST"});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not link quotation");
      renderProjectWorkspace(data.project);
      showToast("Quotation linked to project");
    } catch (error) { showToast(error.message, true); }
    finally { if (button.isConnected) button.disabled = false; }
    return;
  }
  if (event.target.id !== "projectUpdateForm") return;
  event.preventDefault();
  const form = event.target;
  const payload = Object.fromEntries(new FormData(form).entries());
  payload.progress_percent = Number(payload.progress_percent || 0);
  payload.completed_milestones = [...form.querySelectorAll('input[name="completed_milestones"]:checked')].map(input => input.value);
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const response = await apiFetch(`${API}/projects/${encodeURIComponent(activeProjectId)}/updates`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not save project update");
    renderProjectWorkspace(data.project);
    showToast("Daily project update saved");
  } catch(error) { showToast(error.message,true); }
  finally { button.disabled = false; }
});
document.getElementById("projectForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form).entries());
  payload.deliverables = String(payload.deliverables || "").split("\n").map(value=>value.trim()).filter(Boolean);
  payload.estimated_budget = Number(payload.estimated_budget || 0);
  payload.expected_duration_days = Number(payload.expected_duration_days || 30);
  payload.planned_purchase_requests = Number(payload.planned_purchase_requests || 0);
  const startDate = new Date(payload.start_date + "T00:00:00");
  const durationDue = new Date(startDate); durationDue.setDate(durationDue.getDate() + payload.expected_duration_days);
  const enteredDue = new Date(payload.due_date + "T00:00:00");
  if (payload.expected_duration_days > 0 && (!payload.due_date || Math.abs((enteredDue-durationDue)/86400000) > 1)) {
    payload.due_date = durationDue.toISOString().slice(0,10);
  }
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const response = await apiFetch(`${API}/projects`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not create project");
    form.reset();
    document.getElementById("projectModal").classList.remove("open");
    showToast("Project workspace created");
    await loadProjects();
    await openProjectWorkspace(data.project.project_id);
  } catch(error) { showToast(error.message,true); }
  finally { button.disabled = false; }
});
// --- Data Fetching ---
async function populateRequestProjects(selectedProjectId = "") {
  const select = document.getElementById("requestProjectId");
  if (!select) return;
  select.innerHTML = '<option value="">Loading projects…</option>';
  try {
    const response = await apiFetch(API + "/projects");
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not load project workspaces");
    const projects = data.projects || [];
    projectRecords = projects;
    const available = projects.filter(project => String(project.status || "").toLowerCase() !== "completed");
    select.innerHTML = '<option value="">Select a project workspace</option>' + available.map(project =>
      '<option value="' + escapeHtml(project.project_id) + '">' + escapeHtml(project.title) + ' · ' + escapeHtml(project.project_id) + '</option>'
    ).join("");
    if (selectedProjectId) select.value = selectedProjectId;
    if (!available.length) select.innerHTML = '<option value="">Create a project workspace first</option>';
  } catch (error) {
    select.innerHTML = '<option value="">Could not load projects — close and try again</option>';
    showToast(error.message, true);
  }
}
async function fetchDashboard() {
  const res = await apiFetch(`${API}/dashboard`);
  const data = await res.json();
  dashboardData = data;
  const quoteResponse = await apiFetch(`${API}/quotes`);
  const quoteData = quoteResponse.ok ? await quoteResponse.json() : { quotes: [] };
  quoteRecords = quoteData.quotes || [];
  const invoiceResponse = await apiFetch(`${API}/invoices`);
  const invoiceData = invoiceResponse.ok ? await invoiceResponse.json() : { invoices: [] };
  invoiceRecords = invoiceData.invoices || [];
  renderDashboard();
}
function projectDate(value) {
  if (!value) return "Not set";
  const parsed = new Date(value + "T00:00:00");
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString("en-GB", {day:"numeric",month:"short",year:"numeric"});
}
function dueBadge(status, label) {
  const tone = status === "overdue" ? "overdue" : status === "due_soon" || status === "due_today" ? "soon" : status === "complete" ? "complete" : status === "unscheduled" ? "unscheduled" : "ontime";
  return `<span class="project-due-badge ${tone}">${escapeHtml(label || "No due date")}</span>`;
}
async function loadProjects() {
  const grid = document.getElementById("projectCards");
  if (!grid) return;
  grid.innerHTML = '<div class="project-loading">Loading project workspaces…</div>';
  try {
    const response = await apiFetch(`${API}/projects`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not load projects");
    projectRecords = data.projects || [];
    renderProjectCards();
  } catch (error) {
    grid.innerHTML = `<div class="project-empty"><h3>Projects could not be loaded</h3><p>${escapeHtml(error.message)}</p><button class="btn btn-ghost" id="retryProjects">Try again</button></div>`;
  }
}
async function loadProjectTrends() {
  const panel = document.getElementById("projectTrendsPanel");
  if (!panel) return;
  try {
    const response = await apiFetch(`${API}/projects/trends`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not load completion trends");
    const months = data.months || [];
    const maxCount = Math.max(1,...months.map(item=>Number(item.count||0)));
    const featured = data.project_of_the_month;
    panel.innerHTML = `
      <div class="project-trends-head"><div><span class="eyebrow">DELIVERY PERFORMANCE</span><h2>Project completion trends</h2><p>Track projects formally closed as completed. Only closed projects count toward these figures.</p></div><div class="project-trends-total"><strong>${Number(data.completed_this_month||0)}</strong><span>completed in ${escapeHtml(data.current_month)}</span></div></div>
      <div class="project-month-feature">${featured ? `
        <div class="project-month-cup">✦</div><div class="project-month-copy"><span class="eyebrow">PROJECT OF THE MONTH · ${escapeHtml(data.current_month)}</span><h3>${escapeHtml(featured.title)}</h3><p>${escapeHtml(featured.client_name || "Client not specified")} · Completed ${new Date(featured.completed_at).toLocaleDateString("en-GB",{day:"numeric",month:"short",year:"numeric"})}</p><small>Closed by ${escapeHtml(featured.closed_by || "Workspace user")}</small></div>
      ` : `<div class="project-month-cup">✦</div><div class="project-month-copy"><span class="eyebrow">PROJECT OF THE MONTH</span><h3>No completed project yet</h3><p>Close a finished project to feature it here and include it in the monthly trends.</p></div>`}</div>
      <div class="project-trend-chart"><div class="project-trend-chart-head"><b>Monthly completions</b><span>Last 12 months</span></div><div class="project-trend-bars">${months.map(item=>`<div class="project-trend-column" title="${escapeHtml(item.label)}: ${Number(item.count)} completed"><div class="project-trend-bar-track"><i style="height:${Math.max(item.count?8:0,Number(item.count||0)/maxCount*100)}%"></i></div><b>${Number(item.count||0)}</b><small>${escapeHtml(item.label.split(" ")[0])}</small></div>`).join("")}</div></div>
      <div class="project-trend-history"><b>Completed projects by month</b><div>${months.filter(item=>item.count).slice().reverse().map(item=>`<span><strong>${escapeHtml(item.label)}</strong><small>${item.projects.map(p=>escapeHtml(p.title)).join(" · ")}</small><em>${item.count}</em></span>`).join("") || '<p class="project-muted">No projects have been closed yet.</p>'}</div></div>`;
  } catch(error) {
    panel.innerHTML = `<div class="project-empty"><h3>Trends unavailable</h3><p>${escapeHtml(error.message)}</p></div>`;
  }
}
function renderProjectCards() {
  const grid = document.getElementById("projectCards");
  const filter = document.getElementById("projectStatusFilter")?.value || "all";
  const filteredProjects = projectRecords.filter(project => {
    const status = String(project.status || "").toLowerCase();
    const completed = status === "completed";
    const terminated = status === "terminated";
    return filter === "all" || (filter === "completed" && completed) || (filter === "terminated" && terminated) || (filter === "uncompleted" && !completed && !terminated);
  });
  document.getElementById("projectCount").textContent = `${filteredProjects.length} ${filteredProjects.length === 1 ? "project" : "projects"}`;
  if (!filteredProjects.length) {
    const message = filter === "completed" ? "No completed projects yet." : filter === "terminated" ? "No terminated projects." : filter === "uncompleted" ? "No uncompleted projects. Great work!" : "No project workspaces yet.";
    grid.innerHTML = `<div class="project-empty"><span>▦</span><h3>${escapeHtml(message)}</h3><p>${filter === "all" ? "Create your first project to get started." : "Try another filter or update a project's completion status."}</p>${filter === "all" ? '<button class="btn btn-primary" data-modal="projectModal">Create first project</button>' : ""}</div>`;
    return;
  }
  grid.innerHTML = filteredProjects.map(project => `
    <article class="project-card">
      <div class="project-card-top"><span class="project-status">${escapeHtml((project.status || "active").replace("_"," "))}</span>${dueBadge(project.due_status,project.due_label)}</div>
      <h3>${escapeHtml(project.title)}</h3><p class="project-client">${escapeHtml(project.client_name || "Client not specified")}</p>
      <div class="project-card-meta"><span>Lead</span><b>${escapeHtml(project.procurement_lead || "Unassigned")}</b><span>Purchase requests</span><b>${Number(project.purchase_requests_count || 0)}</b></div>
      <div class="project-card-progress"><div><span>Overall progress</span><b>${Number(project.progress_percent || 0).toFixed(0)}%</b></div><div class="project-progress-track"><i style="width:${Math.max(0,Math.min(100,Number(project.progress_percent||0)))}%"></i></div></div>
      <div class="project-card-footer"><span>Due ${projectDate(project.due_date)}</span><div class="project-card-actions"><button type="button" class="text-btn" data-open-project="${escapeHtml(project.project_id)}">Open workspace →</button>${String(project.status || "").toLowerCase() === "completed" ? `<button type="button" class="btn btn-danger project-delete-btn" data-delete-project="${escapeHtml(project.project_id)}" aria-label="Delete completed project">Delete</button>` : ""}</div></div>
    </article>`).join("");
}
async function openProjectWorkspace(projectId) {
  activeProjectId = projectId;
  document.getElementById("projectsListView").hidden = true;
  document.getElementById("projectWorkspaceView").hidden = false;
  const container = document.getElementById("projectWorkspaceContent");
  container.innerHTML = '<div class="project-loading">Loading project overview and activity…</div>';
  try {
    const response = await apiFetch(`${API}/projects/${encodeURIComponent(projectId)}/overview`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not load project workspace");
    renderProjectWorkspace(data.project);
  } catch (error) {
    container.innerHTML = `<div class="project-empty"><h3>Workspace unavailable</h3><p>${escapeHtml(error.message)}</p></div>`;
  }
}
function renderProjectWorkspace(project) {
  const container = document.getElementById("projectWorkspaceContent");
  const projectLocked = ["completed","terminated"].includes(String(project.status || "").toLowerCase());
  const milestones = project.milestones || [];
  const updates = project.updates || [];
  const deliverables = project.deliverables || [];
  const progress = Math.max(0,Math.min(100,Number(project.progress_percent || 0)));
  const latestReported = updates.length ? Number(updates[0].progress_percent || 0) : 0;
  const purchaseRequests = project.purchase_requests || [];
  const purchaseRequestsMarkup = purchaseRequests.length ? `<div class="project-request-table-wrap"><table class="project-request-table"><thead><tr><th>REFERENCE</th><th>ITEM / MATERIAL</th><th>QUANTITY</th><th>REQUIRED BY</th><th>PRIORITY</th><th>STATUS</th></tr></thead><tbody>${purchaseRequests.map(r => `<tr><td><b>${escapeHtml(r.code)}</b></td><td><b>${escapeHtml(r.material)}</b><small>${escapeHtml(r.category)}</small></td><td>${escapeHtml(r.quantity)} ${escapeHtml(r.unit)}</td><td>${projectDate(r.required_by)}</td><td><span class="project-request-priority ${String(r.priority).toLowerCase()==="urgent"?"urgent":""}">${escapeHtml(r.priority)}</span></td><td><span class="project-request-status">${escapeHtml(r.status)}</span></td></tr>`).join("")}</tbody></table></div>` : '<div class="project-feed-empty">No purchase requests have been added to this project yet. Add the first request to start tracking procurement requirements here.</div>';
  const milestoneMarkup = milestones.map(item => `
    <div class="project-milestone ${item.completed ? "is-complete" : ""}">
      <span class="project-milestone-check">${item.completed ? "✓" : "○"}</span><div><b>${escapeHtml(item.label)}</b><small>${item.completed ? "Completed" + (item.completed_at ? " · " + projectDate(item.completed_at.slice(0,10)) : "") : "Awaiting completion"}</small></div><strong>${Number(item.weight || 0)}%</strong>
    </div>`).join("");
  const updateMarkup = updates.length ? updates.map(item => `
    <article class="project-update-item">
      <div class="project-update-avatar">${escapeHtml((item.author || "U").slice(0,1).toUpperCase())}</div>
      <div class="project-update-body"><div class="project-update-meta"><b>${escapeHtml(item.author)}</b><span>${escapeHtml(item.update_type.replace("_"," "))}</span><time>${new Date(item.created_at).toLocaleString("en-GB",{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"})}</time></div>
      <div class="project-update-status">${escapeHtml(item.status)} · Reported workstream progress ${Number(item.progress_percent).toFixed(0)}%</div>
      ${item.field_notes ? `<p>${escapeHtml(item.field_notes)}</p>` : ""}
      ${item.blockers ? `<div class="project-update-blocker"><b>Blocker</b><p>${escapeHtml(item.blockers)}</p></div>` : ""}
      ${item.next_steps ? `<div class="project-update-next"><b>Next steps</b><p>${escapeHtml(item.next_steps)}</p></div>` : ""}
      ${item.completed_milestones?.length ? `<small class="project-update-milestones">Milestones completed: ${item.completed_milestones.map(escapeHtml).join(", ")}</small>` : ""}
      </div>
    </article>`).join("") : '<div class="project-feed-empty">No daily updates yet. Record the first field or procurement update below.</div>';
  container.innerHTML = `
    <section class="project-workspace-hero" data-project-status="${escapeHtml(project.status || "active")}">
      <div><div class="eyebrow">PROJECT WORKSPACE · ${escapeHtml(project.project_id)}</div><h1>${escapeHtml(project.title)}</h1><p>${escapeHtml(project.client_name || "Client not specified")} · Procurement lead: ${escapeHtml(project.procurement_lead || "Unassigned")}</p></div>
      <div class="project-hero-due">${dueBadge(project.due_status,project.due_label)}<small>${project.completed_at ? "Completed " + projectDate(project.completed_at.slice(0,10)) : "Target completion · " + projectDate(project.due_date)}</small>${project.status === "completed" ? '<span class="project-closed-label">✓ Project completed</span>' : project.status === "terminated" ? '<span class="project-closed-label">Archived / terminated</span>' : '<button type="button" class="btn btn-primary project-close-btn" data-close-project>✓ Close project</button>'}</div>
    </section>
${project.status === "completed" ? `<div class="project-closure-notice"><b>Project completed and closed</b><span>Closed by ${escapeHtml(project.closed_by || "Workspace user")} on ${projectDate((project.completed_at || "").slice(0,10))}. This project remains available for reporting and reference.</span></div>` : ""}
    ${project.status === "terminated" ? `<div class="project-closure-notice"><b>Project terminated and archived</b><span>Archived by ${escapeHtml(project.terminated_by || "Workspace user")} on ${projectDate((project.terminated_at || "").slice(0,10))}. Existing procurement records and reports are retained.</span>${project.termination_note ? `<p>${escapeHtml(project.termination_note)}</p>` : ""}</div>` : ""}
    <section class="project-intake-details">
      <div class="project-section-heading"><div><span class="eyebrow">PROJECT BRIEF</span><h2>Project requirements</h2></div></div>
      <div class="project-intake-grid">
        <div><span>Location</span><b>${escapeHtml(project.project_location || "Not provided")}</b></div>
        <div><span>Category</span><b>${escapeHtml(project.project_category || "Not provided")}</b></div>
        <div><span>Estimated budget</span><b>${Number(project.estimated_budget || 0).toLocaleString("en-NG",{style:"currency",currency:"NGN",maximumFractionDigits:0})}</b></div>
        <div><span>Expected duration</span><b>${Number(project.expected_duration_days || 0)} days</b></div>
        <div><span>Priority</span><b>${escapeHtml((project.priority || "medium").toUpperCase())}</b></div>
        <div><span>Status</span><b>${escapeHtml((project.status || "active").replace("_"," ").toUpperCase())}</b></div>
        <div><span>Planned purchase requests</span><b>${Number(project.planned_purchase_requests || 0)}</b></div>
        <div><span>Requests created</span><b>${Number(project.purchase_requests_count || 0)}</b></div>
      </div>
    </section>
    <section class="project-overview-grid">
      <article class="project-overview-card project-summary-card"><span class="eyebrow">PROJECT INTRODUCTION</span><h2>Overview & scope</h2><p>${escapeHtml(project.executive_summary || "No executive summary has been added.")}</p><div class="project-client-details"><b>Client / agency</b><span>${escapeHtml(project.client_name || "Not specified")}</span><b>Agency details</b><span>${escapeHtml(project.agency_details || "Not provided")}</span><b>Procurement lead</b><span>${escapeHtml(project.procurement_lead || "Unassigned")}</span></div></article>
      <article class="project-overview-card project-progress-card"><span class="eyebrow">DELIVERY HEALTH</span><h2>Project progress</h2><div class="project-progress-number">${progress.toFixed(0)}<small>%</small></div><div class="project-progress-track large"><i style="width:${progress}%"></i></div><div class="project-progress-legend"><span>Milestones <b>${Number(project.progress_breakdown?.milestones || 0).toFixed(0)}%</b></span><span>Daily updates <b>${Number(project.progress_breakdown?.daily_updates || 0).toFixed(0)}%</b></span></div><p>Calculated from 4 procurement milestones (60%) and the latest reported workstream progress (40%).</p></article>
    </section>
    <section class="project-overview-card project-timeline-card"><div class="project-section-heading"><div><span class="eyebrow">TIMELINE & DUE DATE TRACKER</span><h2>Procurement milestones</h2></div><div class="project-date-pair"><span>Start <b>${projectDate(project.start_date)}</b></span><span>Due <b>${projectDate(project.due_date)}</b></span></div></div><div class="project-milestones">${milestoneMarkup}</div></section>
    <section class="project-overview-card project-purchase-requests-card">
      <div class="project-section-heading"><div><span class="eyebrow">PROJECT PROCUREMENT</span><h2>Purchase requests</h2><p>Manage multiple independent material and service requests under this project.</p></div><div class="project-request-actions"><span class="project-deliverable-count">${purchaseRequests.length} requests</span>${projectLocked ? '<span class="project-closed-label">Project closed</span>' : '<button type="button" class="btn btn-primary" data-add-project-request>＋ Add request</button>'}</div></div>
      <div class="project-request-list">${purchaseRequestsMarkup}</div>
    </section>
    <section class="project-overview-card project-buyer-confirmation-card">
      <div class="project-section-heading"><div><span class="eyebrow">PROCUREMENT GATE · STAGE 3</span><h2>Buyer confirmation</h2><p>Record the buyer's response received by email, WhatsApp or phone. No buyer login or automated email link is required.</p></div><span class="project-deliverable-count" id="buyerConfirmationStatus">Loading status…</span></div>
      <div id="buyerConfirmationHistory" class="project-linked-quote-list"><div class="project-feed-empty">Loading confirmation history…</div></div>
      ${projectLocked ? '<p class="project-muted">This project is closed. Buyer confirmation records are read-only.</p>' : `<form id="buyerConfirmationForm" class="project-link-quote-form buyer-confirmation-form">
        <label>Buyer name<input name="buyer_name" required minlength="2" maxlength="200" placeholder="Name of the person who responded"></label>
        <label>Buyer organisation<input name="organisation" maxlength="200" placeholder="Organisation / department (optional)"></label>
        <label>Response received through<select name="channel" required><option value="email">Email</option><option value="whatsapp">WhatsApp</option><option value="phone">Phone call</option></select></label>
        <label>Buyer response<select name="status" required><option value="confirmed">Confirmed — requirements accepted</option><option value="changes_requested">Changes requested</option><option value="rejected">Rejected — do not proceed</option></select></label>
        <label>Requirements confirmed / changes requested<textarea name="requirements_confirmed" rows="3" required placeholder="Record specifications, quantities, scope or requested amendments"></textarea></label>
        <label>Budget and delivery acceptance<select name="budget_delivery_accepted" required><option value="yes">Accepted</option><option value="conditional">Accepted with conditions</option><option value="no">Not accepted</option><option value="not_discussed">Not discussed</option></select></label>
        <label>Buyer comments / conditions<textarea name="comments" rows="2" placeholder="Optional additional instructions, conditions or reference to the email / call"></textarea></label>
        <button class="btn btn-primary" type="submit">Save buyer confirmation</button>
      </form>`}
    </section>
    <section class="project-overview-card project-deliverables-card"><div class="project-section-heading"><div><span class="eyebrow">EXPECTED OUTPUTS</span><h2>Key deliverables</h2></div><span class="project-deliverable-count">${deliverables.length} items</span></div>${deliverables.length ? '<ul class="project-deliverables">'+deliverables.map(item=>'<li>'+escapeHtml(item)+'</li>').join("")+'</ul>' : '<p class="project-muted">No deliverables have been listed.</p>'}</section>
    <section class="project-overview-card project-linked-quotes">
      <div class="project-section-heading"><div><span class="eyebrow">COMMERCIAL RECORDS</span><h2>Linked quotations</h2><p>Attach quotations already recorded in ProcureAI without changing their original purchase request.</p></div><span class="project-deliverable-count">${(project.linked_quotations || []).length} linked</span></div>
      ${projectLocked ? '<p class="project-muted">This project is closed. Linked quotations are retained as read-only records.</p>' : `<form id="projectLinkQuotationForm" class="project-link-quote-form">
        <label>Choose an existing quotation<select name="quote_id" id="projectQuotationSelect" required><option value="">Loading available quotations…</option></select></label>
        <button class="btn btn-primary" type="submit">Link quotation to project</button>
      </form>`}
      <div class="project-linked-quote-list">${(project.linked_quotations || []).length ? project.linked_quotations.map(q=>`
        <article class="project-linked-quote-row">
          <div><b>#${q.id} · ${escapeHtml(q.supplier)}</b><span>${escapeHtml(q.request_code)} · ${escapeHtml(q.material)}</span></div>
          <div><b>${formatMoney(q.total_landed)}</b><span>${Number(q.lead_days)} day lead time</span></div>
          <button type="button" class="text-btn danger" data-unlink-project-quote="${q.id}">Unlink</button>
        </article>`).join("") : '<div class="project-feed-empty">No quotations linked to this project yet.</div>'}</div>
    </section>
    <section class="project-activity-layout">
      <article class="project-overview-card project-feed-card"><div class="project-section-heading"><div><span class="eyebrow">TEAM JOURNAL</span><h2>Daily activity log</h2><p>Field notes, procurement updates, blockers and next actions.</p></div><span class="project-deliverable-count">${updates.length} entries</span></div><div class="project-update-feed">${updateMarkup}</div></article>
      <article class="project-overview-card project-update-form-card"><span class="eyebrow">TEAM JOURNAL</span><h2>${projectLocked ? "Project updates locked" : "Submit daily update"}</h2><p class="project-muted">${projectLocked ? "This project is closed. Historical updates remain available above." : "Your signed-in account is recorded automatically as the author."}</p>
        ${projectLocked ? ""  : `<form id="projectUpdateForm">
          <label>Update type<select name="update_type"><option value="daily">Daily update</option><option value="field_note">Field note</option><option value="blocker">Blocker / issue</option><option value="milestone">Milestone update</option></select></label>
          <label>Current status<input name="status" maxlength="80" value="In progress" required></label>
          <label>Reported workstream progress <strong id="projectReportedProgressValue">${latestReported}%</strong><input name="progress_percent" type="range" min="0" max="100" value="${latestReported}" step="5" id="projectReportedProgress"></label>
          <label>Field notes<textarea name="field_notes" rows="4" placeholder="What was completed today? Include inspections, supplier contact, site findings…"></textarea></label>
          <label>Blockers / risks<textarea name="blockers" rows="2" placeholder="Delays, access issues, missing documents or dependencies"></textarea></label>
          <label>Next steps<textarea name="next_steps" rows="2" placeholder="Actions planned for the next work period"></textarea></label>
          <div class="project-milestone-checklist"><b>Mark milestones completed</b>${milestones.map(item=>`<label class="project-check-option"><input type="checkbox" name="completed_milestones" value="${escapeHtml(item.key)}" ${item.completed ? "disabled" : ""}><span>${escapeHtml(item.label)}${item.completed ? " · already complete" : ""}</span></label>`).join("")}</div>
          <button class="btn btn-primary project-submit-update" type="submit">Post project update</button>
        </form>`}
      </article>
    </section>
`;
  const progressInput = document.getElementById("projectReportedProgress");
  progressInput?.addEventListener("input", () => { document.getElementById("projectReportedProgressValue").textContent = progressInput.value + "%"; });
  loadProjectQuotationOptions(project.project_id);
  loadBuyerConfirmations(project.project_id);
  const buyerForm = document.getElementById("buyerConfirmationForm");
  buyerForm?.addEventListener("submit", async event => {
    event.preventDefault();
    const button = buyerForm.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      const payload = Object.fromEntries(new FormData(buyerForm).entries());
      const response = await apiFetch(`${API}/projects/${encodeURIComponent(project.project_id)}/buyer-confirmations`, {
        method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not save buyer confirmation");
      showToast("Buyer confirmation saved");
      await loadBuyerConfirmations(project.project_id);
    } catch(error) { showToast(error.message,true); }
    finally { button.disabled = false; }
  });
}
async function loadBuyerConfirmations(projectId) {
  const status = document.getElementById("buyerConfirmationStatus");
  const history = document.getElementById("buyerConfirmationHistory");
  const form = document.getElementById("buyerConfirmationForm");
  if (!status || !history) return;
  try {
    const response = await apiFetch(`${API}/projects/${encodeURIComponent(projectId)}/buyer-confirmations`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not load buyer confirmation");
    const latest = data.latest;
    status.textContent = !latest ? "Not confirmed" : latest.status === "confirmed" ? "Buyer confirmed" : latest.status === "changes_requested" ? "Changes requested" : "Rejected";
    status.className = "project-deliverable-count " + (!latest ? "buyer-status-pending" : latest.status === "confirmed" ? "buyer-status-confirmed" : "buyer-status-attention");
    history.innerHTML = latest ? (() => {
      const channel = {email:"Email",whatsapp:"WhatsApp",phone:"Phone"}[latest.channel] || latest.channel;
      const outcome = {confirmed:"Confirmed",changes_requested:"Changes requested",rejected:"Rejected"}[latest.status] || latest.status;
      const acceptance = {yes:"Accepted",no:"Not accepted",conditional:"Accepted with conditions",not_discussed:"Not discussed"}[latest.budget_delivery_accepted] || latest.budget_delivery_accepted;
      return `<article class="project-linked-quote-row buyer-confirmation-record"><div><b>${escapeHtml(latest.buyer_name)} · ${escapeHtml(outcome)}</b><span>${escapeHtml(latest.organisation || "Organisation not specified")} · ${escapeHtml(channel)} · Budget / delivery: ${escapeHtml(acceptance)}</span><p>${escapeHtml(latest.requirements_confirmed)}</p>${latest.comments ? `<small>${escapeHtml(latest.comments)}</small>` : ""}</div><div><b>Last updated ${new Date(latest.recorded_at).toLocaleString("en-GB")}</b><span>By ${escapeHtml(latest.recorded_by)}</span></div></article>`;
    })() : '<div class="project-feed-empty">No buyer confirmation saved yet. Complete the form below once the buyer has responded.</div>';
    if (form && latest) {
      for (const [name, value] of Object.entries(latest)) {
        const field = form.elements.namedItem(name);
        if (field && ["buyer_name","organisation","channel","status","requirements_confirmed","budget_delivery_accepted","comments"].includes(name)) field.value = value ?? "";
      }
      const submit = form.querySelector('button[type="submit"]');
      if (submit) submit.textContent = "Update buyer confirmation";
    } else if (form) {
      const submit = form.querySelector('button[type="submit"]');
      if (submit) submit.textContent = "Save buyer confirmation";
    }
  } catch(error) {
    status.textContent = "Unavailable";
    history.innerHTML = `<div class="project-feed-empty">${escapeHtml(error.message)}</div>`;
  }
}
async function loadProjectQuotationOptions(projectId) {
  const select = document.getElementById("projectQuotationSelect");
  if (!select) return;
  try {
    const response = await apiFetch(`${API}/projects/${encodeURIComponent(projectId)}/quotations`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not load quotations");
    const available = (data.quotations || []).filter(q => !q.already_linked);
    select.innerHTML = '<option value="">Select a quotation…</option>' + available.map(q =>
      `<option value="${q.id}">#${q.id} · ${escapeHtml(q.supplier)} · ${escapeHtml(q.request_code)} · ${escapeHtml(q.material)} · ${formatMoney(q.total_landed)}</option>`
    ).join("");
    if (!available.length) select.innerHTML = '<option value="">No unlinked quotations available</option>';
  } catch (error) {
    select.innerHTML = '<option value="">Could not load quotations</option>';
    showToast(error.message, true);
  }
}

function formatMoney(val) {
  return "₦" + Number(val).toLocaleString("en-NG", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
}
function filterRecords(records, search, status, fields) {
  const term = String(search || "").trim().toLowerCase();
  return (records || []).filter(record => {
    const matchesText = !term || fields.some(field => String(record[field] ?? "").toLowerCase().includes(term));
    const matchesStatus = !status || String(record.status || "") === status;
    return matchesText && matchesStatus;
  });
}
function supplierServiceCategories(supplier) {
  const services = String(supplier.services || "").toLowerCase();
  const contact = String(supplier.contact || "").toLowerCase();
  const text = services + " " + contact;
  const categories = [];
  if (/panel|switchboard|distribution board|power distribution/.test(text)) categories.push("panels");
  if (/breaker|protection|switchgear|fuse|circuit/.test(text)) categories.push("protection");
  if (/light|led|luminaire|lighting/.test(text)) categories.push("lighting");
  if (/cable|wiring|termination|conductor/.test(text)) categories.push("cables");
  if (/install|commission|maintenance|ground|automation|testing/.test(text)) categories.push("installation");
  if (!categories.length || /electrical (products|accessories|materials)|project supply|distribution/.test(text)) categories.push("supplies");
  return [...new Set(categories)];
}
function supplierMatchesCategory(supplier, category) {
  return !category || supplierServiceCategories(supplier).includes(category);
}
function escapePdfHtml(value) { return String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char])); }
function exportPdf(type) {
  const datasets = {
    requests: { rows: filterRecords(dashboardData.requests, document.getElementById("requestSearch")?.value, document.getElementById("requestStatusFilter")?.value, ["code","material","category","site","status"]), fields: ["code","material","category","quantity","unit","site","required_by","priority","status"] },
    suppliers: { rows: filterRecords(dashboardData.suppliers, document.getElementById("supplierSearch")?.value, document.getElementById("supplierStatusFilter")?.value, ["name","city","contact","email","phone","services","status"]), fields: ["name","city","email","phone","website","address","services","rating","on_time","status"] },
    quotes: { rows: filterRecords(quoteRecords, document.getElementById("quoteSearch")?.value, "", ["supplier","request_code","material","category","note"]), fields: ["supplier","request_code","material","unit_price","freight","tax_rate","lead_days","payment_days","note","created_at"] },
    orders: { rows: filterRecords(dashboardData.orders, document.getElementById("orderSearch")?.value, document.getElementById("orderStatusFilter")?.value, ["code","supplier","material","status"]), fields: ["code","supplier","material","quantity","unit_price","freight","tax","total","required_by","status","decision_note"] }
  };
  const dataset = datasets[type]; if (!dataset) return;
  const popup = window.open("", "_blank"); if (!popup) return showToast("Allow pop-ups to create the PDF report.", true);
  const titles = {requests:"Purchase Requests",suppliers:"Seller Directory",quotes:"Received Quotations",orders:"Purchase Orders"};
  const title = titles[type] || "ProcureAI Report";
  const labels = {code:"Request / PO reference",material:"Material / item",category:"Category",quantity:"Quantity",unit:"Unit",site:"Delivery location / site",required_by:"Required delivery date",priority:"Priority",status:"Status",name:"Supplier name",city:"City",email:"Email address",phone:"Phone number",website:"Website",address:"Business address",services:"Services / capabilities",rating:"Supplier rating",on_time:"On-time delivery (%)",supplier:"Supplier",request_code:"Request reference",unit_price:"Unit price (NGN)",freight:"Freight / delivery (NGN)",tax_rate:"Tax rate (%)",lead_days:"Lead time (days)",payment_days:"Payment terms (days)",note:"Commercial notes",created_at:"Date received",tax:"Tax amount (NGN)",total:"Commercial total (NGN)",decision_note:"Approval / decision note"};
  const moneyFields = new Set(["unit_price","freight","tax","total"]);
  const dateFields = new Set(["required_by","created_at"]);
  const formatValue = (field,value) => {
    if (value === null || value === undefined || value === "") return "—";
    if (moneyFields.has(field) && Number.isFinite(Number(value))) return "₦" + Number(value).toLocaleString("en-NG",{minimumFractionDigits:2,maximumFractionDigits:2});
    if (field === "on_time" || field === "tax_rate") return Number(value).toLocaleString("en-NG") + "%";
    if (field === "quantity" && Number.isFinite(Number(value))) return Number(value).toLocaleString("en-NG");
    if (dateFields.has(field)) { const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString("en-GB",{dateStyle:"medium"}); }
    return String(value);
  };
  const tableHead = dataset.fields.map(field => "<th>"+escapePdfHtml(labels[field] || field)+"</th>").join("");
  const tableRows = dataset.rows.map(row => "<tr>"+dataset.fields.map(field => "<td>"+escapePdfHtml(formatValue(field,row[field]))+"</td>").join("")+"</tr>").join("");
  const generated = new Date().toLocaleString("en-GB",{dateStyle:"medium",timeStyle:"short"});
  const filterSummary = type === "requests" ? (document.getElementById("requestStatusFilter")?.value || "All statuses") : type === "suppliers" ? (document.getElementById("supplierStatusFilter")?.value || "All statuses") : type === "orders" ? (document.getElementById("orderStatusFilter")?.value || "All statuses") : "All matching records";
  const reportHtml = "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>"+escapePdfHtml(title)+" - ProcureAI</title><style>@page{size:A4 landscape;margin:13mm}*{box-sizing:border-box}body{font:10px/1.5 Arial,Helvetica,sans-serif;color:#1d2b3b;margin:0;background:#fff}header{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;border-bottom:2px solid #233d5b;padding:0 0 14px;margin-bottom:16px}.brand{font-size:10px;font-weight:800;letter-spacing:1.5px;color:#315f91;text-transform:uppercase}.brand span{color:#172b4d}h1{font-size:22px;line-height:1.2;margin:7px 0 4px;color:#172b4d}header p{margin:0;color:#68788b;font-size:10px}.meta{min-width:170px;text-align:right;color:#68788b;font-size:9px}.summary{display:flex;gap:10px;margin:0 0 15px}.summary-card{padding:9px 12px;border:1px solid #e1e7ee;border-radius:7px;min-width:130px;background:#f8fafc}.summary-card span{display:block;color:#6c7b8d;font-size:8px;text-transform:uppercase;letter-spacing:.6px;font-weight:bold}.summary-card b{display:block;margin-top:3px;color:#203c5b;font-size:13px}.table-wrap{width:100%;overflow:visible}table{width:100%;border-collapse:collapse;table-layout:auto}thead{display:table-header-group}th{background:#edf3f9;color:#314d6b;text-align:left;font-size:8px;text-transform:uppercase;letter-spacing:.035em;font-weight:700}th,td{border:1px solid #dce4ec;padding:7px 6px;vertical-align:top;overflow-wrap:anywhere;word-break:normal}td{font-size:9px}tbody tr:nth-child(even){background:#f8fafc}tr{break-inside:avoid}.empty{padding:28px;text-align:center;color:#68788b;border:1px dashed #cbd5df;border-radius:8px;margin-top:12px}.report-note{margin-top:13px;padding:10px 12px;border-left:3px solid #d49a35;background:#fff9ed;color:#6d562b;font-size:9px}footer{margin-top:18px;padding-top:9px;border-top:1px solid #dce4ec;color:#778597;font-size:8px;display:flex;justify-content:space-between;gap:12px}@media print{body{-webkit-print-color-adjust:exact;print-color-adjust:exact}}@media(max-width:650px){header{display:block}.meta{text-align:left;margin-top:12px}.summary{flex-wrap:wrap}.table-wrap{overflow-x:auto}table{min-width:760px}}</style></head><body><header><div><div class='brand'>Procure<span>AI</span> · Gestro Global Consult Ltd</div><h1>"+escapePdfHtml(title)+"</h1><p>Structured procurement register · "+escapePdfHtml(filterSummary)+"</p></div><div class='meta'>Generated: "+escapePdfHtml(generated)+"<br>Records included: "+dataset.rows.length+"</div></header><section class='summary'><div class='summary-card'><span>Records in report</span><b>"+dataset.rows.length+"</b></div><div class='summary-card'><span>Report type</span><b>"+escapePdfHtml(title)+"</b></div><div class='summary-card'><span>Applied view</span><b>"+escapePdfHtml(filterSummary)+"</b></div></section>"+(dataset.rows.length?"<div class='table-wrap'><table><thead><tr>"+tableHead+"</tr></thead><tbody>"+tableRows+"</tbody></table></div>":"<div class='empty'>No records match the current filters. Adjust the search or status filter and export again.</div>")+"<div class='report-note'>Confidential procurement information. Confirm technical specifications, quantities, dates and commercial terms against the original records before making commitments.</div><footer><span>ProcureAI · Internal procurement report</span><span>Generated "+escapePdfHtml(generated)+"</span></footer><script>window.onload=function(){window.focus();window.print();};<\/script></body></html>";
  popup.document.open(); popup.document.write(reportHtml); popup.document.close();
}

async function loadInvoices() {
  const body = document.getElementById("allInvoices");
  if (!body) return;
  try {
    const response = await apiFetch(`${API}/invoices`);
    const raw = await response.text();
    let data;
    try { data = raw ? JSON.parse(raw) : {}; }
    catch { throw new Error(`Invoice register returned an unexpected server response (HTTP ${response.status}). Please refresh and try again.`); }
    if (!response.ok) throw new Error(data.detail || `Could not load invoices (HTTP ${response.status})`);
    invoiceRecords = data.invoices || [];
    const search = document.getElementById("invoiceSearch")?.value || "";
    const status = document.getElementById("invoiceStatusFilter")?.value || "";
    const visible = filterRecords(invoiceRecords, search, status, ["invoice_number","order_code","supplier","material","status"]);
    body.innerHTML = visible.map(invoice => `
      <tr>
        <td class="code">${escapeHtml(invoice.invoice_number)}</td>
        <td>${escapeHtml(invoice.order_code)}</td>
        <td class="material">${escapeHtml(invoice.supplier)}</td>
        <td>${escapeHtml(invoice.material)}</td>
        <td>${formatMoney(invoice.total)}</td>
        <td>${escapeHtml(invoice.issue_date)}</td>
        <td>${escapeHtml(invoice.due_date)}</td>
        <td><span class="pill ${invoice.status === "Paid" ? "green" : "amber"}">${escapeHtml(invoice.status)}</span></td>
        <td><button type="button" class="action-btn" data-export-invoice="${invoice.id}">↓ PDF</button>${invoice.status !== "Paid" ? ` <button type="button" class="action-btn" data-mark-invoice-paid="${invoice.id}">Mark paid</button>` : ""}</td>
      </tr>`).join("") || '<tr><td colspan="9" class="empty-state">No invoices match this view. Generate one from an approved purchase order.</td></tr>';
  } catch (error) {
    body.innerHTML = `<tr><td colspan="9">${escapeHtml(error.message)}</td></tr>`;
  }
}
async function createInvoice(orderId) {
  if (!window.confirm("Generate an invoice from this approved purchase order? The invoice will use the approved PO quantities and totals.")) return;
  try {
    const response = await apiFetch(`${API}/orders/${encodeURIComponent(orderId)}/invoice`, {method:"POST"});
    const raw = await response.text();
    let data;
    try { data = raw ? JSON.parse(raw) : {}; }
    catch { throw new Error(`Invoice request returned an unexpected server response (HTTP ${response.status}). Check the invoice register before retrying to avoid duplicates.`); }
    if (!response.ok) throw new Error(data.detail || `Could not create invoice (HTTP ${response.status})`);
    showToast("Invoice " + data.invoice_number + " created");
    await fetchDashboard();
    document.querySelector('.nav-item[data-page="invoices"]')?.click();
  } catch (error) { showToast(error.message, true); }
}
async function exportInvoice(invoiceId) {
  const popup = window.open("", "_blank");
  if (!popup) return showToast("Allow pop-ups to create the invoice PDF.", true);
  popup.document.write("<!doctype html><title>Preparing invoice…</title><p style='font:16px Arial;padding:24px'>Preparing invoice PDF…</p>");
  try {
    const response = await apiFetch(`${API}/invoices/${encodeURIComponent(invoiceId)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not load invoice");
    const inv = data.invoice, safe = value => escapePdfHtml(value ?? "—");
    const money = value => formatMoney(Number(value || 0));
    const dateText = value => value ? new Date(value + "T00:00:00").toLocaleDateString("en-GB",{day:"2-digit",month:"long",year:"numeric"}) : "—";
    const subtotal = Number(inv.quantity || 0) * Number(inv.unit_price || 0);
    const html = `<!doctype html><html><head><meta charset="utf-8"><title>${safe(inv.invoice_number)} - ProcureAI</title><style>
      @page{size:A4;margin:18mm}*{box-sizing:border-box}body{font:12px Arial,sans-serif;color:#17251f;margin:0}
      header{display:flex;justify-content:space-between;gap:20px;border-bottom:3px solid #203c32;padding-bottom:18px;margin-bottom:22px}
      .brand{font-size:19px;font-weight:800}.brand small{display:block;font-size:9px;color:#64736c;letter-spacing:.12em;margin-top:5px}
      h1{font-size:24px;margin:0 0 8px;text-align:right}.meta{text-align:right;color:#64736c;line-height:1.7}
      .status{display:inline-block;border:1px solid ${inv.invoice_status === "Paid" ? "#27734d" : "#b87916"};color:${inv.invoice_status === "Paid" ? "#27734d" : "#94600d"};padding:6px 10px;font-weight:bold;font-size:10px}
      .parties{display:grid;grid-template-columns:1fr 1fr;gap:25px;margin:24px 0}.party h2{font-size:11px;color:#64736c;text-transform:uppercase;letter-spacing:.08em}.party b{display:block;font-size:14px;margin:7px 0}
      .facts{display:flex;gap:12px;margin:18px 0}.fact{background:#f1f5f2;padding:11px;flex:1}.fact small{display:block;color:#64736c;font-size:9px;text-transform:uppercase;margin-bottom:5px}
      table{width:100%;border-collapse:collapse;margin-top:10px}th{background:#edf2ef;text-align:left;font-size:9px;text-transform:uppercase}th,td{border:1px solid #d9e1dc;padding:11px 8px}td.num,th.num{text-align:right}
      .totals{width:48%;margin:14px 0 0 auto}.totals td{padding:8px}.totals tr:last-child{font-weight:bold;font-size:15px;background:#edf2ef}
      .notice{margin-top:24px;padding:12px;background:#f6f8f6;border:1px solid #dce5df;line-height:1.6}
      footer{border-top:1px solid #d9e1dc;margin-top:36px;padding-top:10px;color:#64736c;font-size:9px}
      @media print{body{-webkit-print-color-adjust:exact;print-color-adjust:exact}}</style></head><body>
      <header><div class="brand">${safe(inv.supplier)}<small>SUPPLIER INVOICE</small></div><div><h1>INVOICE</h1><span class="status">${safe(String(inv.invoice_status).toUpperCase())}</span><div class="meta"><b>${safe(inv.invoice_number)}</b><br>Issue date: ${safe(dateText(inv.issue_date))}<br>Due date: ${safe(dateText(inv.due_date))}</div></div></header>
      <div class="parties"><section class="party"><h2>Bill to</h2><b>${safe(inv.buyer_name)}</b>Procurement / accounts payable<br>${safe(inv.site)}</section><section class="party"><h2>From</h2><b>${safe(inv.supplier)}</b>${safe(inv.supplier_contact)}<br>${safe(inv.supplier_address || inv.supplier_city)}<br>${safe(inv.supplier_email)}<br>${safe(inv.supplier_phone)}</section></div>
      <div class="facts"><div class="fact"><small>Purchase order</small><b>${safe(inv.order_code)}</b></div><div class="fact"><small>Purchase request</small><b>${safe(inv.request_code)}</b></div><div class="fact"><small>Delivery required</small><b>${safe(dateText(inv.required_by))}</b></div></div>
      <h2>Invoice details</h2><table><thead><tr><th>Description</th><th class="num">Quantity</th><th class="num">Unit price</th><th class="num">Amount</th></tr></thead><tbody><tr><td>${safe(inv.material)}<br><small>${safe(inv.category)}</small></td><td class="num">${Number(inv.quantity).toLocaleString()} ${safe(inv.unit)}</td><td class="num">${money(inv.unit_price)}</td><td class="num">${money(subtotal)}</td></tr></tbody></table>
      <table class="totals"><tr><td>Subtotal</td><td class="num">${money(subtotal)}</td></tr><tr><td>Tax</td><td class="num">${money(inv.tax)}</td></tr><tr><td>Freight / delivery</td><td class="num">${money(inv.freight)}</td></tr><tr><td>Total due</td><td class="num">${money(inv.total)}</td></tr></table>
      <div class="notice"><b>Payment terms</b><br>Payment is due by ${safe(dateText(inv.due_date))}. This invoice references approved purchase order ${safe(inv.order_code)}. Please quote the invoice number when making payment.</div>
      <footer>Generated by ProcureAI from the approved purchase order. Verify supplier payment instructions independently before transferring funds.</footer>
      <script>window.onload=function(){window.print();};<\/script></body></html>`;
    popup.document.open(); popup.document.write(html); popup.document.close();
  } catch (error) {
    popup.document.body.innerHTML = "<p style='font:16px Arial;padding:24px'>" + escapePdfHtml(error.message) + "</p>";
    showToast(error.message, true);
  }
}
document.getElementById("invoiceSearch")?.addEventListener("input", loadInvoices);
document.getElementById("invoiceStatusFilter")?.addEventListener("change", loadInvoices);
document.addEventListener("click", async event => {
  const create = event.target.closest("[data-create-invoice]");
  if (create) return createInvoice(create.dataset.createInvoice);
  const open = event.target.closest("[data-open-invoice]");
  if (open) return exportInvoice(open.dataset.openInvoice);
  const exportButton = event.target.closest("[data-export-invoice]");
  if (exportButton) return exportInvoice(exportButton.dataset.exportInvoice);
  const paid = event.target.closest("[data-mark-invoice-paid]");
  if (paid) {
    if (!confirm("Mark this invoice as paid?")) return;
    try {
      const response = await apiFetch(`${API}/invoices/${encodeURIComponent(paid.dataset.markInvoicePaid)}/paid`, {method:"POST"});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not update invoice");
      showToast("Invoice marked as paid"); await fetchDashboard(); await loadInvoices();
    } catch (error) { showToast(error.message, true); }
  }
});

async function exportPurchaseOrder(orderId) {
  const popup = window.open("", "_blank");
  if (!popup) return showToast("Allow pop-ups to export this purchase order.", true);
  popup.document.write("<!doctype html><title>Preparing purchase order…</title><p style='font:16px Arial;padding:24px'>Preparing purchase order PDF…</p>");
  try {
    const response = await apiFetch(`${API}/orders/${encodeURIComponent(orderId)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not load purchase order");
    const po = data.order;
    const status = String(po.status || "Pending approval");
    const approved = status.toLowerCase() === "approved";
    const rejected = status.toLowerCase() === "rejected";
    const documentTitle = approved ? "PURCHASE ORDER" : rejected ? "REJECTED — NOT VALID FOR SUPPLY" : "PURCHASE ORDER PROPOSAL";
    const safe = value => escapePdfHtml(value ?? "—");
    const money = value => formatMoney(Number(value || 0));
    const dateText = value => value ? new Date(value).toLocaleDateString("en-GB",{day:"2-digit",month:"long",year:"numeric"}) : "—";
    const subtotal = Number(po.quantity||0)*Number(po.unit_price||0);
    const html = `<!doctype html><html><head><meta charset="utf-8"><title>${safe(po.code)} - ProcureAI</title><style>
    @page{size:A4;margin:17mm}*{box-sizing:border-box}body{font:12px Arial,sans-serif;color:#17251f;margin:0}
    header{display:flex;justify-content:space-between;gap:24px;align-items:flex-start;border-bottom:3px solid #203c32;padding-bottom:18px;margin-bottom:22px}
    .brand{font-size:20px;font-weight:800}.brand small{display:block;font-size:9px;color:#64736c;letter-spacing:.14em;margin-top:5px}
    h1{font-size:22px;margin:0 0 8px;text-align:right}.status{font-size:10px;font-weight:800;letter-spacing:.08em;padding:7px 10px;border:1px solid ${approved?"#27734d":rejected?"#a33":"#b87916"};color:${approved?"#27734d":rejected?"#a33":"#94600d"};display:inline-block}
    .meta{text-align:right;color:#64736c;font-size:11px;line-height:1.7;margin-top:8px}.notice{border:1px solid ${approved?"#cbd5cf":"#e7c98e"};background:${approved?"#f4f8f5":"#fff9eb"};padding:11px 13px;margin:0 0 20px;font-size:11px;line-height:1.5}
    h2{font-size:13px;margin:0 0 9px;color:#203c32;text-transform:uppercase;letter-spacing:.06em}.parties{display:grid;grid-template-columns:1fr 1fr;gap:26px;margin-bottom:22px}.party{line-height:1.65}.party b{display:block;font-size:13px;margin-bottom:3px}
    .facts{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:18px 0 22px}.fact{background:#f4f7f5;padding:10px}.fact small{display:block;color:#64736c;font-size:9px;text-transform:uppercase;margin-bottom:5px}.fact b{font-size:11px}
    table{width:100%;border-collapse:collapse;margin-top:8px}th{background:#edf2ef;text-align:left;font-size:9px;text-transform:uppercase}th,td{border:1px solid #d9e1dc;padding:10px 8px;vertical-align:top}td.num,th.num{text-align:right}
    .totals{width:48%;margin:14px 0 0 auto}.totals td{padding:7px 9px}.totals tr:last-child{font-weight:800;font-size:14px;background:#edf2ef}
    .terms{margin-top:24px;font-size:11px;line-height:1.6}.terms p{margin:5px 0;color:#43534b}.signatures{display:grid;grid-template-columns:1fr 1fr;gap:45px;margin-top:48px}.signature{border-top:1px solid #87958e;padding-top:8px;color:#64736c;font-size:10px}
    footer{border-top:1px solid #d9e1dc;margin-top:28px;padding-top:9px;color:#64736c;font-size:9px;line-height:1.5}@media print{body{-webkit-print-color-adjust:exact;print-color-adjust:exact}}</style></head><body>
    <header><div class="brand">GESTRO GLOBAL CONSULT LTD<small>PROCUREMENT & INDUSTRIAL OPERATIONS</small></div><div><h1>${documentTitle}</h1><div style="text-align:right"><span class="status">${safe(status.toUpperCase())}</span></div><div class="meta"><b>${safe(po.code)}</b><br>Created: ${safe(dateText(po.created_at))}<br>Required by: ${safe(dateText(po.required_by))}</div></div></header>
    ${!approved ? `<div class="notice"><b>${rejected?"REJECTED DOCUMENT":"DRAFT — PENDING INTERNAL APPROVAL"}</b><br>${rejected?"This order has been rejected and is not valid for supply.":"This is an internal purchase-order proposal only. It is not an approved purchase order, award notice, authority to supply, or commitment to pay. Do not commence work or dispatch goods."}</div>` : '<div class="notice"><b>APPROVED PURCHASE ORDER</b><br>This purchase order has been approved internally. Supplier acceptance and applicable contract terms remain subject to the organisation’s procurement process.</div>'}
    <div class="parties"><section><h2>Supplier</h2><div class="party"><b>${safe(po.supplier)}</b>${safe(po.supplier_contact)}<br>${safe(po.supplier_address || po.supplier_city)}<br>${safe(po.supplier_email)}<br>${safe(po.supplier_phone)}</div></section><section><h2>Deliver to / Requesting site</h2><div class="party"><b>Gestro Global Consult Ltd</b>${safe(po.site)}<br>Required delivery: ${safe(dateText(po.required_by))}<br>Priority: ${safe(po.priority)}</div></section></div>
    <div class="facts"><div class="fact"><small>Purchase request</small><b>${safe(po.request_code)}</b></div><div class="fact"><small>Material / equipment</small><b>${safe(po.material)}</b></div><div class="fact"><small>Category</small><b>${safe(po.category)}</b></div></div>
    <h2>Order details</h2><table><thead><tr><th>Description</th><th class="num">Quantity</th><th class="num">Unit price</th><th class="num">Line subtotal</th></tr></thead><tbody><tr><td>${safe(po.material)}<br><small>${safe(po.request_code)} · ${safe(po.site)}</small></td><td class="num">${Number(po.quantity||0).toLocaleString()} ${safe(po.unit)}</td><td class="num">${money(po.unit_price)}</td><td class="num">${money(subtotal)}</td></tr></tbody></table>
    <table class="totals"><tbody><tr><td>Subtotal</td><td class="num">${money(subtotal)}</td></tr><tr><td>Tax (${safe(po.tax_rate ?? "—")}%)</td><td class="num">${money(po.tax)}</td></tr><tr><td>Freight / delivery</td><td class="num">${money(po.freight)}</td></tr><tr><td>Commercial total</td><td class="num">${money(po.total)}</td></tr></tbody></table>
    <div class="terms"><h2>Delivery & payment terms</h2><p><b>Delivery lead time:</b> ${safe(po.lead_days)} days</p><p><b>Payment terms:</b> Net ${safe(po.payment_days)} days</p><p><b>Supplier quotation note:</b> ${safe(po.quotation_note)}</p><p><b>Internal decision note:</b> ${safe(po.decision_note)}</p></div>
    ${approved ? '<div class="signatures"><div class="signature">Authorised by · Gestro Global Consult Ltd</div><div class="signature">Supplier acknowledgement · Name / Signature / Date</div></div>' : '<div class="signatures"><div class="signature">Prepared by · Procurement team</div><div class="signature">Reviewed / approved by · Authorised officer</div></div>'}
    <footer>ProcureAI · Confidential procurement document · Generated ${safe(new Date().toLocaleString())}. Verify quantities, specifications, delivery arrangements and commercial terms before approval.</footer><script>window.onload=function(){window.focus();window.print();};<\/script></body></html>`;
    popup.document.open(); popup.document.write(html); popup.document.close();
  } catch (error) { popup.close(); showToast(error.message || "Could not export purchase order.", true); }
}
async function openPurchaseRequestDetails(requestId) {
  const modal = document.getElementById("requestDetailModal");
  const content = document.getElementById("requestDetailContent");
  const title = document.getElementById("requestDetailTitle");
  if (!modal || !content) return;
  content.innerHTML = '<div class="request-detail-loading">Loading purchase request…</div>';
  title.textContent = "Purchase request details";
  modal.classList.add("open");
  try {
    const response = await apiFetch(`${API}/requests/${encodeURIComponent(requestId)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not load purchase request");
    const r = data.request;
    title.textContent = r.code || "Purchase request details";
    const displayDate = value => {
      if (!value) return "Not recorded";
      const parsed = new Date(value);
      return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString(undefined,{dateStyle:"medium",timeStyle:"short"});
    };
    const detailSections = [
      {title:"Request overview",description:"The item and classification being procured.",fields:[
        ["Request reference",r.code],["Material / item",r.material],["Category",r.category],["Quantity",`${Number(r.quantity).toLocaleString()} ${r.unit || ""}`]
      ]},
      {title:"Project & delivery",description:"Where and when the requirement is needed.",fields:[
        ["Project workspace",r.project_title || "Project details unavailable"],["Delivery location / site",r.site],["Required delivery date",displayDate(r.required_by)]
      ]},
      {title:"Request tracking",description:"Current workflow state and record history.",fields:[
        ["Current status",r.status],["Priority",r.priority],["Date created",displayDate(r.created_at)]
      ]}
    ];
    const statusClass = String(r.status || "").toLowerCase() === "open" ? "blue" : "green";
    content.innerHTML = `
      <div class="request-detail-hero">
        <div class="request-detail-title-block"><span class="eyebrow">PURCHASE REQUEST · ${escapeHtml(r.code || "UNREFERENCED")}</span><h3>${escapeHtml(r.material || "Untitled request")}</h3><p>${escapeHtml(r.project_title || "Unassigned project")} <span aria-hidden="true">·</span> ${escapeHtml(r.site || "Site not provided")}</p></div>
        <span class="pill ${statusClass}">${escapeHtml(r.status || "Unknown")}</span>
      </div>
      ${detailSections.map(section=>`
        <section class="request-detail-section" aria-label="${escapeHtml(section.title)}">
          <div class="request-detail-section-heading"><h4>${escapeHtml(section.title)}</h4><p>${escapeHtml(section.description)}</p></div>
          <div class="request-detail-grid">${section.fields.map(([label,value])=>`<div class="request-detail-field"><span>${escapeHtml(label)}</span><b>${escapeHtml(value ?? "Not provided")}</b></div>`).join("")}</div>
        </section>`).join("")}
      <div class="request-detail-note"><span class="request-note-icon" aria-hidden="true">i</span><div><b>Next procurement step</b><p>Collect supplier offers against this request in RFQs & quotations. Buyer confirmation and approval remain part of the project workflow.</p></div></div>
      <div class="request-detail-actions"><button type="button" class="btn btn-ghost" data-close>Close details</button><button type="button" class="btn btn-primary" data-open-quotes>View quotations</button></div>`;
    content.querySelector("[data-close]")?.addEventListener("click", () => modal.classList.remove("open"));
  } catch (error) {
    content.innerHTML = `<div class="request-detail-error"><b>Request details unavailable</b><p>${escapeHtml(error.message)}</p></div>`;
  }
}
document.getElementById("requestDetailContent")?.addEventListener("click", event => {
  if (!event.target.closest("[data-open-quotes]")) return;
  document.getElementById("requestDetailModal")?.classList.remove("open");
  document.querySelector('.nav-item[data-page="quotes"]')?.click();
});
document.getElementById("allRequests")?.addEventListener("click", event => {
  const open = event.target.closest("[data-open-request]");
  if (open) openPurchaseRequestDetails(open.dataset.openRequest);
});
function renderDashboard() {
  const { metrics, requests, suppliers, orders, recent_quotes } = dashboardData;
  // Metrics
  document.getElementById("mRequests").textContent = metrics.open_requests;
  document.getElementById("mSuppliers").textContent = metrics.suppliers;
  document.getElementById("mApprovals").textContent = metrics.pending_approvals;
  document.getElementById("mSpend").textContent = formatMoney(metrics.approved_spend);
  document.getElementById("requestCount").textContent = metrics.open_requests;
  document.getElementById("approvalCount").textContent = metrics.pending_approvals;
  // Recent Requests
  const reqBody = document.getElementById("recentRequests");
  reqBody.innerHTML = requests.slice(0, 5).map(r => `
    <tr>
      <td class="code">${r.code}</td>
      <td class="material">${r.material}</td>
      <td>${Number(r.quantity).toLocaleString()} ${r.unit}</td>
      <td>${r.required_by}</td>
      <td><span class="pill ${r.priority === 'Urgent' ? 'red' : 'gray'}">${r.priority}</span></td>
      <td><span class="pill ${r.status === 'Open' ? 'blue' : 'green'}">${r.status}</span></td>
    </tr>
  `).join("");
  // All Requests
  const visibleRequests = filterRecords(requests, document.getElementById("requestSearch")?.value, document.getElementById("requestStatusFilter")?.value, ["code","material","category","site","status"]);
  document.getElementById("allRequests").innerHTML = visibleRequests.map(r => `
    <tr>
      <td class="code">${r.code}</td>
      <td class="material">${r.material}</td>
      <td>${r.category}</td>
      <td>${Number(r.quantity).toLocaleString()} ${r.unit}</td>
      <td>${r.site}</td>
      <td>${r.required_by}</td>
      <td><span class="pill ${r.priority === 'Urgent' ? 'red' : 'gray'}">${r.priority}</span></td>
      <td><span class="pill ${r.status === 'Open' ? 'blue' : 'green'}">${escapeHtml(r.status)}</span></td>
      <td class="request-row-actions"><button type="button" class="action-btn request-open-btn" data-open-request="${r.id}">Open</button><button type="button" class="action-btn reject" data-delete-request="${r.id}" data-request-code="${escapeHtml(r.code)}">Delete</button></td>
    </tr>
  `).join("");
  // Suppliers
  document.getElementById("topSuppliers").innerHTML = suppliers.slice(0, 4).map(s => `
    <div class="supplier-row">
      <div class="supplier-logo">${escapeHtml(s.name.substring(0,2).toUpperCase())}</div>
      <div class="supplier-info">
        <b>${escapeHtml(s.name)} ${s.is_demo ? '<span class="pill amber">DEMO</span>' : ''}</b>
        <small>${escapeHtml(s.city)} · ${Number(s.on_time || 0) > 0 ? Number(s.on_time).toFixed(0) + "% on-time" : "No delivery history recorded"}</small>
      </div>
      <div class="rating">${Number(s.rating || 0) > 0 ? "★ " + Number(s.rating).toFixed(1) : "Not rated"}</div>
    </div>
  `).join("");
  const selectedSupplierCategory = document.getElementById("supplierCategoryFilter")?.value || "";
  const visibleSuppliers = filterRecords(suppliers, document.getElementById("supplierSearch")?.value, document.getElementById("supplierStatusFilter")?.value, ["name","city","contact","email","phone","services","status"])
    .filter(s => supplierMatchesCategory(s, selectedSupplierCategory));
  document.getElementById("supplierCards").innerHTML = visibleSuppliers.map(s => `
    <div class="supplier-card">
      <div class="supplier-card-top">
        <div class="supplier-logo">${escapeHtml(s.name.substring(0,2).toUpperCase())}</div>
        <span class="rating">${Number(s.rating || 0) > 0 ? "★ " + Number(s.rating).toFixed(1) : "Not rated"}</span>
      </div>
      <h3>${escapeHtml(s.name)} ${s.is_demo ? '<span class="pill amber">DEMO DATA</span>' : ''}</h3>
      <p>${escapeHtml(s.city)} · ${escapeHtml(s.phone || s.email || s.contact || "No contact supplied")}</p>
      <p class="supplier-capabilities">${escapeHtml(s.services || "Services not yet listed")}</p>
      <p class="supplier-capabilities">${escapeHtml([s.email,s.website,s.address].filter(Boolean).join(" · "))}</p>
      ${s.contact ? `<p class="supplier-source-note"><b>Source / verification note:</b> ${escapeHtml(s.contact)}</p>` : ""}
      <div class="supplier-stats">
        <div><b>${Number(s.on_time || 0) > 0 ? Number(s.on_time).toFixed(0) + "%" : "No history"}</b><small>On-time</small></div>
        <div><b>${escapeHtml(s.status)}</b><small>Status</small></div>
      </div>
      <div class="supplier-card-actions"><button type="button" class="action-btn edit-seller-btn" data-edit-seller="${s.id}" aria-label="Edit ${escapeHtml(s.name)}" title="Edit seller"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L8 18l-4 1 1-4Z"/></svg><span>Edit details</span></button><button type="button" class="action-btn reject" data-delete-seller="${s.id}" aria-label="Delete ${escapeHtml(s.name)}"><span>Delete seller</span></button></div>
    </div>
  `).join("");
  // Recent Quotes
  document.getElementById("recentQuotes").innerHTML = recent_quotes.slice(0, 5).map(q => `
    <div class="quote-item" role="button" tabindex="0" data-open-quote="${q.id}">
      <div>
        <b>${q.supplier} ${q.is_demo ? '<span class="pill amber">DEMO</span>' : ''}</b>
        <small>${q.material} (${q.request_code})</small>
      </div>
      <div class="quote-price">${formatMoney(q.unit_price)}</div>
    </div>
  `).join("");
  // All Quotes Table
  const visibleQuotes = filterRecords(quoteRecords, document.getElementById("quoteSearch")?.value, "", ["supplier","request_code","material","category","note"]);
  document.getElementById("allQuotes").innerHTML = visibleQuotes.map(q => `
    <tr>
      <td class="material">${q.supplier}</td>
      <td class="code">${q.request_code}</td>
      <td>${q.material}</td>
      <td>${formatMoney(q.unit_price)}</td>
      <td>${formatMoney(q.freight)}</td>
      <td>${q.lead_days} days</td>
      <td>Net ${q.payment_days}</td>
      <td class="quote-actions"><button class="action-btn" data-open-quote="${q.id}">Open</button>
        ${q.has_purchase_order ? '<span class="pill gray">PO linked · locked</span>' : `<button type="button" class="icon-action" data-edit-quote="${q.id}" aria-label="Edit quotation ${q.id}" title="Edit quotation">✎</button><button type="button" class="action-btn reject" data-delete-quote="${q.id}">Delete</button>`}</td>
    </tr>
  `).join("");
  // Orders Table
  const visibleOrders = filterRecords(orders, document.getElementById("orderSearch")?.value, document.getElementById("orderStatusFilter")?.value, ["code","supplier","material","status"]);
  document.getElementById("allOrders").innerHTML = visibleOrders.map(o => `
    <tr>
      <td class="code">${o.code}</td>
      <td class="material">${o.supplier}</td>
      <td>${o.material}</td>
      <td>${formatMoney(o.total)}</td>
      <td>${o.required_by}</td>
      <td><span class="pill ${o.status === 'Approved' ? 'green' : (o.status === 'Rejected' ? 'red' : 'amber')}">${o.status}</span></td>
      <td>
        <button type="button" class="action-btn" data-export-po="${o.id}">↓ Export PDF</button>
        ${o.status === 'Approved' ? (invoiceRecords.find(i => Number(i.order_id) === Number(o.id)) ? `<button type="button" class="action-btn" data-open-invoice="${invoiceRecords.find(i => Number(i.order_id) === Number(o.id)).id}">View invoice</button>` : `<button type="button" class="action-btn" data-create-invoice="${o.id}">＋ Create invoice</button>`) : ''}
        ${o.status === 'Pending approval' ? `
          <button class="action-btn" onclick="decideOrder(${o.id}, 'approve')">Approve</button>
          <button class="action-btn reject" onclick="decideOrder(${o.id}, 'reject')">Reject</button>
        ` : ''}
      </td>
    </tr>
  `).join("");
  // Populate analysis dropdown
  const analysisSelect = document.getElementById("analysisRequest");
  if (analysisSelect.options.length === 0) {
    analysisSelect.innerHTML = requests.filter(r => r.status === "Open").map(r =>
      `<option value="${r.id}">${r.code} - ${r.material}</option>`
    ).join("");
  }
}
document.addEventListener("click", async event => {
  const unlinkButton = event.target.closest("[data-unlink-project-quote]");
  if (unlinkButton && activeProjectId) {
    if (!confirm("Unlink this quotation from the project? The quotation itself will remain unchanged.")) return;
    try {
      const response = await apiFetch(`${API}/projects/${encodeURIComponent(activeProjectId)}/quotations/${unlinkButton.dataset.unlinkProjectQuote}`, {method:"DELETE"});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not unlink quotation");
      renderProjectWorkspace(data.project);
      showToast("Quotation unlinked from project");
    } catch (error) { showToast(error.message, true); }
    return;
  }
  const deleteSellerButton = event.target.closest("[data-delete-seller]");
  if (deleteSellerButton) {
    const seller = (dashboardData.suppliers || []).find(item => Number(item.id) === Number(deleteSellerButton.dataset.deleteSeller));
    if (!seller) return showToast("Seller record could not be found. Refresh and try again.", true);
    if (!confirm("Delete seller " + seller.name + "? This is only allowed when no quotations or purchase orders are linked. This action cannot be undone.")) return;
    try {
      const response = await apiFetch(API + "/suppliers/" + encodeURIComponent(seller.id), {method:"DELETE"});
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Could not delete seller");
      showToast("Seller " + result.deleted_supplier + " deleted");
      await fetchDashboard();
    } catch (error) { showToast(error.message, true); }
    return;
  }
  const editButton = event.target.closest("[data-edit-seller]");
  if (!editButton) return;
  const seller = (dashboardData.suppliers || []).find(item => Number(item.id) === Number(editButton.dataset.editSeller));
  if (!seller) return showToast("Seller record could not be found. Refresh and try again.", true);
  const form = document.getElementById("supplierForm");
  form.reset();
  ["name","city","contact","email","phone","website","address","services","rating","on_time","status"].forEach(key => {
    if (form.elements[key]) form.elements[key].value = seller[key] ?? "";
  });
  form.dataset.sellerId = String(seller.id);
  document.getElementById("supplierModalTitle").textContent = "Edit seller information";
  document.getElementById("supplierModalDescription").textContent = "Update this seller's contact details, technical capabilities and verification status.";
  setFormSubmitLabel(form, "Save changes");
  document.getElementById("supplierModal").classList.add("open");
});

// --- Administrator request and quotation controls ---
document.addEventListener("click", event => {
  const exportPoButton = event.target.closest("[data-export-po]");
  if (exportPoButton) exportPurchaseOrder(exportPoButton.dataset.exportPo);
  const deleteButton = event.target.closest("[data-delete-request]");
  if (deleteButton) window.deleteRequest(deleteButton.dataset.deleteRequest, deleteButton.dataset.requestCode);
  const quoteButton = event.target.closest("[data-open-quote]");
  if (quoteButton) window.openQuote(quoteButton.dataset.openQuote);
  const editQuoteButton = event.target.closest("[data-edit-quote]");
  if (editQuoteButton) window.editQuote(editQuoteButton.dataset.editQuote);
  const deleteQuoteButton = event.target.closest("[data-delete-quote]");
  if (deleteQuoteButton) window.deleteQuote(deleteQuoteButton.dataset.deleteQuote);
});
window.deleteRequest = async function(id, code) {
  if (!confirm("Delete purchase request " + code + "? Linked quotations and purchase orders will also be deleted. This cannot be undone.")) return;
  const response = await apiFetch(API + "/requests/" + id, { method: "DELETE" });
  const data = await response.json();
  if (!response.ok) return showToast(data.detail || data.error || "Could not delete request", true);
  showToast("Request " + data.deleted_request + " deleted");
  await fetchDashboard();
};

window.editQuote = async function(id) {
  try {
    const response = await apiFetch(API + "/quotes/" + id), quote = await response.json();
    if (!response.ok) throw new Error(quote.detail || "Could not load quotation");
    if (quote.has_purchase_order) throw new Error("This quotation is locked because a purchase order is linked to it.");
    const form = document.getElementById("quoteForm"); form.reset();
    document.getElementById("quoteRequest").innerHTML = (dashboardData.requests || []).map(r => `<option value="${r.id}">${escapeHtml(r.code)} - ${escapeHtml(r.material)}</option>`).join("");
    document.getElementById("quoteSupplier").innerHTML = (dashboardData.suppliers || []).map(s => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join("");
    ["request_id","supplier_id","unit_price","freight","tax_rate","lead_days","payment_days","note"].forEach(key => { if (form.elements[key]) form.elements[key].value = quote[key] ?? ""; });
    form.dataset.quoteId = String(id);
    { const node = document.getElementById("quoteModalTitle"); if (node) node.textContent = "Edit registered quotation"; }
    { const node = document.getElementById("quoteModalDescription"); if (node) node.textContent = "Any previous seller request email will be reset and must be sent again before PO generation."; }
    setFormSubmitLabel(form, "Save changes");
    document.getElementById("quoteDetailModal").classList.remove("open");
    document.getElementById("quoteModal").classList.add("open");
  } catch (error) { showToast(error.message, true); }
};
window.deleteQuote = async function(id) {
  if (!confirm("Delete quotation #" + id + "? This cannot be undone. Quotations linked to a purchase-order proposal are protected.")) return;
  try {
    const response = await apiFetch(API + "/quotes/" + id, {method:"DELETE"}), result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Could not delete quotation");
    document.getElementById("quoteDetailModal").classList.remove("open");
    showToast("Quotation deleted"); await fetchDashboard();
  } catch (error) { showToast(error.message, true); }
};
window.openQuote = async function(id) {
  const modal = document.getElementById("quoteDetailModal");
  const content = document.getElementById("quoteDetailContent");
  document.getElementById("quoteDetailTitle").textContent = "Quotation #" + id;
  content.textContent = "Loading quotation details…";
  modal.classList.add("open");
  try {
    const response = await apiFetch(API + "/quotes/" + id);
    const q = await response.json();
    if (!response.ok) throw new Error(q.detail || "Quotation could not be loaded");
    const fields = [["Supplier",q.supplier],["Supplier contact",q.supplier_contact || "Not provided"],["Supplier location",q.supplier_city || "Not provided"],["Linked request",q.request_code],["Material / equipment",q.material],["Category",q.category],["Quantity",Number(q.quantity).toLocaleString() + " " + q.unit],["Project / site",q.site],["Required by",q.required_by],["Priority",q.priority],["Request status",q.request_status],["Unit price",formatMoney(q.unit_price)],["Freight",formatMoney(q.freight)],["Tax rate",q.tax_rate + "%"],["Tax amount",formatMoney(q.tax_amount)],["Subtotal",formatMoney(q.subtotal)],["Total landed",formatMoney(q.total_landed)],["Lead time",q.lead_days + " days"],["Payment terms","Net " + q.payment_days + " days"],["Technical compliance",q.technical_status || "pending"],["Technical review notes",q.technical_notes || "Not reviewed"],["Reviewed by",q.technical_reviewed_by || "Not reviewed"],["Review date",q.technical_reviewed_at || "—"],["Quotation date",new Date(q.created_at).toLocaleString()],["Notes",q.note || "None"]];
    content.innerHTML = "<div class=\"quote-detail-grid\">" + fields.map(pair => "<div class=\"quote-detail-field\"><small>" + pair[0] + "</small><b>" + escapeHtml(String(pair[1] ?? "—")) + "</b></div>").join("") + "</div>" + (q.has_purchase_order ? '<p class="quote-lock-note">This quotation is locked because a purchase-order proposal is linked to it.</p>' : '<div class="quote-detail-actions"><button type="button" class="btn btn-ghost" data-edit-quote="' + q.id + '">✎ Edit quotation</button><button type="button" class="btn btn-ghost reject" data-delete-quote="' + q.id + '">Delete quotation</button></div>');
    if (!q.has_purchase_order) {
      const reviewForm = document.createElement("form");
      reviewForm.className = "technical-review-form";
      const heading = document.createElement("strong");
      heading.textContent = "Technical compliance review";
      const help = document.createElement("p");
      help.textContent = "Compare the supplier's datasheets, ratings, quantities and deviations against the purchase request. A quotation cannot progress to a purchase order until marked compliant.";
      const statusLabel = document.createElement("label");
      statusLabel.textContent = "Review outcome";
      const statusSelect = document.createElement("select");
      statusSelect.name = "status";
      [["pending","Pending review"],["compliant","Compliant"],["deviation","Deviation — buyer decision required"],["non_compliant","Non-compliant"]].forEach(([value,label]) => {
        const option = document.createElement("option"); option.value = value; option.textContent = label; statusSelect.append(option);
      });
      statusSelect.value = q.technical_status || "pending";
      statusLabel.append(statusSelect);
      const notesLabel = document.createElement("label");
      notesLabel.textContent = "Evidence and review notes";
      const notes = document.createElement("textarea");
      notes.name = "notes"; notes.rows = 3; notes.maxLength = 5000;
      notes.placeholder = "Record the datasheet / standard checked, ratings, scope match and any deviations.";
      notes.value = q.technical_notes || "";
      notesLabel.append(notes);
      const submit = document.createElement("button");
      submit.type = "submit"; submit.className = "btn btn-primary"; submit.textContent = "Save technical review";
      reviewForm.append(heading, help, statusLabel, notesLabel, submit);
      reviewForm.addEventListener("submit", async event => {
        event.preventDefault(); submit.disabled = true;
        try {
          const response = await apiFetch(API + "/quotes/" + id + "/technical-review", {
            method:"POST", headers:{"Content-Type":"application/json"},
            body:JSON.stringify({status:statusSelect.value,notes:notes.value})
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.detail || "Could not save technical review");
          showToast("Technical review saved");
          await window.openQuote(id);
          await fetchDashboard();
        } catch(error) { showToast(error.message,true); }
        finally { submit.disabled = false; }
      });
      content.append(reviewForm);
    }
  } catch (error) { content.textContent = error.message; }
};

// --- Actions ---
window.decideOrder = async function(id, action) {
  if (!confirm(`Are you sure you want to ${action} this order?`)) return;
  const res = await apiFetch(`${API}/orders/${id}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, note: "Approved via dashboard" })
  });
  if (res.ok) {
    showToast(`Order ${action}d successfully`);
    fetchDashboard();
  } else {
    const err = await res.json();
    showToast(err.error, true);
  }
};
let latestAnalysis = null;
let selectedPoQuoteId = null;
let supplierRequestEmailSent = false;
function setPoEmailState(sent, recipient) {
  supplierRequestEmailSent = Boolean(sent);
  const sendButton = document.getElementById("sendSupplierRequest");
  const generateButton = document.getElementById("confirmGeneratePo");
  const status = document.getElementById("supplierEmailStatus");
  if (sendButton) { sendButton.disabled = supplierRequestEmailSent; sendButton.textContent = supplierRequestEmailSent ? "Request emailed" : "Email request to seller"; }
  if (generateButton) generateButton.disabled = !supplierRequestEmailSent;
  if (status) status.textContent = supplierRequestEmailSent ? "Request email sent to " + recipient + ". You can now generate the PO proposal." : "The seller must receive this request email before a PO proposal can be generated.";
}
async function openPoPreview(quoteId) {
  const offer = latestAnalysis?.offers?.find(item => Number(item.id) === Number(quoteId));
  if (!offer) return showToast("Run the analysis again before generating a PO.", true);
  if (!["verified", "active"].includes(String(offer.supplier_status || "").toLowerCase()) || String(offer.supplier || "").toLowerCase().includes("[fictional demo]")) return showToast("Verify this seller before sending a request or generating a purchase order.", true);
  selectedPoQuoteId = Number(quoteId); setPoEmailState(false, "");
  const req = latestAnalysis.request, qty = Number(req.quantity);
  const subtotal = qty * Number(offer.unit_price), tax = subtotal * Number(offer.tax_rate) / 100, freight = Number(offer.freight);
  const items = [["Purchase request",req.code],["Equipment / material",req.material],["Category",req.category],["Quantity",qty.toLocaleString()+" "+req.unit],["Project / site",req.site],["Required by",req.required_by],["Selected supplier",offer.supplier],["Unit price",formatMoney(offer.unit_price)],["Subtotal",formatMoney(subtotal)],["Tax ("+offer.tax_rate+"%)",formatMoney(tax)],["Freight",formatMoney(freight)],["Commercial PO total",formatMoney(subtotal+tax+freight)],["Analysis estimated total",formatMoney(offer.estimated_total)],["Delivery lead time",offer.lead_days+" days"],["Payment terms","Net "+offer.payment_days+" days"]];
  document.getElementById("poPreviewContent").innerHTML = '<div class="po-preview-grid">'+items.map(([k,v])=>'<div class="po-preview-field"><small>'+escapeHtml(k)+'</small><b>'+escapeHtml(v)+'</b></div>').join("")+'</div><p class="po-preview-note">The commercial PO total uses the supplier’s quoted price, tax and freight. The analysis estimate is shown separately for decision support.</p><div class="po-email-step"><b>Step 1 · Send request to seller</b><p id="supplierEmailStatus">Checking seller email status…</p><button type="button" class="btn btn-ghost" id="sendSupplierRequest">Email request to seller</button></div><div class="po-approval-note"><span>Step 2 · Internal approval</span><b>The proposal will remain Pending approval. Emailing the request does not create a purchase commitment or authorize supply.</b></div>';
  document.getElementById("poPreviewModal").classList.add("open");
  try {
    const response = await apiFetch(API + "/quotes/" + selectedPoQuoteId); const detail = await response.json();
    if (!response.ok) throw new Error(detail.detail || "Could not check seller email status");
    setPoEmailState(detail.request_email_sent, detail.supplier_email || "seller");
    if (!detail.supplier_email) document.getElementById("supplierEmailStatus").textContent = "No seller email is saved. Update the seller record before sending.";
  } catch (error) { document.getElementById("supplierEmailStatus").textContent = error.message; }
  document.getElementById("sendSupplierRequest")?.addEventListener("click", async () => {
    const sendButton = document.getElementById("sendSupplierRequest"); sendButton.disabled = true; sendButton.textContent = "Sending request…";
    try {
      const sent = await apiFetch(API + "/quotes/" + selectedPoQuoteId + "/send-request", {method:"POST"}); const result = await sent.json();
      if (!sent.ok) throw new Error(result.detail || "Could not email the seller");
      setPoEmailState(true, result.recipient || "seller"); showToast(result.already_sent ? "Request email was already sent." : "Purchase request emailed to seller.");
    } catch (error) { sendButton.disabled = false; sendButton.textContent = "Email request to seller"; showToast(error.message, true); }
  }, { once:true });
}
document.getElementById("confirmGeneratePo").addEventListener("click", async () => {
  if (!selectedPoQuoteId || !supplierRequestEmailSent) return;
  const button=document.getElementById("confirmGeneratePo"); button.disabled=true; button.textContent="Creating proposal…";
  try {
    const response=await apiFetch(API+"/orders/from-analysis",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({quote_id:selectedPoQuoteId})});
    const data=await response.json(); if(!response.ok) throw new Error(data.detail||"Could not generate purchase order");
    document.getElementById("poPreviewModal").classList.remove("open"); selectedPoQuoteId=null;
    showToast("PO proposal "+data.code+" created — pending approval"); await fetchDashboard(); document.querySelector('[data-page="orders"]')?.click();
  } catch(error){showToast(error.message,true)}
  finally{button.textContent="Generate PO proposal";button.disabled=!supplierRequestEmailSent}
});
document.getElementById("runAnalysis").addEventListener("click", async () => {
  const rid = document.getElementById("analysisRequest").value;
  if (!rid) return;
  const button = document.getElementById("runAnalysis");
  const resultContainer = document.getElementById("analysisResult");
  button.disabled = true; button.textContent = "Reviewing bids…";
  resultContainer.innerHTML = '<div class="empty-state"><span>◈</span><h3>Evaluating procurement options</h3><p>Reviewing quotations, delivery risks and possible alternatives…</p></div>';
  try {
    const res = await apiFetch(`${API}/analysis?request_id=${encodeURIComponent(rid)}`);
    const responseText = await res.text();
    let data;
    try {
      data = responseText ? JSON.parse(responseText) : {};
    } catch (_) {
      const preview = responseText.replace(/\s+/g, " ").slice(0, 180);
      throw new Error(`The server returned an unreadable response (HTTP ${res.status}). ${preview || "Please retry in a moment."}`);
    }
    if (!res.ok) throw new Error(data.detail || data.error || `Could not analyse this purchase request (HTTP ${res.status})`);
    latestAnalysis = data;
    const rec = data.recommendation;
    const preferred = data.preferred_offer;
    const judgement = data.decision_support || {};
    const engineLabel = judgement.engine === "openai" ? "AI BID JUDGEMENT" : "PROCUREMENT RULE REVIEW";
    let html = `
      <section class="bid-judgement">
        <div class="bid-judgement-top"><span class="eyebrow">${engineLabel}</span><span class="pill ${rec ? "green" : "amber"}">${escapeHtml(judgement.recommended_action || "Review completed")}</span></div>
        <h3>${escapeHtml(judgement.overall_assessment || "The procurement review is complete.")}</h3>
        <p>${escapeHtml(judgement.solution || "Review the recommendations below and complete due diligence.")}</p>
        ${judgement.caveat ? `<small>${escapeHtml(judgement.caveat)}</small>` : ""}
      </section>`;
    if (!data.offers || data.offers.length === 0) {
      html += `<section class="panel bid-no-quotes"><h3>No quotations received yet</h3><p>ProcureAI has completed a sourcing-gap review instead of stopping here. Invite suitable sellers, request comparable quotations and then run the analysis again.</p><div class="bid-next-step"><b>Recommended next step</b><span>${escapeHtml(judgement.solution || "Invite additional sellers and request complete quotations.")}</span></div></section>`;
    } else {
      if (rec) {
        html += `<div class="recommendation"><div><span class="eyebrow">SHORTLIST FOR HUMAN REVIEW</span><h3>${escapeHtml(rec.supplier)} currently meets the basic checks</h3><p>${escapeHtml(data.disclaimer || "")}</p></div><div class="recommend-price">${formatMoney(rec.estimated_total)}<br><small style="font-size:10px;color:#73877f;font-weight:400">Estimated TCO</small></div></div>`;
      } else if (preferred) {
        html += `<div class="bid-caution"><b>No offer is currently award-eligible.</b><span>${escapeHtml(preferred.supplier)} is the closest candidate by the current comparison, but still needs the issues below resolved. This is not an award recommendation.</span></div>`;
      }
      html += `<div class="analysis-table"><table><thead><tr><th>SUPPLIER</th><th>UNIT PRICE</th><th>FREIGHT</th><th>LEAD TIME</th><th>CHECK</th><th>TCO (EST.)</th><th>NEXT ACTION</th></tr></thead><tbody>`;
      data.offers.forEach(o => {
        const eligible = !!o.award_eligible;
        const review = o.ai_review || {};
        const action = eligible ? `<button class="action-btn generate-po-btn" data-generate-po="${o.id}">Review PO</button>` : `<span class="po-unavailable">${!["verified","active"].includes(String(o.supplier_status||"").toLowerCase()) ? "Verify seller" : "Resolve gaps"}</span>`;
        html += `<tr><td class="material">${escapeHtml(o.supplier)}</td><td>${formatMoney(o.unit_price)}</td><td>${formatMoney(o.freight)}</td><td>${Number(o.lead_days)} days</td><td><span class="pill ${eligible ? "green" : (o.feasible ? "amber" : "red")}">${eligible ? "Eligible for review" : (o.feasible ? "Conditional" : "Needs action")}</span></td><td><b>${formatMoney(o.estimated_total)}</b></td><td>${action}</td></tr>`;
      });
      html += `</tbody></table></div>`;
      html += `<section class="bid-review-list"><div class="panel-heading"><div><h2>Seller-by-seller judgement</h2><p>What each bidder did well, what needs to improve and the next action.</p></div></div>`;
      data.offers.forEach(o => {
        const review = o.ai_review || {};
        const list = (items, fallback) => (Array.isArray(items) && items.length ? items : [fallback]).map(x => `<li>${escapeHtml(x)}</li>`).join("");
        html += `<article class="bid-seller-review"><div class="bid-seller-heading"><div><b>${escapeHtml(o.supplier)}</b><small>Quotation #${o.id}</small></div><span class="pill ${review.risk_level === "low" ? "green" : review.risk_level === "high" ? "red" : "amber"}">${escapeHtml((review.risk_level || (o.award_eligible ? "low" : "medium")).toUpperCase())} RISK</span></div><div class="bid-review-columns"><div><h4>Strengths</h4><ul>${list(review.strengths,"Quotation received and captured for comparison.")}</ul></div><div><h4>Gaps to address</h4><ul>${list(review.gaps,(o.issues||[]).join(" ") || "Confirm technical compliance and commercial validity.")}</ul></div><div><h4>How to improve</h4><ul>${list(review.improvements,"Confirm specifications, stock, warranty and delivery commitments.")}</ul></div></div><div class="bid-next-step"><b>Recommended next action</b><span>${escapeHtml(review.next_step || "Complete due diligence before proceeding.")}</span></div></article>`;
      });
      html += `</section>`;
    }
    const alternatives = Array.isArray(judgement.alternative_recommendations) && judgement.alternative_recommendations.length
      ? judgement.alternative_recommendations
      : (data.alternatives || []);
    html += `<section class="panel bid-alternatives"><div class="panel-heading"><div><h2>Alternative sellers & sourcing options</h2><p>Potential leads not yet quoted for this request. They are not pre-approved.</p></div></div>`;
    if (alternatives.length) {
      html += `<div class="bid-alternative-grid">`;
      alternatives.forEach(s => {
        const name = s.supplier || s.name || "Potential seller";
        html += `<article><b>${escapeHtml(name)}</b><small>${escapeHtml(s.city || s.reason || "Supplier directory match")}</small><p>${escapeHtml(s.reason || "Potential alternative based on the supplier directory.")}</p><span>${escapeHtml(s.verification_note || "Confirm capability, availability, references and registration before inviting to quote.")}</span></article>`;
      });
      html += `</div>`;
    } else {
      html += `<p class="muted">No suitable unquoted seller match was found in the current supplier register. Add more suppliers or broaden the sourcing area.</p>`;
    }
    html += `<div class="bid-next-step"><b>Alternative solution</b><span>${escapeHtml(judgement.solution || "Seek revised bids, widen sourcing or review delivery sequencing.")}</span></div></section>`;
    html += `<p class="bid-disclaimer">${escapeHtml(data.disclaimer || "Decision support only. Human approval is required before any purchase commitment.")}</p>`;
    resultContainer.innerHTML = html;
  } catch (error) {
    resultContainer.innerHTML = `<div class="empty-state"><h3>Could not complete the review</h3><p>${escapeHtml(error.message)}</p></div>`;
  } finally {
    button.disabled = false; button.textContent = "Analyse quotations →";
  }
});
document.addEventListener("click", event => { const trigger=event.target.closest("[data-generate-po]"); if(trigger) openPoPreview(trigger.dataset.generatePo); });
// --- Modals & Forms ---
document.querySelector('[data-modal="supplierModal"]').addEventListener("click", () => {
  const form = document.getElementById("supplierForm");
  form.reset(); delete form.dataset.sellerId;
  document.getElementById("supplierModalTitle").textContent = "Add seller information";
  document.getElementById("supplierModalDescription").textContent = "Capture the seller's identity and technical capabilities. New sellers start unverified.";
  setFormSubmitLabel(form, "Save seller");
});
document.querySelectorAll("[data-modal]").forEach(btn => {
  btn.addEventListener("click", () => {
    const modal = document.getElementById(btn.dataset.modal);
    modal.classList.add("open");
    if (btn.dataset.modal === "requestModal") {
      document.getElementById("requestForm").reset();
      document.getElementById("requestProjectId").value = "";
      populateRequestProjects();
      document.querySelector("#requestModal .modal-head h2").textContent = "Create purchase request";
    }
    if (btn.dataset.modal === "projectModal") {
      const form = document.getElementById("projectForm");
      const today = new Date();
      const due = new Date(today); due.setDate(due.getDate()+30);
      if (!form.elements.start_date.value) form.elements.start_date.value = today.toISOString().slice(0,10);
      if (!form.elements.due_date.value) form.elements.due_date.value = due.toISOString().slice(0,10);
      if (!form.elements.expected_duration_days.value) form.elements.expected_duration_days.value = "30";
      if (!form.elements.planned_purchase_requests.value) form.elements.planned_purchase_requests.value = "1";
    }
  });
});
document.querySelectorAll("[data-close]").forEach(btn => {
  btn.addEventListener("click", () => {
    const backdrop = btn.closest(".modal-backdrop");
    if (!backdrop) return;
    backdrop.classList.remove("open");
    if (backdrop.id === "requestModal") {
      document.getElementById("requestForm").reset();
      document.querySelector("#requestModal .modal-head h2").textContent = "Create purchase request";
    }
    if (backdrop.id === "quoteModal") {
      const form = document.getElementById("quoteForm");
      if (form) { form.reset(); delete form.dataset.quoteId; setFormSubmitLabel(form, "Save quotation"); }
      const title = document.getElementById("quoteModalTitle");
      const description = document.getElementById("quoteModalDescription");
      if (title) title.textContent = "Add supplier quotation";
      if (description) description.textContent = "";
    }
  });
});
const requestUnitSelect = document.getElementById("requestUnit");
const requestCustomUnitWrap = document.getElementById("requestCustomUnitWrap");
const requestCustomUnitInput = document.getElementById("requestCustomUnit");
function syncRequestCustomUnit() {
  const custom = requestUnitSelect?.value === "other";
  if (requestCustomUnitWrap) requestCustomUnitWrap.hidden = !custom;
  if (requestCustomUnitInput) requestCustomUnitInput.required = custom;
}
requestUnitSelect?.addEventListener("change", syncRequestCustomUnit);
document.getElementById("requestForm")?.addEventListener("reset", () => {
  requestCustomUnitWrap?.setAttribute("hidden", "");
  if (requestCustomUnitInput) requestCustomUnitInput.required = false;
});
document.getElementById("requestForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const formData = new FormData(e.target);
  const payload = Object.fromEntries(formData.entries());
  if (payload.unit === "other") {
    payload.unit = (payload.custom_unit || "").trim();
    if (!payload.unit) {
      showToast("Enter the unit of measurement for this request.", true);
      requestCustomUnitInput?.focus();
      return;
    }
  }
  delete payload.custom_unit;
  const projectId = payload.project_id || "";
  if (!projectId) {
    showToast("Select a project workspace before creating a purchase request.", true);
    return;
  }
  const submitButton = e.target.querySelector('button[type="submit"]');
  if (submitButton) submitButton.disabled = true;
  try {
    const res = await apiFetch(`${API}/requests`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
    });
    const result = await res.json();
    if (!res.ok) throw new Error(result.detail || result.error || "Could not create purchase request");
    document.getElementById("requestModal").classList.remove("open");
    e.target.reset();
    document.querySelector("#requestModal .modal-head h2").textContent = "Create purchase request";
    showToast(projectId ? `Purchase request ${result.code} added to project` : "Purchase request created");
    await fetchDashboard();
    if (projectId && activeProjectId === projectId) await openProjectWorkspace(projectId);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    if (submitButton) submitButton.disabled = false;
  }
});
// Populate quote modal dropdowns
document.querySelector('[data-modal="quoteModal"]').addEventListener("click", () => {
  const form = document.getElementById("quoteForm");
  if (form.dataset.quoteId) return;
  form.reset();
  document.getElementById("quoteModalTitle").textContent = "Add supplier quotation";
  document.getElementById("quoteModalDescription").textContent = "";
  document.getElementById("quoteParseStatus").textContent = "Review all extracted values before saving.";
  document.getElementById("quoteParseReview").hidden = true;
  document.getElementById("quoteParseReview").innerHTML = "";
  document.getElementById("quoteExtractedLineItemLabel").hidden = true;
  document.getElementById("quoteExtractedLineItem").replaceChildren();
  parsedQuoteLineItems = [];
  setFormSubmitLabel(form, "Save quotation");
  document.getElementById("quoteRequest").innerHTML = dashboardData.requests.filter(r => r.status === "Open").map(r => `<option value="${r.id}">${escapeHtml(r.code)} - ${escapeHtml(r.material)}</option>`).join("");
  document.getElementById("quoteSupplier").innerHTML = dashboardData.suppliers.map(s => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join("");
});
document.getElementById("parseQuoteMessage").addEventListener("click", async () => {
  const form = document.getElementById("quoteForm");
  const message = document.getElementById("quoteSourceMessage").value.trim();
  const status = document.getElementById("quoteParseStatus");
  const review = document.getElementById("quoteParseReview");
  const button = document.getElementById("parseQuoteMessage");
  const lineLabel = document.getElementById("quoteExtractedLineItemLabel");
  const linePicker = document.getElementById("quoteExtractedLineItem");
  if (message.length < 3) { status.textContent = "Paste the supplier's message first."; return; }
  button.disabled = true; button.textContent = "Parsing…"; status.textContent = "Checking the message for quotation details…";
  review.hidden = true; review.innerHTML = ""; parsedQuoteLineItems = [];
  lineLabel.hidden = true; linePicker.replaceChildren();
  try {
    const response = await apiFetch(API + "/quotes/parse", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not parse supplier message");
    const x = data.extracted || {}, filled = [];
    parsedQuoteLineItems = Array.isArray(x.line_items) ? x.line_items : [];
    const setValue = (key,value) => { if (value !== null && value !== undefined && form.elements[key]) { form.elements[key].value = value; filled.push(key); } };
    setValue("freight",x.freight); setValue("tax_rate",x.tax_rate);
    setValue("lead_days",x.lead_days); setValue("payment_days",x.payment_days);
    if (parsedQuoteLineItems.length > 1) {
      parsedQuoteLineItems.forEach((item,index) => {
        const option = document.createElement("option");
        option.value = String(index);
        option.textContent = item.description + " — " + Number(item.quantity).toLocaleString() + " " + item.unit + " @ ₦" + Number(item.unit_price).toLocaleString("en-NG") + "/" + item.price_unit;
        linePicker.append(option);
      });
      lineLabel.hidden = false;
      setValue("unit_price",parsedQuoteLineItems[0].unit_price);
      filled.push("unit_price");
    } else {
      setValue("unit_price",x.unit_price);
    }
    const selectedRequest = (dashboardData.requests || []).find(r => String(r.id) === String(form.elements.request_id.value));
    const normalizeUnit = value => ({kgs:"kg",kilogram:"kg",kilograms:"kg",pcs:"piece",pieces:"piece",units:"unit",ton:"tonne",tons:"tonne",mt:"tonne"}[String(value || "").toLowerCase()] || String(value || "").toLowerCase());
    const chosen = parsedQuoteLineItems[0] || null;
    const supplierQuantity = chosen ? chosen.quantity : x.quantity;
    const supplierUnit = chosen ? chosen.unit : x.unit;
    const quantityNote = supplierQuantity !== null && supplierQuantity !== undefined
      ? (selectedRequest ? (Math.abs(Number(selectedRequest.quantity)-Number(supplierQuantity)) < 0.000001 && normalizeUnit(selectedRequest.unit) === normalizeUnit(supplierUnit)
        ? "Supplier quantity matches the selected request."
        : "Supplier states " + Number(supplierQuantity).toLocaleString() + " " + supplierUnit + "; selected request is " + Number(selectedRequest.quantity).toLocaleString() + " " + selectedRequest.unit + ". Confirm the discrepancy manually.")
        : "Supplier quantity detected: " + Number(supplierQuantity).toLocaleString() + " " + supplierUnit + ". Select the correct purchase request to compare.")
      : "No supplier quantity was confidently recognised.";
    const lines = [];
    if (x.line_items?.length > 1) lines.push(x.line_items.length + " separately priced material lines detected. Choose the matching line above; save each item against its own purchase request.");
    if (x.unit_price !== null && !chosen) lines.push("Unit price: ₦" + Number(x.unit_price).toLocaleString("en-NG"));
    if (x.freight !== null) lines.push("Freight: ₦" + Number(x.freight).toLocaleString("en-NG"));
    if (x.tax_rate !== null) lines.push("Tax: " + x.tax_rate + "%");
    if (x.lead_days !== null) lines.push("Lead time: " + x.lead_days + " days");
    if (x.payment_days !== null) lines.push("Payment period: " + x.payment_days + " days");
    if (x.payment_terms) lines.push("Payment terms detected: " + x.payment_terms + ". Verify the day-based field and retain the original terms in the source message.");
    lines.push(quantityNote);
    review.innerHTML = "<strong>Extraction review</strong><ul>" + lines.map(line => "<li>" + escapeHtml(line) + "</li>").join("") + "</ul><small>Supplier quantities never overwrite requested quantities. Nothing is saved until you review and submit the quotation.</small>";
    review.hidden = false;
    status.textContent = filled.length ? "Recognised " + filled.length + " form field(s). Please verify them." : "No supported price or delivery fields found. Enter the values manually.";
  } catch(error) { status.textContent = error.message; }
  finally { button.disabled = false; button.textContent = "✦ Parse message"; }
});
document.getElementById("quoteExtractedLineItem").addEventListener("change", event => {
  const item = parsedQuoteLineItems[Number(event.target.value)];
  if (!item) return;
  document.getElementById("quoteForm").elements.unit_price.value = item.unit_price;
  document.getElementById("quoteParseStatus").textContent = "Selected " + item.description + ". Confirm the purchase request and quantity before saving.";
});
document.getElementById("quoteForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget, payload = Object.fromEntries(new FormData(form).entries());
  const sourceMessage = String(payload.source_message || "").trim();
  delete payload.source_message;
  if (sourceMessage) payload.note = [String(payload.note || "").trim(), "Supplier's original message:\n" + sourceMessage].filter(Boolean).join("\n\n");
  ["supplier_id","request_id","lead_days","payment_days","unit_price","freight","tax_rate"].forEach(key => payload[key] = Number(payload[key]));
  const quoteId = form.dataset.quoteId;
  const res = await apiFetch(API + (quoteId ? "/quotes/" + quoteId : "/quotes"), {
    method: quoteId ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
  });
  const result = await res.json();
  if (res.ok) {
    document.getElementById("quoteModal").classList.remove("open"); form.reset(); delete form.dataset.quoteId;
    document.getElementById("quoteModalTitle").textContent = "Add supplier quotation";
    document.getElementById("quoteModalDescription").textContent = "";
    setFormSubmitLabel(form, "Save quotation");
    showToast(quoteId ? (result.supplier_email_reset ? "Quotation updated. Re-email the seller before generating a PO." : "Quotation updated") : "Quotation saved");
    await fetchDashboard();
  } else showToast(result.detail || "Could not save quotation", true);
});
// Register search/filter controls and PDF exports.
["requestSearch","requestStatusFilter","supplierSearch","supplierStatusFilter","supplierCategoryFilter","quoteSearch","orderSearch","orderStatusFilter"].forEach(id => {
  const control = document.getElementById(id);
  if (control) control.addEventListener("input", renderDashboard);
  if (control && control.tagName === "SELECT") control.addEventListener("change", renderDashboard);
});
document.querySelectorAll("[data-export]").forEach(button => button.addEventListener("click", () => exportPdf(button.dataset.export)));

// Add seller information to the shared supplier register.
document.getElementById("supplierForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form).entries());
  ["rating","on_time"].forEach(key => payload[key] = Number(payload[key] || 0));
  const button = form.querySelector('button[type="submit"], button:not([type])');
  if (button) button.disabled = true;
  try {
    const sellerId = form.dataset.sellerId;
    const response = await apiFetch(API + (sellerId ? "/suppliers/" + encodeURIComponent(sellerId) : "/suppliers"), { method:sellerId ? "PUT" : "POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || (sellerId ? "Could not update seller" : "Could not save seller"));
    document.getElementById("supplierModal").classList.remove("open");
    form.reset();
    delete form.dataset.sellerId;
    document.getElementById("supplierModalTitle").textContent = "Add seller information";
    document.getElementById("supplierModalDescription").textContent = "Capture the seller's identity and technical capabilities. New sellers start unverified.";
    setFormSubmitLabel(form, "Save seller");
    showToast(sellerId ? "Seller details updated" : "Seller information saved");
    await fetchDashboard();
  } catch (error) { showToast(error.message, true); }
  finally { if (button) button.disabled = false; }
});

// --- Administrator company configuration ---
function splitLines(value) { return value.split(/\r?\n|,/).map(item => item.trim()).filter(Boolean); }
async function loadCompanyProfile() {
  const response = await apiFetch(API + "/admin/company-profile");
  if (!response.ok) return;
  const profile = await response.json();
  document.getElementById("companyName").value = profile.company_name || "";
  document.getElementById("companyRegistration").value = profile.registration_number || "";
  document.getElementById("companyIndustry").value = profile.industry || "";
  document.getElementById("companyServices").value = (profile.services || []).join("\n");
  document.getElementById("companyModes").value = (profile.service_modes || []).join("\n");
  document.getElementById("companyClients").value = (profile.client_segments || []).join("\n");
  document.getElementById("companyCurrency").value = profile.currency || "NGN";
}
document.getElementById("companyProfileForm").addEventListener("submit", async event => {
  event.preventDefault();
  const status = document.getElementById("companyProfileStatus");
  status.textContent = "Saving…";
  const payload = {
    company_name: document.getElementById("companyName").value.trim(),
    registration_number: document.getElementById("companyRegistration").value.trim(),
    industry: document.getElementById("companyIndustry").value.trim(),
    services: splitLines(document.getElementById("companyServices").value),
    service_modes: splitLines(document.getElementById("companyModes").value),
    client_segments: splitLines(document.getElementById("companyClients").value),
    currency: document.getElementById("companyCurrency").value
  };
  try {
    const response = await apiFetch(API + "/admin/company-profile", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not save company configuration");
    status.textContent = "Company configuration saved.";
    showToast("Company configuration saved");
  } catch (error) { status.textContent = error.message; showToast(error.message, true); }
});
// --- Administrator database backup and recovery ---
async function loadDatabaseBackups() {
  const status = document.getElementById("databaseBackupStatus");
  const list = document.getElementById("databaseBackupList");
  if (!status || !list) return;
  status.textContent = "Checking backup storage…";
  try {
    const response = await apiFetch(API + "/admin/database/backups");
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not load database backups");
    status.textContent = data.automatic_backup
      ? "Automatic daily backups are enabled. Seven recent copies are retained."
      : "Automatic backups are disabled. Create a backup manually.";
    list.replaceChildren();
    if (!data.backups.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No database backups are available yet.";
      list.append(empty);
      return;
    }
    data.backups.forEach(backup => {
      const row = document.createElement("div");
      row.className = "database-backup-row";
      const meta = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = backup.filename;
      const detail = document.createElement("small");
      detail.textContent = new Date(backup.created_at).toLocaleString() + " · " + Math.ceil(backup.size_bytes / 1024) + " KB";
      meta.append(name, detail);
      const actions = document.createElement("div");
      actions.className = "backup-row-actions";
      const verify = document.createElement("button");
      verify.type = "button";
      verify.className = "btn btn-ghost";
      verify.textContent = "Verify restore";
      verify.addEventListener("click", async () => {
        verify.disabled = true;
        status.textContent = "Testing backup in an isolated temporary database…";
        try {
          const result = await apiFetch(API + "/admin/database/backups/" + encodeURIComponent(backup.filename) + "/verify-restore", {method:"POST"});
          const body = await result.json();
          if (!result.ok) throw new Error(body.detail || "Restore verification failed");
          status.textContent = "Restore test passed: " + body.verification.table_count + " tables and " + body.verification.row_count + " rows checked. Live database was not changed.";
          showToast("Backup restore verification passed");
        } catch (error) { status.textContent = error.message; showToast(error.message, true); }
        finally { verify.disabled = false; }
      });
      const download = document.createElement("a");
      download.className = "btn btn-secondary";
      download.href = API + "/admin/database/backups/" + encodeURIComponent(backup.filename) + "/download";
      download.textContent = "Download";
      download.setAttribute("download", backup.filename);
      actions.append(verify, download);
      row.append(meta, actions);
      list.append(row);
    });
  } catch (error) {
    status.textContent = error.message;
    list.replaceChildren();
  }
}
document.getElementById("createDatabaseBackup")?.addEventListener("click", async event => {
  const button = event.currentTarget;
  const status = document.getElementById("databaseBackupStatus");
  button.disabled = true;
  status.textContent = "Creating and integrity-checking a database backup…";
  try {
    const response = await apiFetch(API + "/admin/database/backups", {method:"POST"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not create database backup");
    status.textContent = "Backup created and integrity checked: " + data.backup.filename;
    showToast("Database backup created");
    await loadDatabaseBackups();
  } catch (error) { status.textContent = error.message; showToast(error.message, true); }
  finally { button.disabled = false; }
});
document.getElementById("refreshDatabaseBackups")?.addEventListener("click", loadDatabaseBackups);

// --- Utilities ---
function showToast(msg, isError = false) {
  const toast = document.getElementById("toast");
  toast.textContent = msg;
  toast.style.background = isError ? "#c64c55" : "#15243a";
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 3000);
}
// Set today's date in header
document.getElementById("today").textContent = new Date().toLocaleDateString("en-GB", { day: 'numeric', month: 'long', year: 'numeric' }).toUpperCase();
// Authentication gate: dashboard data is never requested before a valid session exists.
document.getElementById("loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.getElementById("loginError");
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  error.textContent = "";
  try {
    const response = await apiFetch(`${API}/auth/login`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.getElementById("loginUsername").value.trim(),
        password: document.getElementById("loginPassword").value
      })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || data.error || "Unable to sign in");
    document.getElementById("loginPassword").value = "";
    showApp();
    await Promise.all([fetchDashboard(), loadCompanyProfile(), loadDatabaseBackups()]);
  } catch (err) {
    error.textContent = err.message || "Sign-in failed. Please try again.";
  } finally { button.disabled = false; }
});
document.getElementById("logoutBtn").addEventListener("click", async () => {
  try { await apiFetch(`${API}/auth/logout`, { method: "POST" }); }
  finally { dashboardData = {}; showLogin("You have signed out."); }
});
document.getElementById("refreshBtn").addEventListener("click", fetchDashboard);
async function initializeApp() {
  try {
    const response = await apiFetch(`${API}/auth/me`);
    if (!response.ok) return showLogin();
    showApp();
    await Promise.all([fetchDashboard(), loadCompanyProfile()]);
  } catch { showLogin("Unable to reach ProcureAI. Check your connection and try again."); }
}
initializeApp();
