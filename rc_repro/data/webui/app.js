"use strict";
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

// kind: "error" (default) | "ok" | "info". Toasts were unconditionally red, so
// "copied" and "pruned 3" flashed like failures; and every toast shared one fixed
// position, so a repeating failure (the 4s poll against a bad token) piled
// unreadable banners exactly on top of each other.
function toast(msg, kind = "error") {
  let stack = $("#toasts");
  if (!stack) { stack = el("div", { id: "toasts", class: "toasts" }); document.body.append(stack); }
  const last = stack.lastElementChild;
  if (last && last.textContent === msg) last.remove();   // collapse an identical repeat
  const t = el("div", { class: "toast " + kind, role: "status" }, msg);
  stack.append(t);
  while (stack.childElementCount > 4) stack.removeChild(stack.firstChild);
  setTimeout(() => t.remove(), 4000);
}

// Server-built URLs are always http://localhost:<port> -- that is genuinely the
// repro's ROOT_URL, and the ports are published on the DOCKER HOST. When the GUI
// itself is reached from somewhere else (serve --allow-host behind a proxy, a
// remote lab), "localhost" resolves to the user's own machine and every link is
// dead. Point them at whatever host the GUI was loaded from instead.
function localUrl(u) {
  if (!u) return u;
  try {
    const url = new URL(u, location.origin);
    if (url.hostname === "localhost" || url.hostname === "127.0.0.1") url.hostname = location.hostname;
    return url.toString();
  } catch (_) { return u; }
}

// Set once the server says a login is in force. Until then every 401 is a token
// problem, not an expired session, and bouncing to /signin would be wrong.
let ACCOUNTS = false;
let MY_ROLE = "";
// What this session may do, so the interface can say so BEFORE the click rather
// than refusing after it. The server is the boundary -- every one of these is
// enforced in `guard` regardless -- but a button that always answers with a red
// toast reads as a broken feature, not as a permission.
// Token mode has no roles, so everything is allowed there exactly as before.
const canWrite = () => MY_ROLE !== "readonly";
const canAdmin = () => MY_ROLE === "admin";
const READONLY_WHY = "Your role is readonly. Workspace logs carry LDAP bind "
  + "passwords and OAuth secrets, so readonly cannot read them either.";
let SIGNING_OUT = false;

// A session can expire mid-session (12h idle / 7d absolute) or be revoked from
// another browser. Both arrive as a 401 on whatever the page happened to be
// doing -- the 4s poll, an SSE reconnect, a click. Without this the dashboard
// just started failing silently, which is the failure mode the old
// no-logout-no-login-page design had no answer for either.
function toSignIn(reason) {
  if (SIGNING_OUT) return;
  SIGNING_OUT = true;
  const next = encodeURIComponent(location.pathname + location.search);
  location.assign(`/signin?e=${reason}&next=${next}`);
}

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (opts.body) headers["Content-Type"] = "application/json";
  // same-origin credentials so the session cookie rides along; it is HttpOnly,
  // so this file can neither read nor forge it.
  const r = await fetch(path, Object.assign({ credentials: "same-origin" }, opts, { headers }));
  const data = await r.json().catch(() => ({}));
  if (r.status === 401) { toSignIn("expired"); throw new Error("signed out"); }
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

// ---- repro list (master) ----------------------------------------------------
let ALL_REPROS = [];
let SELECTED = null;
// What the stage is showing: "home" when nothing is picked, "workspace" for
// the selected one, "activity" for the box-wide log. The rail selection and
// the view are separate on purpose -- opening Activity must not silently
// deselect the workspace you were working on.
let VIEW = "home";
// Every view change goes through here, so "what is on the stage" and "what the
// top bar says is current" cannot disagree. They did: Activity and Scenarios were
// reachable and nothing led back, because the brand was a <div> and no link was
// ever marked.
function setView(v) {
  VIEW = v;
  for (const [id, name] of [["#btn-home", "home"], ["#btn-jobs", "activity"],
                            ["#btn-scenarios", "scenarios"]]) {
    const b = $(id);
    if (b) b.toggleAttribute("aria-current", VIEW === name);
  }
}
function goHome() {
  SELECTED = null;
  closeLogs();
  if (dstate.statsTimer) { clearInterval(dstate.statsTimer); dstate.statsTimer = null; }
  dstate.detail = null;
  setView("home");
  renderHome();
  render();
}
const view = { filter: "", status: "", sort: "name",
               scope: localStorage.getItem("rc_scope") || "all" };
// lifecycle.list_repros() reports "?" when docker is down, which is not a usable
// CSS class token (and had no rule), so those cards rendered unstyled.
const stateClass = (s) => (s === "?" ? "unknown" : s);
// Colour the health badge from HEALTH, not state: "Up 2 minutes (unhealthy)" is
// state=running + health=unhealthy, and colouring by state painted it GREEN.
const HEALTH_TONE = { running: "green", bad: "bad", warn: "warn", stopped: "warn" };
function healthClass(r) {
  const h = (r.health || "").toLowerCase();
  if (h.includes("unhealthy")) return "bad";
  if (h.includes("starting")) return "warn";
  if (h === "healthy" || h === "running") return "running";
  return stateClass(r.state);
}
let ME = "";                  // the signed-in user, "" when nobody is signed in
// A job that has not reached a terminal state. "queued" is one: heavy and
// measurement jobs wait for a slot before they start, and testing only for
// "running" made every one of them look like it had already finished.
const jobActive = (s) => s === "running" || s === "queued";
const dstate = { tab: "overview", detail: null, statsTimer: null, points: [] };

async function loadRepros() {
  try {
    const [{ repros }, health] = await Promise.all([
      api("/api/repros"), api("/api/health").catch(() => ({ docker: false })),
    ]);
    ALL_REPROS = repros;
    refreshEdgeBadge();          // its own request: /api/health must stay cheap
    // Identity comes from /api/session now, not from a field bolted onto
    // /api/health -- an endpoint left open for uptime checks should not be
    // reading credentials. Empty on a single-user box (no accounts, no login),
    // in which case the chip stays hidden and every card renders as it always has.
    try {
      const me = await api("/api/session");
      ACCOUNTS = !!me.accounts;
      ME = me.user || "";
      MY_ROLE = me.role || "";
    } catch (_) { /* signed out; api() has already redirected */ }
    // The top bar too: "+ New repro" and "Prune down" are member+, so a readonly
    // session should not be offered them at all.
    for (const [sel, ok] of [["#btn-new", canWrite()], ["#btn-prune", canWrite()],
                             ["#btn-bench", canWrite()]]) {
      const b = $(sel); if (b) b.hidden = !ok;
    }
    // Only worth offering once somebody else's workspaces can appear.
    const scope = $("#scope-filter");
    if (scope) {
      scope.hidden = !ME || !ALL_REPROS.some((r) => (r.owner || r.created_by) &&
                                                    (r.owner || r.created_by) !== ME);
      scope.value = view.scope;
    }
    const who = $("#whoami");
    who.textContent = ME + (MY_ROLE && MY_ROLE !== "admin" ? ` · ${MY_ROLE}` : "");
    who.hidden = !ME;
    DOCKER_OK = !!health.docker;
    const dockerTxt = "docker: " + (health.docker ? "up" : "down");
    const badge = $("#docker-badge");
    badge.textContent = dockerTxt; badge.className = "chip " + (health.docker ? "up" : "down");
    $("#sb-docker").textContent = dockerTxt;
    if (SELECTED && !ALL_REPROS.find((r) => r.name === SELECTED)) closeDetail();
    render();
    refreshHome();
  } catch (e) { toast(e.message); }
}

// ---- the edge ---------------------------------------------------------------
// One Traefik serves every https name on the box. It is NOT a workspace, so it
// never appears in the grid and cannot be stopped from here -- but it holds :443
// for everyone, and something invisible that can take every name down at once is
// worse than one more chip.
let EDGE = null;
// Docker's last known state, so the home page can lead with it being down —
// nothing can start, stop or report while it is, and that outranks every other
// thing the page might say.
let DOCKER_OK = true;

async function refreshEdgeBadge() {
  const badge = $("#edge-badge");
  try { EDGE = await api("/api/edge"); }
  catch (_) { badge.hidden = true; return; }        // no gui extra / no permission
  if (!EDGE.installed) { badge.hidden = true; return; }

  // A route the edge cannot reach answers 502 rather than erroring, so it would
  // otherwise look like a broken workspace. Surfaced ahead of "running".
  const broken = (EDGE.routes || []).filter((r) => !r.reachable).length;
  const n = (EDGE.routes || []).length;
  badge.hidden = false;
  if (!EDGE.running) {
    badge.textContent = `edge: stopped`;
    badge.className = "chip down";
  } else if (broken) {
    badge.textContent = `edge: ${broken} unreachable`;
    badge.className = "chip down";
  } else {
    badge.textContent = `edge: ${n} name${n === 1 ? "" : "s"}`;
    badge.className = "chip up";
  }
}

function openEdge() {
  const body = $("#edge-body");
  body.innerHTML = "";
  if (!EDGE || !EDGE.installed) {
    body.append(el("p", { class: "kv" },
      "No edge yet — it starts with the first --https or --domain workspace."));
  } else {
    body.append(el("div", { class: "kv" },
      EDGE.running ? "Running — holds :80 and :443 for every https name"
                   : "STOPPED — every https name on this box is unreachable"));
    if (EDGE.domain) body.append(el("div", { class: "kv" }, `GUI name: ${EDGE.domain}`));
    if (!(EDGE.routes || []).length) {
      body.append(el("p", { class: "kv" }, "No names registered yet."));
    }
    for (const r of EDGE.routes || []) {
      body.append(el("div", { class: "dcheck " + (r.reachable ? "ok" : "warn") },
        el("span", { class: "dmark" }, r.reachable ? "✓" : "!"),
        el("span", {}, `${r.host || r.name} → ${r.name}`
          + (r.reachable ? "" : "  (unreachable — answers 502)"))));
    }
    if ((EDGE.routes || []).some((r) => !r.reachable)) {
      body.append(el("p", { class: "banner warn" },
        "Run `rc-repro edge restart` to re-attach them."));
    }
  }
  if (!$("#edge-dialog").open) $("#edge-dialog").showModal();
}

function render() {
  const grid = $("#repros");
  grid.innerHTML = "";
  const running = ALL_REPROS.filter((r) => r.state === "running").length;
  // With docker unreachable every state is "?", so "0 running" would be a claim we
  // cannot support (and #running-pill is styled green).
  const unknown = ALL_REPROS.length > 0 && ALL_REPROS.every((r) => r.state === "?");
  const pill = $("#running-pill");
  pill.textContent = unknown ? "state unknown" : `${running} running`;
  pill.className = "pill " + (unknown ? "unknown" : "running");
  $("#sb-count").textContent = `${ALL_REPROS.length} repro(s) · ` +
    (unknown ? "state unknown (docker unavailable)" : `${running} running`);
  $("#sb-refreshed").textContent = "Last refreshed: " + new Date().toLocaleTimeString();
  $(".panes").classList.toggle("no-detail", VIEW !== "workspace" || !SELECTED);
  // render() runs on the four-second poll, so it must redraw the RAIL without
  // touching a stage it does not own. Activity and Scenarios paint themselves and
  // then stay put; anything else here reset them to home within four seconds of
  // opening, which is how the Scenarios page shipped in v0.40.0 and vanished
  // while you were reading it.
  if (VIEW === "home" && !SELECTED) renderHome();
  if (VIEW !== "workspace") $("#actpane").innerHTML = "";

  let list = ALL_REPROS.filter((r) =>
    (!view.filter || r.name.toLowerCase().includes(view.filter)) &&
    (!view.status || r.state === view.status) &&
    (view.scope !== "mine" || !ME || (r.owner || r.created_by) === ME));
  const key = view.sort;
  list.sort((a, b) => key === "port" ? a.host_port - b.host_port
    : String(a[key]).localeCompare(String(b[key])));

  if (!ALL_REPROS.length) grid.append(el("p", { class: "empty" }, "No workspaces yet. Start with “+ New workspace”."));
  else if (!list.length) grid.append(el("p", { class: "empty" }, "Nothing matches this filter."));
  for (const r of list) grid.append(card(r));
}

function card(r) {
  // A row, not a card. The old one was 130px tall and carried six buttons; six
  // workspaces meant 36 buttons on screen and 4.5 of them visible. This is 52px,
  // has no buttons at all, and shows the whole name -- which is the thing anyone
  // is actually scanning for. Everything you can DO lives in the stage, next to
  // the workspace it acts on.
  const busy = pendingOn(r.name);
  const state = busy ? "working" : stateClass(r.state || "?");
  const row = el("button", {
    class: "wrow",
    "data-state": state,
    "aria-current": r.name === SELECTED ? "true" : "false",
    title: r.name,
    onclick: () => selectRepro(r.name),
  });
  const r1 = el("span", { class: "r1" }, el("span", { class: "nm" }, r.name));
  if (r.default) r1.append(el("span", { class: "star", title: "used by CLI commands with no --name" }, "★"));
  r1.append(el("span", { class: "ver" }, r.rc_version || "?"));
  const bits = [r.preset, ":" + r.host_port, r.created_by].filter(Boolean);
  if (r.monitoring) bits.push("monitored");
  row.append(r1, el("span", { class: "meta" }, bits.join(" · ")));
  row.append(el("span", { class: "r3" },
    el("span", { class: "wstate " + (busy ? "working" : stateClass(r.state)) },
      busy ? (BUSY_VERB[busy] || busy) : (r.state === "?" ? "state unknown" : r.state)),
    el("span", { class: "wage" }, r.uptime || "")));
  return row;
}

const PENDING = new Map();
const BUSY_VERB = {
  Stop: "Stopping", Start: "Starting", Restart: "Restarting", Down: "Removing",
  "Make default": "Setting default", "API token": "Minting",
  "Check TLS": "Checking", Env: "Applying",
};
const pendingOn = (name) => PENDING.get(name) || "";

// One builder for the card and the detail panel so a pending action looks the
// same in both. `pending` is the label of whatever is running on this repro:
// every button is disabled while it is set, because these are compose commands
// against a single project and Restart on top of an in-flight Stop raced them.
function actionButton(label, fn, { cls = "", small = false, pending = "", title = "" } = {}) {
  const klass = "btn" + (small ? " small" : "") + (cls ? " " + cls : "");
  const attrs = title ? { title } : {};
  if (!pending) return el("button", { ...attrs, class: klass, onclick: () => fn() }, label);
  if (pending !== label) return el("button", { ...attrs, class: klass, disabled: "" }, label);
  return el("button", { ...attrs, class: klass + " working", disabled: "" },
    el("span", { class: "spin" }), (BUSY_VERB[label] || label) + "…");
}
const actBtn = (label, fn, cls = "", pending = "") =>
  stop(actionButton(label, fn, { cls, small: true, pending }));

// Run a slow synchronous action, keeping the UI honest about it from click to
// completion. The state lives in PENDING so both re-render paths (poll -> render,
// job callbacks -> renderDetail) redraw it instead of wiping it.
async function runAction(name, label, fn) {
  if (PENDING.has(name)) return;          // a second click while one is in flight
  PENDING.set(name, label);
  render();
  if (SELECTED === name) renderDetail();
  try { await fn(); }
  catch (e) { toast(e.message); }
  finally {
    PENDING.delete(name);
    await loadRepros();                   // re-renders with the real new state
    if (SELECTED === name) await refreshDetail({ force: true });
  }
}


