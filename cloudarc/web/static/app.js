/* CloudArc console — dependency-free SPA over the REST API. All data is HTML-escaped before insertion. */
"use strict";

const S = { token: null, me: null, tenant: null, tenantInfo: null, config: null, charts: [] };
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 });
const inr0 = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const money = (v, whole) => (v == null ? "—" : (whole ? inr0 : inr).format(v));
const pct = (v, d = 1) => (v == null ? "—" : `${Number(v).toFixed(d)}%`);
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const fmtDate = (s) => { if (!s) return "—"; const d = new Date(String(s).slice(0, 10) + "T00:00:00"); return `${String(d.getDate()).padStart(2, "0")} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`; };
const fmtTs = (s) => (s ? `${fmtDate(s)} ${String(s).slice(11, 16)}` : "—");
const region = (k) => ({ centralindia: "Central India", southindia: "South India", westindia: "West India", global: "Global" }[k] || k || "—");
const short = (id) => (id ? String(id).split("/").pop() : "—");
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function toast(msg, ms = 3500) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), ms);
}

async function api(path, { method = "GET", body, form, raw } = {}) {
  const headers = { Authorization: `Bearer ${S.token}` };
  let payload;
  if (form) payload = form;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  let r = await fetch(path, { method, headers, body: payload });
  if (r.status === 401 && S.msal && (await refreshSsoToken())) {
    headers.Authorization = `Bearer ${S.token}`;
    r = await fetch(path, { method, headers, body: payload });
  }
  if (r.status === 401) { logout(); throw new Error("Session expired"); }
  if (!r.ok) {
    let detail = r.statusText;
    try { const j = await r.json(); detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (_) { /* not json */ }
    throw new Error(detail);
  }
  if (raw) return r;
  if (r.status === 204) return null;
  return r.json();
}
const T = (p) => `/api/tenants/${encodeURIComponent(S.tenant)}${p}`;
const qs = (o) => { const u = new URLSearchParams(); Object.entries(o).forEach(([k, v]) => { if (Array.isArray(v)) v.forEach((x) => x !== "" && x != null && u.append(k, x)); else if (v !== "" && v != null) u.append(k, v); }); const s = u.toString(); return s ? `?${s}` : ""; };

async function download(path, fallbackName) {
  try {
    const r = await api(path, { raw: true, method: path.includes("/reports/monthly") ? "POST" : "GET" });
    const blob = await r.blob();
    const cd = r.headers.get("Content-Disposition") || "";
    const m = /filename="?([^";]+)"?/.exec(cd);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = m ? m[1] : fallbackName;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (e) { toast(e.message); }
}

/* ---------- auth ---------- */
async function boot() {
  applyTheme(localStorageGet("cloudarc.theme"));
  try { S.config = await (await fetch("/api/config")).json(); } catch (_) { S.config = {}; }
  if (S.config.auth_mode === "entra" && S.config.entra_client_id) {
    $("#sso-btn").classList.remove("hidden");
    try { await initMsal(); if (await refreshSsoToken()) { await start(); return; } } catch (_) { /* fall through to login */ }
  }
  S.token = sessionStorageGet("cloudarc.token");
  if (S.token) { try { await start(); return; } catch (_) { S.token = null; } }
  showLogin();
}
function localStorageGet(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* storage blocked */ } }
function sessionStorageGet(k) { try { return sessionStorage.getItem(k); } catch (_) { return null; } }
function sessionStorageSet(k, v) { try { if (v == null) sessionStorage.removeItem(k); else sessionStorage.setItem(k, v); } catch (_) { /* blocked */ } }

function showLogin() { $("#login").classList.remove("hidden"); $("#shell").classList.add("hidden"); }
function logout() {
  sessionStorageSet("cloudarc.token", null); S.token = null; location.hash = "";
  if (S.msal) { const acct = S.msal.getActiveAccount(); S.msal.clearCache?.({ account: acct }); }
  showLogin();
}

$("#token-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  S.token = $("#token").value.trim();
  try { await start(); await api("/api/session", { method: "POST" }); sessionStorageSet("cloudarc.token", S.token); }
  catch (err) { $("#login-error").textContent = err.message || "Sign-in failed"; S.token = null; }
});

/* Entra ID SSO: MSAL (bundled, no CDN) with a popup that lands on a blank page. Access tokens live in
   MSAL's sessionStorage cache and are refreshed silently, so a session survives the 1-hour token lifetime. */
const SSO_SCOPES = () => [`api://${S.config.entra_client_id}/access_as_user`];
async function initMsal() {
  if (S.msal) return S.msal;
  if (!window.msal) await loadScript("/static/vendor/msal-browser-3.30.0.min.js");
  const app = new window.msal.PublicClientApplication({
    auth: { clientId: S.config.entra_client_id, authority: `https://login.microsoftonline.com/${S.config.entra_tenant_id}`,
            redirectUri: `${location.origin}/static/blank.html` },
    cache: { cacheLocation: "sessionStorage" },
  });
  await app.initialize();
  const acct = app.getAllAccounts()[0];
  if (acct) app.setActiveAccount(acct);
  S.msal = app;
  return app;
}
async function refreshSsoToken() {
  const acct = S.msal?.getActiveAccount();
  if (!acct) return false;
  try {
    const res = await S.msal.acquireTokenSilent({ scopes: SSO_SCOPES(), account: acct });
    S.token = res.accessToken;
    return true;
  } catch (_) { return false; }
}
$("#sso-btn").addEventListener("click", async () => {
  $("#login-error").textContent = "";
  try {
    const app = await initMsal();
    const res = await app.loginPopup({ scopes: SSO_SCOPES(), prompt: "select_account" });
    app.setActiveAccount(res.account);
    S.token = res.accessToken;
    await start();
    await api("/api/session", { method: "POST" });
  } catch (err) {
    $("#login-error").textContent = /not provisioned/.test(err.message || "")
      ? "Signed in with Microsoft, but this account has no CloudArc access yet. Ask a platform admin to add you."
      : (err.message || "Microsoft sign-in failed");
  }
});
function loadScript(src) { return new Promise((ok, fail) => { const s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = fail; document.head.appendChild(s); }); }

async function start() {
  S.me = await api("/api/me");
  $("#login").classList.add("hidden");
  $("#shell").classList.remove("hidden");
  $("#whoami").textContent = S.me.email + (S.me.is_platform_admin ? " · platform admin" : "");
  $$("[data-admin]").forEach((a) => a.classList.toggle("hidden", !S.me.is_platform_admin));
  const sel = $("#tenant-select");
  sel.innerHTML = S.me.tenants.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("");
  const saved = localStorageGet("cloudarc.tenant");
  S.tenant = S.me.tenants.some((t) => t.id === saved) ? saved : S.me.tenants[0]?.id || null;
  if (S.tenant) sel.value = S.tenant;
  await loadTenant();
  route();
}

$("#tenant-select").addEventListener("change", async (e) => { S.tenant = e.target.value; localStorageSet("cloudarc.tenant", S.tenant); await loadTenant(); route(); });
$("#logout-btn").addEventListener("click", logout);
$("#menu-btn").addEventListener("click", () => $(".sidebar").classList.toggle("open"));
$("#theme-btn").addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  applyTheme(next); localStorageSet("cloudarc.theme", next); route();
});
function applyTheme(t) { if (t === "dark" || t === "light") document.documentElement.dataset.theme = t; }

async function loadTenant() {
  if (!S.tenant) { S.tenantInfo = null; $("#as-of").textContent = ""; return; }
  S.tenantInfo = await api(T(""));
  $("#as-of").textContent = S.tenantInfo.data_as_of ? `Data as of ${fmtDate(S.tenantInfo.data_as_of)} · role: ${S.tenantInfo.role.replace("_", " ")}` : "No cost data yet";
}
const canWrite = () => ["analyst", "tenant_admin"].includes(S.tenantInfo?.role);
const isTenantAdmin = () => S.tenantInfo?.role === "tenant_admin";

