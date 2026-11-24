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
// --- Navigation ---
document.querySelectorAll(".nav-item").forEach(btn => {
  btn.addEventListener("click", (e) => {
    document.querySelectorAll(".nav-item").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    const page = btn.dataset.page;
    document.getElementById(`page-${page}`).classList.add("active");
    document.getElementById("crumbTitle").textContent = btn.textContent.trim().split(" ")[0];
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
// --- Data Fetching ---
async function fetchDashboard() {
  const res = await apiFetch(`${API}/dashboard`);
  const data = await res.json();
  dashboardData = data;
  const quoteResponse = await apiFetch(`${API}/quotes`);
  const quoteData = quoteResponse.ok ? await quoteResponse.json() : { quotes: [] };
  quoteRecords = quoteData.quotes || [];
  renderDashboard();
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
function csvCell(value) {
  const text = String(value ?? "");
  return '"' + text.replace(/"/g, '""') + '"';
}
function exportCsv(type) {
  const datasets = {
    requests: { rows: filterRecords(dashboardData.requests, document.getElementById("requestSearch")?.value, document.getElementById("requestStatusFilter")?.value, ["code","material","category","site","status"]), fields: ["code","material","category","quantity","unit","site","required_by","priority","status"] },
    suppliers: { rows: filterRecords(dashboardData.suppliers, document.getElementById("supplierSearch")?.value, document.getElementById("supplierStatusFilter")?.value, ["name","city","contact","email","phone","services","status"]), fields: ["name","city","contact","email","phone","website","address","services","rating","on_time","status"] },
    quotes: { rows: filterRecords(quoteRecords, document.getElementById("quoteSearch")?.value, "", ["supplier","request_code","material","category","note"]), fields: ["supplier","request_code","material","unit_price","freight","tax_rate","lead_days","payment_days","note","created_at"] },
    orders: { rows: filterRecords(dashboardData.orders, document.getElementById("orderSearch")?.value, document.getElementById("orderStatusFilter")?.value, ["code","supplier","material","status"]), fields: ["code","supplier","material","quantity","unit_price","freight","tax","total","required_by","status","decision_note"] }
  };
  const dataset = datasets[type];
  if (!dataset) return;
  const csv = [dataset.fields, ...dataset.rows.map(row => dataset.fields.map(field => row[field]))]
    .map(row => row.map(csvCell).join(",")).join("\r\n");
  const blob = new Blob(["\\uFEFF", csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url; link.download = `procureai-${type}-${new Date().toISOString().slice(0,10)}.csv`;
  document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
}
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
      <td><span class="pill ${r.status === 'Open' ? 'blue' : 'green'}">${r.status}</span></td>
      <td><button class="action-btn reject" data-delete-request="${r.id}" data-request-code="${r.code}">Delete</button></td>
    </tr>
  `).join("");
  // Suppliers
  document.getElementById("topSuppliers").innerHTML = suppliers.slice(0, 4).map(s => `
    <div class="supplier-row">
      <div class="supplier-logo">${escapeHtml(s.name.substring(0,2).toUpperCase())}</div>
      <div class="supplier-info">
        <b>${escapeHtml(s.name)} ${s.is_demo ? '<span class="pill amber">DEMO</span>' : ''}</b>
        <small>${escapeHtml(s.city)} · ${Number(s.on_time || 0)}% on-time</small>
      </div>
      <div class="rating">★ ${Number(s.rating || 0).toFixed(1)}</div>
    </div>
  `).join("");
  const visibleSuppliers = filterRecords(suppliers, document.getElementById("supplierSearch")?.value, document.getElementById("supplierStatusFilter")?.value, ["name","city","contact","email","phone","services","status"]);
  document.getElementById("supplierCards").innerHTML = visibleSuppliers.map(s => `
    <div class="supplier-card">
      <div class="supplier-card-top">
        <div class="supplier-logo">${escapeHtml(s.name.substring(0,2).toUpperCase())}</div>
        <span class="rating">★ ${Number(s.rating || 0).toFixed(1)}</span>
      </div>
      <h3>${escapeHtml(s.name)} ${s.is_demo ? '<span class="pill amber">DEMO DATA</span>' : ''}</h3>
      <p>${escapeHtml(s.city)} · ${escapeHtml(s.phone || s.email || s.contact || "No contact supplied")}</p>
      <p class="supplier-capabilities">${escapeHtml(s.services || "Services not yet listed")}</p>
      <p class="supplier-capabilities">${escapeHtml([s.email,s.website,s.address].filter(Boolean).join(" · "))}</p>
      <div class="supplier-stats">
        <div><b>${s.on_time}%</b><small>On-time</small></div>
        <div><b>${s.status}</b><small>Status</small></div>
      </div>
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
      <td><button class="action-btn" data-open-quote="${q.id}">Open</button></td>
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
// --- Administrator request and quotation controls ---
document.addEventListener("click", event => {
  const deleteButton = event.target.closest("[data-delete-request]");
  if (deleteButton) window.deleteRequest(deleteButton.dataset.deleteRequest, deleteButton.dataset.requestCode);
  const quoteButton = event.target.closest("[data-open-quote]");
  if (quoteButton) window.openQuote(quoteButton.dataset.openQuote);
});
window.deleteRequest = async function(id, code) {
  if (!confirm("Delete purchase request " + code + "? Linked quotations and purchase orders will also be deleted. This cannot be undone.")) return;
  const response = await apiFetch(API + "/requests/" + id, { method: "DELETE" });
  const data = await response.json();
  if (!response.ok) return showToast(data.detail || data.error || "Could not delete request", true);
  showToast("Request " + data.deleted_request + " deleted");
  await fetchDashboard();
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
    const fields = [["Supplier",q.supplier],["Supplier contact",q.supplier_contact || "Not provided"],["Supplier location",q.supplier_city || "Not provided"],["Linked request",q.request_code],["Material / equipment",q.material],["Category",q.category],["Quantity",Number(q.quantity).toLocaleString() + " " + q.unit],["Project / site",q.site],["Required by",q.required_by],["Priority",q.priority],["Request status",q.request_status],["Unit price",formatMoney(q.unit_price)],["Freight",formatMoney(q.freight)],["Tax rate",q.tax_rate + "%"],["Tax amount",formatMoney(q.tax_amount)],["Subtotal",formatMoney(q.subtotal)],["Total landed",formatMoney(q.total_landed)],["Lead time",q.lead_days + " days"],["Payment terms","Net " + q.payment_days + " days"],["Quotation date",new Date(q.created_at).toLocaleString()],["Notes",q.note || "None"]];
    content.innerHTML = "<div class=\"quote-detail-grid\">" + fields.map(pair => "<div class=\"quote-detail-field\"><small>" + pair[0] + "</small><b>" + String(pair[1] ?? "—").replaceAll("<","&lt;").replaceAll(">","&gt;") + "</b></div>").join("") + "</div>";
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
document.getElementById("runAnalysis").addEventListener("click", async () => {
  const rid = document.getElementById("analysisRequest").value;
  if (!rid) return;
  const res = await apiFetch(`${API}/analysis?request_id=${rid}`);
  const data = await res.json();
 
  const resultContainer = document.getElementById("analysisResult");
  if (!data.offers || data.offers.length === 0) {
    resultContainer.innerHTML = `<div class="empty-state"><span>◈</span><h3>No quotes found</h3><p>Add quotations for this request to run an analysis.</p></div>`;
    return;
  }
  const rec = data.recommendation;
  let html = "";
 
  if (rec) {
    html += `
      <div class="recommendation">
        <div>
          <span class="eyebrow">AI RECOMMENDATION</span>
          <h3>${rec.supplier} is the best value</h3>
          <p>${data.disclaimer}</p>
        </div>
        <div class="recommend-price">${formatMoney(rec.estimated_total)}<br><small style="font-size:10px;color:#73877f;font-weight:400">Est. Landed Cost</small></div>
      </div>
    `;
  }
  html += `<div class="analysis-table"><table><thead><tr>
    <th>SUPPLIER</th><th>UNIT PRICE</th><th>FREIGHT</th><th>LEAD TIME</th><th>FEASIBILITY</th><th>TCO (EST)</th>
  </tr></thead><tbody>`;
 
  data.offers.forEach(o => {
    html += `
      <tr>
        <td class="material">${o.supplier}</td>
        <td>${formatMoney(o.unit_price)}</td>
        <td>${formatMoney(o.freight)}</td>
        <td>${o.lead_days} days</td>
        <td><span class="pill ${o.feasible ? 'green' : 'red'}">${o.feasible ? 'Feasible' : 'Infeasible'}</span></td>
        <td><b>${formatMoney(o.estimated_total)}</b></td>
      </tr>
    `;
  });
  html += `</tbody></table></div>`;
  resultContainer.innerHTML = html;
});
// --- Modals & Forms ---
document.querySelectorAll("[data-modal]").forEach(btn => {
  btn.addEventListener("click", () => document.getElementById(btn.dataset.modal).classList.add("open"));
});
document.querySelectorAll("[data-close]").forEach(btn => {
  btn.addEventListener("click", () => btn.closest(".modal-backdrop").classList.remove("open"));
});
document.getElementById("requestForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const formData = new FormData(e.target);
  const payload = Object.fromEntries(formData.entries());
 
  const res = await apiFetch(`${API}/requests`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
  });
  if (res.ok) {
    document.getElementById("requestModal").classList.remove("open");
    e.target.reset();
    showToast("Purchase request created");
    fetchDashboard();
  } else {
    const err = await res.json();
    showToast(err.error, true);
  }
});
// Populate quote modal dropdowns
document.querySelector('[data-modal="quoteModal"]').addEventListener("click", () => {
  document.getElementById("quoteRequest").innerHTML = dashboardData.requests.filter(r => r.status === "Open").map(r =>
    `<option value="${r.id}">${r.code} - ${r.material}</option>`
  ).join("");
  document.getElementById("quoteSupplier").innerHTML = dashboardData.suppliers.map(s =>
    `<option value="${s.id}">${s.name}</option>`
  ).join("");
});
document.getElementById("quoteForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const formData = new FormData(e.target);
  const payload = Object.fromEntries(formData.entries());
 
  const res = await apiFetch(`${API}/quotes`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
  });
  if (res.ok) {
    document.getElementById("quoteModal").classList.remove("open");
    e.target.reset();
    showToast("Quotation saved");
    fetchDashboard();
  } else {
    const err = await res.json();
    showToast(err.error, true);
  }
});
// Register search/filter controls and CSV exports.
["requestSearch","requestStatusFilter","supplierSearch","supplierStatusFilter","quoteSearch","orderSearch","orderStatusFilter"].forEach(id => {
  const control = document.getElementById(id);
  if (control) control.addEventListener("input", renderDashboard);
  if (control && control.tagName === "SELECT") control.addEventListener("change", renderDashboard);
});
document.querySelectorAll("[data-export]").forEach(button => button.addEventListener("click", () => exportCsv(button.dataset.export)));

// Add seller information to the shared supplier register.
document.getElementById("supplierForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form).entries());
  ["rating","on_time"].forEach(key => payload[key] = Number(payload[key] || 0));
  const button = form.querySelector('button[type="submit"], button:not([type])');
  if (button) button.disabled = true;
  try {
    const response = await apiFetch(API + "/suppliers", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Could not save seller");
    document.getElementById("supplierModal").classList.remove("open");
    form.reset();
    showToast("Seller information saved");
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
    await Promise.all([fetchDashboard(), loadCompanyProfile()]);
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