// The action pane: everything you can do TO this workspace, grouped by intent.
//
// It used to be thirteen buttons in one row of the panel, in one weight -- "Open
// RC", which you press ninety times out of a hundred, sat next to "Capacity",
// which takes forty minutes, next to "Down", which destroys a customer
// reproduction. The row was read every single time because nothing in it was
// ranked.
//
// The grouping is not decoration, it is the teaching: "put data in it" explains
// what Seed does to somebody who never read the CLI's help. That only works if
// they can SEE it, which is why this is a pane and not a menu.
function renderActionPane(d, busyLabel) {
  const pane = $("#actpane");
  pane.innerHTML = "";
  if (!canWrite()) {
    pane.append(el("p", { class: "apnote", title: READONLY_WHY },
      "You have the readonly role, so nothing here is available to you. "
      + "Opening the workspace and reading this panel are."));
    return;
  }
  const running = d.state === "running";
  const groups = [
    ["Put data in it", [
      ["Add sample data", () => openSeed(d.name), running],
      ["Import settings", () => openImport(d.name), running],
    ]],
    ["Measure it", [
      ["Run a load test", () => openPerf(d.name, d.monitoring), running],
      ["Find capacity", () => openCap(d.name), running],
      // Named for the thing rather than for one of its parts: it attaches
      // Prometheus, Grafana, Loki and the exporters, and "stream to Grafana"
      // described the last hop of it.
      [d.monitoring ? "Detach monitoring" : "Attach monitoring",
       () => doMonitor(d.name, !!d.monitoring), running],
    ]],
    ["Connect to it", [
      ["PAT and Token", () => doPat(d.name), running],
      ["Send an API call", () => openCall(d.name), running],
    ]],
    ["Keep or move it", [
      ["Back up now", () => doBackup(d.name), running],
      ["Upgrade version", () => openUpgrade(d.name, d.rc_version), running],
      ["Check the certificate", () => doTlsStatus(d.name), running && !!d.public_url],
      ["Use for CLI commands", () => doDefault(d.name), !d.is_default],
    ]],
  ];
  for (const [title, items] of groups) {
    const usable = items.filter(([, , ok]) => ok !== false || running);
    if (!usable.length) continue;
    pane.append(el("div", { class: "apgroup" }, title));
    const listEl = el("div", { class: "aplist" });
    for (const [label, fn, ok] of items) {
      const b = el("button", { onclick: fn }, label);
      if (!ok || busyLabel) b.disabled = true;
      listEl.append(b);
    }
    pane.append(listEl);
  }
  if (!running) {
    pane.append(el("p", { class: "apnote" },
      `Most of these need the workspace running. It is ${d.state === "?" ? "unreachable" : d.state}.`));
  }
  // Pinned to the bottom, not trailing the last group: it is not the eleventh
  // item on a menu, it is the thing you have to travel to.
  const danger = el("div", { class: "apdanger" });
  const btn = el("button", { onclick: () => doDown(d.name) }, "Take this workspace down");
  if (busyLabel) btn.disabled = true;
  danger.append(btn, el("p", {},
    "Removes its containers. You are asked separately before any data is deleted."));
  pane.append(danger);
}


// ---- home ------------------------------------------------------------------
// What the stage shows when nothing is selected. It used to say "Select a
// workspace", which answers a question nobody asked.
//
// It leads with CAPACITY because that is what actually limits this tool: `up`
// refuses without headroom, and seven concurrent stacks once OOM-killed a 10 GB
// host with every individual create having succeeded. "Room for about 2 more" is
// the sentence that decides what you do next; a row of totals would look like a
// dashboard and say nothing.
let HOME = { cap: null, audit: [], at: 0 };
const HOME_EVERY = 15000;

async function refreshHome(force) {
  if (!force && Date.now() - HOME.at < HOME_EVERY) return;
  HOME.at = Date.now();
  const [cap, audit] = await Promise.all([
    api("/api/machine").catch(() => null),
    api("/api/audit?limit=6").catch(() => ({ lines: [] })),
  ]);
  if (cap) HOME.cap = cap;
  HOME.audit = (audit && audit.lines) || [];
  if (!SELECTED) renderHome();
}

function capacityCard(cap) {
  const box = el("div", { class: "capcard" });
  if (!cap || !cap.known) {
    box.append(el("div", { class: "caphead" }, el("span", { class: "lbl" }, "Memory on this machine")),
      el("p", { class: "apnote", style: "padding:0" },
        "rc-repro reads /proc/meminfo, which this machine does not have — so it "
        + "cannot say how much room is left, and `up` will not refuse on memory here."));
    return box;
  }
  const usedMb = Math.max(0, cap.total_mb - cap.available_mb);
  const pct = (n) => Math.max(0, Math.min(100, (n / cap.total_mb) * 100));
  const gb = (mb) => (mb / 1024).toFixed(1);
  const head = el("div", { class: "caphead" },
    el("span", { class: "lbl" }, "Memory on this machine"));
  // Speak up only when the answer is nearly no. "room for about 6 more" is a
  // number you read past; the bar below already shows how full the machine is,
  // and this line exists to catch the moment it stops being fine.
  if (cap.room <= 1) {
    head.append(el("span", { class: "caproom" + (cap.room < 1 ? " tight" : "") },
      cap.room >= 1 ? `room for about ${cap.room} more` : "not enough for another workspace"));
  }
  box.append(head);
  box.append(el("div", { class: "capnum" }, `${gb(usedMb)} GB`,
    el("small", {}, ` of ${gb(cap.total_mb)} GB in use`)));
  box.append(el("div", { class: "capbar" },
    el("i", { class: "used", style: `width:${pct(usedMb)}%` }),
    el("i", { class: "reserved", style: `width:${pct(cap.reserve_mb)}%`,
              title: "kept back for the machine itself" })));
  box.append(el("div", { class: "caplegend" },
    el("span", {}, el("i", { class: "k used" }), `in use ${gb(usedMb)} GB`),
    el("span", {}, el("i", { class: "k reserved" }), `held back ${gb(cap.reserve_mb)} GB`),
    el("span", {}, el("i", { class: "k free" }),
      `free ${gb(Math.max(0, cap.available_mb - cap.reserve_mb))} GB`)));
  return box;
}

function attentionItems() {
  const out = [];
  if (!DOCKER_OK) {
    out.push(["Docker is not answering",
      "Nothing can start, stop or report state until it is back.", openDoctor, "Check"]);
  }
  const broken = ((EDGE && EDGE.routes) || []).filter((r) => !r.reachable);
  for (const r of broken) {
    out.push([`The edge cannot reach ${r.name}`,
      "Its https name answers 502 rather than erroring, so it looks like a broken "
      + "workspace instead of a broken route.", openEdge, "Look"]);
  }
  for (const r of ALL_REPROS.filter((x) => x.state === "down")) {
    out.push([`${r.name} is down`,
      `It still holds port :${r.host_port} and its data volume.`,
      () => selectRepro(r.name), "Review"]);
  }
  return out;
}

function renderHome() {
  const panel = $("#detail");
  panel.innerHTML = "";
  const running = ALL_REPROS.filter((r) => r.state === "running");
  const hour = new Date().getHours();
  const part = hour < 12 ? "morning" : hour < 18 ? "afternoon" : "evening";
  const head = el("div", { class: "home-head" },
    el("h1", {}, ME ? `Good ${part}, ${ME}` : "Your workspaces"),
    el("p", {}, `${ALL_REPROS.length} workspace(s) · ${running.length} running`
      + (DOCKER_OK ? "" : " · docker is not answering")));
  const body = el("div", { class: "home-body" });
  const items = attentionItems();
  if (items.length) {
    const card = el("div", { class: "hcard" },
      el("div", { class: "hcard-h" }, el("span", { class: "lbl" }, "Needs you"),
        el("span", { class: "lbl" }, String(items.length))));
    for (const [title, why, fn, label] of items) {
      card.append(el("div", { class: "att" },
        el("span", { class: "att-i" }, "▲"),
        el("span", { class: "att-t" }, el("b", {}, title), " — " + why),
        el("button", { class: "btn small", onclick: fn }, label)));
    }
    body.append(card);
  }

  const rcard = el("div", { class: "hcard" },
    el("div", { class: "hcard-h" }, el("span", { class: "lbl" }, "Running now")));
  if (running.length) {
    const grid = el("div", { class: "mini" });
    for (const r of running) {
      grid.append(el("button", { class: "minicard", onclick: () => selectRepro(r.name) },
        el("span", { class: "n" }, r.name),
        el("span", { class: "m" }, `${r.rc_version} · ${r.preset} · :${r.host_port}`),
        el("span", { class: "m up" }, r.uptime || "")));
    }
    rcard.append(grid);
  } else {
    rcard.append(el("p", { class: "empty" }, canWrite()
      ? "Nothing is running. Start one from the list, or create a workspace."
      : "Nothing is running."));
  }
  body.append(rcard);
  body.append(capacityCard(HOME.cap));

  const acard = el("div", { class: "hcard" },
    el("div", { class: "hcard-h" }, el("span", { class: "lbl" }, "Recent activity"),
      el("button", { class: "btn small", onclick: () => openJobs("history") }, "All activity")));
  if (HOME.audit.length) {
    for (const a of HOME.audit) {
      acard.append(el("div", { class: "arow", "data-denied": String(a.outcome === "denied") },
        el("span", { class: "who" }, a.actor || "—"),
        el("span", { class: "kind" }, a.kind),
        el("span", { class: "what" }, a.label || ""),
        el("span", { class: "when" }, (a.ts || "").replace("T", " ").slice(5, 16))));
    }
  } else {
    acard.append(el("p", { class: "empty" }, "Nothing recorded yet."));
  }
  body.append(acard);
  panel.append(head, body);
}


// ---- scenarios --------------------------------------------------------------
// Where a preset's notes finally live. They were printed by `up` and `info` and
// shown nowhere in the browser -- which for `oidc` means a GUI-only user could
// not discover that the scenario does not work at all until an /etc/hosts line
// exists. The notes are pre-wrapped for an 80-column terminal, so they are set
// in a <pre> and allowed to scroll rather than re-flowed into a narrow column.
async function openScenarios() {
  setView("scenarios");
  $(".panes").classList.add("no-detail");
  $("#actpane").innerHTML = "";
  const panel = $("#detail");
  panel.innerHTML = "";
  const head = el("div", { class: "home-head" }, el("h1", {}, "Scenarios"),
    el("p", {}, "What each one adds to a plain Rocket.Chat, and what you have to do to use it."));
  const body = el("div", { class: "home-body" });
  body.append(el("p", { class: "empty" }, "loading…"));
  panel.append(head, body);
  let list;
  try { list = (await api("/api/presets")).presets || []; }
  catch (e) { body.innerHTML = ""; body.append(el("p", { class: "empty" }, e.message)); return; }
  if (VIEW !== "scenarios") return;                 // a newer view won the race
  body.innerHTML = "";
  for (const s of list) {
    const card = el("div", { class: "hcard scen" });
    const h = el("div", { class: "hcard-h" }, el("b", {}, s.name));
    if (s.requires_license) h.append(el("span", { class: "pill small" }, "needs a licence"));
    card.append(h);
    const inner = el("div", { class: "scen-b" });
    if (s.description) inner.append(el("p", { class: "scen-d" }, s.description));
    if (s.notes && s.notes.length) {
      inner.append(el("div", { class: "section-label" }, "Using this scenario"));
      inner.append(noteBlock(s.notes));
    }
    card.append(inner);
    const foot = el("div", { class: "scen-f" },
      el("span", {}, s.ports && s.ports.length
        ? "Extra ports: " + s.ports.join(", ") : "No extra ports"));
    if (canWrite()) {
      foot.append(el("button", { class: "btn small", onclick: () => openCreate(s.name) },
        "Create a workspace with this"));
    }
    card.append(foot);
    body.append(card);
  }
}