/* ---------- routing ---------- */
const VIEWS = {
  dashboard: ["Dashboard", viewDashboard], explorer: ["Cost explorer", viewExplorer], allocation: ["Allocation & tags", viewAllocation],
  budgets: ["Budgets", viewBudgets], alerts: ["Alerts", viewAlerts], recommendations: ["Recommendations", viewRecommendations],
  inventory: ["Inventory", viewInventory], reports: ["Reports", viewReports], accounts: ["Accounts & data", viewAccounts],
  admin: ["Administration", viewAdmin],
};
window.addEventListener("hashchange", route);
async function route() {
  if (!S.me) return;
  const key = (location.hash.slice(1) || "dashboard").split("?")[0];
  const [title, fn] = VIEWS[key] || VIEWS.dashboard;
  $("#page-title").textContent = title;
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#${key}`));
  $(".sidebar").classList.remove("open");
  S.charts.forEach((c) => c.destroy()); S.charts = [];
  const view = $("#view");
  if (!S.tenant && key !== "admin") { view.innerHTML = `<div class="card empty">You are not assigned to any client yet. Ask a platform admin.</div>`; return; }
  view.innerHTML = `<div class="empty">Loading…</div>`;
  try { await fn(view); } catch (e) { view.innerHTML = `<div class="card error">${esc(e.message)}</div>`; }
}

