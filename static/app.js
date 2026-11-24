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
  renderDashboard();
}
function formatMoney(val) {
  return "₦" + Number(val).toLocaleString("en-NG", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
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
  document.getElementById("allRequests").innerHTML = requests.map(r => `
    <tr>
      <td class="code">${r.code}</td>
      <td class="material">${r.material}</td>
      <td>${r.category}</td>
      <td>${Number(r.quantity).toLocaleString()} ${r.unit}</td>
      <td>${r.site}</td>
      <td>${r.required_by}</td>
      <td><span class="pill ${r.priority === 'Urgent' ? 'red' : 'gray'}">${r.priority}</span></td>
      <td><span class="pill ${r.status === 'Open' ? 'blue' : 'green'}">${r.status}</span></td>
      <td></td>
    </tr>
  `).join("");
  // Suppliers
  document.getElementById("topSuppliers").innerHTML = suppliers.slice(0, 4).map(s => `
    <div class="supplier-row">
      <div class="supplier-logo">${s.name.substring(0,2).toUpperCase()}</div>
      <div class="supplier-info">
        <b>${s.name} ${s.is_demo ? '<span class="pill amber">DEMO</span>' : ''}</b>
        <small>${s.city} · ${s.on_time}% on-time</small>
      </div>
      <div class="rating">★ ${s.rating}</div>
    </div>
  `).join("");
  document.getElementById("supplierCards").innerHTML = suppliers.map(s => `
    <div class="supplier-card">
      <div class="supplier-card-top">
        <div class="supplier-logo">${s.name.substring(0,2).toUpperCase()}</div>
        <span class="rating">★ ${s.rating}</span>
      </div>
      <h3>${s.name} ${s.is_demo ? '<span class="pill amber">DEMO DATA</span>' : ''}</h3>
      <p>${s.city} · ${s.contact}</p>
      <div class="supplier-stats">
        <div><b>${s.on_time}%</b><small>On-time</small></div>
        <div><b>${s.status}</b><small>Status</small></div>
      </div>
    </div>
  `).join("");
  // Recent Quotes
  document.getElementById("recentQuotes").innerHTML = recent_quotes.slice(0, 5).map(q => `
    <div class="quote-item">
      <div>
        <b>${q.supplier} ${q.is_demo ? '<span class="pill amber">DEMO</span>' : ''}</b>
        <small>${q.material} (${q.request_code})</small>
      </div>
      <div class="quote-price">${formatMoney(q.unit_price)}</div>
    </div>
  `).join("");
  // All Quotes Table
  document.getElementById("allQuotes").innerHTML = recent_quotes.map(q => `
    <tr>
      <td class="material">${q.supplier}</td>
      <td class="code">${q.request_code}</td>
      <td>${q.material}</td>
      <td>${formatMoney(q.unit_price)}</td>
      <td>${formatMoney(q.freight)}</td>
      <td>${q.lead_days} days</td>
      <td>Net ${q.payment_days}</td>
    </tr>
  `).join("");
  // Orders Table
  document.getElementById("allOrders").innerHTML = orders.map(o => `
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