// The Scenario card: what this workspace adds beyond a plain Rocket.Chat, where
// those things are, and what you have to do to use them — in one block instead of
// a "Where things are" list and a "Using this scenario" list that never referred
// to each other.
function scenarioCard(d) {
  const card = el("div", { class: "panelcard" });
  // Title it by what is actually IN it. A plain workspace with monitoring
  // attached has links and notes but no scenario, and calling that block
  // "Scenario · default" described the preset field rather than the contents.
  const scenario = d.preset && d.preset !== "default" ? d.preset : "";
  const head = el("div", { class: "panelcard-h" },
    el("span", { class: "section-label flat" },
      scenario ? "Scenario · " + scenario : "What is in this workspace"));
  if (scenario) {
    head.append(el("button", { class: "linkbtn", onclick: openScenarios },
      "About this scenario ↗"));
  }
  card.append(head);

  // A command that is NOT a url is something you must set up before the scenario
  // works at all -- `oidc` is broken until 127.0.0.1 keycloak is in /etc/hosts.
  // A command that IS a url is just a place to go, and colouring that as a warning
  // would be crying wolf.
  const items = parseNotes(d.notes);
  const setup = items.find((i) => i.kind === "cmd" && !/^https?:\/\//.test(i.value));
  if (setup) {
    card.append(el("div", { class: "setup" },
      el("span", { class: "setup-i" }, "▲"),
      el("span", { class: "setup-t" },
        el("b", {}, "Do this once, on your machine"),
        el("span", { class: "setup-w" },
          "This scenario cannot work until the line below exists. Needs sudo."),
        el("code", {}, setup.value)),
      el("button", { class: "cp", onclick: () => copy(setup.value) }, "Copy")));
  }

  // A note that names a place the link table ALREADY lists is not a second place,
  // it is the rest of what is known about that one. Monitoring is the plain case:
  // the links say where Grafana is, and the notes say the same thing again plus
  // the password — which is how the same three urls came to be on screen twice,
  // once as rows and once as prose.
  const key = (u) => String(u || "").replace(/\/+$/, "");
  const byUrl = new Map();
  for (const it of items) if (it.kind === "place") byUrl.set(key(it.url), it);
  const shown = new Set();
  for (const l of d.links || []) {
    const note = byUrl.get(key(l.url));
    if (note) shown.add(note);
    card.append(placeRow({ label: l.label, url: l.url, kind: l.kind,
                           what: note ? note.what : "", creds: note ? note.creds : "",
                           sub: note ? note.sub : "" }));
  }

  const rest = items.filter((i) => i !== setup && !shown.has(i));
  if (rest.length) {
    card.append(renderNoteItems(el("div", { class: "panelcard-b notes" }), rest));
  }
  return card;
}

// ---- detail panel -----------------------------------------------------------
async function selectRepro(name) {
  setView("workspace");
  SELECTED = name; dstate.tab = "overview"; dstate.points = [];
  // Tear the PREVIOUS repro's panel down first. Leaving it up meant that while the
  // fetch was in flight -- and permanently if it failed -- the list highlighted B
  // while the panel still showed A, with A's destructive Down button wired to A.
  closeLogs();
  if (dstate.statsTimer) { clearInterval(dstate.statsTimer); dstate.statsTimer = null; }
  dstate.detail = null;
  render();
  const panel = $("#detail");
  panel.innerHTML = "";
  panel.append(el("div", { class: "placeholder" }, `Loading ${name}…`));
  let detail;
  try { detail = await api(`/api/repros/${name}/detail`); }
  catch (e) {
    toast(e.message);
    panel.innerHTML = "";
    panel.append(el("div", { class: "placeholder" }, `Could not load ${name}.`));
    return;
  }
  if (SELECTED !== name) return;        // a newer selection won the race
  dstate.detail = detail;
  renderDetail();
}
function closeDetail() {
  setView("home");
  SELECTED = null; dstate.detail = null;
  if (dstate.statsTimer) { clearInterval(dstate.statsTimer); dstate.statsTimer = null; }
  closeLogs();
  render();
}
function switchTab(t) { dstate.tab = t; renderDetail(); }

function renderDetail() {
  const d = dstate.detail; if (!d) return;
  if (dstate.statsTimer) { clearInterval(dstate.statsTimer); dstate.statsTimer = null; }
  const panel = $("#detail"); panel.innerHTML = "";
  const busyLabel = pendingOn(d.name);
  // Same builder as the cards: `dBtn` disables every action while one is in
  // flight and animates the one doing the work.
  const dBtn = (label, fn, cls = "", title = "") =>
    actionButton(label, fn, { cls, title, pending: busyLabel });
  const head = el("div", { class: "d-head" },
    el("span", { class: "dot " + stateClass(d.state) }, "●"),
    el("span", { class: "name" }, d.name),
    busyLabel
      ? el("span", { class: "pill working" }, (BUSY_VERB[busyLabel] || busyLabel).toLowerCase() + "…")
      : el("span", { class: "pill " + stateClass(d.state) }, d.state),
    el("button", { class: "close", onclick: closeDetail }, "×"));
  const sub = el("div", { class: "d-sub" },
    el("b", {}, d.rc_version || "?"), el("i", {}, "/"), `mongo ${d.mongo_tag || "?"}`);
  if (d.preset) sub.append(el("i", {}, "·"), d.preset);
  sub.append(el("i", {}, "·"), ":" + d.host_port);
  if (d.created_by) {
    sub.append(el("i", {}, "·"), d.created_by + (d.created_by === ME ? " (you)" : ""));
  }
  if (d.public_url) {
    const route = (EDGE && EDGE.routes || []).find((r) => r.name === d.name);
    sub.append(el("i", {}, "·"),
      el("span", { class: route && route.reachable === false ? "bad" : "" },
        route && route.reachable === false
          ? "https — unreachable, answers 502"
          : (TLS_LABEL[d.tls] || "https")));
  }
  const tabs = el("div", { class: "tabs" });
  for (const t of ["overview", "logs", "containers", "env vars", "backups"]) {
    const key = t === "env vars" ? "env" : t;
    // Logs and env are member+ on the server: both hand over real credentials.
    // Rendering a tab that can only answer 403 is worse than not rendering it.
    if (!canWrite() && (key === "logs" || key === "env")) continue;
    tabs.append(el("button", { class: "tab" + (dstate.tab === key ? " active" : ""), onclick: () => switchTab(key) },
      t.charAt(0).toUpperCase() + t.slice(1)));
  }
  if (!canWrite() && (dstate.tab === "logs" || dstate.tab === "env")) dstate.tab = "overview";
  const actions = el("div", { class: "d-actions" });
  if (d.state !== "?") {
    actions.append(el("a", { href: localUrl(d.root_url), target: "_blank", style: "text-decoration:none" },
      el("button", { class: "btn primary" }, "↗ Open RC")));
  }
  if (!canWrite()) {
    actions.append(el("span", { class: "inline-note", title: READONLY_WHY },
      "readonly — you can look, but not change anything here"));
  } else if (d.state === "running") {
    actions.append(dBtn("Stop", () => doState(d.name, "stop")));
    actions.append(dBtn("Restart", () => doState(d.name, "restart")));
  } else if (d.state === "stopped") {
    actions.append(dBtn("Start", () => doState(d.name, "start")));
  } else if (d.state === "down") {
    actions.append(dBtn("Bring up", () => doBringUp(d.name), "primary"));
  } else if (d.state === "?") {
    actions.append(el("span", { class: "inline-note" }, "docker unavailable — actions disabled"));
  } else {
    // restarting / created / paused / dead
    actions.append(dBtn("Stop", () => doState(d.name, "stop")));
    actions.append(dBtn("Restart", () => doState(d.name, "restart")));
  }
  renderActionPane(d, busyLabel);
  panel.append(head, sub, tabs, actions, el("div", { class: "d-body", id: "d-body" }));
  renderTab();
}

function closeLogs() { if (dstate.logsWS) { try { dstate.logsWS.close(); } catch (_) {} dstate.logsWS = null; } }
function copy(text) { navigator.clipboard.writeText(text); toast("copied", "ok"); }
// A urlbox row whose value is plain text rather than a link, with a copy button.
const box2 = (label, shown, toCopy) => el("div", { class: "urlbox" },
  el("div", {}, el("div", { class: "k" }, label), el("div", { class: "flat" }, shown)),
  el("button", { class: "copy", onclick: () => copy(toCopy) }, "copy"));
function renderTab() {
  const d = dstate.detail, body = $("#d-body"); if (!body) return;
  closeLogs();
  body.innerHTML = "";
  if (dstate.tab === "overview") {
    const kv = (k, v, cls = "") => el("div", { class: "kv" }, el("div", { class: "k" }, k), el("div", { class: "v " + cls }, v));
    // Exactly six, exactly like v4: three columns, two full rows, no hole. The
    // owner, the port and the scenario moved into the identity line above, which
    // is where they read as a sentence rather than as three more boxes.
    const grid = el("div", { class: "kv-grid" },
      kv("RC Version", d.rc_version), kv("MongoDB", d.mongo_tag),
      kv("Uptime", d.uptime || "—", d.uptime ? "green" : ""),
      kv("Port", ":" + d.host_port), kv("Scenario", d.preset || "default"),
      // healthClass() knows unhealthy/starting/healthy; the kv only understood the
      // literal string "healthy", so a container reporting "running" rendered plain.
      kv("Health", d.health || d.state || "—", HEALTH_TONE[healthClass(d)] || ""));

    // Where its https name is actually served from. Without this the panel shows
    // a workspace with no TLS port and no Traefik, which now looks like HTTPS is
    // simply missing rather than handled one layer up.
    // A climbing restart count separates "slow to boot" from "crash-looping".
    if (typeof d.restarts === "number" && d.restarts > 0) {
      grid.append(kv("RC restarts", String(d.restarts), d.restarts >= 2 ? "bad" : "warn"));
    }
    body.append(grid);
    if (d.state === "restarting" || (d.restarts || 0) >= 2) {
      body.append(el("p", { class: "banner bad" },
        `Rocket.Chat has restarted ${d.restarts || 0}× — usually resource pressure `
        + `(free some repros, or raise Docker's CPU/RAM) or a boot error. Check the Logs tab.`));
    }
    if ((d.links && d.links.length) || (d.notes && d.notes.length)) {
      body.append(scenarioCard(d));
    }
    if (d.state === "running") {
      const card = el("div", { class: "panelcard" },
        el("div", { class: "panelcard-h" },
          el("span", { class: "section-label flat" }, "Resource usage (live)"),
          el("span", { class: "chart-legend" },
            el("span", {}, el("span", { class: "sw", style: "background:var(--blue)" }), "CPU % (left)"),
            el("span", {}, el("span", { class: "sw", style: "background:var(--green)" }), "Mem MB (right)"))),
        el("div", { class: "chart-box" }, el("div", { id: "chart" })));
      body.append(card);
      startStats();
    }
    const url = el("div", { class: "urlbox" },
      el("div", {}, el("div", { class: "k" }, "URL"),
        el("a", { href: localUrl(d.root_url), target: "_blank" }, localUrl(d.root_url))),
      el("button", { class: "copy", onclick: () => copy(localUrl(d.root_url)) }, "copy"));
    body.append(url);
    // The admin login was only ever shown in the create dialog's result box, so
    // once that was closed nothing in the GUI could tell you the password again --
    // even though it has always been in this very payload. (It is a fixed local
    // sandbox credential, not a secret; the CLI prints it on up/ready/info.)
    if (d.login) {
      body.append(box2("Admin login", `${d.login.user} / ${d.login.password}`, d.login.password));
    }
    // The env tab masks credentials and points the reader at the compose file on
    // disk; this is where that file lives.
    if (d.workspace) body.append(box2("Workspace", d.workspace, d.workspace));
  } else if (dstate.tab === "logs") {
    renderLogs(body, d);
  } else if (dstate.tab === "containers") {
    const t = el("table", { class: "dtable" }, el("tr", {}, el("th", {}, "service"), el("th", {}, "state"), el("th", {}, "status")));
    for (const c of (d.containers || [])) t.append(el("tr", {}, el("td", {}, c.service), el("td", {}, c.state), el("td", { class: "v" }, c.status || c.health || "")));
    body.append(t);
    if (!(d.containers || []).length) {
      body.append(el("p", { class: "empty" },
        d.state === "?" ? "Docker is unavailable." : "No containers — this repro is down."));
    }
  } else if (dstate.tab === "env") {
    const t = el("table", { class: "dtable" },
      el("tr", {}, el("th", {}, "name"), el("th", {}, "value"), el("th", {}, "")));
    for (const e of (d.env || [])) {
      // Two kinds live in this list and they are not interchangeable: a Rocket.Chat
      // SETTING only works with the OVERWRITE_SETTING_ prefix, a plain env var only
      // works without it. Show the setting id itself and tag it, rather than a wall
      // of OVERWRITE_SETTING_* that hides which is which.
      const isSetting = e.key.startsWith(ENV_SETTING_PREFIX);
      const shown = isSetting ? e.key.slice(ENV_SETTING_PREFIX.length) : e.key;
      const name = el("td", {}, shown);
      if (isSetting) name.append(el("span", { class: "pill small" }, "setting"));
      if (e.override) name.append(el("span", { class: "yours" }, "*"));
      t.append(el("tr", {}, name, el("td", { class: "v" }, e.value),
        el("td", {}, el("button", {
          class: "btn small danger",
          title: e.override ? "Remove this override" : "Remove this variable from the workspace",
          // The FULL key, not the displayed one — that is what compose holds.
          onclick: () => doEnvChange(d.name, {}, {}, [e.key]),
        }, "remove"))));
    }
    body.append(t);
    if (!(d.env || []).length) body.append(el("p", { class: "empty" }, "No environment variables."));
    body.append(el("p", { class: "hint" },
      "* = set by you. Changing anything here recreates the Rocket.Chat container — "
      + "MongoDB keeps running, so no data is lost."));

    const kind = el("select", { class: "input", "aria-label": "kind" },
      el("option", { value: "setting" }, "Rocket.Chat setting"),
      el("option", { value: "env" }, "Plain env var"));
    const key = el("input", { class: "input", placeholder: "Message_AllowEditing", "aria-label": "name" });
    const val = el("input", { class: "input", placeholder: "false", "aria-label": "value" });
    const why = el("p", { class: "hint" }, "");
    const explain = () => {
      const setting = kind.value === "setting";
      key.placeholder = setting ? "Message_AllowEditing" : "MY_FLAG";
      val.placeholder = setting ? "false" : "true";
      why.textContent = setting
        ? "Anything you would change in Admin → Settings. The OVERWRITE_SETTING_ prefix"
          + " it needs is added for you — without it Rocket.Chat ignores the variable"
          + " silently. Re-applied on every boot, but the admin UI can still change it"
          + " while the container runs."
        : "A real environment variable (MONGO_URL, NODE_ENV, a feature flag). Passed"
          + " through exactly as typed.";
    };
    kind.addEventListener("change", explain);
    explain();
    const add = el("button", { class: "btn primary", onclick: () => {
      const k = key.value.trim();
      if (!k) { toast("enter a name"); return; }
      const payload = { [k]: val.value };
      doEnvChange(d.name, kind.value === "env" ? payload : {},
                  kind.value === "setting" ? payload : {}, []);
    } }, "Set + restart");
    body.append(el("div", { class: "row2" }, kind, key, val, add), why);
  } else if (dstate.tab === "backups") {
    renderBackups(body, d);
  }
}

// ---- backup / restore / upgrade ------------------------------------------------
// A backup is a bundle: the database PLUS the version, preset and parameters that
// produced it. That is what lets "Restore as new" rebuild a whole workspace from
// one file rather than needing a matching repro to already exist.

const fmtBytes = (n) => {
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + " " + u[i];
};
const shortName = (p) => String(p || "").split("/").pop();

async function renderBackups(body, d) {
  body.append(el("p", { class: "hint" },
    "Backups hold this workspace's Rocket.Chat database plus the version and preset "
    + "that produced it. Sidecar data (uploads, IdP state) is not included."));
  const bar = el("div", { class: "row2" },
    el("button", { class: "btn primary", onclick: () => doBackup(d.name) }, "Back up now"));
  body.append(bar);
  const list = el("div", {}, el("p", { class: "empty" }, "loading…"));
  body.append(list);

  let rows = [];
  try {
    rows = (await api(`/api/backups?name=${encodeURIComponent(d.name)}`)).backups || [];
  } catch (e) {
    list.innerHTML = ""; list.append(el("p", { class: "empty" }, String(e.message || e)));
    return;
  }
  list.innerHTML = "";
  if (!rows.length) {
    list.append(el("p", { class: "empty" }, "No backups yet for this workspace."));
    return;
  }
  const t = el("table", { class: "dtable" }, el("tr", {},
    el("th", {}, "bundle"), el("th", {}, "from"), el("th", {}, "size"), el("th", {}, "")));
  for (const b of rows) {
    if (b.error) {
      t.append(el("tr", {}, el("td", {}, shortName(b.path)),
        el("td", { class: "v bad", colspan: "3" }, "unreadable: " + b.error)));
      continue;
    }
    const label = el("td", {}, shortName(b.path));
    if (b.label) label.append(el("span", { class: "pill small" }, b.label));
    t.append(el("tr", {}, label,
      el("td", { class: "v" }, `RC ${b.rc_version}`),
      el("td", { class: "v" }, fmtBytes(b.bytes)),
      el("td", {},
        el("button", { class: "btn small primary", onclick: () => doRestore(b, d.name) }, "restore"),
        el("button", { class: "btn small", onclick: () => doRestore(b, "", true) }, "as new"),
        el("button", { class: "btn small danger", onclick: () => doDeleteBackup(b) }, "delete"))));
  }
  t.append();
  list.append(t);
}

async function doBackup(name) {
  const note = prompt(`Back up ${name}\n\nOptional note stored in the bundle:`, "");
  if (note === null) return;
  try {
    const { job_id } = await api(`/api/repros/${encodeURIComponent(name)}/backup`,
      { method: "POST", body: JSON.stringify({ label: note }) });
    streamJob(job_id, `Backing up ${name}`, (res) => {
      toast(`backed up to ${shortName(res && res.path)}`, "ok");
    });
  } catch (e) { toast(e.message); }
}

async function doRestore(b, into, asNew = false) {
  // Ask the SERVER whether this is allowed before offering the button's action:
  // a downgrade is refused, and an older-into-newer restore is a migration the
  // user has to opt into. Learning that after a job started is too late.
  let verdict = null;
  if (!asNew && into) {
    try {
      verdict = (await api("/api/backups/compatibility", {
        method: "POST", body: JSON.stringify({ bundle: b.path, name: into }),
      })).compatibility;
    } catch (e) { toast(String(e.message || e), "bad"); return; }
    if (verdict && !verdict.allowed) { toast(verdict.blocked_reason, "bad"); return; }
  }
  const lines = [];
  if (asNew) {
    lines.push(`Create a NEW workspace from ${shortName(b.path)} (RC ${b.rc_version}).`);
  } else {
    lines.push(`Restore ${shortName(b.path)} into ${into}.`,
      "", "Existing collections are DROPPED — this replaces the data, it does not merge.");
  }
  for (const w of ((verdict && verdict.warnings) || [])) lines.push("", "⚠ " + w);
  if (!confirm(lines.join("\n"))) return;
  try {
    const { job_id } = await api("/api/restore", {
      method: "POST",
      body: JSON.stringify({
        bundle: b.path, name: asNew ? "" : into, new: asNew,
        allow_upgrade: !!(verdict && verdict.requires_flag === "allow_upgrade"),
      }),
    });
    streamJob(job_id, asNew ? `Restoring ${shortName(b.path)} as a new workspace`
                            : `Restoring ${shortName(b.path)} into ${into}`,
      (res) => { toast(`restored ${(res && res.name) || ""}`, "ok"); refresh(); });
  } catch (e) { toast(e.message); }
}

async function doDeleteBackup(b) {
  if (!confirm(`Delete ${shortName(b.path)}?\n\nThis cannot be undone.`)) return;
  try {
    await api(`/api/backups?path=${encodeURIComponent(b.path)}`, { method: "DELETE" });
    toast("backup deleted", "ok");
    renderTab();
  } catch (e) { toast(String(e.message || e), "bad"); }
}

let UPGRADE_TARGET = "";
function openUpgrade(name, current) {
  UPGRADE_TARGET = name;
  $("#upgrade-title").textContent = `Upgrade: ${name}`;
  $("#upgrade-current").textContent = current || "?";
  $("#upgrade-to").value = "";
  $("#upgrade-plan").textContent = "";
  $("#upgrade-plan").className = "hint";
  $("#upgrade-go").disabled = true;
  $("#upgrade-dialog").showModal();
}

async function checkUpgradePlan() {
  const to = $("#upgrade-to").value.trim();
  const out = $("#upgrade-plan"), go = $("#upgrade-go");
  go.disabled = true;
  if (!to) { out.textContent = ""; out.className = "hint"; return; }
  out.textContent = "checking…"; out.className = "hint";
  try {
    const s = await api(`/api/repros/${encodeURIComponent(UPGRADE_TARGET)}/upgrade`
      + `?to=${encodeURIComponent(to)}`);
    if (!s.can_upgrade) { out.textContent = s.reason; out.className = "hint bad"; return; }
    const p = s.plan || {};
    if (!p.allowed) { out.textContent = p.blocked_reason; out.className = "hint bad"; return; }
    const bits = [`${p.from_version} → ${p.to_version}`,
                  `MongoDB ${p.from_mongo}` + (p.from_mongo === p.to_mongo ? "" : ` → ${p.to_mongo}`)];
    out.textContent = bits.join(" · ") + (p.warnings || []).map((w) => "\n⚠ " + w).join("");
    out.className = (p.warnings || []).length ? "hint warn" : "hint ok";
    go.disabled = false;
  } catch (e) {
    out.textContent = String(e.message || e); out.className = "hint bad";
  }
}

async function submitUpgrade() {
  const to = $("#upgrade-to").value.trim();
  const noBackup = $("#upgrade-nobackup").checked;
  const name = UPGRADE_TARGET;
  $("#upgrade-dialog").close();
  if (noBackup && !confirm(
    "Without a pre-upgrade backup there is nothing to roll back to if the "
    + "migrations fail.\n\nContinue?")) return;
  try {
    const { job_id } = await api(`/api/repros/${encodeURIComponent(name)}/upgrade`,
      { method: "POST", body: JSON.stringify({ to, no_backup: noBackup }) });
    streamJob(job_id, `Upgrading ${name} to ${to}`, (res) => {
      const errs = (res && res.migration_errors) || [];
      toast(errs.length
        ? `upgraded to ${res.to_version} with ${errs.length} migration error(s)`
        : `upgraded to ${(res && res.to_version) || to}`, errs.length ? "warn" : "ok");
      refresh();
    });
  } catch (e) { toast(e.message); }
}

// ---- env vars ---------------------------------------------------------------
// An env var cannot change inside a running container, so this recreates the
// Rocket.Chat service. It goes through streamJob because that recreate takes
// seconds to tens of seconds -- long enough that a silent button looks broken.
const ENV_SETTING_PREFIX = "OVERWRITE_SETTING_";

// `setting` is kept separate from `set` all the way to the server, which adds the
// OVERWRITE_SETTING_ prefix. The browser deliberately does not prepend it, so the
// rule lives in exactly one place.
function doEnvChange(name, set, setting, unset) {
  const changed = Object.keys(set).concat(Object.keys(setting)).concat(unset).join(", ");
  if (unset.length && !confirm(`Remove ${unset.join(", ")} from ${name}?\n\n`
      + "This recreates the Rocket.Chat container. Data is kept.")) return;
  return runAction(name, "Env", async () => {
    const { job_id } = await api(`/api/repros/${name}/env`,
      { method: "POST", body: JSON.stringify({ set, setting, unset }) });
    streamJob(job_id, `Env change on ${name}: ${changed}`);
  });
}

// ---- logs viewer ------------------------------------------------------------
const LEVELS = ["trace", "debug", "info", "warn", "error", "fatal"];
const PINO = { 10: "trace", 20: "debug", 30: "info", 40: "warn", 50: "error", 60: "fatal" };
const MONGOSEV = { D: "debug", I: "info", W: "warn", E: "error", F: "fatal" };
const LOG_MAX = 3000;
const logv = { buf: [], min: "info", svc: "", q: "", follow: true };

function parseLogLine(line) {
  const bar = line.indexOf("|");
  let service = "", content = line;
  if (bar > 0 && bar < 40) { service = line.slice(0, bar).trim().replace(/-\d+$/, ""); content = line.slice(bar + 1).trim(); }
  let level = "info", msg = content, ts = "";
  const b = content.indexOf("{");
  if (b >= 0) {
    try {
      const o = JSON.parse(content.slice(b));
      msg = o.msg || o.message || content;
      if (typeof o.level === "number") level = PINO[o.level] || "info";
      else if (o.s) level = MONGOSEV[o.s] || "info";
      else if (typeof o.level === "string" && LEVELS.includes(o.level.toLowerCase())) level = o.level.toLowerCase();
      const t = o.time || (o.t && o.t.$date);
      if (t) { const d = new Date(t); if (!isNaN(d)) ts = d.toLocaleTimeString(); }
      if (o.name) msg = `[${o.name}] ${msg}`;
    } catch (_) { /* not JSON — keep raw */ }
  }
  return { service, level, msg, ts, raw: line };
}
function passes(e) {
  return LEVELS.indexOf(e.level) >= LEVELS.indexOf(logv.min)
    && (!logv.svc || e.service === logv.svc)
    && (!logv.q || e.msg.toLowerCase().includes(logv.q));
}
function logRow(e) {
  return el("div", { class: "logrow lv-" + e.level },
    el("span", { class: "lt" }, e.ts || ""),
    el("span", { class: "ll lv-" + e.level }, e.level.toUpperCase()),
    el("span", { class: "ls" }, e.service || ""),
    el("span", { class: "lm" }, e.msg));
}
// The service filter's options, rebuilt from whatever services the buffer has seen.
// Its own function because it has TWO callers: a full re-render, and each arriving
// log line. It used to live only inside renderLogList(), which the WebSocket handler
// never calls -- it appends rows straight to the DOM to avoid re-rendering the whole
// list per line. So the dropdown stayed on "all services" no matter how many
// services were streaming, and only filled in once something ELSE forced a
// re-render: changing the level, typing in search, or toggling follow.
function refreshServiceOptions() {
  const sel = $("#log-svc"); if (!sel) return;
  const svcs = [...new Set(logv.buf.map((e) => e.service).filter(Boolean))].sort();
  // Nothing to do when the options already match; this runs per log line.
  const shown = [...sel.options].map((o) => o.value).filter(Boolean);
  if (shown.length === svcs.length && shown.every((s, i) => s === svcs[i])) return;
  const cur = logv.svc;
  // Built with el() rather than an innerHTML template: a "service" is just the
  // text before the first `|` of a container log line (parseLogLine), so it is
  // attacker-influenced content and must never be parsed as markup.
  sel.innerHTML = "";
  sel.append(el("option", { value: "" }, "all services"));
  for (const s of svcs) sel.append(el("option", { value: s }, s));
  // A service that has aged out of the buffer shows "all services", as the
  // previous `selected`-attribute build did (setting .value to a missing
  // option would instead blank the select).
  if (!svcs.includes(cur)) logv.svc = "";   // else passes() filters to permanent empty
  sel.value = logv.svc;
}

function renderLogList() {
  const box = $("#logview"); if (!box) return;
  box.innerHTML = "";
  for (const e of logv.buf) if (passes(e)) box.append(logRow(e));
  refreshServiceOptions();
  if (logv.follow) box.scrollTop = box.scrollHeight;
}
function renderLogs(body, d) {
  logv.buf = [];
  const ctl = el("div", { class: "logctl" });
  const levelSel = el("select", { class: "input", onchange: (e) => { logv.min = e.target.value; renderLogList(); } },
    ...["trace", "debug", "info", "warn", "error"].map((l) => el("option", { value: l }, l + "+")));
  levelSel.value = logv.min;
  const svcSel = el("select", { id: "log-svc", class: "input", onchange: (e) => { logv.svc = e.target.value; renderLogList(); } }, el("option", { value: "" }, "all services"));
  const search = el("input", { class: "input", placeholder: "search…", "aria-label": "filter log lines",
    oninput: (e) => { logv.q = e.target.value.trim().toLowerCase(); renderLogList(); } });
  // logv persists across tab switches by design, but the input was rebuilt EMPTY --
  // so an active search looked like "the container stopped logging".
  search.value = logv.q;
  const followCb = el("input", { type: "checkbox", onchange: (e) => { logv.follow = e.target.checked; renderLogList(); } });
  followCb.checked = logv.follow;
  const follow = el("label", { class: "logfollow" }, followCb, " follow");
  const clear = el("button", { class: "btn small", onclick: () => { logv.buf = []; renderLogList(); } }, "Clear");
  ctl.append(levelSel, svcSel, search, follow, clear);
  if (d.grafana_url) ctl.append(el("a", { class: "linkchip monitor", href: localUrl(d.grafana_url) + "/d/rcrepro-logs", target: "_blank" }, "Logs in Grafana (Loki)"));
  const box = el("div", { class: "logview", id: "logview" });
  body.append(ctl, box);

  const proto = location.protocol === "https:" ? "wss" : "ws";
  // No credential in the URL any more: the session cookie rides along on the
  // upgrade automatically, which is the only reason ?t= ever existed here.
  const ws = new WebSocket(`${proto}://${location.host}/api/repros/${d.name}/logs/stream?tail=300`);
  dstate.logsWS = ws;
  ws.onmessage = (m) => {
    const e = parseLogLine(m.data);
    logv.buf.push(e);
    if (logv.buf.length > LOG_MAX) logv.buf.shift();
    // OUTSIDE the passes() check below, deliberately: a service that only logs at
    // info still has to appear in the filter while the level is set to error, or
    // you cannot select the service whose errors you are looking for.
    refreshServiceOptions();
    if (passes(e)) {
      const b = $("#logview");
      b.append(logRow(e));
      // The buffer was capped but the DOM was not, so rows accumulated without
      // limit on a chatty repro. Keep the node count in step with the buffer.
      while (b.childElementCount > LOG_MAX) b.removeChild(b.firstChild);
      if (logv.follow) b.scrollTop = b.scrollHeight;
    }
  };
  ws.onclose = () => { if (dstate.logsWS === ws) dstate.logsWS = null; };
}

function startStats() {
  if (dstate.statsTimer) clearInterval(dstate.statsTimer);
  const poll = async () => {
    if (document.hidden) return;   // same reasoning as the dashboard poll below
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
  // var(), not literals: the chart has to follow the theme like everything else.
  const MUT = "var(--muted)", GRID = "var(--line)", CPU = "var(--blue)", MEM = "var(--green)";

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
    g += `<text x="${(x0 + x1) / 2}" y="${(y0 + y1) / 2}" fill="var(--dim)" text-anchor="middle">collecting…</text>`;
  }
  box.innerHTML = g + `</svg>`;
}

// ---- actions ----------------------------------------------------------------
async function refreshDetail({ force = false } = {}) {
  if (!SELECTED) return;
  const name = SELECTED;
  let d;
  try { d = await api(`/api/repros/${name}/detail`); } catch (_) { return; }   // keep old
  if (SELECTED !== name) return;
  dstate.detail = d;
  // Re-rendering rebuilds the tab body, which resets logv.buf and reopens the log
  // WebSocket -- so an unrelated job finishing used to blank the log the user was
  // reading. A slightly stale header beats losing the logs; it refreshes on switch.
  // `force` is for a finished lifecycle action, which MUST redraw: its spinner is
  // in the action row, so skipping the render would strand "Stopping…" on screen.
  if (!force && dstate.tab === "logs" && dstate.logsWS) return;
  renderDetail();
}
// Stop/start/restart are a synchronous POST (no job to stream), and `docker compose
// stop` alone spends RC's 10s SIGTERM grace period before returning -- so runAction
// carries the feedback. It also owns the reload, which is why nothing here calls
// loadRepros/refreshDetail itself.
const STATE_LABEL = { stop: "Stop", start: "Start", restart: "Restart" };
function doState(name, action) {
  return runAction(name, STATE_LABEL[action] || action, () =>
    api(`/api/repros/${name}/state`, { method: "POST", body: JSON.stringify({ action }) }));
}
async function doMonitor(name, off) {
  if (off && !confirm(`Detach Prometheus + Grafana from ${name}?\n\nThis also deletes their data volumes.`)) return;
  try {
    const { job_id } = await api(`/api/repros/${name}/monitor?off=${off ? "true" : "false"}`, { method: "POST" });
    streamJob(job_id, `${off ? "Detaching" : "Attaching"} monitoring: ${name}`, renderCreateResult);
  } catch (e) { toast(e.message); }
}
async function doBringUp(name) {
  try {
    const { job_id } = await api(`/api/repros/${name}/up`, { method: "POST" });
    streamJob(job_id, `Bringing up ${name}`, renderCreateResult);
  } catch (e) { toast(e.message); }
}
async function doDown(name) {
  // Two steps so Cancel can actually abort: the single confirm sent the DELETE
  // on both branches, so a mis-click still tore the stack down.
  if (!confirm(`Remove ${name}'s containers?\n\nOK to continue, Cancel to abort.`)) return;
  // Everyone can act on everything -- that is deliberate, support engineers cover
  // for each other. What they must not do is destroy a colleague's data without
  // being told whose it is.
  const owner = (ALL_REPROS.find((r) => r.name === name) || {}).owner
              || (ALL_REPROS.find((r) => r.name === name) || {}).created_by || "";
  const whose = owner && owner !== ME
    ? `\n\nThis workspace belongs to ${owner}, not you — the server will refuse `
      + `unless you are an admin.\nHand it over first:  rc-repro chown -n ${name} --to ${ME}`
    : "";
  const vol = confirm(
    `Also DELETE ${name}'s data volume and record?${whose}\n\n` +
    `OK = delete everything (irreversible).\nCancel = keep the data.`);
  // Also synchronous, and slower than Stop (it tears containers down and may
  // remove volumes), so it gets the same treatment.
  return runAction(name, "Down", () =>
    api(`/api/repros/${name}?volumes=${vol}&confirm=${vol}`, { method: "DELETE" }));
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
  // Validate first: closing the dialog before checking discarded the typed spec and
  // reset the mode selector, so the user had to re-pick "Bulk --scale" every time.
  if (scale && !f.scale.value.trim()) { toast("enter a scale spec"); return; }
  $("#seed-dialog").close();
  try {
    let job;
    if (scale) {
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
let IMPORT_UPLOAD_ID = null;
function openImport(name) {
  IMPORT_TARGET = name;
  $("#import-title").textContent = `Import customer config: ${name}`;
  $("#import-file").value = "";
  IMPORT_UPLOAD_ID = null;
  $("#import-form").only.value = "";
  const plan = $("#import-plan"); plan.hidden = true; plan.innerHTML = "";
  $("#import-apply").disabled = true;
  $("#import-dialog").showModal();
}
async function previewImport() {
  const f = $("#import-form");
  if (!f.file.files.length) { toast("choose a settings.json file"); return; }
  const btn = $("#import-preview");
  if (btn.disabled) return;              // a double-click orphaned a second upload
  btn.disabled = true;
  const fd = new FormData();
  fd.append("file", f.file.files[0]);
  fd.append("only", f.only.value.trim());
  let plan;
  try {
    const r = await fetch(`/api/repros/${IMPORT_TARGET}/config-import/plan`, {
      method: "POST", credentials: "same-origin", body: fd });
    plan = await r.json();
    if (!r.ok) throw new Error(plan.error || `HTTP ${r.status}`);
  } catch (e) { toast(e.message); return; }
  finally { $("#import-preview").disabled = false; }
  const box = $("#import-plan");
  const c = plan.counts;
  // Everything below comes from the uploaded dump's setting ids — escape all of
  // it: a crafted `_id` would otherwise run script in a page holding the token.
  let html = `<b>apply ${c.apply}</b> &middot; skip ${c.redacted} redacted, ${c.denied} identity/env`;
  if (plan.oauth_services.length) html += `<br>oauth pre-create: ${escapeHtml(plan.oauth_services.join(", "))}`;
  if (plan.redacted.length) html += `<br><span class="warn">set by hand (redacted): ${escapeHtml(plan.redacted.join(", "))}</span>`;
  html += "<hr>" + plan.apply.map((a) => `<div class="kv"><code>${escapeHtml(a.id)}</code> = ${escapeHtml(a.value)}</div>`).join("");
  box.innerHTML = html; box.hidden = false;
  IMPORT_UPLOAD_ID = plan.upload_id || null;
  $("#import-apply").disabled = plan.apply.length === 0 || !IMPORT_UPLOAD_ID;
}
function escapeHtml(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
async function applyImport() {
  $("#import-dialog").close();
  try { const { job_id } = await api(`/api/repros/${IMPORT_TARGET}/config-import`, { method: "POST", body: JSON.stringify({ upload_id: IMPORT_UPLOAD_ID }) }); streamJob(job_id, `Importing config: ${IMPORT_TARGET}`); }
  catch (e) { toast(e.message); }
}
async function showLogs(name) {
  closeJobStream();          // a plain log view must not share the dialog with a job
  openJob(`Logs: ${name}`);
  $("#job-log").textContent = "loading…";
  try { const { logs } = await api(`/api/repros/${name}/logs?tail=200`); $("#job-log").textContent = logs || "(no output)"; }
  catch (e) { $("#job-log").textContent = "error: " + e.message; }
}
function reportPrune(r) {
  const targets = (r.targets || []).length, removed = (r.removed || []).length;
  if (!targets) { toast("nothing to prune", "info"); return; }
  if (removed === targets) { toast(`pruned ${removed}`, "ok"); return; }
  // lifecycle.prune() warns per-repro through emit, and the endpoint is synchronous
  // so those warnings are discarded -- the shortfall is the only signal we get.
  toast(`pruned ${removed} of ${targets}; ${targets - removed} could not be cleaned up`);
}

// Prune is the same story as Stop -- a synchronous call that removes containers
// and volumes for every `down` repro -- but its button lives in the static top
// bar, so unlike the cards nothing re-renders it and a local swap is enough.
async function withBusy(btn, verb, fn) {
  const label = btn.textContent;
  btn.disabled = true;
  btn.classList.add("working");
  btn.textContent = "";
  btn.append(el("span", { class: "spin" }), verb);
  try { await fn(); } finally {
    btn.classList.remove("working");
    btn.disabled = false;
    btn.textContent = label;
  }
}
async function doPrune() {
  if (!confirm("Delete every 'down' repro, including data volumes and records?")) return;
  await withBusy($("#btn-prune"), "Pruning…", async () => {
    try {
      const r = await api("/api/prune", { method: "POST", body: JSON.stringify({ confirm: true }) });
      reportPrune(r);
      await loadRepros();
    } catch (e) { toast(e.message); }
  });
}

// ---- job streaming (SSE) ----------------------------------------------------
// One dialog, so exactly one job may own it at a time. JOB is that job; a stream
// belonging to anything else must never write into it (two jobs used to interleave
// their output and steal each other's title).
let JOB = null;

function openJob(title) { $("#job-title").textContent = title; $("#job-log").textContent = ""; $("#job-dialog").showModal(); }
function logLine(ev) {
  const line = el("div", { class: ev.level }, (ev.pct != null ? `[${Math.round(ev.pct)}%] ` : "") + ev.message);
  const pre = $("#job-log"); pre.append(line); pre.scrollTop = pre.scrollHeight;
}
function closeJobStream() {
  if (JOB && JOB.es) { try { JOB.es.close(); } catch (_) { /* already closed */ } }
  JOB = null;
}

function streamJob(jobId, title, onResult) {
  closeJobStream();                       // a new job supersedes whatever was showing
  openJob(title);
  JOB = { id: jobId, title, onResult, idx: 0, es: null, retries: 0, finished: false };
  connectJob(JOB);
}

function connectJob(job) {
  // `since` resumes from the last event we saw, so a reconnect cannot duplicate
  // or skip lines. The server has always accepted it; nothing used to send it.
  const es = new EventSource(
    `/api/jobs/${job.id}/stream?since=${job.idx}`);
  job.es = es;
  es.onmessage = (m) => {
    if (JOB !== job) { es.close(); return; }      // superseded — do not write here
    if (m.lastEventId) job.idx = Number(m.lastEventId) + 1;
    const ev = JSON.parse(m.data);
    logLine(ev);
    if (ev.terminal) finishJob(job, ev.level === "error", ev.data || {});
  };
  es.onerror = () => {
    es.close();
    if (JOB !== job || job.finished) return;
    // EventSource reports no detail, and closing it defeats its own reconnect, so
    // ask the job endpoint what actually happened instead of freezing the dialog.
    reconcileJob(job);
  };
}

function finishJob(job, failed, data) {
  job.finished = true;
  if (job.es) { try { job.es.close(); } catch (_) { /* already closed */ } }
  $("#job-title").textContent = failed ? "Failed" : "Done";
  if (!failed && job.onResult && data.result) job.onResult(data.result);
  loadRepros().then(refreshDetail);
}

async function reconcileJob(job) {
  let state;
  try { state = await api(`/api/jobs/${job.id}`); }
  catch (e) {
    logLine({ level: "error", message: `progress stream lost, and the job could not be queried: ${e.message}` });
    finishJob(job, true, {});
    return;
  }
  if (jobActive(state.status)) {
    if (job.retries++ < 5) {
      logLine({ level: "warn", message: "progress stream dropped — reconnecting…" });
      setTimeout(() => { if (JOB === job && !job.finished) connectJob(job); }, 1000);
    } else {
      logLine({ level: "warn", message: `still running, but the progress stream keeps dropping (${job.id})` });
    }
    return;
  }
  // It finished while we were disconnected; this endpoint carries the outcome.
  logLine(state.status === "error"
    ? { level: "error", message: state.error || "failed" }
    : { level: "info", message: "done" });
  // The server keeps a hundred job summaries but only the newest ten results, so
  // an old benchmark reopened from the history has nothing left to render. Say so:
  // an empty panel and a discarded one look identical otherwise.
  if (state.result_dropped) {
    logLine({ level: "warn", message: "this job finished a while ago and its full "
      + "output is no longer held in memory — the progress above is what remains." });
  }
  finishJob(job, state.status === "error", { result: state.result, error: state.error });
}

// The job keeps running server-side when the dialog is dismissed, and there is no
// job list to reopen it from -- so poll for the outcome rather than losing it.
async function watchDetachedJob(job) {
  toast(`${job.title} continues in the background`, "info");
  for (let i = 0; i < 900; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    let state;
    try { state = await api(`/api/jobs/${job.id}`); } catch (_) { return; }
    if (jobActive(state.status)) continue;
    toast(state.status === "error" ? `${job.title} failed: ${state.error || ""}` : `${job.title} finished`,
          state.status === "error" ? "error" : "ok");
    loadRepros().then(refreshDetail);
    return;
  }
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
function renderCreateResult(r) {
  const pre = $("#job-log");
  const box = el("div", { class: "result" });
  const kv = (k, v) => el("div", {}, el("b", {}, k + ": "), v);
  box.append(el("div", { html: "<hr>" }));
  if (r.rc_version) box.append(kv("Rocket.Chat", `${r.rc_version}  ·  Mongo ${r.mongo_tag || "?"}  ·  preset ${r.preset || "?"}`));
  if (r.root_url) box.append(kv("URL", localUrl(r.root_url)));
  if (r.login) box.append(kv("Login", `${r.login.user} / ${r.login.password}`));
  for (const u of (r.instance_urls || [])) box.append(kv("instance", localUrl(u)));
  if (r.grafana_url) box.append(kv("Grafana", localUrl(r.grafana_url)));
  pre.append(box);
  appendNotes(pre, r.notes);
  pre.scrollTop = pre.scrollHeight;
}

// Preset notes carry the things you cannot guess: the Keycloak console URL and
// realm, the /etc/hosts line oidc and presigned s3_minio need, where Mailpit is.
// The CLI prints them after `up` and from `info`; the GUI showed them nowhere.
// A preset's notes are free prose written for a terminal, and dumping them into
// a <div> per line (or a <pre>) makes the one line that MATTERS -- the /etc/hosts
// entry `oidc` does not work without -- look exactly like the sentence beside it.
//
// Three shapes are actually in the data, and none of them are invented:
//   * an INDENTED line is a thing to type or paste: "    127.0.0.1  keycloak",
//     "    http://localhost:8081/admin/...". It gets a code box and a Copy button.
//   * a line CONTAINING a url is a place to go. The url becomes a link.
//   * everything else is prose, and consecutive prose lines are one paragraph --
//     they were wrapped for an 80-column terminal, not written as separate points.
// Urls become links and `backticked` spans become code. Both are already in the
// notes as plain text -- a preset that says "pass --reg-token" or "`loadtest
// --live`" is naming something you type, and leaving the backticks on screen is
// showing the reader the markup instead of the meaning.
const TOKEN_RE = /`([^`]+)`|\bhttps?:\/\/[^\s)]+/;

function linkify(line) {
  const out = el("span", {});
  // "Status -> Targets", "Container logs -> Loki", "Admin → Email": the ASCII
  // spelling is there because a terminal cannot render the arrow, and a browser
  // can. Same character the presets already use where they were free to.
  let rest = String(line).replace(/ -> /g, " → ");
  for (;;) {
    const m = TOKEN_RE.exec(rest);
    if (!m) { out.append(rest); return out; }
    out.append(rest.slice(0, m.index));
    if (m[1]) out.append(el("code", { class: "inline" }, m[1]));
    else out.append(el("a", { href: localUrl(m[0]), target: "_blank", rel: "noreferrer" }, m[0]));
    rest = rest.slice(m.index + m[0].length);
  }
}

// A note line that names a PLACE. Every preset that has one writes it the same
// way, because a human wrote it for a human:
//     Keycloak admin console: http://localhost:8081  (admin / admin)
//     Mailpit (EVERY email RC sends, for ALL users, lands here): http://localhost:8025
//     Grafana:    http://localhost:5050  (admin/admin; anonymous view enabled)
// That is a NAME, optionally what it is FOR, a URL, and sometimes the CREDENTIALS
// — the same four things the link rows above already show — and setting it as a
// grey paragraph buried the two facts you came for. The url must START the value:
// "Container logs -> Loki: open the 'Rocket.Chat Logs' dashboard in Grafana (…)"
// mentions a url inside a sentence, and it is a sentence, not a location.
const PLACE_RE = /^([^:()]{2,40}?)(?:\s*\(([^)]*)\))?:[ \t]+((?:https?:\/\/|<)\S*)[ \t]*(?:\(([^)]*)\))?[ \t]*\.?$/;
// "admin/admin; anonymous view enabled" is a password AND a remark; "Status ->
// Targets: rocketchat, …" is only a remark. Only the a/b shape is a credential.
const CREDS_RE = /^([\w.@+-]+ ?\/ ?[\w.@+-]+)(?:[;,]\s*(.*))?$/;
// Past this, a description stops being a column and becomes a second line —
// rather than ellipsising it away, which loses the half of it that matters.
const WHAT_MAX = 34;

// Read a preset's notes into the shapes that are in them. Indentation carries
// most of the structure, and every preset already uses it:
//   4+ spaces  something to type or paste  ("    127.0.0.1  keycloak")
//   2 spaces   a rider on the line above ("  — one shared inbox; …")
//   column 0   a new point — UNLESS the line before it did not finish a
//              sentence, in which case it is just terminal wrapping
//              ("…can reach Keycloak at the" / "same URL RC's backend uses.")
// Joining every consecutive prose line instead, as a first pass did, turned the
// email preset's five separate points into one unreadable wall.
function parseNotes(notes) {
  const items = [];
  let para = null, place = null, prev = "";
  for (const raw of notes || []) {
    const line = String(raw);
    if (!line.trim()) { para = null; place = null; prev = ""; continue; }
    if (/^\s{4,}\S/.test(line)) {
      items.push({ kind: "cmd", value: line.trim() });
      // The paste box ENDS the row above it. saml's last line is a remark about
      // the Keycloak console, but hanging it back on that row prints it above the
      // url the sentence before it promised ("…or open Users directly:").
      para = null; place = null; prev = line.trim();
      continue;
    }
    const indented = /^\s{1,3}\S/.test(line);
    const m = indented ? null : PLACE_RE.exec(line.trim());
    if (m) {
      const paren = (m[4] || "").trim();
      const creds = CREDS_RE.exec(paren);
      place = { kind: "place", label: m[1].trim(), what: (m[2] || "").trim(),
                url: m[3], creds: creds ? creds[1] : "",
                sub: creds ? (creds[2] || "").trim() : paren };
      if (place.what.length > WHAT_MAX) {
        place.sub = place.sub ? place.what + " · " + place.sub : place.what;
        place.what = "";
      }
      items.push(place); para = null; prev = line;
      continue;
    }
    if (place && indented) {                    // a rider on the row above
      place.sub = place.sub ? place.sub + " " + line.trim() : line.trim();
      prev = line;
      continue;
    }
    // A column-0 line continues the one above ONLY if that one broke mid-sentence,
    // and the reliable signal is what it ENDS with. Ending punctuation is not
    // enough on its own: livechat has two separate points that each end in a url,
    // and one that starts with a lowercase username. A line that trails off in a
    // plain lowercase word ("…can reach Keycloak at the") was wrapped; one ending
    // in a url, a path, a number or punctuation was finished.
    const tail = prev.trim();
    const wrapped = para && /[a-z][a-z'’]*$/.test(tail) && !/\/\S*$/.test(tail);
    if (!para || !(indented || wrapped)) {
      para = { kind: "prose", lines: [] };
      items.push(para);
    }
    para.lines.push(line.trim());
    place = null; prev = line;
  }
  return items;
}

// One row shape for "a thing, and where it is", used by the links the server
// reports AND by the note lines that name one, so a scenario's console does not
// appear once as a row and again as a sentence three lines below it.
function placeRow(p) {
  const real = !String(p.url || "").startsWith("<");
  const row = real
    ? el("a", { class: "linkrow " + (p.kind || ""), href: localUrl(p.url),
                target: "_blank", rel: "noreferrer" })
    : el("div", { class: "linkrow static " + (p.kind || "") });
  row.append(el("span", { class: "l-n" }, p.label));
  if (p.what) row.append(el("span", { class: "l-w" }, p.what));
  row.append(el("span", { class: "l-u" }, p.url));
  if (p.creds) row.append(el("span", { class: "l-c" }, p.creds));
  if (real) row.append(el("span", { class: "l-go" }, "↗"));
  if (p.sub) row.append(el("span", { class: "l-sub" }, linkify(p.sub)));
  return row;
}

function renderNoteItems(box, items) {
  let group = null;                       // consecutive places are one table
  for (const it of items) {
    if (it.kind !== "place") group = null;
    if (it.kind === "cmd") {
      box.append(el("div", { class: "note-cmd" },
        el("code", {}, it.value),
        el("button", { class: "cp", onclick: () => copy(it.value) }, "Copy")));
    } else if (it.kind === "place") {
      if (!group) { group = el("div", { class: "linkrows one" }); box.append(group); }
      group.append(placeRow(it));
    } else {
      const para = el("p", { class: "note-p" });
      it.lines.forEach((l, i) => { if (i) para.append(" "); para.append(linkify(l)); });
      box.append(para);
    }
  }
  return box;
}

function noteBlock(notes) {
  return renderNoteItems(el("div", { class: "notes" }), parseNotes(notes));
}

function appendNotes(parent, notes) {
  if (!notes || !notes.length) return;
  parent.append(el("div", { class: "section-label" }, "Using this scenario"));
  parent.append(noteBlock(notes));
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
  // Everything below was already in the response and was being thrown away. It is
  // the part a support ticket actually needs: WHICH step is slow, whether Mongo did
  // a COLLSCAN, whether the Node event loop saturated, and when errors started.
  renderDiagnosis(pre, r);
  if (r.grafana_url) {
    const url = `${localUrl(r.grafana_url)}/d/rcrepro-k6-loadtest?from=now-15m&to=now&kiosk`;
    pre.append(el("div", { class: "result" },
      el("a", { href: url, target: "_blank", style: "text-decoration:none" }, el("button", { class: "btn small" }, "Open k6 dashboard in Grafana"))));
    const frame = el("iframe", { src: url, class: "grafana-embed" });
    pre.append(frame);
  }
  pre.scrollTop = pre.scrollHeight;
}


// ---- load-test diagnosis rendering -----------------------------------------
function resultTable(headers, rows) {
  const t = el("table", { class: "dtable" }, el("tr", {}, ...headers.map((h) => el("th", {}, h))));
  for (const row of rows) {
    t.append(el("tr", {}, ...row.map((cell, i) =>
      el("td", i ? { class: "v" } : {}, String(cell)))));
  }
  return t;
}
const ms = (v) => (v == null ? "-" : `${Math.round(v)}ms`);

// p95 per bucket as a sparkline, with error buckets marked. timeline.render_ascii
// is terminal art; this reuses the SVG approach from the live stats chart.
function timelineSvg(tl) {
  const b = tl.buckets || [];
  if (b.length < 2) return null;
  const W = 560, H = 74, pad = 4;
  const max = Math.max(...b.map((x) => x.p95), 1);
  const x = (i) => pad + (i / (b.length - 1)) * (W - 2 * pad);
  const y = (v) => H - 18 - (v / max) * (H - 26);
  const line = b.map((x_, i) => `${x(i).toFixed(1)},${y(x_.p95).toFixed(1)}`).join(" ");
  const marks = b.map((x_, i) => x_.errors
    ? `<rect x="${(x(i) - 1.5).toFixed(1)}" y="${H - 14}" width="3" height="8" fill="var(--red)"/>` : "").join("");
  const wrap = el("div", { class: "spark" });
  wrap.innerHTML =
    `<svg viewBox="0 0 ${W} ${H}" width="100%" style="height:auto;display:block" font-size="9">` +
    `<polyline points="${line}" fill="none" stroke="var(--blue)" stroke-width="1.6"/>${marks}` +
    `<text x="${pad}" y="${H - 3}" fill="var(--muted)">0s</text>` +
    `<text x="${W - pad}" y="${H - 3}" fill="var(--muted)" text-anchor="end">${Math.round(tl.span_s || 0)}s</text>` +
    `<text x="${pad}" y="9" fill="var(--muted)">peak p95 ${Math.round(max)}ms</text></svg>`;
  return wrap;
}

function renderDiagnosis(pre, r) {
  const s = r.summary || {}, d = r.diag || {};
  const section = (label, node) => {
    if (!node) return;
    pre.append(el("div", { class: "section-label" }, label));
    pre.append(node);
  };

  if (r.slo && r.slo.length) {
    section("SLO rules", resultTable(["rule", "threshold", "actual", ""], r.slo.map((x) => [
      x.key, `${x.op} ${x.raw}`,
      x.measured === false ? "not measured" : (x.key === "error" ? `${x.actual.toFixed(2)}%`
        : x.key === "rps" ? x.actual.toFixed(1) : ms(x.actual)),
      x.ok ? "PASS" : "FAIL"])));
  }
  // Per-step latency is the single most useful table for a journey run: it names
  // the slow step instead of leaving you with one aggregate p95.
  const steps = s.steps || {};
  const order = ["login", "rooms", "open", "post", "sync", "read"];
  const names = Object.keys(steps).sort((a, b) => {
    const ia = order.indexOf(a), ib = order.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });
  if (names.length) {
    section("Per-step latency", resultTable(["step", "count", "p50", "p95", "p99"],
      names.map((n) => [n, Math.round(steps[n].count || 0), ms(steps[n].p50), ms(steps[n].p95), ms(steps[n].p99)])));
  }
  const st = s.status || {};
  const codes = ["2xx", "429", "4xx", "5xx", "other"].filter((k) => st[k]);
  if (codes.length) {
    section("Responses", resultTable(["class", "count"], codes.map((k) => [k, Math.round(st[k])])));
  }
  if (d.rcmetrics && Object.keys(d.rcmetrics).length) {
    section("RC internals", resultTable(["instance", "event-loop lag peak", "lag p99", "heap peak", "ddp users"],
      Object.keys(d.rcmetrics).sort().map((svc) => {
        const m = d.rcmetrics[svc];
        const peak = m.eventloop_lag_max_s || m.eventloop_lag_s, p99 = m.eventloop_lag_p99_s;
        return [svc, peak ? ms(peak.max * 1000) : "-", p99 ? ms(p99.max * 1000) : "-",
          m.heap_used_bytes ? `${Math.round(m.heap_used_bytes.max / 1e6)}MB` : "-",
          m.ddp_users ? Math.round(m.ddp_users.max) : "-"];
      })));
  }
  const mg = d.mongo;
  if (mg && (mg.total || (mg.slow || []).length)) {
    section(`Slow MongoDB queries (${mg.total} profiled, ${mg.collscan} COLLSCAN)`,
      resultTable(["time", "namespace", "op", "plan", "docs", "returned"],
        (mg.slow || []).map((q) => [ms(q.millis), q.ns, q.op, q.plan || "?", q.docs, q.ret])));
  }
  if (d.timeline) {
    const spark = timelineSvg(d.timeline);
    if (spark) {
      section(`Latency over time (${d.timeline.buckets.length} buckets of ${d.timeline.width_s}s)`, spark);
      if (d.timeline.first_error_s != null) {
        pre.append(el("div", { class: "inline-note" }, `errors began ~${d.timeline.first_error_s}s in`));
      }
    }
  }
  if (r.resources && Object.keys(r.resources).length) {
    section("Resource cost", resultTable(["container", "idle CPU", "peak CPU", "peak RAM"],
      Object.keys(r.resources).sort().map((c) => {
        const x = r.resources[c];
        return [c, `${Math.round(x.idle_cpu)}%`, `${Math.round(x.peak_cpu)}%`,
          `${Math.round(x.peak_mem / 1e6)}MB`];
      })));
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
    start: parseInt(f.start.value, 10) || 10, max_vus: parseInt(f.max_vus.value, 10) || 640,
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
  const t = el("table", { class: "dtable" }, el("tr", {}, el("th", {}, "VUs"), el("th", {}, "req/s"), el("th", {}, "p95"), el("th", {}, "err"), el("th", {}, "lag peak"), el("th", {}, "result")));
  for (const s of (r.steps || [])) t.append(el("tr", {},
    el("td", {}, s.vus), el("td", { class: "v" }, (s.rps || 0).toFixed(1)),
    el("td", { class: "v" }, (s.p95 || 0).toFixed(0) + "ms"), el("td", { class: "v" }, ((s.error_rate || 0) * 100).toFixed(2) + "%"),
    el("td", { class: "v" }, s.lag_max_s ? `${Math.round(s.lag_max_s * 1000)}ms` : "-"),
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

// ---- default repro / API token ----------------------------------------------
function doDefault(name) {
  return runAction(name, "Make default", async () => {
    await api(`/api/repros/${name}/default`, { method: "POST" });
    toast(`${name} is now the default repro`, "ok");
  });
}
let PAT_HEADERS = "";
// Through runAction, not a bare fetch: this is a login plus a 2FA-guarded POST
// against a workspace that may still be booting, so it can sit there for tens of
// seconds. Silent, it looked like a dead button -- and the second click would
// *regenerate* the token, invalidating the one the first click was about to show.
function doPat(name) {
  return runAction(name, "API token", async () => {
    const r = await api(`/api/repros/${name}/pat`, { method: "POST", body: JSON.stringify({}) });
    PAT_HEADERS = `-H "X-Auth-Token: ${r.token}" -H "X-User-Id: ${r.user_id}"`;
    $("#pat-title").textContent = `PAT and Token: ${name}`;
    $("#pat-body").textContent =
      `# ${r.root_url}  (label: ${r.label}, bypass_2fa: ${r.bypass_2fa})\n`
      + `X-Auth-Token: ${r.token}\nX-User-Id: ${r.user_id}\n\n`
      + `curl ${PAT_HEADERS} ${r.root_url}/api/v1/me\n`;
    $("#pat-dialog").showModal();
  });
}

// ---- API call console -------------------------------------------------------
// `rc-repro api` in the GUI: pick a method and path, get the status and body
// back in place. Not routed through runAction like the buttons above -- this
// dialog is meant to be used repeatedly, so it disables its own Send button for
// the duration instead of freezing every other action on the repro.
let CALL_TARGET = null;
let CALL_TEXT = "";
// Rocket.Chat answers with JSON on one unbroken line -- a users.list is a single
// wrapped blob you cannot read. Indent it. Two things it must not do: choke on a
// body that isn't JSON (a proxy's HTML error page, a bare 204), and freeze the
// tab re-serialising a huge one, so both fall through to the raw text with the
// reason shown next to the status.
const PRETTY_MAX = 512 * 1024;
function prettyJson(text) {
  if (text.length > PRETTY_MAX) return { text, note: "too large to indent" };
  try { return { text: JSON.stringify(JSON.parse(text), null, 2), note: "" }; }
  catch (_) { return { text, note: "not JSON — shown raw" }; }
}
function openCall(name) {
  CALL_TARGET = name;
  CALL_TEXT = "";
  $("#call-title").textContent = `API call: ${name}`;
  const out = $("#call-out");
  out.textContent = "";
  out.hidden = true;                      // no stale response from the last repro
  $("#call-dialog").showModal();
}
async function submitCall() {
  // Through .elements, not `f.method` -- `method` is a real HTMLFormElement
  // property (the form's own HTTP method). A control named `method` only shadows
  // it by the legacy override rule, so `f.method` would quietly become the string
  // "get" the day this field is renamed. .elements has no such collision.
  const f = $("#call-form").elements;
  const req = {
    method: f.method.value, path: f.path.value.trim(), data: f.data.value.trim(),
    pat: f.pat.checked, two_fa: f.two_fa.checked,
  };
  if (!req.path) { toast("enter an API path, e.g. /api/v1/me"); return; }
  const btn = $("#call-send");
  const out = $("#call-out");
  btn.disabled = true; btn.textContent = "Sending…";
  out.hidden = false;
  out.textContent = `${req.method} ${req.path} …`;
  try {
    const r = await api(`/api/repros/${CALL_TARGET}/call`, { method: "POST", body: JSON.stringify(req) });
    // A 4xx/5xx from Rocket.Chat arrives here as a successful call with a
    // failing status -- colour it red, but show it as a response, not an error.
    const shown = r.text ? prettyJson(r.text) : { text: "", note: "empty body" };
    CALL_TEXT = shown.text;               // copy what is on screen, indentation and all
    out.textContent = "";
    out.append(
      el("span", { class: r.status < 400 ? "done" : "error" },
        `${req.method} ${r.url}\nHTTP ${r.status}  [${r.tag}]  in ${r.elapsed_ms}ms`
        + (shown.note ? `  ·  ${shown.note}` : "") + "\n\n"),
      shown.text);
  } catch (e) {
    CALL_TEXT = e.message;
    out.textContent = "";
    out.append(el("span", { class: "error" }, e.message));
  } finally {
    btn.disabled = false; btn.textContent = "Send";
    out.scrollTop = 0;
  }
}

// ---- doctor -----------------------------------------------------------------
// The badge could only say docker up/down. When it said down, every card showed
// "docker unavailable — actions disabled" and nothing said WHY or what to do,
// while `rc-repro doctor` had known the answer all along.
const DOCTOR_MARK = { ok: "✓", warn: "⚠", fail: "✗" };
const DOCTOR_VERDICT = {
  ok: ["ok", "All good — rc-repro is ready."],
  warn: ["warn", "Usable, with warnings above."],
  fail: ["bad", "Not ready — fix the ✗ item(s) above."],
};
async function openDoctor() {
  const body = $("#doctor-body");
  body.innerHTML = "";
  body.append(el("div", { class: "kv" }, "Checking…"));
  if (!$("#doctor-dialog").open) $("#doctor-dialog").showModal();
  let rep;
  try { rep = await api("/api/doctor"); }
  catch (e) { body.innerHTML = ""; body.append(el("p", { class: "banner bad" }, e.message)); return; }
  body.innerHTML = "";
  for (const c of rep.checks || []) {
    body.append(el("div", { class: "dcheck " + c.status },
      el("span", { class: "dmark" }, DOCTOR_MARK[c.status] || "·"),
      el("span", {}, c.message)));
  }
  // No repro tally. `rc-repro doctor` prints one because a terminal has nowhere
  // else to say it; here the rail lists every workspace by name and the status bar
  // counts them, both of them permanently and both of them behind this dialog.
  const [cls, text] = DOCTOR_VERDICT[rep.verdict] || DOCTOR_VERDICT.warn;
  body.append(el("p", { class: "banner " + cls }, text));
}

// ---- activity (jobs) --------------------------------------------------------
// A job outlives the dialog that started it AND a page refresh, but nothing could
// list one: a 20-minute benchmark whose dialog got closed finished server-side
// with its result unreachable. The manager already retains them (MAX_JOBS), so
// this only needed somewhere to look.
const JOB_RESULT_RENDERER = {
  create: renderCreateResult, up: renderCreateResult,
  monitor: renderCreateResult, "monitor-off": renderCreateResult,
  loadtest: renderPerfResult, capacity: renderCapResult, benchmark: renderBenchResult,
};
const jobTitle = (j) => (j.label ? `${j.kind}: ${j.label}` : j.kind);
function jobAge(j) {
  const end = j.finished_at || Date.now() / 1000;
  const s = Math.max(0, Math.round(end - j.started_at));
  const dur = s >= 60 ? `${Math.floor(s / 60)}m${s % 60}s` : `${s}s`;
  if (j.status === "queued") return "waiting for a slot";
  return j.status === "running" ? `${dur} and counting` : dur;
}
let ACT_TAB = "running";

async function openJobs(tab) {
  if (tab) ACT_TAB = tab;
  setView("activity");
  $(".panes").classList.add("no-detail");
  $("#actpane").innerHTML = "";
  const panel = $("#detail");
  panel.innerHTML = "";
  const head = el("div", { class: "home-head" }, el("h1", {}, "Activity"),
    el("p", {}, "What is running right now, and everything that has happened on this box."));
  const tabs = el("div", { class: "tabs", id: "act-tabs" },
    el("button", { class: "tab" + (ACT_TAB === "running" ? " active" : ""), id: "act-tab-running",
                   onclick: () => openJobs("running") }, "In progress"),
    el("button", { class: "tab" + (ACT_TAB === "history" ? " active" : ""), id: "act-tab-history",
                   onclick: () => openJobs("history") }, "History"));
  head.append(tabs);
  const body = el("div", { class: "home-body" });
  const card = el("div", { class: "hcard" });
  card.append(el("p", { class: "hint", id: "jobs-hint" }, ACT_TAB === "running"
    ? "Jobs keep running on the server after you leave this page — including across "
      + "a refresh. Click one to reopen its output."
    : (canAdmin()
        ? "Everything anyone has done on this box. Refusals are recorded too."
        : "Everything you have done on this box. Admins see everyone's.")));
  const filter = el("div", { id: "jobs-filter", class: "row2" },
    el("input", { id: "act-grep", class: "input", placeholder: "filter by action or target…",
                  "aria-label": "filter activity",
                  oninput: () => { if (ACT_TAB === "history") renderHistory($("#jobs-list")); } }),
    el("label", { class: "logfollow" },
      el("input", { type: "checkbox", id: "act-denied",
                    onchange: () => { if (ACT_TAB === "history") renderHistory($("#jobs-list")); } }),
      " refusals only"));
  filter.hidden = ACT_TAB !== "history";
  const box = el("div", { id: "jobs-list", class: "plan" });
  box.append(el("p", { class: "empty" }, "loading…"));
  card.append(filter, box);
  body.append(card);
  panel.append(head, body);
  if (ACT_TAB === "running") return renderRunning(box);
  return renderHistory(box);
}

async function renderRunning(box) {
  let rows;
  try { rows = (await api("/api/jobs")).jobs; }
  catch (e) { box.innerHTML = ""; box.append(el("p", { class: "empty" }, e.message)); return; }
  box.innerHTML = "";
  if (!rows.length) { box.append(el("div", { class: "empty" }, "No jobs yet.")); return; }
  for (const j of rows) {
    const row = el("button", {
      class: "jobrow", type: "button",
      onclick: () => reopenJob(j),
    },
      el("span", { class: "jstatus " + j.status }, j.status),
      el("span", { class: "jkind" }, jobTitle(j)));
    // The API has returned `actor` since accounts landed and this dropped it on
    // the floor -- which is most of why the shared box could not answer "who?".
    if (j.actor) row.append(el("span", { class: "jactor" }, j.actor));
    row.append(el("span", { class: "jage" }, jobAge(j)));
    box.append(row);
  }
}

async function renderHistory(box) {
  const q = $("#act-grep").value.trim();
  const denied = $("#act-denied").checked;
  let res;
  try { res = await api("/api/audit?limit=200" + (q ? `&q=${encodeURIComponent(q)}` : "")); }
  catch (e) { box.innerHTML = ""; box.append(el("p", { class: "empty" }, e.message)); return; }
  const rows = (res.lines || []).filter((r) => !denied || r.outcome === "denied");
  box.innerHTML = "";
  if (!rows.length) { box.append(el("p", { class: "empty" }, "Nothing recorded yet.")); return; }
  for (const r of rows) {
    const row = el("div", { class: "jobrow" });
    row.append(el("span", { class: "jstatus " + (r.outcome === "denied" ? "error" : "done") },
      r.outcome === "denied" ? "denied" : r.kind));
    row.append(el("span", { class: "jkind" },
      (r.outcome === "denied" ? "" : "") + r.label));
    if (r.actor) row.append(el("span", { class: "jactor" }, r.actor));
    // Only shown when it is NOT a checked session: that is the common, trustworthy
    // case, and a column that says "session" on every line teaches nothing.
    if (r.origin && r.origin !== "session") {
      row.append(el("span", { class: "pill small", title: "how this identity was established" }, r.origin));
    }
    row.append(el("span", { class: "jage" }, (r.ts || "").slice(0, 19).replace("T", " ")));
    box.append(row);
  }
  if (res.truncated) {
    box.append(el("p", { class: "hint" },
      "Stopped early — the log is large. Filter it, or ship it somewhere with an index."));
  }
}
// since=0, so the retained events replay from the start; the terminal event is
// among them, which is what re-renders a finished job's result table.
function reopenJob(j) { streamJob(j.id, jobTitle(j), JOB_RESULT_RENDERER[j.kind]); }

// ---- create dialog ----------------------------------------------------------
let PRESETS = [];
let ACME_EMAIL_REMEMBERED = false;
// Who may set rc_image / reg_token / bind, and which fields those are -- both from
// the server, which owns the policy (gui.create_policy). Defaulting to false means
// a failed /api/settings hides them rather than offering fields the create would
// reject.
let MAY_SET_PRIVILEGED = false;
let PRIVILEGED_FIELDS = ["rc_image", "reg_token", "bind"];

async function openCreate(preset) {
  try { PRESETS = (await api("/api/presets")).presets; } catch (e) { toast(e.message); return; }
  // Whether `config set acme.email` has been run. The email field is optional only
  // if it has -- and the GUI cannot set it, so a first-time user must be told to
  // type one rather than discovering it from a failed job.
  try {
    const s = await api("/api/settings");
    ACME_EMAIL_REMEMBERED = s.acme_email_remembered;
    MAY_SET_PRIVILEGED = !!s.may_set_privileged;
    if (Array.isArray(s.privileged_fields) && s.privileged_fields.length) {
      PRIVILEGED_FIELDS = s.privileged_fields;
    }
  } catch (_) { ACME_EMAIL_REMEMBERED = false; MAY_SET_PRIVILEGED = false; }
  syncHttpsFields();
  const sel = $("#preset-select");
  sel.innerHTML = "";
  for (const p of PRESETS) sel.append(el("option", { value: p.name }, p.name + (p.requires_license ? " (license)" : "")));
  sel.value = preset && PRESETS.some((p) => p.name === preset) ? preset : "default";
  renderPresetParams();
  $("#create-form").existing.value = "reuse";
  syncCreateSeed();
  $("#version-hint").textContent = "";
  // These decide what CODE runs and where it listens, and the server refuses them
  // from whoever `gui.create_policy` says (admin by default, `anyone` on a box
  // where every account is a colleague). Ask the SERVER who that is rather than
  // deciding from the role here: two places computing the same permission is how
  // the dialog ends up offering a field the API rejects, which is exactly what it
  // did -- app.py's comment claimed the GUI "never sends them for a member", and
  // it sent all three for everybody.
  //
  // Hidden rather than disabled: a greyed-out field invites "why can't I?", and on
  // a box that has opened the policy there is nothing to explain.
  for (const name of PRIVILEGED_FIELDS) {
    const input = $("#create-form")[name];
    if (input && input.closest("label")) {
      input.closest("label").hidden = !MAY_SET_PRIVILEGED;
      if (!MAY_SET_PRIVILEGED) input.value = "";
    }
  }
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
// ---- HTTPS: certificate check ------------------------------------------------
const TLS_LABEL = {
  local: "local CA (run trust-ca)",
  acme: "Let's Encrypt",
  own: "supplied certificate",
};

function doTlsStatus(name) {
  return runAction(name, "Check TLS", async () => {
    const r = await api(`/api/repros/${name}/tls`);
    const lines = [`${r.public_url}`, ""];
    if (!r.serving) {
      lines.push(`NOT serving TLS on this host's port ${r.port}`, `  ${r.error}`, "",
                 "Rocket.Chat itself is fine on its plain http port — this is the TLS layer.");
    } else {
      lines.push(`Issuer   ${r.issuer || "?"}`, `Subject  ${r.subject || "?"}`,
                 `Expires  ${(r.dates || "").replace("notAfter=", "")}`, "");
      if (r.fallback) {
        lines.push("This is Traefik's built-in placeholder certificate, so ACME never "
                 + "succeeded. Check the logs for 'acme'.");
      } else if (r.mode === "local") {
        lines.push(r.trusted_via_ca ? "Serving rc-repro's local CA."
                                    : "WARNING: does not chain to rc-repro's CA.");
        lines.push(r.trusted ? "This machine trusts it — trust-ca has been run."
                             : "Not trusted here yet: run `rc-repro trust-ca` once.");
      } else if (r.trusted) {
        lines.push("Serving a certificate this machine trusts.");
      } else {
        lines.push("Serving a real certificate that this machine does not trust — "
                 + "normal for Let's Encrypt staging.");
      }
      if (r.public_issuer && r.public_issuer !== r.issuer) {
        lines.push("", `The public name serves a DIFFERENT certificate (${r.public_issuer}).`,
                   "That is what a proxy in front looks like (Cloudflare orange cloud).");
      } else if (r.public_error) {
        lines.push("", `The public name is not reachable from here: ${r.public_error}`);
      }
    }
    $("#pat-title").textContent = `TLS: ${name}`;
    $("#pat-body").textContent = lines.join("\n");
    PAT_HEADERS = "";
    $("#pat-dialog").showModal();
  });
}

// ---- HTTPS section of the create dialog -------------------------------------
// Two modes, and only one of them asks for anything: a domain and an email, the
// same pair the official Rocket.Chat compose takes as DOMAIN and LETSENCRYPT_EMAIL.
const HTTPS_MODE_HINT = {
  "": "",
  acme: "Traefik obtains a real, publicly trusted certificate from Let's Encrypt."
      + " The domain must already resolve to this host with port 443 reachable —"
      + " Let's Encrypt validates by connecting to it.",
  local: "A certificate signed by rc-repro's own CA. Works offline, no domain, no rate"
       + " limits. Browsers warn until `rc-repro trust-ca` has been run once. A phone"
       + " cannot use it — use Let's Encrypt for mobile.",
};

function syncHttpsFields() {
  const mode = $("#https-mode").value;
  $("#https-mode-hint").textContent = HTTPS_MODE_HINT[mode] || "";
  for (const el of document.querySelectorAll(".https-acme")) el.hidden = mode !== "acme";
  const email = $("#create-form") && $("#create-form").acme_email;
  if (email) {
    email.required = mode === "acme" && !ACME_EMAIL_REMEMBERED;
    email.placeholder = ACME_EMAIL_REMEMBERED
      ? "remembered — leave blank to reuse it"
      : "you@example.com";
  }
}

function applyHttpsToRequest(f, req) {
  const mode = f.https_mode ? f.https_mode.value : "";
  if (!mode) return true;
  const val = (k) => (f[k] && f[k].value.trim()) || "";
  if (mode === "local") { req.https = true; return true; }
  if (!val("domain")) { toast("HTTPS: a domain is required for Let's Encrypt"); return false; }
  if (!val("acme_email") && !ACME_EMAIL_REMEMBERED) {
    toast("HTTPS: a contact email is required for Let's Encrypt");
    return false;
  }
  req.domain = val("domain");
  // The email may be blank when it is remembered in config. Everything else --
  // the challenge, the port, whether to publish publicly -- is derived server-side,
  // so the form does not ask for it.
  if (val("acme_email")) req.acme_email = val("acme_email");
  return true;
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
  if (f.seed.checked && f.seed_profile) req.seed_profile = f.seed_profile.value;
  if (!req.version) { toast("version is required"); return; }
  for (const inp of f.querySelectorAll("input[name^='set:']")) {
    if (inp.value.trim()) req.params[inp.name.slice(4)] = inp.value.trim();
  }
  for (const k of ["name", "reg_token", "mongo", "rc_image", "bind", "root_url"]) {
    if (f[k] && f[k].value.trim()) req[k] = f[k].value.trim();
  }
  for (const k of ["pin", "offline", "no_pull"]) if (f[k] && f[k].checked) req[k] = true;
  if (!applyHttpsToRequest(f, req)) return;      // validates before closing the dialog
  // One choice, not two checkboxes: either flag bypasses reuse, and `fresh` already
  // implies `force`, so ticking both was meaningless.
  const existing = f.existing ? f.existing.value : "reuse";
  if (existing === "force") req.force = true;
  if (existing === "fresh") {
    if (!confirm(`This DELETES ${req.name || "the existing repro"}'s data volume — all users, `
      + `channels, messages and uploads — then rebuilds it.\n\nContinue?`)) return;
    req.fresh = true;
  }
  $("#create-dialog").close();
  try { const { job_id } = await api("/api/repros", { method: "POST", body: JSON.stringify(req) }); streamJob(job_id, `Creating ${req.version} (${req.preset})`, renderCreateResult); }
  catch (e) { toast(e.message); }
}

// ---- your account: sessions and the way out ---------------------------------
// Server-side sessions are what make any of this possible. HTTP Basic had no
// logout at all -- the browser cached the credential until the tab closed, and
// "revocation is a file edit" was the honest description.
const fmtWhen = (epoch) => {
  const s = Math.max(0, Math.round(Date.now() / 1000 - epoch));
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 172800) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
};

async function openSessions() {
  $("#session-title").textContent = `Your sessions`;
  const body = $("#session-body");
  body.innerHTML = "";
  body.append(el("p", { class: "empty" }, "loading…"));
  $("#session-dialog").showModal();
  let rows;
  try { rows = (await api("/api/sessions")).sessions || []; }
  catch (e) { body.innerHTML = ""; body.append(el("p", { class: "empty" }, e.message)); return; }
  body.innerHTML = "";
  if (!rows.length) { body.append(el("p", { class: "empty" }, "No other sessions.")); return; }
  for (const s of rows) {
    const row = el("div", { class: "jobrow" },
      el("span", { class: "jstatus " + (s.current ? "done" : "queued") },
        s.current ? "this one" : "other"),
      el("span", { class: "jkind" }, `${s.label} · seen ${fmtWhen(s.last_seen)}`));
    if (!s.current) {
      row.append(el("button", {
        class: "btn small danger", onclick: () => revokeSession(s.sid),
      }, "Revoke"));
    }
    body.append(row);
  }
}

async function revokeSession(sid) {
  try { await api(`/api/sessions?sid=${encodeURIComponent(sid)}`, { method: "DELETE" });
    toast("session revoked", "ok"); openSessions(); }
  catch (e) { toast(e.message); }
}

// A real form POST, not fetch: sign-out ends with a redirect to the sign-in page,
// and letting the browser follow it is simpler and works even if this script is
// mid-failure. The logs socket and any SSE stream are closed first so nothing is
// still reading from a session that no longer exists.
function signOut(everywhere) {
  SIGNING_OUT = true;
  stopPolling(); closeLogs(); closeJobStream();
  if (!everywhere) {
    const f = el("form", { method: "post", action: "/signout" });
    document.body.append(f); f.submit();
    return;
  }
  fetch("/api/sessions?all=1", { method: "DELETE", credentials: "same-origin" })
    .finally(() => location.assign("/signin?e=signedout"));
}

// ---- people (admin only) -----------------------------------------------------
// The whole dialog is admin-only; a member gets 403 from every endpoint behind it,
// so the entry point is simply not rendered rather than rendered-and-refused.
const ROLE_HELP = {
  admin: "everything, including managing people",
  member: "full workspace lifecycle — today's behaviour",
  readonly: "look only. No logs, env or tokens: those carry real secrets",
};

async function openPeople() {
  const body = $("#people-body");
  body.innerHTML = "";
  body.append(el("p", { class: "empty" }, "loading…"));
  $("#people-dialog").showModal();
  let data;
  try { data = await api("/api/users"); }
  catch (e) { body.innerHTML = ""; body.append(el("p", { class: "empty" }, e.message)); return; }
  body.innerHTML = "";
  for (const u of data.users) {
    const row = el("div", { class: "jobrow" });
    row.append(el("span", { class: "jstatus " + (u.role === "admin" ? "done" : "queued") }, u.role));
    const label = el("span", { class: "jkind" }, u.name === ME ? `${u.name} (you)` : u.name);
    // Blank role == admin is the MIGRATION for accounts made before roles existed,
    // not a default. Saying so here is what stops it looking like a mistake.
    if (u.implicit) label.append(el("span", { class: "yours", title: "role column is blank, which means admin" }, " implicit"));
    row.append(label);
    row.append(el("span", { class: "jage" },
      u.sessions ? `${u.sessions} session${u.sessions === 1 ? "" : "s"}` : "—"));
    const sel = el("select", { class: "input", "aria-label": `role for ${u.name}`,
      onchange: (e) => changeRole(u.name, e.target.value) },
      ...data.roles.map((r) => el("option", { value: r, title: ROLE_HELP[r] || "" }, r)));
    sel.value = u.role;
    row.append(sel);
    row.append(el("button", { class: "btn small", title: "Generate a new password",
      onclick: () => resetPassword(u.name) }, "reset"));
    row.append(el("button", { class: "btn small danger",
      onclick: () => removePerson(u.name) }, "remove"));
    body.append(row);
  }
}

// The password exists in exactly one place for exactly one moment: here.
function showOnce(title, name, password) {
  $("#pat-title").textContent = title;
  $("#pat-body").textContent =
    `${name}\n\nPassword: ${password}\n\n`
    + "Shown once. rc-repro stores only a scrypt hash, so nobody — including you —\n"
    + "can read it back. Send it however you would send anything else, and have\n"
    + "them change it after signing in.";
  PAT_HEADERS = password;      // so "Copy headers" copies the password
  $("#pat-dialog").showModal();
}

async function addPerson() {
  const name = $("#new-user").value.trim();
  if (!name) { toast("enter a name"); return; }
  try {
    const r = await api("/api/users", { method: "POST",
      body: JSON.stringify({ name, role: $("#new-role").value }) });
    $("#new-user").value = "";
    openPeople();
    showOnce(`Account created: ${r.name}`, `${r.name} · ${r.role}`, r.password);
  } catch (e) { toast(e.message); }
}

async function changeRole(name, role) {
  try {
    const r = await api(`/api/users/${encodeURIComponent(name)}/role`,
      { method: "POST", body: JSON.stringify({ role }) });
    toast(`${name} is now ${r.role}` + (r.sessions_ended ? ` (${r.sessions_ended} session(s) ended)` : ""), "ok");
    openPeople();
    if (name === ME) location.reload();     // your own menu just changed
  } catch (e) { toast(e.message); openPeople(); }
}

async function resetPassword(name) {
  if (!confirm(`Reset ${name}'s password?\n\nTheir current one stops working and every session they have ends.`)) return;
  try {
    const r = await api(`/api/users/${encodeURIComponent(name)}/password`, { method: "POST" });
    openPeople();
    showOnce(`Password reset: ${name}`, name, r.password);
  } catch (e) { toast(e.message); }
}

async function removePerson(name) {
  if (!confirm(`Remove ${name}?\n\nThey are signed out immediately and can no longer sign in.\nTheir workspaces are left alone.`)) return;
  try {
    await api(`/api/users/${encodeURIComponent(name)}`, { method: "DELETE" });
    toast(`${name} removed`, "ok"); openPeople();
  } catch (e) { toast(e.message); }
}

// ---- wiring -----------------------------------------------------------------
$("#people-close").addEventListener("click", () => $("#people-dialog").close());
$("#people-add-btn").addEventListener("click", addPerson);
// ---- account menu ----------------------------------------------------------
function closeMe() {
  $("#me-menu").hidden = true;
  $("#whoami").setAttribute("aria-expanded", "false");
}
$("#whoami").addEventListener("click", (e) => {
  e.stopPropagation();
  const m = $("#me-menu");
  const open = m.hidden;
  m.hidden = !open;
  $("#whoami").setAttribute("aria-expanded", String(open));
  if (open) {
    $("#me-name").textContent = ME;
    $("#me-role").textContent = MY_ROLE || "";
    $("#me-people").hidden = MY_ROLE !== "admin";
    $("#me-sessions-n").textContent = "";
    // Fetched when the menu opens rather than on every poll: it is one line of
    // context, and nobody needs it four seconds fresh.
    api("/api/sessions").then((r) => {
      const n = (r.sessions || []).length;
      $("#me-sessions-n").textContent = n ? `${n} active` : "";
    }).catch(() => {});
  }
});
document.addEventListener("click", closeMe);
$("#me-menu").addEventListener("click", (e) => e.stopPropagation());
$("#me-sessions").addEventListener("click", () => { closeMe(); openSessions(); });
$("#me-people").addEventListener("click", () => { closeMe(); openPeople(); });
$("#me-passwd").addEventListener("click", () => { closeMe(); openPasswd(); });
$("#me-out").addEventListener("click", () => { closeMe(); signOut(false); });
$("#session-close").addEventListener("click", () => $("#session-dialog").close());
// ---- change your own password ----------------------------------------------
function openPasswd() {
  const f = $("#passwd-form");
  f.old.value = ""; f.new.value = "";
  $("#passwd-err").hidden = true;
  $("#passwd-dialog").showModal();
  f.old.focus();
}
$("#passwd-cancel").addEventListener("click", () => $("#passwd-dialog").close());
$("#passwd-go").addEventListener("click", async () => {
  const f = $("#passwd-form");
  const err = $("#passwd-err");
  err.hidden = true;
  if (!f.old.value || !f.new.value) {
    err.textContent = "Both fields are needed."; err.hidden = false; return;
  }
  try {
    // The endpoint ends every OTHER session and re-issues this one, so there is
    // nothing to do here but say so -- no reload, no re-login.
    const r = await api("/api/me/password", {
      method: "POST",
      body: JSON.stringify({ old: f.old.value, new: f.new.value }),
    });
    $("#passwd-dialog").close();
    const n = r.sessions_ended || 0;
    toast(n ? `Password changed. ${n} other session(s) signed out.` : "Password changed.", "ok");
  } catch (e) {
    err.textContent = e.message; err.hidden = false;
  }
});
$("#session-all").addEventListener("click", () => {
  if (confirm("Sign out of every browser you have signed in from?")) signOut(true);
});
$("#btn-new").addEventListener("click", () => openCreate());
$("#btn-scenarios").addEventListener("click", openScenarios);
$("#btn-home").addEventListener("click", goHome);
$(".brand").addEventListener("click", goHome);
$("#btn-refresh").addEventListener("click", loadRepros);

// ---- theme ------------------------------------------------------------------
// Stored per browser, not per account: it is a property of the screen you are
// sitting at, and the same person on a projector and a laptop wants different
// answers. First visit follows the OS rather than picking for them.
const THEME_KEY = "rc-repro-theme";
function applyTheme(name) {
  document.documentElement.dataset.theme = name === "dark" ? "dark" : "light";
  // Also in a cookie, because the SIGN-IN page is server-rendered with no script
  // (deliberately -- see web/signin.py) and localStorage is unreadable without
  // one. Nothing but the palette depends on it, so it is not HttpOnly and carries
  // no security weight; the server accepts only the two literal values.
  try {
    document.cookie = "rc_repro_theme=" + (name === "dark" ? "dark" : "light")
      + ";path=/;max-age=31536000;samesite=lax";
  } catch (_) { /* cookies off; prefers-color-scheme still applies */ }
  const b = $("#theme-toggle");
  if (b) {
    b.textContent = name === "dark" ? "light" : "dark";
    b.setAttribute("aria-label", `Switch to the ${name === "dark" ? "light" : "dark"} theme`);
  }
}
function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch (_) { /* private mode */ }
  applyTheme(saved || (window.matchMedia
    && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
}
$("#theme-toggle").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(next);
  try { localStorage.setItem(THEME_KEY, next); } catch (_) { /* private mode */ }
});
initTheme();
// The initial VIEW is an assignment, not a setView() call, so nothing had
// marked the top bar on first load — you arrived on Home with no link lit.
setView(VIEW);
$("#btn-prune").addEventListener("click", doPrune);
$("#filter").addEventListener("input", (e) => { view.filter = e.target.value.trim().toLowerCase(); render(); });
$("#status-filter").addEventListener("change", (e) => { view.status = e.target.value; render(); });
$("#sort-by").addEventListener("change", (e) => { view.sort = e.target.value; render(); });
$("#scope-filter").addEventListener("change", (e) => {
  view.scope = e.target.value; localStorage.setItem("rc_scope", view.scope); render();
});
$("#preset-select").addEventListener("change", renderPresetParams);
$("#https-mode").addEventListener("change", syncHttpsFields);
// The profile only means anything when seeding is on, so don't show it otherwise.
function syncCreateSeed() {
  $("#create-seed-profile-row").hidden = !$("#create-form").seed.checked;
}
$("#create-form").seed.addEventListener("change", syncCreateSeed);
// Resolve the version as it is typed: which MongoDB it pairs with, and whether this
// engine can even run that MongoDB -- before committing to a multi-GB pull.
let versionTimer = null;
$("#create-form").version.addEventListener("input", (e) => {
  const v = e.target.value.trim();
  const out = $("#version-hint");
  clearTimeout(versionTimer);
  if (!v) { out.textContent = ""; out.className = "hint"; return; }
  versionTimer = setTimeout(async () => {
    try {
      const r = await api(`/api/versions/${encodeURIComponent(v)}`);
      out.textContent = `Mongo ${r.mongo_tag} (${r.mongo_flavor})` +
        (r.warning ? ` — ${r.warning}` : `  ·  via ${r.source}`);
      out.className = r.warning ? "hint bad" : "hint";
    } catch (err) {
      out.textContent = err.message;
      out.className = "hint bad";
    }
  }, 400);
});
$("#create-cancel").addEventListener("click", () => $("#create-dialog").close());
$("#create-submit").addEventListener("click", (e) => { e.preventDefault(); submitCreate(); });
$("#job-close").addEventListener("click", () => $("#job-dialog").close());
$("#job-dialog").addEventListener("close", () => {
  const job = JOB;
  closeJobStream();      // stop streaming into a hidden dialog (and stop the leak)
  if (job && !job.finished) watchDetachedJob(job);
});
$("#seed-mode").addEventListener("change", syncSeedMode);
$("#seed-cancel").addEventListener("click", () => $("#seed-dialog").close());
$("#seed-clear").addEventListener("click", clearScale);
$("#seed-submit").addEventListener("click", (e) => { e.preventDefault(); submitSeed(); });
$("#upgrade-cancel").addEventListener("click", () => $("#upgrade-dialog").close());
// Resolved server-side as you type: the version -> MongoDB pairing and the
// downgrade/major refusals are the backend's to decide, and finding out after a
// job started is too late. `change` too, so paste-and-blur is not missed.
$("#upgrade-to").addEventListener("change", checkUpgradePlan);
$("#upgrade-to").addEventListener("blur", checkUpgradePlan);
$("#upgrade-go").addEventListener("click", (e) => { e.preventDefault(); submitUpgrade(); });
$("#import-cancel").addEventListener("click", () => $("#import-dialog").close());
$("#import-preview").addEventListener("click", (e) => { e.preventDefault(); previewImport(); });
$("#import-apply").addEventListener("click", applyImport);
$("#perf-cancel").addEventListener("click", () => $("#perf-dialog").close());
$("#perf-submit").addEventListener("click", submitPerf);
$("#btn-bench").addEventListener("click", openBench);
$("#btn-jobs").addEventListener("click", () => openJobs("running"));
$("#pat-close").addEventListener("click", () => $("#pat-dialog").close());
$("#pat-copy").addEventListener("click", () => copy(PAT_HEADERS));
// A live admin credential should not outlive the dialog showing it. Bound to
// `close` rather than the button: Escape dismisses a <dialog> without it.
$("#pat-dialog").addEventListener("close", () => {
  PAT_HEADERS = "";
  $("#pat-body").textContent = "";
});
// On `submit`, not the Send button's click, so Enter in the path field sends too.
// preventDefault is what keeps the dialog open: this form is method="dialog", so
// without it every send would also dismiss the response it just fetched.
$("#call-form").addEventListener("submit", (e) => { e.preventDefault(); submitCall(); });
$("#call-close").addEventListener("click", () => $("#call-dialog").close());
$("#call-copy").addEventListener("click", () =>
  CALL_TEXT ? copy(CALL_TEXT) : toast("nothing to copy — send a request first"));
$("#docker-badge").addEventListener("click", openDoctor);
$("#edge-badge").addEventListener("click", openEdge);
$("#edge-close").addEventListener("click", () => $("#edge-dialog").close());
$("#doctor-recheck").addEventListener("click", openDoctor);
$("#doctor-close").addEventListener("click", () => $("#doctor-dialog").close());
$("#cap-cancel").addEventListener("click", () => $("#cap-dialog").close());
$("#cap-submit").addEventListener("click", submitCap);
$("#bench-cancel").addEventListener("click", () => $("#bench-dialog").close());
$("#bench-submit").addEventListener("click", (e) => { e.preventDefault(); submitBench(); });

const POLL_MS = 4000;
let pollTimer = null;
function startPolling() { if (!pollTimer) pollTimer = setInterval(loadRepros, POLL_MS); }
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }
// Each poll costs several docker CLI invocations (info + compose ls + ps); a
// hidden tab kept paying for them forever, and every open tab multiplied it.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopPolling();
  else { loadRepros(); startPolling(); }
});

loadRepros();
startPolling();
