"use strict";
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const kid of kids) n.append(kid);
  return n;
};

function toast(msg) {
  const t = el("div", { class: "toast" }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), 4000);
}

async function api(path, opts = {}) {
  const headers = Object.assign({ "X-RC-Repro-Token": TOKEN }, opts.headers || {});
  if (opts.body) headers["Content-Type"] = "application/json";
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

// ---- repro list (master) ----------------------------------------------------
let ALL_REPROS = [];
let SELECTED = null;
const view = { filter: "", status: "", sort: "name" };
const dstate = { tab: "overview", detail: null, statsTimer: null, points: [] };

async function loadRepros() {
  try {
    const [{ repros }, health] = await Promise.all([
      api("/api/repros"), api("/api/health").catch(() => ({ docker: false })),
    ]);
    ALL_REPROS = repros;
    const dockerTxt = "docker: " + (health.docker ? "up" : "down");
    const badge = $("#docker-badge");
    badge.textContent = dockerTxt; badge.className = "chip " + (health.docker ? "up" : "down");
    $("#sb-docker").textContent = dockerTxt;
    if (SELECTED && !ALL_REPROS.find((r) => r.name === SELECTED)) closeDetail();
    render();
  } catch (e) { toast(e.message); }
}

function render() {
  const grid = $("#repros");
  grid.innerHTML = "";
  const running = ALL_REPROS.filter((r) => r.state === "running").length;
  $("#running-pill").textContent = `${running} running`;
  $("#sb-count").textContent = `${ALL_REPROS.length} repro(s) · ${running} running`;
  $("#sb-refreshed").textContent = "Last refreshed: " + new Date().toLocaleTimeString();
  $(".layout").classList.toggle("no-detail", !SELECTED);
  if (!SELECTED) $("#detail").innerHTML = `<div class="placeholder">Select a workspace to see details, logs, containers and live stats.</div>`;

  let list = ALL_REPROS.filter((r) =>
    (!view.filter || r.name.toLowerCase().includes(view.filter)) &&
    (!view.status || r.state === view.status));
  const key = view.sort;
  list.sort((a, b) => key === "port" ? a.host_port - b.host_port
    : String(a[key]).localeCompare(String(b[key])));

  if (!ALL_REPROS.length) grid.append(el("p", { class: "empty" }, "No repros yet. Click “+ New repro”."));
  for (const r of list) grid.append(card(r));
  grid.append(el("div", { class: "card new", onclick: openCreate },
    el("div", { class: "plus" }, "+"),
    el("div", { class: "t" }, "New repro"),
    el("div", { class: "s" }, "Spin up a fresh RC + Mongo sandbox")));
}

function card(r) {
  const head = el("div", { class: "card-head" }, el("span", { class: "name" }, r.name));
  if (r.default) head.append(el("span", { class: "pill default" }, "default"));
  head.append(el("span", { class: "pill " + r.state }, r.state));
  head.append(el("span", { class: "chev" }, "›"));

  const meta = el("div", { class: "card-meta" },
    `RC ${r.rc_version} · Mongo ${r.mongo_tag} · :${r.host_port} · ${r.preset}`
    + (r.monitoring ? " · monitored" : ""));

  const actions = el("div", { class: "card-actions" });
  actions.append(stop(el("a", { href: r.root_url, target: "_blank", style: "text-decoration:none" },
    el("button", { class: "btn small primary" }, "Open RC"))));
  if (r.state === "running") {
    actions.append(actBtn("Stop", () => doState(r.name, "stop")));
    actions.append(actBtn("Restart", () => doState(r.name, "restart")));
    actions.append(actBtn("Seed", () => openSeed(r.name)));
  } else if (r.state === "stopped") {
    actions.append(actBtn("Start", () => doState(r.name, "start")));
  }
  actions.append(actBtn("Logs", () => showLogs(r.name)));
  actions.append(actBtn("Down", () => doDown(r.name), "danger"));

  const foot = el("div", { class: "card-foot" },
    el("span", {}, "Uptime: " + (r.uptime || "—")),
    el("span", { class: "health " + r.state }, r.health || r.state));

  const c = el("div", { class: "card st-" + r.state + (r.name === SELECTED ? " selected" : ""), onclick: () => selectRepro(r.name) },
    head, meta, actions, foot);
  return c;
}

function stop(node) { node.addEventListener("click", (e) => e.stopPropagation()); return node; }
const actBtn = (label, fn, cls = "") =>
  stop(el("button", { class: "btn small " + cls, onclick: fn }, label));

// ---- detail panel -----------------------------------------------------------
async function selectRepro(name) {
  SELECTED = name; dstate.tab = "overview"; dstate.points = [];
  render();
  try { dstate.detail = await api(`/api/repros/${name}/detail`); }
  catch (e) { toast(e.message); return; }
  renderDetail();
}
function closeDetail() {
  SELECTED = null; dstate.detail = null;
  if (dstate.statsTimer) { clearInterval(dstate.statsTimer); dstate.statsTimer = null; }
  render();
}
function switchTab(t) { dstate.tab = t; renderDetail(); }

function renderDetail() {
  const d = dstate.detail; if (!d) return;
  if (dstate.statsTimer) { clearInterval(dstate.statsTimer); dstate.statsTimer = null; }
  const panel = $("#detail"); panel.innerHTML = "";
  const head = el("div", { class: "d-head" },
    el("span", { class: "dot " + d.state }, "●"),
    el("span", { class: "name" }, d.name),
    el("span", { class: "pill " + d.state }, d.state),
    el("button", { class: "close", onclick: closeDetail }, "×"));
  const tabs = el("div", { class: "tabs" });
  for (const t of ["overview", "logs", "containers", "env vars"]) {
    const key = t === "env vars" ? "env" : t;
    tabs.append(el("button", { class: "tab" + (dstate.tab === key ? " active" : ""), onclick: () => switchTab(key) },
      t.charAt(0).toUpperCase() + t.slice(1)));
  }
  const actions = el("div", { class: "d-actions" });
  actions.append(el("a", { href: d.root_url, target: "_blank", style: "text-decoration:none" },
    el("button", { class: "btn primary" }, "↗ Open RC")));
  if (d.state === "running") {
    actions.append(el("button", { class: "btn", onclick: () => doState(d.name, "stop") }, "Stop"));
    actions.append(el("button", { class: "btn", onclick: () => doState(d.name, "restart") }, "Restart"));
    actions.append(el("button", { class: "btn", onclick: () => openSeed(d.name) }, "Seed"));
    actions.append(el("button", { class: "btn", onclick: () => openImport(d.name) }, "Config"));
    actions.append(el("button", { class: "btn", onclick: () => openPerf(d.name, d.monitoring) }, "Load test"));
    actions.append(el("button", { class: "btn", onclick: () => openCap(d.name) }, "Capacity"));
  } else if (d.state === "stopped") {
    actions.append(el("button", { class: "btn", onclick: () => doState(d.name, "start") }, "Start"));
  }
  actions.append(el("button", { class: "btn danger", onclick: () => doDown(d.name) }, "Down"));
  panel.append(head, tabs, actions, el("div", { class: "d-body", id: "d-body" }));
  renderTab();
}

function renderTab() {
  const d = dstate.detail, body = $("#d-body"); if (!body) return;
  body.innerHTML = "";
  if (dstate.tab === "overview") {
    const kv = (k, v, cls = "") => el("div", { class: "kv" }, el("div", { class: "k" }, k), el("div", { class: "v " + cls }, v));
    body.append(el("div", { class: "kv-grid" },
      kv("RC Version", d.rc_version), kv("MongoDB", d.mongo_tag),
      kv("Port", ":" + d.host_port), kv("Uptime", d.uptime || "—", "green"),
      kv("Preset", d.preset), kv("Health", d.health || "—", d.health === "healthy" ? "green" : "")));
    if (d.links && d.links.length) {
      body.append(el("div", { class: "section-label" }, "Links"));
      const links = el("div", { class: "card-links" });
      for (const l of d.links) links.append(el("a", { class: "linkchip " + (l.kind || ""), href: l.url, target: "_blank" }, l.label));
      body.append(links);
    }
    if (d.state === "running") {
      body.append(el("div", { class: "section-label" }, "Resource usage (live · CPU % / Mem MB)"));
      const box = el("div", { class: "chart-box" }, el("div", { id: "chart" }),
        el("div", { class: "chart-legend" },
          el("span", {}, el("span", { class: "sw", style: "background:#58a6ff" }), "CPU %"),
          el("span", {}, el("span", { class: "sw", style: "background:#3fb950" }), "Mem MB")));
      body.append(box);
      startStats();
    }
    const url = el("div", { class: "urlbox" },
      el("div", {}, el("div", { class: "k" }, "Local URL"), el("a", { href: d.root_url, target: "_blank" }, d.root_url)),
      el("button", { class: "copy", onclick: () => { navigator.clipboard.writeText(d.root_url); toast("copied"); } }, "copy"));
    body.append(url);
  } else if (dstate.tab === "logs") {
    body.append(el("pre", { class: "log", id: "d-logs" }, "loading…"));
    api(`/api/repros/${d.name}/logs?tail=200`).then((x) => { const p = $("#d-logs"); if (p) p.textContent = x.logs || "(no output)"; })
      .catch((e) => { const p = $("#d-logs"); if (p) p.textContent = "error: " + e.message; });
  } else if (dstate.tab === "containers") {
    const t = el("table", { class: "dtable" }, el("tr", {}, el("th", {}, "service"), el("th", {}, "state"), el("th", {}, "status")));
    for (const c of (d.containers || [])) t.append(el("tr", {}, el("td", {}, c.service), el("td", {}, c.state), el("td", { class: "v" }, c.status || c.health || "")));
    body.append(t);
  } else if (dstate.tab === "env") {
    const t = el("table", { class: "dtable" }, el("tr", {}, el("th", {}, "key"), el("th", {}, "value")));
    for (const e of (d.env || [])) t.append(el("tr", {}, el("td", {}, e.key), el("td", { class: "v" }, e.value)));
    body.append(t);
  }
}

function startStats() {
  if (dstate.statsTimer) clearInterval(dstate.statsTimer);
  const poll = async () => {
    try {
      const s = await api(`/api/repros/${SELECTED}/stats`);
      dstate.points.push({ cpu: s.cpu || 0, mem: s.mem_mb || 0 });
      if (dstate.points.length > 60) dstate.points.shift();
      drawChart();
    } catch (_) { /* ignore transient */ }
  };
  poll();
  dstate.statsTimer = setInterval(poll, 3000);
}

const STATS_INTERVAL_S = 3;
function niceMax(v, floor) {
  v = Math.max(v, floor);
  const pow = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * pow >= v) return m * pow;
  return 10 * pow;
}
function fmtAgo(s) { return s <= 0 ? "now" : (s >= 60 ? `-${Math.round(s / 60)}m` : `-${s}s`); }

