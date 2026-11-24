// ProcureAI administration console
(() => {
  const API = "/api/admin";
  const $ = (s, r = document) => r.querySelector(s);
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = v => "₦" + Number(v).toLocaleString("en-NG", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const when = s => (s ? esc(String(s).replace("T", " ").slice(0, 16)) : "—");
  const NAV = [
    ["overview", "▦", "Overview"], ["users", "☺", "Users"], ["suppliers", "♧", "Suppliers"],
    ["requests", "▤", "Purchase requests"], ["orders", "▣", "Orders & approvals"],
    ["settings", "⚙", "Approval policy"], ["audit", "☰", "Audit log"],
  ];
  let me = null, page = "overview", cache = {};

  // ---------------------------------------------------------------- helpers
  async function api(method, path, payload) {
    const opts = { method, credentials: "same-origin", headers: {} };
    if (method !== "GET") { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(payload || {}); }
    const res = await fetch(API + path, opts);
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON error */ }
    if (res.status === 401 && path !== "/login") { showLogin(); throw new Error(data.error || "Sign in required"); }
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  }
  let toastTimer;
  function toast(msg, isError = false) {
    const t = $("#toast");
    t.textContent = msg; t.style.background = isError ? "#c64c55" : "#15243a"; t.classList.add("show");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), 3500);
  }
  const attempt = async fn => { try { return await fn(); } catch (e) { toast(e.message, true); } };

  // ---------------------------------------------------------------- modal + forms
  function openModal(eyebrow, title, html) {
    $("#modalEyebrow").textContent = eyebrow; $("#modalTitle").textContent = title;
    $("#modalBody").innerHTML = html; $("#modal").classList.add("open");
    const first = $("#modalBody input, #modalBody select"); if (first) first.focus();
  }
  const closeModal = () => $("#modal").classList.remove("open");
  $("#modalClose").addEventListener("click", closeModal);
  $("#modal").addEventListener("mousedown", e => { if (e.target.id === "modal") closeModal(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

  function field(f) {
    const attrs = [f.required ? "required" : "", f.min != null ? `min="${f.min}"` : "", f.max != null ? `max="${f.max}"` : "",
      f.step ? `step="${f.step}"` : "", f.placeholder ? `placeholder="${esc(f.placeholder)}"` : "",
      f.autocomplete ? `autocomplete="${f.autocomplete}"` : ""].join(" ");
    const input = f.type === "select"
      ? `<select name="${f.name}">${f.options.map(o => `<option ${o === f.value ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`
      : `<input name="${f.name}" type="${f.type || "text"}" value="${esc(f.value ?? "")}" ${attrs}>`;
    return `<label${f.wide ? ' class="wide"' : ""}>${esc(f.label)}${input}${f.help ? `<small>${esc(f.help)}</small>` : ""}</label>`;
  }
  function formModal({ eyebrow, title, fields, submit, hint, danger, onSubmit }) {
    openModal(eyebrow, title, `${hint ? `<p class="hint">${esc(hint)}</p>` : ""}
      <form id="modalForm" class="form-grid">${fields.map(field).join("")}
      <div class="form-actions wide"><button type="button" class="btn btn-ghost" id="modalCancel">Cancel</button>
      <button class="btn ${danger ? "btn-danger" : "btn-primary"}">${esc(submit)}</button></div></form>`);
    $("#modalCancel").addEventListener("click", closeModal);
    $("#modalForm").addEventListener("submit", async e => {
      e.preventDefault();
      await attempt(async () => { await onSubmit(Object.fromEntries(new FormData(e.target).entries())); });
    });
  }
  function secretModal(title, intro, secret) {
    openModal("ONE-TIME PASSWORD", title, `<p class="hint">${esc(intro)}</p><div class="secret-box mono">${esc(secret)}</div>
      <div class="form-actions"><button class="btn btn-primary" id="secretDone">Done</button></div>`);
    $("#secretDone").addEventListener("click", closeModal);
  }

  // ---------------------------------------------------------------- auth views
  function showLogin() {
    me = null; $("#appView").hidden = true; $("#loginView").hidden = false;
    $("#loginForm").reset(); $("#loginError").textContent = "";
  }
  function showApp() {
    $("#loginView").hidden = true; $("#appView").hidden = false;
    $("#meName").textContent = me.name; $("#meEmail").textContent = me.email;
    $("#meAvatar").textContent = me.name.split(/\s+/).map(w => w[0]).join("").slice(0, 2).toUpperCase();
    $("#nav").innerHTML = NAV.map(([id, icon, label]) =>
      `<button class="nav-item ${id === page ? "active" : ""}" data-page="${id}"><span>${icon}</span> ${label}</button>`).join("");
    go(page);
  }
  $("#loginForm").addEventListener("submit", async e => {
    e.preventDefault(); const btn = $("#loginBtn"); btn.disabled = true; $("#loginError").textContent = "";
    try {
      const d = Object.fromEntries(new FormData(e.target).entries());
      me = (await api("POST", "/login", d)).user; showApp();
    } catch (err) { $("#loginError").textContent = err.message; }
    btn.disabled = false;
  });
  $("#logoutBtn").addEventListener("click", async () => { try { await api("POST", "/logout"); } catch (_) {} showLogin(); });
  $("#passwordBtn").addEventListener("click", () => formModal({
    eyebrow: "ACCOUNT", title: "Change password", submit: "Update password",
    hint: "Your other signed-in sessions will be ended.",
    fields: [
      { name: "current", label: "Current password", type: "password", required: true, wide: true, autocomplete: "current-password" },
      { name: "new", label: "New password", type: "password", required: true, wide: true, autocomplete: "new-password", help: "At least 10 characters." },
    ],
    onSubmit: async d => { await api("POST", "/password", d); closeModal(); toast("Password updated"); },
  }));
  $("#mobileMenu").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
  $("#nav").addEventListener("click", e => {
    const b = e.target.closest(".nav-item"); if (!b) return;
    page = b.dataset.page;
    document.querySelectorAll("#nav .nav-item").forEach(n => n.classList.toggle("active", n === b));
    $("#sidebar").classList.remove("open"); go(page);
  });

  // ---------------------------------------------------------------- pages
  const pill = (txt, color) => `<span class="pill ${color}">${esc(txt)}</span>`;
  const statusColor = s => ({ Approved: "green", Active: "green", Open: "blue", Rejected: "red", Cancelled: "gray", Suspended: "red", "Pending approval": "amber", Awarded: "green" }[s] || "gray");
  const table = (heads, rows, cols) => `<div class="table-wrap"><table><thead><tr>${heads.map(h => `<th>${h}</th>`).join("")}</tr></thead>
    <tbody>${rows || `<tr class="empty-row"><td colspan="${cols || heads.length}">Nothing here yet</td></tr>`}</tbody></table></div>`;
  const heading = (eyebrow, title, sub, action = "") =>
    `<div class="page-heading"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p class="subheading">${sub}</p></div>${action}</div>`;

  async function go(p) {
    page = p; $("#crumbTitle").textContent = NAV.find(n => n[0] === p)[2];
    const el = $("#content"); el.innerHTML = '<p class="subheading">Loading…</p>';
    try { el.innerHTML = await pages[p](); } catch (e) { if (me) el.innerHTML = `<div class="empty-state"><h3>Could not load</h3><p>${esc(e.message)}</p></div>`; }
  }
  const reload = () => go(page);

  const pages = {
    async overview() {
      const o = await api("GET", "/overview");
      const sum = obj => Object.values(obj).reduce((a, b) => a + b, 0);
      return heading("CONTROL CENTRE", "Administration overview", "System health, pending decisions and recent admin activity.") + `
      <div class="metric-grid five">
        <article class="metric"><div class="metric-top"><span>Users</span><span class="metric-icon purple">☺</span></div><strong>${sum(o.users)}</strong><small>${o.users.admin || 0} administrator(s)</small></article>
        <article class="metric"><div class="metric-top"><span>Active suppliers</span><span class="metric-icon blue">♧</span></div><strong>${o.suppliers.Active || 0}</strong><small>${o.suppliers.Suspended || 0} suspended</small></article>
        <article class="metric"><div class="metric-top"><span>Open requests</span><span class="metric-icon amber">▤</span></div><strong>${o.requests.Open || 0}</strong><small>${sum(o.requests)} in total</small></article>
        <article class="metric"><div class="metric-top"><span>Approved spend</span><span class="metric-icon green">₦</span></div><strong>${money(o.approved_spend)}</strong><small>${o.orders.Approved || 0} approved orders</small></article>
      </div>
      <div class="content-grid">
        <section class="panel"><div class="panel-heading"><div><h2>Awaiting approval</h2><p>Orders that tripped an approval rule</p></div><button class="text-btn" data-goto="orders">Review →</button></div>
          ${o.pending_orders.map(p => `<div class="stack-row"><div><b>${esc(p.code)} · ${esc(p.supplier)}</b><small>${esc(p.decision_note)}</small></div><b>${money(p.total)}</b></div>`).join("") || '<p class="subheading">No orders are waiting.</p>'}
        </section>
        <section class="panel"><div class="panel-heading"><div><h2>Recent activity</h2><p>Latest audit entries</p></div><button class="text-btn" data-goto="audit">Full log →</button></div>
          ${o.recent_audit.map(a => `<div class="stack-row"><div><b>${esc(a.action)}</b><small>${esc(a.actor)} · ${when(a.at)}</small></div><span class="muted">${esc(a.target)}</span></div>`).join("")}
        </section>
      </div>`;
    },

    async users() {
      cache.users = (await api("GET", "/users")).users;
      const rows = cache.users.map(u => `<tr>
        <td class="material">${esc(u.name)}${u.id === me.id ? " " + pill("You", "gray") : ""}</td><td>${esc(u.email)}</td>
        <td>${pill(u.role, u.role === "admin" ? "dark" : "gray")}</td><td>${pill(u.active ? "Active" : "Deactivated", u.active ? "green" : "red")}</td>
        <td>${when(u.last_login)}</td><td class="table-actions">
          <button class="action-btn" data-action="user-edit" data-id="${u.id}">Edit</button>
          <button class="action-btn" data-action="user-reset" data-id="${u.id}">Reset password</button></td></tr>`).join("");
      return heading("ACCESS CONTROL", "Users", "Who can sign in, and what they can do. Only administrators can open this console.",
        '<button class="btn btn-primary" data-action="user-new">＋ New user</button>') +
        `<section class="panel">${table(["NAME", "EMAIL", "ROLE", "STATUS", "LAST SIGN-IN", ""], rows)}</section>`;
    },

    async suppliers() {
      cache.suppliers = (await api("GET", "/suppliers")).suppliers;
      const rows = cache.suppliers.map(s => `<tr>
        <td class="material">${esc(s.name)}</td><td>${esc(s.city)}</td><td>${esc(s.contact)}</td><td>★ ${s.rating}</td><td>${s.on_time}%</td>
        <td>${pill(s.status, statusColor(s.status))}</td><td>${s.quote_count} / ${s.order_count}</td>
        <td class="table-actions"><button class="action-btn" data-action="supplier-edit" data-id="${s.id}">Edit</button>
          <button class="action-btn danger" data-action="supplier-delete" data-id="${s.id}">Delete</button></td></tr>`).join("");
      return heading("SUPPLIER NETWORK", "Suppliers", "Maintain the register. Suspended suppliers cannot receive purchase orders.",
        '<button class="btn btn-primary" data-action="supplier-new">＋ New supplier</button>') +
        `<section class="panel">${table(["SUPPLIER", "CITY", "CONTACT", "RATING", "ON-TIME", "STATUS", "QUOTES / ORDERS", ""], rows)}</section>`;
    },

    async requests() {
      cache.requests = (await api("GET", "/requests")).requests;
      const rows = cache.requests.map(r => `<tr>
        <td class="code">${esc(r.code)}</td><td class="material">${esc(r.material)}</td><td>${Number(r.quantity).toLocaleString()} ${esc(r.unit)}</td>
        <td>${esc(r.site)}</td><td>${esc(r.required_by)}</td><td>${pill(r.priority, r.priority === "Urgent" ? "red" : "gray")}</td>
        <td>${pill(r.status, statusColor(r.status))}</td><td>${r.quote_count} / ${r.order_count}</td>
        <td class="table-actions">
          <button class="action-btn" data-action="request-priority" data-id="${r.id}">${r.priority === "Urgent" ? "Make normal" : "Make urgent"}</button>
          ${r.status === "Open" ? `<button class="action-btn" data-action="request-status" data-status="Cancelled" data-id="${r.id}">Cancel</button>` : ""}
          ${r.status === "Cancelled" || r.status === "Awarded" ? `<button class="action-btn" data-action="request-status" data-status="Open" data-id="${r.id}">Reopen</button>` : ""}
          <button class="action-btn danger" data-action="request-delete" data-id="${r.id}">Delete</button></td></tr>`).join("");
      return heading("DEMAND MANAGEMENT", "Purchase requests", "Correct priorities, cancel or reopen requests, and remove records entered in error.") +
        `<section class="panel">${table(["REQUEST", "MATERIAL", "QUANTITY", "SITE", "REQUIRED BY", "PRIORITY", "STATUS", "QUOTES / ORDERS", ""], rows)}</section>`;
    },

    async orders() {
      cache.orders = (await api("GET", "/orders")).orders;
      const rows = cache.orders.map(o => `<tr>
        <td class="code">${esc(o.code)}</td><td class="material">${esc(o.supplier)}</td><td>${esc(o.material)}</td><td>${money(o.total)}</td>
        <td>${pill(o.status, statusColor(o.status))}</td><td style="white-space:normal;max-width:260px">${esc(o.decision_note)}</td>
        <td class="table-actions">${o.status === "Pending approval" ? `
          <button class="action-btn" data-action="order-decide" data-verb="approve" data-id="${o.id}">Approve</button>
          <button class="action-btn reject" data-action="order-decide" data-verb="reject" data-id="${o.id}">Reject</button>` : ""}</td></tr>`).join("");
      return heading("AWARD & CONTROL", "Orders & approvals", "Decisions made here are attributed to you in the audit log.") +
        `<section class="panel">${table(["ORDER", "SUPPLIER", "MATERIAL", "TOTAL", "STATUS", "NOTES", ""], rows)}</section>`;
    },

    async settings() {
      const s = (await api("GET", "/settings")).settings;
      return heading("POLICY", "Approval policy", "These rules decide when a new purchase order needs a human decision.") + `
      <section class="panel settings-form"><form id="settingsForm"><div class="form-grid">
        ${field({ name: "approval_limit", label: "Automatic approval limit (₦)", type: "number", min: 1, step: "0.01", required: true, value: s.approval_limit, wide: true, help: "Orders above this total need approval." })}
        ${field({ name: "min_supplier_rating", label: "Minimum supplier rating", type: "number", min: 0, max: 5, step: "0.1", required: true, value: s.min_supplier_rating, wide: true, help: "Suppliers rated below this need approval (0–5)." })}
        </div><button class="btn btn-primary">Save policy</button></form></section>`;
    },

    async audit() {
      cache.audit = (await api("GET", "/audit?limit=500")).audit;
      return heading("ACCOUNTABILITY", "Audit log", "Every sign-in and administrative change, newest first.",
        '<input class="search" id="auditSearch" placeholder="Filter log…">') +
        `<section class="panel"><div id="auditTable">${auditTable("")}</div></section>`;
    },
  };
  function auditTable(q) {
    q = q.toLowerCase();
    const rows = cache.audit.filter(a => !q || [a.actor, a.action, a.target, a.detail].join(" ").toLowerCase().includes(q))
      .map(a => `<tr><td>${when(a.at)}</td><td>${esc(a.actor)}</td><td class="code">${esc(a.action)}</td><td>${esc(a.target)}</td><td style="white-space:normal">${esc(a.detail)}</td></tr>`).join("");
    return table(["WHEN (UTC)", "ACTOR", "ACTION", "TARGET", "DETAIL"], rows);
  }

  // ---------------------------------------------------------------- actions
  const find = (list, id) => cache[list].find(x => x.id === Number(id));
  const done = msg => { closeModal(); toast(msg); reload(); };
  const actions = {
    "user-new": () => formModal({
      eyebrow: "ACCESS CONTROL", title: "New user", submit: "Create user",
      hint: "Leave the password blank to generate a one-time password you can pass on securely.",
      fields: [
        { name: "name", label: "Full name", required: true, wide: true },
        { name: "email", label: "Email", type: "email", required: true, wide: true },
        { name: "role", label: "Role", type: "select", options: ["viewer", "manager", "admin"], value: "viewer" },
        { name: "password", label: "Password (optional)", type: "password", autocomplete: "new-password" },
      ],
      onSubmit: async d => {
        const r = await api("POST", "/users", d);
        if (r.temporary_password) { secretModal("User created", `Share this one-time password with ${r.user.email}. It is not shown again.`, r.temporary_password); reload(); }
        else done("User created");
      },
    }),
    "user-edit": id => {
      const u = find("users", id), self = u.id === me.id;
      formModal({
        eyebrow: "ACCESS CONTROL", title: "Edit user", submit: "Save changes",
        hint: self ? "You can't change your own role or deactivate yourself." : "Deactivating a user ends their sessions immediately.",
        fields: [
          { name: "name", label: "Full name", required: true, value: u.name, wide: true },
          ...(self ? [] : [
            { name: "role", label: "Role", type: "select", options: ["viewer", "manager", "admin"], value: u.role },
            { name: "active", label: "Status", type: "select", options: ["Active", "Deactivated"], value: u.active ? "Active" : "Deactivated" }]),
        ],
        onSubmit: async d => { if (d.active) d.active = d.active === "Active"; await api("PATCH", `/users/${u.id}`, d); done("User updated"); },
      });
    },
    "user-reset": id => {
      const u = find("users", id);
      formModal({
        eyebrow: "ACCESS CONTROL", title: `Reset password`, submit: "Reset password", danger: true,
        hint: `Resets the password for ${u.email} and signs them out everywhere. Leave blank to generate a one-time password.`,
        fields: [{ name: "password", label: "New password (optional)", type: "password", wide: true, autocomplete: "new-password" }],
        onSubmit: async d => {
          const r = await api("POST", `/users/${u.id}/reset-password`, d);
          if (r.temporary_password) secretModal("Password reset", `Share this one-time password with ${u.email}.`, r.temporary_password); else done("Password reset");
        },
      });
    },
    "supplier-new": () => supplierForm(null),
    "supplier-edit": id => supplierForm(find("suppliers", id)),
    "supplier-delete": async id => {
      const s = find("suppliers", id);
      if (!confirm(`Delete ${s.name}? This cannot be undone.`)) return;
      await attempt(async () => { await api("DELETE", `/suppliers/${s.id}`); toast("Supplier deleted"); reload(); });
    },
    "request-priority": id => {
      const r = find("requests", id);
      attempt(async () => { await api("PATCH", `/requests/${r.id}`, { priority: r.priority === "Urgent" ? "Normal" : "Urgent" }); toast("Priority updated"); reload(); });
    },
    "request-status": (id, el) => {
      const r = find("requests", id), status = el.dataset.status;
      if (!confirm(`${status === "Open" ? "Reopen" : "Cancel"} ${r.code}?`)) return;
      attempt(async () => { await api("PATCH", `/requests/${r.id}`, { status }); toast(`${r.code} is now ${status}`); reload(); });
    },
    "request-delete": id => {
      const r = find("requests", id);
      if (!confirm(`Delete ${r.code}? This cannot be undone.`)) return;
      attempt(async () => { await api("DELETE", `/requests/${r.id}`); toast("Request deleted"); reload(); });
    },
    "order-decide": (id, el) => {
      const o = find("orders", id), verb = el.dataset.verb;
      formModal({
        eyebrow: "AWARD & CONTROL", title: `${verb === "approve" ? "Approve" : "Reject"} ${o.code}`, submit: verb === "approve" ? "Approve order" : "Reject order",
        danger: verb === "reject", hint: `${o.supplier} · ${money(o.total)}`,
        fields: [{ name: "note", label: "Decision note", wide: true, placeholder: "Optional reason" }],
        onSubmit: async d => { await api("POST", `/orders/${o.id}/decision`, { action: verb, note: d.note }); done(`Order ${verb === "approve" ? "approved" : "rejected"}`); },
      });
    },
  };
  function supplierForm(s) {
    formModal({
      eyebrow: "SUPPLIER NETWORK", title: s ? "Edit supplier" : "New supplier", submit: s ? "Save changes" : "Create supplier",
      fields: [
        { name: "name", label: "Name", required: true, value: s?.name, wide: true },
        { name: "city", label: "City", required: true, value: s?.city },
        { name: "contact", label: "Contact email", value: s?.contact },
        { name: "rating", label: "Rating (0–5)", type: "number", min: 0, max: 5, step: "0.1", value: s?.rating ?? 0, required: true },
        { name: "on_time", label: "On-time delivery (%)", type: "number", min: 0, max: 100, step: "1", value: s?.on_time ?? 0, required: true },
        { name: "status", label: "Status", type: "select", options: ["Active", "Suspended"], value: s?.status || "Active" },
      ],
      onSubmit: async d => { await api(s ? "PATCH" : "POST", s ? `/suppliers/${s.id}` : "/suppliers", d); done(s ? "Supplier updated" : "Supplier created"); },
    });
  }

  document.addEventListener("click", e => {
    const g = e.target.closest("[data-goto]");
    if (g) { document.querySelector(`#nav .nav-item[data-page="${g.dataset.goto}"]`).click(); return; }
    const a = e.target.closest("[data-action]");
    if (a && actions[a.dataset.action]) actions[a.dataset.action](a.dataset.id, a);
  });
  document.addEventListener("submit", e => {
    if (e.target.id !== "settingsForm") return;
    e.preventDefault();
    attempt(async () => {
      await api("PUT", "/settings", Object.fromEntries(new FormData(e.target).entries()));
      toast("Approval policy saved"); reload();
    });
  });
  document.addEventListener("input", e => { if (e.target.id === "auditSearch") $("#auditTable").innerHTML = auditTable(e.target.value); });

  // ---------------------------------------------------------------- boot
  (async () => {
    try { me = (await api("GET", "/me")).user; showApp(); }
    catch (_) { if (!me) showLogin(); }
  })();
})();