/* ---------- shared UI ---------- */
function shareCell(p) { return `<div class="share"><span class="num" style="min-width:48px">${pct(p)}</span><div class="bar"><span style="width:${Math.max(0, Math.min(100, p || 0))}%"></span></div></div>`; }
function table(cols, rows, { empty = "No data", rowAttr } = {}) {
  if (!rows.length) return `<div class="empty">${esc(empty)}</div>`;
  return `<div class="table-wrap"><table><thead><tr>${cols.map((c) => `<th class="${c.num ? "num" : ""}">${esc(c.label)}</th>`).join("")}</tr></thead>
  <tbody>${rows.map((r, i) => `<tr ${rowAttr ? rowAttr(r, i) : ""}>${cols.map((c) => `<td class="${c.num ? "num" : ""}">${c.html ? c.html(r) : esc(c.get ? c.get(r) : r[c.key])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
function sevStatus(sev) { const m = { critical: "critical", warning: "warning", info: "info" }; return `<span class="status ${m[sev] || "info"}">${esc(sev)}</span>`; }
function exportButtons(path, name) {
  return `<span><button class="btn small" data-dl="${esc(path)}${path.includes("?") ? "&" : "?"}format=csv" data-name="${esc(name)}.csv">CSV</button>
  <button class="btn small" data-dl="${esc(path)}${path.includes("?") ? "&" : "?"}format=xlsx" data-name="${esc(name)}.xlsx">XLSX</button></span>`;
}
function wireDownloads(root) { $$("[data-dl]", root).forEach((b) => b.addEventListener("click", () => download(b.dataset.dl, b.dataset.name))); }

function chartTheme() {
  return { text: css("--text-secondary"), grid: css("--grid"), series: css("--series-1"), soft: css("--series-1-soft"), critical: css("--critical"), surface: css("--surface-1") };
}
function lineChart(canvas, series, anomalies) {
  if (!window.Chart) return;
  const th = chartTheme();
  const flagged = new Map(anomalies.map((a) => [a.date, a]));
  const c = new Chart(canvas, {
    type: "line",
    data: {
      labels: series.map((p) => p.date),
      datasets: [
        { label: "Daily cost", data: series.map((p) => p.cost), borderColor: th.series, backgroundColor: th.soft, fill: true, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0.25 },
        { label: "Anomaly", data: series.map((p) => (flagged.has(p.date) ? p.cost : null)), showLine: false, pointRadius: 5, pointHoverRadius: 7,
          pointBackgroundColor: th.critical, pointBorderColor: th.surface, pointBorderWidth: 2 },
      ],
    },
    options: {
      maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: anomalies.length > 0, labels: { color: th.text, boxWidth: 10, usePointStyle: true } },
        tooltip: { callbacks: { title: (it) => fmtDate(it[0].label), label: (it) => (it.datasetIndex === 1 ? `Anomaly: +${flagged.get(it.label).deviation_pct}% vs baseline ${money(flagged.get(it.label).baseline)}` : money(it.parsed.y)) } },
      },
      scales: {
        x: { ticks: { color: th.text, maxTicksLimit: 8, callback(v) { return fmtDate(this.getLabelForValue(v)).slice(0, 6); } }, grid: { display: false } },
        y: { ticks: { color: th.text, callback: (v) => inr0.format(v) }, grid: { color: th.grid }, beginAtZero: false },
      },
    },
  });
  S.charts.push(c);
}
function barChart(canvas, labels, values, { horizontal = true, label = "Cost" } = {}) {
  if (!window.Chart) return;
  const th = chartTheme();
  const c = new Chart(canvas, {
    type: "bar",
    data: { labels, datasets: [{ label, data: values, backgroundColor: th.series, borderRadius: 4, borderSkipped: "start", maxBarThickness: 22 }] },
    options: {
      indexAxis: horizontal ? "y" : "x", maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (it) => money(it.parsed[horizontal ? "x" : "y"]) } } },
      scales: {
        x: { ticks: { color: th.text, maxRotation: 0, ...(horizontal ? { callback: (v) => inr0.format(v) } : {}) }, grid: { color: horizontal ? th.grid : "transparent" } },
        y: { ticks: { color: th.text, ...(horizontal ? {} : { callback: (v) => inr0.format(v) }) }, grid: { color: horizontal ? "transparent" : th.grid } },
      },
    },
  });
  S.charts.push(c);
}

/* ---------- dashboard ---------- */
async function viewDashboard(v) {
  const s = await api(T("/costs/summary"));
  const d0 = new Date(s.as_of + "T00:00:00"); d0.setDate(d0.getDate() - 59);
  const [trend, alerts, recs] = await Promise.all([
    api(T(`/costs/trend${qs({ date_from: d0.toISOString().slice(0, 10), date_to: s.as_of })}`)),
    api(T("/alerts?include_acknowledged=false")),
    api(T("/recommendations?status=open")),
  ]);
  const mom = s.month_over_month;
  const fc = s.forecast;
  const saving = recs.reduce((a, r) => a + r.est_monthly_saving, 0);
  v.innerHTML = `
  <div class="grid cols-4">
    <div class="card stat"><div class="label">${esc(s.current.label)}</div><div class="value">${money(s.current.amount)}</div>
      <div class="sub">${s.current.is_partial ? '<span class="pill tag-partial">Month-to-date · partial</span>' : '<span class="pill">Full month</span>'}</div></div>
    <div class="card stat"><div class="label">${esc(s.previous_month.label)}</div><div class="value">${money(s.previous_month.amount)}</div><div class="sub">Closed month</div></div>
    <div class="card stat"><div class="label">${esc(fc.label)}</div><div class="value">${money(fc.expected)}</div>
      <div class="sub">Range ${money(fc.low, 1)} – ${money(fc.high, 1)}</div></div>
    <div class="card stat"><div class="label">Month-over-month (like-for-like)</div>
      <div class="value">${mom ? (mom.change_pct >= 0 ? "+" : "") + pct(mom.change_pct) : "—"}</div>
      <div class="sub">${mom ? esc(mom.basis) : "No prior-month data"}</div></div>
  </div>
  <div class="grid cols-2" style="margin-top:14px">
    <div class="card span-2"><div class="card-head"><h3>Daily cost — last 60 days</h3>
      <span class="muted small">avg ${money(trend.stats.average)} · min ${money(trend.stats.min)} · max ${money(trend.stats.max)} · ${esc(trend.stats.stability || "")}${trend.anomalies.length ? ` · ${trend.anomalies.length} anomal${trend.anomalies.length > 1 ? "ies" : "y"}` : ""}</span></div>
      <div class="chart-box"><canvas id="c-trend" aria-label="Daily cost line chart"></canvas></div></div>
    <div class="card"><div class="card-head"><h3>Top services — month-to-date</h3></div><div class="chart-box short"><canvas id="c-svc" aria-label="Top services bar chart"></canvas></div>
      ${table([{ label: "Service", key: "service" }, { label: "Cost", num: 1, get: (r) => money(r.cost) }, { label: "Share", html: (r) => shareCell(r.share_pct) }], s.top_services.slice(0, 6))}</div>
    <div class="card"><div class="card-head"><h3>Top resources — month-to-date</h3><a href="#explorer" class="small">Explore →</a></div>
      ${table([{ label: "Resource", html: (r) => `<span title="${esc(r.resource)}">${esc(short(r.resource))}</span>` }, { label: "Cost", num: 1, get: (r) => money(r.cost) }, { label: "Share", html: (r) => shareCell(r.share_pct) }], s.top_resources.slice(0, 8))}</div>
    <div class="card"><div class="card-head"><h3>Open alerts</h3><a href="#alerts" class="small">All alerts →</a></div>
      ${table([{ label: "Severity", html: (r) => sevStatus(r.severity) }, { label: "Alert", key: "message" }], alerts.slice(0, 5), { empty: "No open alerts" })}</div>
    <div class="card"><div class="card-head"><h3>Optimization opportunities</h3><a href="#recommendations" class="small">Workbench →</a></div>
      <div class="stat"><div class="value">${money(saving, 1)}<span class="muted small"> / month identified</span></div></div>
      ${table([{ label: "Recommendation", key: "title" }, { label: "Est. saving", num: 1, get: (r) => money(r.est_monthly_saving, 1) }], recs.slice(0, 5), { empty: "No open recommendations" })}</div>
  </div>`;
  lineChart($("#c-trend"), trend.series, trend.anomalies);
  const top = s.top_services.slice(0, 6);
  barChart($("#c-svc"), top.map((r) => r.service), top.map((r) => r.cost));
}

/* ---------- explorer ---------- */
const EX = { path: [], dims: "service", from: "", to: "", tab: "group" };
const DRILL = ["account", "resource_group", "resource", "meter"];
async function viewExplorer(v) {
  const asOf = S.tenantInfo.data_as_of || new Date().toISOString().slice(0, 10);
  if (!EX.from) { EX.from = asOf.slice(0, 8) + "01"; EX.to = asOf; }
  const tagOpts = (await api(T(""))).required_tags.map((t) => `<option value="tag:${esc(t)}">Tag: ${esc(t)}</option>`).join("");
  v.innerHTML = `
  <div class="card">
    <div class="toolbar">
      <label>From<input type="date" id="ex-from" value="${esc(EX.from)}"></label>
      <label>To<input type="date" id="ex-to" value="${esc(EX.to)}"></label>
      <label>View<select id="ex-tab"><option value="group">Group by</option><option value="drill">Drill-down</option><option value="unit">Unit economics</option></select></label>
      <label id="ex-dim-wrap">Group by<select id="ex-dims">
        <option value="service">Service</option><option value="meter_subcategory">Meter sub-category</option><option value="meter">Meter</option>
        <option value="resource_group">Resource group</option><option value="location">Location</option><option value="account">Account</option>
        <option value="resource">Resource</option><option value="resource_type">Resource type</option><option value="pricing_model">Pricing model</option>
        <option value="month">Month</option><option value="day">Day</option>${tagOpts}</select></label>
      <label>Service filter<input id="ex-svc" placeholder="e.g. Virtual Machines"></label>
      <button class="btn primary" id="ex-go">Apply</button>
      <span id="ex-export"></span>
    </div>
    <div id="ex-crumbs" class="crumbs"></div>
    <div id="ex-body"></div>
  </div>`;
  $("#ex-tab").value = EX.tab; $("#ex-dims").value = EX.dims;
  const run = async () => {
    EX.from = $("#ex-from").value; EX.to = $("#ex-to").value; EX.tab = $("#ex-tab").value; EX.dims = $("#ex-dims").value;
    $("#ex-dim-wrap").classList.toggle("hidden", EX.tab !== "group");
    const base = { date_from: EX.from, date_to: EX.to, service: $("#ex-svc").value.trim() };
    const body = $("#ex-body");
    if (EX.tab === "unit") {
      const path = T(`/costs/unit-economics${qs(base)}`);
      const rows = await api(path);
      $("#ex-crumbs").innerHTML = ""; $("#ex-export").innerHTML = exportButtons(path, "unit-economics");
      body.innerHTML = table([{ label: "Service", key: "service" }, { label: "Meter", key: "meter" }, { label: "Unit", key: "unit" },
        { label: "Quantity", num: 1, get: (r) => r.quantity?.toLocaleString("en-IN") }, { label: "Cost", num: 1, get: (r) => money(r.cost) },
        { label: "Cost / unit", num: 1, get: (r) => (r.cost_per_unit == null ? "—" : `${money(r.cost_per_unit)} / ${r.unit || "unit"}`) }], rows);
    } else if (EX.tab === "drill") {
      const level = DRILL[Math.min(EX.path.length, DRILL.length - 1)];
      const filt = { ...base };
      EX.path.forEach((p) => { filt[p.level] = p.value; });
      const path = T(`/costs/drilldown${qs({ ...filt, level })}`);
      const rows = await api(path);
      $("#ex-export").innerHTML = exportButtons(path, `drilldown-${level}`);
      $("#ex-crumbs").innerHTML = [`<a data-i="0">All accounts</a>`].concat(EX.path.map((p, i) => `› <a data-i="${i + 1}">${esc(p.label)}</a>`)).join(" ");
      $$("#ex-crumbs a").forEach((a) => a.addEventListener("click", () => { EX.path = EX.path.slice(0, +a.dataset.i); run(); }));
      const label = (r) => (level === "account" ? r.account_name : level === "resource" ? short(r.resource) : r[level]) || "—";
      body.innerHTML = table([{ label: level.replace("_", " "), html: (r) => (level !== "meter" ? `<a>${esc(label(r))}</a>` : esc(label(r))) },
        { label: "Cost", num: 1, get: (r) => money(r.cost) }, { label: "Share", html: (r) => shareCell(r.share_pct) }], rows,
        { rowAttr: (r, i) => (level !== "meter" ? `class="clickable" data-i="${i}"` : "") });
      $$("tr.clickable", body).forEach((tr) => tr.addEventListener("click", () => {
        const r = rows[+tr.dataset.i];
        EX.path.push({ level: level === "account" ? "account" : level, value: r[level], label: label(r) }); run();
      }));
    } else {
      const path = T(`/costs/group${qs({ ...base, dims: EX.dims })}`);
      const rows = await api(path);
      $("#ex-crumbs").innerHTML = ""; $("#ex-export").innerHTML = exportButtons(path, `cost-by-${EX.dims}`);
      const lab = (r) => (EX.dims === "account" ? r.account_name : EX.dims === "resource" ? short(r[EX.dims]) : EX.dims === "location" ? region(r.location) : r[EX.dims]) ?? "—";
      body.innerHTML = table([{ label: EX.dims.replace("tag:", "Tag: ").replace("_", " "), get: lab }, { label: "Cost", num: 1, get: (r) => money(r.cost) },
        { label: "Share", html: (r) => shareCell(r.share_pct) }], rows);
    }
    wireDownloads(v);
  };
  $("#ex-go").addEventListener("click", run);
  $("#ex-tab").addEventListener("change", () => { EX.path = []; run(); });
  await run();
}

/* ---------- allocation ---------- */
async function viewAllocation(v) {
  const asOf = S.tenantInfo.data_as_of || new Date().toISOString().slice(0, 10);
  const range = { date_from: asOf.slice(0, 8) + "01", date_to: asOf };
  const [cov, centers, alloc] = await Promise.all([api(T(`/costs/tag-coverage${qs(range)}`)), api(T("/cost-centers")), api(T(`/allocation${qs(range)}`))]);
  const tags = S.tenantInfo.required_tags;
  v.innerHTML = `
  <p class="muted">Month-to-date (${fmtDate(range.date_from)} – ${fmtDate(range.date_to)}, partial month).</p>
  <div class="grid cols-2">
    <div class="card"><div class="card-head"><h3>Required-tag coverage</h3>${exportButtons(T(`/exports/tag_compliance${qs(range)}`), "tag-compliance")}</div>
      ${table([{ label: "Tag", key: "tag" }, { label: "Tagged cost", num: 1, get: (r) => money(r.tagged_cost) }, { label: "Untagged", num: 1, get: (r) => money(r.untagged_cost) },
        { label: "Coverage", html: (r) => shareCell(r.coverage_pct) }], cov.required_tags)}
      <h3 style="margin-top:14px">Resources missing required tags</h3>
      ${table([{ label: "Resource", key: "name" }, { label: "Missing", get: (r) => r.missing_tags.join(", ") }, { label: "Cost", num: 1, get: (r) => money(r.cost) }], cov.violations.slice(0, 15), { empty: "All cost carries the required tags" })}</div>
    <div class="card"><div class="card-head"><h3>Cost by tag</h3><select id="tag-dim">${tags.map((t) => `<option>${esc(t)}</option>`).join("")}</select></div><div id="tag-body"></div></div>
    <div class="card span-2"><div class="card-head"><h3>Cost centers</h3>${exportButtons(T(`/allocation${qs(range)}`), "cost-centers")}</div>
      ${table([{ label: "Cost center", key: "cost_center" }, { label: "Split", get: (r) => (r.percent == null ? "—" : `${r.percent}%`) }, { label: "Cost", num: 1, get: (r) => money(r.cost) },
        { label: "Share", html: (r) => shareCell(r.share_pct) }], alloc)}
      <details style="margin-top:10px"><summary class="small">Rules</summary>
        ${table([{ label: "Name", key: "name" }, { label: "Rules (any matches)", html: (r) => `<code>${esc(JSON.stringify(r.rules))}</code>` }, { label: "Split", get: (r) => `${r.percent}%` },
          { label: "", html: (r) => (canWrite() ? `<button class="btn small danger" data-del="${esc(r.id)}">Delete</button>` : "") }], centers, { empty: "No cost centers defined" })}
      </details>
      ${canWrite() ? `<h3 style="margin-top:14px">New cost center</h3>
      <div class="form-grid">
        <label>Name<input id="cc-name" placeholder="e.g. Production workload"></label>
        <label>Match by<select id="cc-kind"><option value="tag">Tag Key=Value</option><option value="rg">Resource group pattern</option><option value="service">Service</option></select></label>
        <label>Value<input id="cc-val" placeholder="Department=IT · app-prod-* · Virtual Machines"></label>
        <label>Split %<input id="cc-pct" type="number" min="1" max="100" value="100"></label>
        <button class="btn primary" id="cc-add">Add</button>
      </div>` : ""}
    </div>
  </div>`;
  const loadTag = async () => {
    const key = $("#tag-dim").value;
    const rows = await api(T(`/costs/group${qs({ ...range, dims: `tag:${key}` })}`));
    $("#tag-body").innerHTML = table([{ label: key, get: (r) => r[`tag:${key}`] }, { label: "Cost", num: 1, get: (r) => money(r.cost) }, { label: "Share", html: (r) => shareCell(r.share_pct) }], rows);
  };
  $("#tag-dim").addEventListener("change", loadTag);
  await loadTag();
  $$("[data-del]", v).forEach((b) => b.addEventListener("click", async () => { await api(T(`/cost-centers/${b.dataset.del}`), { method: "DELETE" }); route(); }));
  $("#cc-add")?.addEventListener("click", async () => {
    const kind = $("#cc-kind").value, val = $("#cc-val").value.trim();
    let rule;
    if (kind === "tag") { const [k, ...rest] = val.split("="); rule = { tags: { [k.trim()]: rest.join("=").trim() } }; }
    else if (kind === "rg") rule = { resource_group_pattern: val };
    else rule = { services: [val] };
    try { await api(T("/cost-centers"), { method: "POST", body: { name: $("#cc-name").value.trim(), rules: [rule], percent: +$("#cc-pct").value } }); toast("Cost center added"); route(); }
    catch (e) { toast(e.message); }
  });
  wireDownloads(v);
}

/* ---------- budgets ---------- */
function budgetCard(b) {
  const actualW = Math.min(100, b.actual_pct), fcPos = Math.min(100, b.forecast_pct);
  const over = b.actual_pct >= 100;
  const state = over ? ["critical", "Over budget"] : b.forecast_pct >= 100 ? ["serious", "Forecast over"] : b.thresholds_crossed.length ? ["warning", `${b.thresholds_crossed.at(-1)}% threshold crossed`] : ["good", "On track"];
  const cal = b.calibration;
  return `<div class="card">
    <div class="card-head"><h3>${esc(b.name)}</h3><span class="status ${state[0]}">${esc(state[1])}</span></div>
    <div class="muted small">${esc(b.scope_type)}${b.scope_value ? ": " + esc(b.scope_value) : ""} · ${esc(b.period)} · ${fmtDate(b.period_start)} – ${fmtDate(b.period_end)}${b.is_partial ? " · period to date" : ""}</div>
    <div class="meter" role="img" aria-label="Actual ${pct(b.actual_pct)} of budget, forecast ${pct(b.forecast_pct)}">
      <div class="fill ${over ? "over" : ""}" style="width:${actualW}%"></div>
      ${b.thresholds.filter((t) => t < 100).map((t) => `<div class="tick" style="left:${t}%"></div>`).join("")}
      ${b.is_partial ? `<div class="forecast" style="left:calc(${fcPos}% - 1px)" title="Forecast"></div>` : ""}
    </div>
    <div class="grid cols-2 small" style="gap:4px">
      <div>Actual <b>${money(b.actual)}</b> (${pct(b.actual_pct, 0)})</div><div class="num">Budget <b>${money(b.amount)}</b></div>
      <div>Forecast <b>${money(b.forecast)}</b> (${pct(b.forecast_pct, 0)})</div><div class="num muted">Thresholds ${b.thresholds.map((t) => `${t}%`).join(" / ")}</div>
    </div>
    ${cal.status === "below_trailing_spend" ? `<div class="callout"><b>Calibration:</b> ${esc(cal.message)}</div>` : ""}
    ${canWrite() ? `<div style="margin-top:8px;display:flex;gap:6px"><button class="btn small" data-edit="${esc(b.id)}" data-amount="${b.amount}">Change amount</button><button class="btn small danger" data-delb="${esc(b.id)}">Delete</button></div>` : ""}
  </div>`;
}
async function viewBudgets(v) {
  const list = await api(T("/budgets"));
  v.innerHTML = `
  ${canWrite() ? `<div class="card" style="margin-bottom:14px"><h3>New budget</h3>
    <div class="form-grid">
      <label>Name<input id="b-name" placeholder="Production monthly"></label>
      <label>Scope<select id="b-scope"><option value="tenant">Whole client</option><option value="account">Account</option><option value="resource_group">Resource group</option><option value="service">Service</option><option value="tag">Tag (Key=Value)</option><option value="cost_center">Cost center</option></select></label>
      <label>Scope value<input id="b-scope-val" placeholder="—"></label>
      <label>Period<select id="b-period"><option>monthly</option><option>quarterly</option><option>annual</option></select></label>
      <label>Amount (₹)<input id="b-amount" type="number" min="1" step="0.01"></label>
      <label>Thresholds %<input id="b-th" value="50, 80, 100"></label>
      <button class="btn primary" id="b-add">Create</button>
    </div><div id="b-cal"></div></div>` : ""}
  <div class="grid cols-2">${list.map(budgetCard).join("") || '<div class="card empty">No budgets yet</div>'}</div>
  <div style="margin-top:14px">${exportButtons(T("/exports/budget_vs_actual"), "budget-vs-actual")}</div>`;
  const preview = async () => {
    const amount = +$("#b-amount").value;
    if (!amount) { $("#b-cal").innerHTML = ""; return; }
    try {
      const c = await api(T(`/budgets/calibration${qs({ scope_type: $("#b-scope").value, scope_value: $("#b-scope-val").value, period: $("#b-period").value, amount })}`));
      $("#b-cal").innerHTML = c.status === "below_trailing_spend" ? `<div class="callout">${esc(c.message)}</div>`
        : c.status === "ok" ? `<div class="callout ok">Budget is at or above trailing 3-month spend (${money(c.expected_period_spend)} per period).</div>` : `<div class="callout">${esc(c.message)}</div>`;
    } catch (e) { $("#b-cal").innerHTML = `<div class="callout">${esc(e.message)}</div>`; }
  };
  ["#b-amount", "#b-scope", "#b-scope-val", "#b-period"].forEach((s) => $(s)?.addEventListener("change", preview));
  $("#b-add")?.addEventListener("click", async () => {
    try {
      const res = await api(T("/budgets"), { method: "POST", body: {
        name: $("#b-name").value.trim(), scope_type: $("#b-scope").value, scope_value: $("#b-scope-val").value.trim() || null,
        period: $("#b-period").value, amount: +$("#b-amount").value, thresholds: $("#b-th").value.split(",").map((x) => +x.trim()).filter(Boolean) } });
      toast(res.calibration.status === "below_trailing_spend" ? "Budget created — below trailing spend, see calibration note" : "Budget created");
      route();
    } catch (e) { toast(e.message); }
  });
  $$("[data-delb]", v).forEach((b) => b.addEventListener("click", async () => { if (confirm("Delete this budget?")) { await api(T(`/budgets/${b.dataset.delb}`), { method: "DELETE" }); route(); } }));
  $$("[data-edit]", v).forEach((b) => b.addEventListener("click", async () => {
    const amt = prompt("New amount (₹)", b.dataset.amount);
    if (!amt) return;
    try { const r = await api(T(`/budgets/${b.dataset.edit}`), { method: "PATCH", body: { amount: +amt } }); toast(r.calibration.status === "below_trailing_spend" ? r.calibration.message : "Budget updated"); route(); }
    catch (e) { toast(e.message); }
  }));
  wireDownloads(v);
}

/* ---------- alerts ---------- */
async function viewAlerts(v) {
  const [list, channels] = await Promise.all([api(T("/alerts")), api(T("/alert-channels"))]);
  v.innerHTML = `
  <div class="card"><div class="card-head"><h3>Alert history</h3>${canWrite() ? `<button class="btn small" id="eval">Evaluate budgets now</button>` : ""}</div>
    ${table([{ label: "Severity", html: (r) => sevStatus(r.severity) }, { label: "Type", get: (r) => r.kind.replace("_", " ") }, { label: "Alert", key: "message" },
      { label: "Raised", get: (r) => fmtTs(r.created_at) }, { label: "Notified", get: (r) => (r.notified_at ? fmtTs(r.notified_at) : "—") },
      { label: "Acknowledged", html: (r) => (r.acknowledged_at ? `${esc(r.acknowledged_by)}<br><span class="muted small">${fmtTs(r.acknowledged_at)}</span>` : canWrite() ? `<button class="btn small" data-ack="${esc(r.id)}">Acknowledge</button>` : "—") }], list, { empty: "No alerts" })}</div>
  <div class="card" style="margin-top:14px"><div class="card-head"><h3>Notification channels</h3>${isTenantAdmin() ? `<button class="btn small" id="ch-test">Send test</button>` : ""}</div>
    ${table([{ label: "Type", key: "kind" }, { label: "Target", key: "target_hint" }, { label: "Added", get: (r) => fmtTs(r.created_at) },
      { label: "", html: (r) => (isTenantAdmin() ? `<button class="btn small danger" data-delch="${esc(r.id)}">Remove</button>` : "") }], channels, { empty: "No channels — alerts are visible here only" })}
    ${isTenantAdmin() ? `<div class="form-grid" style="margin-top:10px"><label>Type<select id="ch-kind"><option value="teams">Microsoft Teams webhook</option><option value="email">E-mail</option><option value="webhook">Generic webhook (Slack/ITSM)</option></select></label>
      <label style="grid-column: span 2">Target<input id="ch-target" placeholder="https://…webhook.office.com/… or ops@example.com"></label><button class="btn primary" id="ch-add">Add channel</button></div>` : ""}</div>`;
  $$("[data-ack]", v).forEach((b) => b.addEventListener("click", async () => { await api(T(`/alerts/${b.dataset.ack}/ack`), { method: "POST" }); route(); }));
  $$("[data-delch]", v).forEach((b) => b.addEventListener("click", async () => { await api(T(`/alert-channels/${b.dataset.delch}`), { method: "DELETE" }); route(); }));
  $("#eval")?.addEventListener("click", async () => { const r = await api(T("/budgets/evaluate"), { method: "POST" }); toast(`${r.raised.length} new alert(s)`); route(); });
  $("#ch-add")?.addEventListener("click", async () => { try { await api(T("/alert-channels"), { method: "POST", body: { kind: $("#ch-kind").value, target: $("#ch-target").value.trim() } }); route(); } catch (e) { toast(e.message); } });
  $("#ch-test")?.addEventListener("click", async () => { const r = await api(T("/alert-channels/test"), { method: "POST" }); toast(`Test sent: ${r.sent} ok, ${r.failed} failed`); });
}

/* ---------- recommendations ---------- */
const REC = { status: "open" };
const NEXT = { open: [["accepted", "Accept"], ["dismissed", "Dismiss"]], accepted: [["implemented", "Mark implemented"], ["dismissed", "Dismiss"], ["open", "Reopen"]],
  implemented: [["verified", "Verify"], ["open", "Reopen"]], dismissed: [["open", "Reopen"]], resolved: [["open", "Reopen"]], verified: [] };
async function viewRecommendations(v) {
  const [list, savings] = await Promise.all([api(T(`/recommendations${qs({ status: REC.status === "all" ? "" : REC.status })}`)), api(T("/recommendations/savings"))]);
  const tile = (k, label) => `<div class="card stat"><div class="label">${label}</div><div class="value">${money(savings[k]?.[k === "verified" ? "realized_monthly_saving" : "est_monthly_saving"] || 0, 1)}</div><div class="sub">${savings[k]?.count || 0} item(s) per month</div></div>`;
  const groups = [["immediate", "Immediate (0–30 days)"], ["short_term", "Short-term (30–90 days)"], ["ongoing", "Ongoing"]];
  const evidence = (ev) => `<details class="evidence"><summary>Evidence</summary><dl>${Object.entries(ev).map(([k, val]) => `<dt>${esc(k.replaceAll("_", " "))}</dt><dd>${esc(typeof val === "object" ? JSON.stringify(val) : val)}</dd>`).join("")}</dl></details>`;
  v.innerHTML = `
  <div class="grid cols-4">${tile("open", "Open")}${tile("accepted", "Accepted")}${tile("implemented", "Implemented")}${tile("verified", "Verified (realized)")}</div>
  <div class="card" style="margin-top:14px">
    <div class="toolbar"><label>Status<select id="rec-status">${["open", "accepted", "implemented", "verified", "dismissed", "resolved", "all"].map((s) => `<option ${s === REC.status ? "selected" : ""}>${s}</option>`).join("")}</select></label>
      ${canWrite() ? `<button class="btn" id="rec-run">Re-run engine</button>` : ""}${exportButtons(T("/recommendations"), "recommendations")}</div>
    ${groups.map(([key, label]) => { const rows = list.filter((r) => r.horizon === key); return rows.length ? `<h3 style="margin-top:12px">${label}</h3>` + table([
      { label: "Recommendation", html: (r) => `<b>${esc(r.title)}</b><div class="small muted">${esc(r.action)}</div>${evidence(r.evidence)}` },
      { label: "Est. saving / mo", num: 1, get: (r) => money(r.est_monthly_saving, 1) },
      { label: "Confidence · effort · risk", get: (r) => `${r.confidence} · ${r.effort} · ${r.risk}` },
      { label: "Status", html: (r) => `${esc(r.status)}${r.status_reason ? `<div class="small muted">${esc(r.status_reason)}</div>` : ""}${r.realized_monthly_saving != null ? `<div class="small">Realized ${money(r.realized_monthly_saving, 1)}/mo</div>` : ""}<div class="small muted">${esc(r.source)}</div>` },
      { label: "", html: (r) => (canWrite() ? (NEXT[r.status] || []).map(([s, l]) => `<button class="btn small" data-rec="${esc(r.id)}" data-to="${s}">${l}</button>`).join(" ") : "") },
    ], rows) : ""; }).join("") || '<div class="empty">Nothing here</div>'}
  </div>`;
  $("#rec-status").addEventListener("change", (e) => { REC.status = e.target.value; route(); });
  $("#rec-run")?.addEventListener("click", async () => { const r = await api(T("/recommendations/run"), { method: "POST" }); toast(`${r.created} new, ${r.updated} refreshed, ${r.resolved} resolved`); route(); });
  $$("[data-rec]", v).forEach((b) => b.addEventListener("click", async () => {
    let reason = null;
    if (b.dataset.to === "dismissed") { reason = prompt("Reason for dismissing"); if (!reason) return; }
    try { await api(T(`/recommendations/${b.dataset.rec}/status`), { method: "POST", body: { status: b.dataset.to, reason } }); route(); } catch (e) { toast(e.message); }
  }));
  wireDownloads(v);
}

/* ---------- inventory ---------- */
async function viewInventory(v) {
  const [inv, ins, ch] = await Promise.all([api(T("/inventory")), api(T("/inventory/insights")), api(T("/inventory/changes"))]);
  v.innerHTML = `
  <div class="grid cols-2">
    <div class="card"><h3>No cost in ${ins.window_days} days (potentially idle)</h3>${table([{ label: "Name", key: "name" }, { label: "Type", key: "type" }, { label: "Group", key: "resource_group" }], ins.no_cost_resources, { empty: "None" })}</div>
    <div class="card"><h3>Cost-bearing resources without tags</h3>${table([{ label: "Name", key: "name" }, { label: "Type", key: "type" }, { label: "Cost", num: 1, get: (r) => money(r.cost) }], ins.untagged_cost_resources, { empty: "None" })}</div>
  </div>
  <div class="card" style="margin-top:14px"><div class="card-head"><h3>Resources (${inv.length})</h3>${exportButtons(T("/costs/resources"), "resource-explorer")}</div>
    ${table([{ label: "Name", key: "name" }, { label: "Type", key: "type" }, { label: "Resource group", key: "resource_group" }, { label: "Location", get: (r) => region(r.location) },
      { label: "SKU", key: "sku" }, { label: "Tags", get: (r) => Object.entries(r.tags).map(([k, x]) => `${k}=${x}`).join(", ") }, { label: "First seen", get: (r) => fmtDate(r.first_seen) }], inv)}</div>
  <div class="card" style="margin-top:14px"><h3>Changes (90 days)</h3>${table([{ label: "When", get: (r) => fmtTs(r.changed_at) }, { label: "Change", key: "change_type" }, { label: "Resource", get: (r) => r.name || short(r.resource_id) }, { label: "Detail", key: "detail" }], ch, { empty: "No changes" })}</div>`;
  wireDownloads(v);
}

/* ---------- reports ---------- */
async function viewReports(v) {
  const [runs, boqs] = await Promise.all([api(T("/reports")), api(T("/boq"))]);
  const asOf = S.tenantInfo.data_as_of || new Date().toISOString().slice(0, 10);
  const d = new Date(asOf + "T00:00:00"); d.setDate(1); d.setMonth(d.getMonth() - 1);
  const lastClosed = d.toISOString().slice(0, 7);
  v.innerHTML = `
  <div class="grid cols-2">
    <div class="card"><h3>Monthly Cost &amp; Governance Report</h3>
      <p class="muted small">Executive summary, integration, inventory, cost analysis, daily pattern, budgets &amp; forecast, tagging &amp; allocation, governance, recommendations and conclusion — figures identical to the dashboards.</p>
      <div class="toolbar"><label>Month<input type="month" id="rp-month" value="${lastClosed}"></label>
        <button class="btn primary" id="rp-docx">Word (DOCX)</button><button class="btn" id="rp-pdf">PDF</button></div>
      ${table([{ label: "Month", key: "month" }, { label: "Format", key: "format" }, { label: "By", key: "created_by" }, { label: "Created", get: (r) => fmtTs(r.created_at) },
        { label: "", html: (r) => `<button class="btn small" data-dl="${esc(T(`/reports/${r.id}/download`))}" data-name="report.${esc(r.format)}">Download</button>` }], runs.slice(0, 10), { empty: "No reports generated yet" })}</div>
    <div class="card"><h3>Standard reports</h3>
      <p class="muted small">Month-to-date unless filtered in the explorer.</p>
      ${[["cost_allocation", "Cost allocation"], ["resource_explorer", "Resource explorer"], ["unit_economics", "Unit economics"], ["budget_vs_actual", "Budget vs actual"], ["tag_compliance", "Tag compliance"]]
        .map(([k, l]) => `<div class="card-head" style="margin:6px 0"><span>${l}</span>${exportButtons(T(`/exports/${k}`), k)}</div>`).join("")}
      <h3 style="margin-top:14px">Reconcile with Azure Cost Management</h3>
      <div class="toolbar"><label>Closed month<input type="month" id="rc-month" value="${lastClosed}"></label><label>Azure total (₹)<input type="number" id="rc-total" step="0.01"></label><button class="btn" id="rc-go">Check</button></div>
      <div id="rc-out"></div></div>
    <div class="card span-2"><div class="card-head"><h3>Approved estimate (BOQ) vs actual</h3></div>
      <div class="toolbar">
        <label>BOQ<select id="bq-name">${boqs.map((b) => `<option>${esc(b.boq_name)}</option>`).join("")}</select></label>
        <label>Month<input type="month" id="bq-month" value="${lastClosed}"></label><button class="btn" id="bq-go" ${boqs.length ? "" : "disabled"}>Compare</button>
        ${canWrite() ? `<label>Import Azure Pricing Calculator export (.xlsx)<input type="file" id="bq-file" accept=".xlsx"></label><label>Name<input id="bq-newname" placeholder="Approved BOQ"></label><button class="btn" id="bq-up">Import</button>` : ""}
      </div><div id="bq-out">${boqs.length ? "" : '<div class="empty">No BOQ imported</div>'}</div></div>
  </div>`;
  $("#rp-docx").addEventListener("click", () => download(T(`/reports/monthly?month=${$("#rp-month").value}&format=docx`), "report.docx"));
  $("#rp-pdf").addEventListener("click", () => download(T(`/reports/monthly?month=${$("#rp-month").value}&format=pdf`), "report.pdf"));
  $("#rc-go").addEventListener("click", async () => {
    try {
      const r = await api(T("/reconcile"), { method: "POST", body: { month: $("#rc-month").value, provider_total: +$("#rc-total").value } });
      $("#rc-out").innerHTML = `<div class="callout ${r.within_tolerance ? "ok" : ""}"><span class="status ${r.within_tolerance ? "good" : "critical"}">${r.within_tolerance ? "Within ±0.5%" : "Outside ±0.5%"}</span> Platform ${money(r.platform_total)} vs Azure ${money(r.provider_total)} — difference ${money(r.difference)} (${pct(r.difference_pct, 3)})</div>`;
    } catch (e) { toast(e.message); }
  });
  const compare = async () => {
    const name = $("#bq-name").value; if (!name) return;
    const path = T(`/boq/variance${qs({ name, month: $("#bq-month").value })}`);
    const r = await api(path);
    $("#bq-out").innerHTML = `<p>${esc(r.label)}: actual <b>${money(r.actual_total)}</b> vs approved <b>${money(r.estimate_total)}</b> — ${r.difference >= 0 ? "over" : "under"} by ${money(Math.abs(r.difference))} (${pct(r.difference_pct)}) ${exportButtons(path, "boq-variance")}</p>` +
      table([{ label: "Component", key: "component" }, { label: "Estimate", num: 1, get: (x) => money(x.estimate) },
        ...r.regions.map((reg) => ({ label: reg, num: 1, get: (x) => money(x.actual_by_region[reg] || 0) })),
        { label: "Actual", num: 1, get: (x) => money(x.actual) }, { label: "Difference", num: 1, get: (x) => `${x.difference >= 0 ? "+" : ""}${money(x.difference)}` },
        { label: "Δ %", num: 1, get: (x) => (x.difference_pct == null ? "—" : pct(x.difference_pct)) }, { label: "Note", key: "note" }], r.lines);
    wireDownloads($("#bq-out"));
  };
  $("#bq-go").addEventListener("click", compare);
  $("#bq-up")?.addEventListener("click", async () => {
    const f = $("#bq-file").files[0]; const name = $("#bq-newname").value.trim() || "Approved BOQ";
    if (!f) return toast("Choose the .xlsx export first");
    const form = new FormData(); form.append("file", f);
    try { const r = await api(T(`/boq?name=${encodeURIComponent(name)}`), { method: "POST", form }); toast(`Imported ${r.items} items (${money(r.monthly_total)}/month)`); route(); } catch (e) { toast(e.message); }
  });
  if (boqs.length) compare();
  wireDownloads(v);
}

/* ---------- accounts & data ---------- */
const WIZ = { step: 0, cred: null, subs: [] };
async function viewAccounts(v) {
  const [accounts, runs, jobs] = await Promise.all([api(T("/accounts")), api(T("/ingestions")), api(T("/sync-jobs"))]);
  const creds = isTenantAdmin() ? await api(T("/credentials")) : [];
  const req = await api("/api/onboarding/requirements");
  const permStatus = (s) => ({ ok: '<span class="status good">Read-only roles OK</span>', missing_roles: '<span class="status critical">Missing roles</span>', over_privileged: '<span class="status serious">Over-privileged</span>' }[s] || '<span class="status info">Not checked</span>');
  v.innerHTML = `
  <div class="card"><h3>Integration health</h3>
    ${table([{ label: "Account", html: (a) => `${esc(a.name || a.external_id)}<div class="small muted mono">${esc(a.external_id)}</div>` }, { label: "Provider", get: (a) => a.provider.toUpperCase() },
      { label: "Permissions", html: (a) => permStatus(a.permission_status) }, { label: "Last sync", html: (a) => `${a.last_sync_status === "failed" ? '<span class="status critical">failed</span>' : a.last_sync_status ? `<span class="status good">${esc(a.last_sync_status)}</span>` : "—"}<div class="small muted">${fmtTs(a.last_sync_at)}${a.last_sync_duration_s ? ` · ${a.last_sync_duration_s.toFixed(0)} s` : ""}</div>${a.last_error ? `<div class="small error">${esc(a.last_error)}</div>` : ""}` },
      { label: "Source", get: (a) => (a.credential_id ? "API (daily)" : "File uploads") },
      { label: "", html: (a) => (canWrite() && a.credential_id ? `<button class="btn small" data-sync="${esc(a.id)}">Sync now</button>` : "") }], accounts, { empty: "No accounts connected" })}</div>
  ${isTenantAdmin() ? `<div class="card" style="margin-top:14px"><h3>Connect an Azure subscription</h3>
    <div class="steps">${["Prerequisites", "Credentials", "Select subscriptions", "Done"].map((s, i) => `<span class="${i === WIZ.step ? "on" : ""}">${i + 1}. ${s}</span>`).join("")}</div>
    <div id="wiz"></div></div>` : ""}
  <div class="grid cols-2" style="margin-top:14px">
    <div class="card"><h3>Upload a billing export</h3><p class="muted small">Azure cost details / exports (CSV), AWS CUR or CUR 2.0 (CSV/Parquet), GCP billing export. Re-uploading a period replaces it — no duplicates.</p>
      ${canWrite() ? `<div class="toolbar"><input type="file" id="up-file" accept=".csv,.gz,.parquet"><select id="up-provider"><option value="">Auto-detect</option><option value="azure">Azure</option><option value="aws">AWS CUR</option><option value="gcp">GCP</option></select><button class="btn primary" id="up-go">Upload</button></div><div id="up-out"></div>` : '<p class="muted">Analyst role required.</p>'}</div>
    <div class="card"><h3>Sync jobs</h3>${table([{ label: "Status", key: "status" }, { label: "Attempts", key: "attempts", num: 1 }, { label: "Requested", get: (j) => `${j.requested_by || "—"} · ${fmtTs(j.created_at)}` }, { label: "Error", key: "last_error" }], jobs.slice(0, 8), { empty: "No sync jobs" })}</div>
    <div class="card span-2"><h3>Ingestion runs &amp; reconciliation</h3>${table([{ label: "When", get: (r) => fmtTs(r.started_at) }, { label: "Source", key: "source" }, { label: "Provider", key: "provider" }, { label: "Status", key: "status" },
      { label: "Rows", num: 1, get: (r) => (r.rows_loaded ?? "—").toLocaleString("en-IN") }, { label: "Period", get: (r) => `${fmtDate(r.date_from)} – ${fmtDate(r.date_to)}` },
      { label: "Source total", num: 1, get: (r) => (r.source_total == null ? "—" : r.source_total.toFixed(2)) }, { label: "Loaded total", num: 1, get: (r) => (r.loaded_total == null ? "—" : r.loaded_total.toFixed(2)) }], runs.slice(0, 15), { empty: "No ingestions" })}</div>
    ${isTenantAdmin() ? `<div class="card span-2"><h3>Stored credentials</h3>${table([{ label: "Client ID", key: "client_id" }, { label: "Directory", key: "directory_id" }, { label: "Secret", key: "secret_hint" }, { label: "Rotated", get: (c) => fmtTs(c.rotated_at) },
      { label: "", html: (c) => `<button class="btn small" data-rot="${esc(c.id)}">Rotate secret</button>` }], creds, { empty: "No credentials stored" })}</div>` : ""}
  </div>`;
  $$("[data-sync]", v).forEach((b) => b.addEventListener("click", async () => { try { await api(T(`/accounts/${b.dataset.sync}/sync`), { method: "POST" }); toast("Sync queued — runs within a minute"); } catch (e) { toast(e.message); } }));
  $$("[data-rot]", v).forEach((b) => b.addEventListener("click", async () => {
    const secret = prompt("New client secret (validated with Entra ID before it replaces the old one)"); if (!secret) return;
    try { await api(T(`/credentials/${b.dataset.rot}/rotate`), { method: "POST", body: { secret } }); toast("Secret rotated"); route(); } catch (e) { toast(e.message); }
  }));
  $("#up-go")?.addEventListener("click", async () => {
    const f = $("#up-file").files[0]; if (!f) return toast("Choose a file");
    const form = new FormData(); form.append("file", f);
    $("#up-out").innerHTML = '<div class="empty">Loading…</div>';
    try {
      const r = await api(T(`/ingest${qs({ provider: $("#up-provider").value })}`), { method: "POST", form });
      $("#up-out").innerHTML = `<div class="callout ${r.reconciled ? "ok" : ""}">${r.rows_loaded.toLocaleString("en-IN")} rows (${esc(r.provider)}) ${fmtDate(r.date_from)} – ${fmtDate(r.date_to)}. Source total ${r.source_total.toFixed(2)}, loaded ${r.loaded_total.toFixed(2)} — ${r.reconciled ? "reconciled" : "MISMATCH"}.</div>`;
      await loadTenant();
    } catch (e) { $("#up-out").innerHTML = `<div class="callout">${esc(e.message)}</div>`; }
  });
  if (isTenantAdmin()) renderWizard(req);
}
function renderWizard(req) {
  const w = $("#wiz"); if (!w) return;
  if (WIZ.step === 0) {
    w.innerHTML = `<ol class="small">${req.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
      ${table([{ label: "Role", key: "role" }, { label: "Scope", key: "scope" }, { label: "Why", key: "why" }], req.roles)}
      <button class="btn primary" id="wz-next" style="margin-top:10px">I've created the app registration →</button>`;
    $("#wz-next").addEventListener("click", () => { WIZ.step = 1; route(); });
  } else if (WIZ.step === 1) {
    w.innerHTML = `<div class="form-grid"><label>Directory (tenant) ID<input id="wz-dir"></label><label>Application (client) ID<input id="wz-client"></label>
      <label>Client secret<input id="wz-secret" type="password" autocomplete="off"></label><button class="btn primary" id="wz-val">Validate</button></div>
      <p class="muted small">The secret is sent once over TLS, encrypted at rest and never displayed again.</p><div id="wz-out"></div>`;
    $("#wz-val").addEventListener("click", async () => {
      WIZ.cred = { directory_id: $("#wz-dir").value.trim(), client_id: $("#wz-client").value.trim(), secret: $("#wz-secret").value };
      $("#wz-out").innerHTML = '<div class="empty">Checking permissions…</div>';
      try { const r = await api(T("/onboarding/validate"), { method: "POST", body: WIZ.cred }); WIZ.subs = r.subscriptions; WIZ.step = 2; route(); }
      catch (e) { $("#wz-out").innerHTML = `<div class="callout">${esc(e.message)}</div>`; }
    });
  } else if (WIZ.step === 2) {
    w.innerHTML = table([{ label: "", html: (s) => `<input type="checkbox" data-sub="${esc(s.subscription_id)}" ${s.permissions.status === "missing_roles" ? "" : "checked"}>` },
      { label: "Subscription", html: (s) => `${esc(s.name)}<div class="mono small muted">${esc(s.subscription_id)}</div>` }, { label: "State", key: "state" },
      { label: "Roles", html: (s) => Object.entries(s.permissions.roles).map(([r, ok]) => `<span class="status ${ok ? "good" : "critical"}">${esc(r)}</span>`).join("<br>") },
      { label: "Write access", html: (s) => (s.permissions.write_actions.length ? '<span class="status serious">Detected — remove it</span>' : '<span class="status good">None</span>') }], WIZ.subs, { empty: "The credential sees no subscriptions" }) +
      `<button class="btn primary" id="wz-done" style="margin-top:10px">Enable selected &amp; start sync</button> <button class="btn ghost" id="wz-back">Back</button>`;
    $("#wz-back").addEventListener("click", () => { WIZ.step = 1; route(); });
    $("#wz-done").addEventListener("click", async () => {
      const subs = $$("[data-sub]").filter((c) => c.checked).map((c) => ({ subscription_id: c.dataset.sub, name: WIZ.subs.find((s) => s.subscription_id === c.dataset.sub)?.name }));
      if (!subs.length) return toast("Select at least one subscription");
      try { await api(T("/onboarding/complete"), { method: "POST", body: { ...WIZ.cred, subscriptions: subs } }); WIZ.cred = null; WIZ.step = 3; route(); } catch (e) { toast(e.message); }
    });
  } else {
    w.innerHTML = `<div class="callout ok">Connected. The initial sync is queued; cost, inventory and metrics appear as soon as it completes.</div><button class="btn" id="wz-again">Connect another</button>`;
    $("#wz-again").addEventListener("click", () => { WIZ.step = 0; route(); });
  }
}

/* ---------- administration ---------- */
async function viewAdmin(v) {
  if (!S.me.is_platform_admin) { v.innerHTML = '<div class="card empty">Platform admin role required.</div>'; return; }
  const [users, auditRows] = await Promise.all([api("/api/users"), api("/api/audit")]);
  const tenants = S.me.tenants;
  const tname = (id) => tenants.find((t) => t.id === id)?.name || id;
  v.innerHTML = `
  <div class="grid cols-2">
    <div class="card"><h3>New client (tenant)</h3><div class="form-grid"><label>Name<input id="t-name"></label><label>Fiscal year starts (month)<input id="t-fy" type="number" min="1" max="12" value="4"></label><button class="btn primary" id="t-add">Create</button></div></div>
    <div class="card"><h3>New user</h3><div class="form-grid"><label>E-mail (Entra UPN)<input id="u-email"></label><label>Name<input id="u-name"></label><label><span><input type="checkbox" id="u-admin"> Platform admin</span></label><button class="btn primary" id="u-add">Add</button></div></div>
    <div class="card span-2"><h3>Users &amp; client access</h3>
      ${table([{ label: "User", html: (u) => `${esc(u.email)}${u.is_platform_admin ? ' <span class="pill">platform admin</span>' : ""}` },
        { label: "Clients", html: (u) => Object.entries(u.tenants).map(([t, r]) => `${esc(tname(t))}: <b>${esc(r)}</b> <a data-revoke="${esc(u.id)}|${esc(t)}">remove</a>`).join("<br>") || '<span class="muted">none</span>' },
        { label: "Grant", html: (u) => `<select data-gt="${esc(u.id)}">${tenants.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("")}</select> <select data-gr="${esc(u.id)}"><option>viewer</option><option>analyst</option><option>tenant_admin</option></select> <button class="btn small" data-grant="${esc(u.id)}">Grant</button>` },
        { label: "", html: (u) => `<button class="btn small" data-tok="${esc(u.id)}">Issue API token</button>` }], users)}</div>
    <div class="card span-2"><h3>Audit log</h3>${table([{ label: "When", get: (r) => fmtTs(r.ts) }, { label: "User", key: "user" }, { label: "Client", get: (r) => (r.tenant_id ? tname(r.tenant_id) : "—") }, { label: "Action", key: "action" }, { label: "Target", get: (r) => short(r.target) }, { label: "IP", key: "ip" }], auditRows.slice(0, 200))}</div>
  </div>`;
  $("#t-add").addEventListener("click", async () => { try { await api("/api/tenants", { method: "POST", body: { name: $("#t-name").value.trim(), fiscal_year_start: +$("#t-fy").value } }); toast("Client created — grant access below"); S.me = await api("/api/me"); await start(); location.hash = "#admin"; } catch (e) { toast(e.message); } });
  $("#u-add").addEventListener("click", async () => { try { await api("/api/users", { method: "POST", body: { email: $("#u-email").value.trim(), name: $("#u-name").value.trim() || null, is_platform_admin: $("#u-admin").checked } }); route(); } catch (e) { toast(e.message); } });
  $$("[data-grant]", v).forEach((b) => b.addEventListener("click", async () => { const id = b.dataset.grant; await api(`/api/users/${id}/tenants/${$(`[data-gt="${id}"]`).value}`, { method: "PUT", body: { role: $(`[data-gr="${id}"]`).value } }); route(); }));
  $$("[data-revoke]", v).forEach((a) => a.addEventListener("click", async () => { const [u, t] = a.dataset.revoke.split("|"); await api(`/api/users/${u}/tenants/${t}`, { method: "DELETE" }); route(); }));
  $$("[data-tok]", v).forEach((b) => b.addEventListener("click", async () => { const r = await api(`/api/users/${b.dataset.tok}/tokens`, { method: "POST" }); prompt("API token (shown once — copy it now):", r.token); }));
}

boot();