function drawChart() {
  const box = $("#chart"); if (!box) return;
  const pts = dstate.points;
  const W = 600, H = 220, mL = 34, mR = 46, mT = 14, mB = 24;
  const x0 = mL, x1 = W - mR, y0 = mT, y1 = H - mB;
  const cpuMax = niceMax(Math.max(...pts.map((p) => p.cpu), 0), 10);
  const memMax = niceMax(Math.max(...pts.map((p) => p.mem), 0), 100);
  const n = pts.length;
  const px = (i) => n < 2 ? x1 : x0 + (i / (n - 1)) * (x1 - x0);
  const py = (v, max) => y1 - (v / max) * (y1 - y0);
  const MUT = "#7d8697", GRID = "#232b3a", CPU = "#58a6ff", MEM = "#3fb950";

  let g = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="height:auto;display:block" font-family="ui-monospace, Menlo, monospace" font-size="10">`;
  // 3 horizontal levels: 0, mid, top — left labels = CPU, right labels = Mem
  for (const f of [0, 0.5, 1]) {
    const y = y1 - f * (y1 - y0);
    g += `<line x1="${x0}" y1="${y}" x2="${x1}" y2="${y}" stroke="${GRID}"/>`;
    g += `<text x="${x0 - 5}" y="${y + 3}" fill="${MUT}" text-anchor="end">${(cpuMax * f).toFixed(0)}</text>`;
    g += `<text x="${x1 + 5}" y="${y + 3}" fill="${MUT}" text-anchor="start">${(memMax * f).toFixed(0)}</text>`;
  }
  // vertical time gridlines + labels (5 ticks)
  const span = (n - 1) * STATS_INTERVAL_S;
  for (let k = 0; k <= 4; k++) {
    const f = k / 4, x = x0 + f * (x1 - x0);
    g += `<line x1="${x}" y1="${y0}" x2="${x}" y2="${y1}" stroke="${GRID}" stroke-dasharray="2 3"/>`;
    g += `<text x="${x}" y="${y1 + 15}" fill="${MUT}" text-anchor="middle">${fmtAgo(Math.round((1 - f) * span))}</text>`;
  }
  // axis titles
  g += `<text x="10" y="${(y0 + y1) / 2}" fill="${MUT}" text-anchor="middle" transform="rotate(-90 10 ${(y0 + y1) / 2})">CPU %</text>`;
  g += `<text x="${W - 8}" y="${(y0 + y1) / 2}" fill="${MUT}" text-anchor="middle" transform="rotate(90 ${W - 8} ${(y0 + y1) / 2})">MB</text>`;

  if (n >= 2) {
    const series = (key, max, color) => {
      const line = pts.map((p, i) => `${px(i).toFixed(1)},${py(p[key], max).toFixed(1)}`).join(" ");
      const area = `${x0},${y1} ${line} ${x1},${y1}`;
      return `<polygon points="${area}" fill="${color}" opacity="0.10"/>`
        + `<polyline points="${line}" fill="none" stroke="${color}" stroke-width="2"/>`;
    };
    g += series("mem", memMax, MEM) + series("cpu", cpuMax, CPU);
  } else {
    g += `<text x="${(x0 + x1) / 2}" y="${(y0 + y1) / 2}" fill="#5a6474" text-anchor="middle">collecting…</text>`;
  }
  box.innerHTML = g + `</svg>`;
}

// ---- actions ----------------------------------------------------------------
async function refreshDetail() {
  if (!SELECTED) return;
  try { dstate.detail = await api(`/api/repros/${SELECTED}/detail`); renderDetail(); } catch (_) { /* keep old */ }
}
async function doState(name, action) {
  try { await api(`/api/repros/${name}/state`, { method: "POST", body: JSON.stringify({ action }) }); await loadRepros(); await refreshDetail(); }
  catch (e) { toast(e.message); }
}
async function doDown(name) {
  const vol = confirm(`Remove ${name}. Also DELETE its data volume + record?\n\nOK = delete everything, Cancel = keep data (just stop).`);
  try {
    await api(`/api/repros/${name}?volumes=${vol}&confirm=${vol}`, { method: "DELETE" });
    loadRepros();
  } catch (e) { toast(e.message); }
}
// ---- seed dialog ------------------------------------------------------------
let SEED_TARGET = null;
function openSeed(name) {
  SEED_TARGET = name;
  $("#seed-title").textContent = `Seed data: ${name}`;
  $("#seed-mode").value = "profile";
  syncSeedMode();
  $("#seed-dialog").showModal();
}
function syncSeedMode() {
  const scale = $("#seed-mode").value === "scale";
  $("#seed-profile-row").hidden = scale;
  $("#seed-scale-row").hidden = !scale;
  $("#seed-hint").textContent = scale
    ? "Bulk Mongo insert: credential-less users, no app hooks. For scale/perf only."
    : "Creates real, loginable users/channels/messages via the REST API.";
}
async function submitSeed() {
  const f = $("#seed-form");
  const scale = f.mode.value === "scale";
  $("#seed-dialog").close();
  try {
    let job;
    if (scale) {
      if (!f.scale.value.trim()) { toast("enter a scale spec"); return; }
      job = await api(`/api/repros/${SEED_TARGET}/scale`, { method: "POST", body: JSON.stringify({ scale: f.scale.value.trim() }) });
      streamJob(job.job_id, `Scale prefill: ${SEED_TARGET}`);
    } else {
      job = await api(`/api/repros/${SEED_TARGET}/seed`, { method: "POST", body: JSON.stringify({ profile: f.profile.value }) });
      streamJob(job.job_id, `Seeding ${SEED_TARGET} (${f.profile.value})`);
    }
  } catch (e) { toast(e.message); }
}
async function clearScale() {
  if (!confirm(`Remove all --scale data from ${SEED_TARGET}?`)) return;
  $("#seed-dialog").close();
  try { const { job_id } = await api(`/api/repros/${SEED_TARGET}/scale`, { method: "DELETE" }); streamJob(job_id, `Clearing scale data: ${SEED_TARGET}`); }
  catch (e) { toast(e.message); }
}

// ---- config-import dialog ---------------------------------------------------
let IMPORT_TARGET = null;
function openImport(name) {
  IMPORT_TARGET = name;
  $("#import-title").textContent = `Import customer config: ${name}`;
  $("#import-file").value = "";
  $("#import-form").only.value = "";
  const plan = $("#import-plan"); plan.hidden = true; plan.innerHTML = "";
  $("#import-apply").disabled = true;
  $("#import-dialog").showModal();
}
async function previewImport() {
  const f = $("#import-form");
  if (!f.file.files.length) { toast("choose a settings.json file"); return; }
  const fd = new FormData();
  fd.append("file", f.file.files[0]);
  fd.append("only", f.only.value.trim());
  let plan;
  try {
    const r = await fetch(`/api/repros/${IMPORT_TARGET}/config-import/plan`, {
      method: "POST", headers: { "X-RC-Repro-Token": TOKEN }, body: fd });
    plan = await r.json();
    if (!r.ok) throw new Error(plan.error || `HTTP ${r.status}`);
  } catch (e) { toast(e.message); return; }
  const box = $("#import-plan");
  const c = plan.counts;
  let html = `<b>apply ${c.apply}</b> &middot; skip ${c.redacted} redacted, ${c.denied} identity/env`;
  if (plan.oauth_services.length) html += `<br>oauth pre-create: ${plan.oauth_services.join(", ")}`;
  if (plan.redacted.length) html += `<br><span class="warn">set by hand (redacted): ${plan.redacted.join(", ")}</span>`;
  html += "<hr>" + plan.apply.map((a) => `<div class="kv"><code>${a.id}</code> = ${escapeHtml(a.value)}</div>`).join("");
  box.innerHTML = html; box.hidden = false;
  $("#import-apply").disabled = plan.apply.length === 0;
}
function escapeHtml(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
async function applyImport() {
  const only = $("#import-form").only.value.trim();
  $("#import-dialog").close();
  try { const { job_id } = await api(`/api/repros/${IMPORT_TARGET}/config-import`, { method: "POST", body: JSON.stringify({ only }) }); streamJob(job_id, `Importing config: ${IMPORT_TARGET}`); }
  catch (e) { toast(e.message); }
}
async function showLogs(name) {
  openJob(`Logs: ${name}`);
  $("#job-log").textContent = "loading…";
  try { const { logs } = await api(`/api/repros/${name}/logs?tail=200`); $("#job-log").textContent = logs || "(no output)"; }
  catch (e) { $("#job-log").textContent = "error: " + e.message; }
}
async function doPrune() {
  if (!confirm("Delete every 'down' repro, including data volumes and records?")) return;
  try { const r = await api("/api/prune", { method: "POST", body: JSON.stringify({ confirm: true }) }); toast(`pruned ${r.removed.length}`); loadRepros(); }
  catch (e) { toast(e.message); }
}

// ---- job streaming (SSE) ----------------------------------------------------
function openJob(title) { $("#job-title").textContent = title; $("#job-log").textContent = ""; $("#job-dialog").showModal(); }
function logLine(ev) {
  const line = el("div", { class: ev.level }, (ev.pct != null ? `[${Math.round(ev.pct)}%] ` : "") + ev.message);
  const pre = $("#job-log"); pre.append(line); pre.scrollTop = pre.scrollHeight;
}
function streamJob(jobId, title, onResult) {
  openJob(title);
  const es = new EventSource(`/api/jobs/${jobId}/stream?t=${encodeURIComponent(TOKEN)}`);
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    logLine(ev);
    if (ev.terminal) {
      es.close();
      $("#job-title").textContent = ev.level === "error" ? "Failed" : "Done";
      if (ev.level !== "error" && onResult && ev.data && ev.data.result) onResult(ev.data.result);
      loadRepros().then(refreshDetail);
    }
  };
  es.onerror = () => { es.close(); loadRepros(); };
}

// ---- load-test dialog -------------------------------------------------------
let PERF_TARGET = null;
function openPerf(name, monitored) {
  PERF_TARGET = name;
  $("#perf-title").textContent = `Load test: ${name}`;
  const live = $("#perf-form").live;
  live.checked = false; live.disabled = !monitored;
  $("#perf-live-row").style.opacity = monitored ? "1" : ".5";
  $("#perf-dialog").showModal();
}
async function submitPerf() {
  const f = $("#perf-form");
  const req = {
    scenario: f.scenario.value, vus: parseInt(f.vus.value, 10) || 10,
    duration: f.duration.value.trim() || "30s", users_n: parseInt(f.users_n.value, 10) || 0,
    slo: f.slo.value.trim(), constrain: f.constrain.value.trim(),
    diag: f.diag.checked, stats: f.stats.checked, live: f.live.checked,
  };
  $("#perf-dialog").close();
  try {
    const { job_id } = await api(`/api/repros/${PERF_TARGET}/loadtest`, { method: "POST", body: JSON.stringify(req) });
    streamJob(job_id, `Load test: ${PERF_TARGET} (${req.scenario})`, renderPerfResult);
  } catch (e) { toast(e.message); }
}
function renderPerfResult(r) {
  const s = r.summary || {};
  const pre = $("#job-log");
  const box = el("div", { class: "result" });
  const pill = r.passed ? '<span class="ok">SLO PASS</span>' : (r.slo && r.slo.length ? '<span class="bad">SLO FAIL</span>' : "");
  box.innerHTML =
    `<hr><b>Results</b> ${pill}<br>` +
    `throughput <b>${(s.rps || 0).toFixed(1)} req/s</b> (${(s.count || 0).toFixed(0)} reqs) &middot; ` +
    `errors ${((s.error_rate || 0) * 100).toFixed(2)}%<br>` +
    `latency p50 ${(s.p50 || 0).toFixed(0)}ms &middot; p95 <b>${(s.p95 || 0).toFixed(0)}ms</b> &middot; p99 ${(s.p99 || 0).toFixed(0)}ms`;
  if (r.verdict && r.verdict.length) box.innerHTML += "<br><b>Verdict:</b><br>" + r.verdict.map((v) => "&bull; " + escapeHtml(v)).join("<br>");
  pre.append(box);
  if (r.grafana_url) {
    const url = `${r.grafana_url}/d/rcrepro-k6-loadtest?from=now-15m&to=now&kiosk`;
    pre.append(el("div", { class: "result" },
      el("a", { href: url, target: "_blank", style: "text-decoration:none" }, el("button", { class: "btn small" }, "Open k6 dashboard in Grafana"))));
    const frame = el("iframe", { src: url, class: "grafana-embed" });
    pre.append(frame);
  }
  pre.scrollTop = pre.scrollHeight;
}

// ---- capacity dialog --------------------------------------------------------
let CAP_TARGET = null;
function openCap(name) { CAP_TARGET = name; $("#cap-title").textContent = `Capacity search: ${name}`; $("#cap-dialog").showModal(); }
async function submitCap() {
  const f = $("#cap-form");
  const req = {
    scenario: f.scenario.value, slo: f.slo.value.trim(),
    start: parseInt(f.start.value, 10) || 10, max_vus: parseInt(f.max_vus.value, 10) || 320,
    step_duration: f.step_duration.value.trim() || "20s", users_n: parseInt(f.users_n.value, 10) || 0,
    constrain: f.constrain.value.trim(),
  };
  $("#cap-dialog").close();
  try { const { job_id } = await api(`/api/repros/${CAP_TARGET}/capacity`, { method: "POST", body: JSON.stringify(req) });
    streamJob(job_id, `Capacity: ${CAP_TARGET}`, renderCapResult); }
  catch (e) { toast(e.message); }
}
function renderCapResult(r) {
  const pre = $("#job-log");
  const box = el("div", { class: "result" });
  box.innerHTML = `<hr><b>Capacity: ${escapeHtml(r.result)}</b>` + (r.why ? `<br><span class="warn">${escapeHtml(r.why)}</span>` : "");
  const t = el("table", { class: "dtable" }, el("tr", {}, el("th", {}, "VUs"), el("th", {}, "req/s"), el("th", {}, "p95"), el("th", {}, "err"), el("th", {}, "result")));
  for (const s of (r.steps || [])) t.append(el("tr", {},
    el("td", {}, s.vus), el("td", { class: "v" }, (s.rps || 0).toFixed(1)),
    el("td", { class: "v" }, (s.p95 || 0).toFixed(0) + "ms"), el("td", { class: "v" }, ((s.error_rate || 0) * 100).toFixed(2) + "%"),
    el("td", { class: s.ok ? "" : "v" }, s.ok ? "PASS" : "FAIL")));
  pre.append(box, t); pre.scrollTop = pre.scrollHeight;
}

// ---- benchmark dialog -------------------------------------------------------
function openBench() { $("#bench-dialog").showModal(); }
async function submitBench() {
  const f = $("#bench-form");
  if (!f.versions.value.trim()) { toast("enter at least two versions"); return; }
  $("#bench-dialog").close();
  try { const { job_id } = await api("/api/benchmark", { method: "POST", body: JSON.stringify({ versions: f.versions.value.trim(), seed_profile: f.seed_profile.value }) });
    streamJob(job_id, "Benchmark", renderBenchResult); }
  catch (e) { toast(e.message); }
}
function renderBenchResult(r) {
  const pre = $("#job-log");
  const t = el("table", { class: "dtable" }, el("tr", {}, el("th", {}, "version"), el("th", {}, "boot"), el("th", {}, "seed"), el("th", {}, "msg/s"), el("th", {}, "p95"), el("th", {}, "RC mem"), el("th", {}, "flag")));
  for (const v of (r.results || [])) {
    if (v.ok) t.append(el("tr", {}, el("td", {}, v.version), el("td", { class: "v" }, (v.boot_s || 0).toFixed(1) + "s"),
      el("td", { class: "v" }, (v.seed_total_s || 0).toFixed(1) + "s"), el("td", { class: "v" }, (v.msg_rate || 0).toFixed(1)),
      el("td", { class: "v" }, (v.msg_p95_ms || 0).toFixed(0) + "ms"), el("td", { class: "v" }, (v.rc_mem_mb || 0).toFixed(0) + "MB"),
      el("td", { class: "warn" }, v.flag || "")));
    else t.append(el("tr", {}, el("td", {}, v.version), el("td", { class: "v", colspan: "6" }, "FAILED: " + (v.error || ""))));
  }
  pre.append(el("div", { class: "result", html: "<hr><b>Benchmark results</b>" }), t);
  pre.scrollTop = pre.scrollHeight;
}

// ---- create dialog ----------------------------------------------------------
let PRESETS = [];
async function openCreate() {
  try { PRESETS = (await api("/api/presets")).presets; } catch (e) { toast(e.message); return; }
  const sel = $("#preset-select");
  sel.innerHTML = "";
  for (const p of PRESETS) sel.append(el("option", { value: p.name }, p.name + (p.requires_license ? " (license)" : "")));
  sel.value = "default";
  renderPresetParams();
  $("#create-dialog").showModal();
}
function renderPresetParams() {
  const p = PRESETS.find((x) => x.name === $("#preset-select").value);
  $("#preset-desc").textContent = p ? p.description : "";
  const box = $("#preset-params"); box.innerHTML = "";
  for (const [key, help] of Object.entries((p && p.params_help) || {})) {
    box.append(el("label", {}, `--set ${key}`, el("input", { name: "set:" + key, placeholder: help })));
  }
}
async function submitCreate() {
  const f = $("#create-form");
  const req = {
    version: f.version.value.trim(),
    preset: f.preset.value,
    port: f.port.value ? parseInt(f.port.value, 10) : 0,
    monitor: f.monitor.checked, seed: f.seed.checked, wait: f.wait.checked,
    params: {},
  };
  if (!req.version) { toast("version is required"); return; }
  for (const inp of f.querySelectorAll("input[name^='set:']")) {
    if (inp.value.trim()) req.params[inp.name.slice(4)] = inp.value.trim();
  }
  for (const k of ["name", "reg_token", "mongo", "rc_image", "bind", "root_url"]) {
    if (f[k] && f[k].value.trim()) req[k] = f[k].value.trim();
  }
  for (const k of ["pin", "offline", "no_pull"]) if (f[k] && f[k].checked) req[k] = true;
  $("#create-dialog").close();
  try { const { job_id } = await api("/api/repros", { method: "POST", body: JSON.stringify(req) }); streamJob(job_id, `Creating ${req.version} (${req.preset})`); }
  catch (e) { toast(e.message); }
}

// ---- wiring -----------------------------------------------------------------
$("#btn-new").addEventListener("click", openCreate);
$("#btn-refresh").addEventListener("click", loadRepros);
$("#btn-prune").addEventListener("click", doPrune);
$("#filter").addEventListener("input", (e) => { view.filter = e.target.value.trim().toLowerCase(); render(); });
$("#status-filter").addEventListener("change", (e) => { view.status = e.target.value; render(); });
$("#sort-by").addEventListener("change", (e) => { view.sort = e.target.value; render(); });
$("#preset-select").addEventListener("change", renderPresetParams);
$("#create-cancel").addEventListener("click", () => $("#create-dialog").close());
$("#create-submit").addEventListener("click", (e) => { e.preventDefault(); submitCreate(); });
$("#job-close").addEventListener("click", () => $("#job-dialog").close());
$("#seed-mode").addEventListener("change", syncSeedMode);
$("#seed-cancel").addEventListener("click", () => $("#seed-dialog").close());
$("#seed-clear").addEventListener("click", clearScale);
$("#seed-submit").addEventListener("click", submitSeed);
$("#import-cancel").addEventListener("click", () => $("#import-dialog").close());
$("#import-preview").addEventListener("click", previewImport);
$("#import-apply").addEventListener("click", applyImport);
$("#perf-cancel").addEventListener("click", () => $("#perf-dialog").close());
$("#perf-submit").addEventListener("click", submitPerf);
$("#btn-bench").addEventListener("click", openBench);
$("#cap-cancel").addEventListener("click", () => $("#cap-dialog").close());
$("#cap-submit").addEventListener("click", submitCap);
$("#bench-cancel").addEventListener("click", () => $("#bench-dialog").close());
$("#bench-submit").addEventListener("click", submitBench);

loadRepros();
setInterval(loadRepros, 4000);
