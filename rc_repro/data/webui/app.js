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
// lifecycle.list_repros() reports "?" when docker is down, which is not a usable
// CSS class token (and had no rule), so those cards rendered unstyled.
const stateClass = (s) => (s === "?" ? "unknown" : s);
// Colour the health badge from HEALTH, not state: "Up 2 minutes (unhealthy)" is
// state=running + health=unhealthy, and colouring by state painted it GREEN.
function healthClass(r) {
  const h = (r.health || "").toLowerCase();
  if (h.includes("unhealthy")) return "bad";
  if (h.includes("starting")) return "warn";
  if (h === "healthy" || h === "running") return "running";
  return stateClass(r.state);
}
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
  // With docker unreachable every state is "?", so "0 running" would be a claim we
  // cannot support (and #running-pill is styled green).
  const unknown = ALL_REPROS.length > 0 && ALL_REPROS.every((r) => r.state === "?");
  const pill = $("#running-pill");
  pill.textContent = unknown ? "state unknown" : `${running} running`;
  pill.className = "pill " + (unknown ? "unknown" : "running");
  $("#sb-count").textContent = `${ALL_REPROS.length} repro(s) · ` +
    (unknown ? "state unknown (docker unavailable)" : `${running} running`);
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
  else if (!list.length) grid.append(el("p", { class: "empty" }, "No repros match this filter."));
  for (const r of list) grid.append(card(r));
  grid.append(el("div", { class: "card new", onclick: openCreate },
    el("div", { class: "plus" }, "+"),
    el("div", { class: "t" }, "New repro"),
    el("div", { class: "s" }, "Spin up a fresh RC + Mongo sandbox")));
}

function card(r) {
  const busyLabel = pendingOn(r.name);
  const head = el("div", { class: "card-head" }, el("span", { class: "name" }, r.name));
  if (r.default) head.append(el("span", { class: "pill default" }, "default"));
  // While an action is in flight the pill reports it rather than the now-stale
  // server state: "stopping" is the honest answer between the click and the reply.
  head.append(busyLabel
    ? el("span", { class: "pill working" }, (BUSY_VERB[busyLabel] || busyLabel).toLowerCase() + "…")
    : el("span", { class: "pill " + stateClass(r.state) }, r.state));
  head.append(el("span", { class: "chev" }, "›"));

  const meta = el("div", { class: "card-meta" },
    `RC ${r.rc_version} · Mongo ${r.mongo_tag} · :${r.host_port} · ${r.preset}`
    + (r.monitoring ? " · monitored" : ""));

  const actions = el("div", { class: "card-actions" });
  // "?" means docker could not be asked, so EVERY action below would fail -- the
  // card used to offer three buttons that all produced a red toast.
  const dockerOk = r.state !== "?";
  if (dockerOk) {
    actions.append(stop(el("a", { href: localUrl(r.root_url), target: "_blank", style: "text-decoration:none" },
      el("button", { class: "btn small primary" }, "Open RC"))));
  }
  if (r.state === "running") {
    actions.append(actBtn("Stop", () => doState(r.name, "stop"), "", busyLabel));
    actions.append(actBtn("Restart", () => doState(r.name, "restart"), "", busyLabel));
    actions.append(actBtn("Seed", () => openSeed(r.name), "", busyLabel));
  } else if (r.state === "stopped") {
    actions.append(actBtn("Start", () => doState(r.name, "start"), "", busyLabel));
  } else if (r.state === "down") {
    // `down` removed the containers (the data volume and record survive), so
    // `compose start` cannot revive it -- recreate from the stored metadata.
    // Without this a "keep the data" Down left the card with no way back up.
    actions.append(actBtn("Bring up", () => doBringUp(r.name), "primary", busyLabel));
  } else if (!dockerOk) {
    actions.append(el("span", { class: "inline-note" }, "docker unavailable"));
  } else {
    // restarting / created / paused / dead -- compose reports these verbatim and
    // they used to get no lifecycle control at all.
    actions.append(actBtn("Stop", () => doState(r.name, "stop"), "", busyLabel));
    actions.append(actBtn("Restart", () => doState(r.name, "restart"), "", busyLabel));
  }
  // `docker compose logs` needs containers; a down repro returned an empty pane.
  if (r.state === "running" || r.state === "stopped") {
    actions.append(actBtn("Logs", () => showLogs(r.name), "", busyLabel));
  }
  if (dockerOk) actions.append(actBtn("Down", () => doDown(r.name), "danger", busyLabel));

  const foot = el("div", { class: "card-foot" },
    el("span", {}, "Uptime: " + (r.uptime || "—")),
    el("span", { class: "health " + healthClass(r) }, r.health || r.state));

  // A plain div with onclick was unreachable by keyboard, and every inner button
  // stopPropagation()s -- so the whole detail panel (logs, env, load test) had no
  // keyboard path at all.
  const c = el("div", {
    class: "card st-" + stateClass(r.state) + (r.name === SELECTED ? " selected" : ""),
    role: "button", tabindex: "0", "aria-label": `${r.name} — ${r.state}`,
    onclick: () => selectRepro(r.name),
    onkeydown: (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectRepro(r.name); }
    },
  }, head, meta, actions, foot);
  return c;
}

function stop(node) { node.addEventListener("click", (e) => e.stopPropagation()); return node; }

// Which repros have a slow synchronous action in flight, name -> the action's
// button label. This has to live outside the DOM: the 4s poll rebuilds the whole
// card grid, so feedback parked on the button node vanished within 4s of the
// click and left a still-running Stop looking clickable again.
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

// ---- detail panel -----------------------------------------------------------
async function selectRepro(name) {
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
  const tabs = el("div", { class: "tabs" });
  for (const t of ["overview", "logs", "containers", "env vars"]) {
    const key = t === "env vars" ? "env" : t;
    tabs.append(el("button", { class: "tab" + (dstate.tab === key ? " active" : ""), onclick: () => switchTab(key) },
      t.charAt(0).toUpperCase() + t.slice(1)));
  }
  const actions = el("div", { class: "d-actions" });
  if (d.state !== "?") {
    actions.append(el("a", { href: localUrl(d.root_url), target: "_blank", style: "text-decoration:none" },
      el("button", { class: "btn primary" }, "↗ Open RC")));
  }
  if (d.state === "running") {
    actions.append(dBtn("Stop", () => doState(d.name, "stop")));
    actions.append(dBtn("Restart", () => doState(d.name, "restart")));
    actions.append(dBtn("Seed", () => openSeed(d.name)));
    actions.append(dBtn("Config", () => openImport(d.name)));
    actions.append(dBtn("Load test", () => openPerf(d.name, d.monitoring)));
    actions.append(dBtn("Capacity", () => openCap(d.name)));
    // The endpoint existed and nothing called it, so "Stream to Grafana" was
    // permanently greyed out for any repro not created with the monitor checkbox.
    actions.append(dBtn(d.monitoring ? "Monitoring off" : "Monitoring on",
      () => doMonitor(d.name, !!d.monitoring)));
    // A PAT is what you need to drive this workspace's API from curl/Postman;
    // the CLI could mint one and the GUI could not.
    actions.append(dBtn("API token", () => doPat(d.name), "",
      "Mint a Personal Access Token for this workspace and show the "
      + "X-Auth-Token / X-User-Id headers to use with curl or Postman."));
    actions.append(dBtn("API call", () => openCall(d.name), "",
      "Send an authenticated REST call to this workspace and see the response "
      + "— the same request `rc-repro api` makes."));
    // `up --wait` only proves RC booted (it polls the internal http port). Traefik
    // gets its certificate in the background afterwards and falls back to a dummy
    // when ACME fails, so HTTPS needs its own check.
    if (d.public_url) {
      actions.append(dBtn("Check TLS", () => doTlsStatus(d.name), "",
        "Make a real TLS connection and report the certificate actually being "
        + "served — the same check as `rc-repro tls-status`."));
    }
  } else if (d.state === "stopped") {
    actions.append(dBtn("Start", () => doState(d.name, "start")));
  } else if (d.state === "down") {
    actions.append(dBtn("Bring up", () => doBringUp(d.name), "primary"));
  } else if (d.state === "?") {
    actions.append(el("span", { class: "inline-note" }, "docker unavailable — actions disabled"));
  } else {
    // restarting / created / paused / dead. These only started reaching the panel
    // once repro_state() stopped flattening them to "stopped"; before that this
    // branch did not exist, so the panel would have offered no lifecycle control
    // at all. The card has always had the same pair.
    actions.append(dBtn("Stop", () => doState(d.name, "stop")));
    actions.append(dBtn("Restart", () => doState(d.name, "restart")));
  }
  // The `default` pill was displayed but unmovable from the GUI -- the create
  // dialog's Pin checkbox was the only way to set it.
  if (!d.is_default) {
    actions.append(dBtn("Make default", () => doDefault(d.name), "",
      "Use this repro for name-less CLI commands (rc-repro use)."));
  }
  if (d.state !== "?") {
    actions.append(dBtn("Down", () => doDown(d.name), "danger"));
  }
  panel.append(head, tabs, actions, el("div", { class: "d-body", id: "d-body" }));
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
    const grid = el("div", { class: "kv-grid" },
      kv("RC Version", d.rc_version), kv("MongoDB", d.mongo_tag),
      kv("Port", ":" + d.host_port), kv("Uptime", d.uptime || "—", d.uptime ? "green" : ""),
      kv("Preset", d.preset), kv("Health", d.health || "—", d.health === "healthy" ? "green" : ""));
    // A climbing restart count separates "slow to boot" from "crash-looping".
    // The backend has always been able to read it (wait_serving warns on it
    // during a create); nothing ever showed it afterwards.
    if (typeof d.restarts === "number" && d.restarts > 0) {
      grid.append(kv("RC restarts", String(d.restarts), d.restarts >= 2 ? "bad" : "warn"));
    }
    // With --https the external URL differs from the port above, and which kind of
    // certificate it is decides whether a browser will trust it — so say both.
    if (d.public_url) {
      grid.append(kv("HTTPS", TLS_LABEL[d.tls] || "on", "green"));
    }
    body.append(grid);
    if (d.state === "restarting" || (d.restarts || 0) >= 2) {
      body.append(el("p", { class: "banner bad" },
        `Rocket.Chat has restarted ${d.restarts || 0}× — usually resource pressure `
        + `(free some repros, or raise Docker's CPU/RAM) or a boot error. Check the Logs tab.`));
    }
    if (d.links && d.links.length) {
      body.append(el("div", { class: "section-label" }, "Links"));
      const links = el("div", { class: "card-links" });
      for (const l of d.links) links.append(el("a", { class: "linkchip " + (l.kind || ""), href: localUrl(l.url), target: "_blank" }, l.label));
      body.append(links);
    }
    appendNotes(body, d.notes);
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
  }
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
function renderLogList() {
  const box = $("#logview"); if (!box) return;
  box.innerHTML = "";
  for (const e of logv.buf) if (passes(e)) box.append(logRow(e));
  // refresh service dropdown options
  const sel = $("#log-svc"); if (sel) {
    const svcs = [...new Set(logv.buf.map((e) => e.service).filter(Boolean))].sort();
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
  const ws = new WebSocket(`${proto}://${location.host}/api/repros/${d.name}/logs/stream?t=${encodeURIComponent(TOKEN)}&tail=300`);
  dstate.logsWS = ws;
  ws.onmessage = (m) => {
    const e = parseLogLine(m.data);
    logv.buf.push(e);
    if (logv.buf.length > LOG_MAX) logv.buf.shift();
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
  const vol = confirm(
    `Also DELETE ${name}'s data volume and record?\n\n` +
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
      method: "POST", headers: { "X-RC-Repro-Token": TOKEN }, body: fd });
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
  if (!confirm("Delete every 'down' repro, including data volumes and records, plus the empty rc-repro-owned Kind cluster?")) return;
  await withBusy($("#btn-prune"), "Pruning…", async () => {
    try {
      const r = await api("/api/prune", { method: "POST", body: JSON.stringify({ confirm: true }) });
      const cluster = r.cluster?.deleted ? ", deleted empty Kind cluster" : "";
      toast(`pruned ${r.removed.length}${cluster}`);
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
    `/api/jobs/${job.id}/stream?since=${job.idx}&t=${encodeURIComponent(TOKEN)}`);
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
  if (state.status === "running") {
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
    if (state.status === "running") continue;
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
function appendNotes(parent, notes) {
  if (!notes || !notes.length) return;
  parent.append(el("div", { class: "section-label" }, "Preset notes"));
  const box = el("div", { class: "notes" });
  for (const n of notes) box.append(el("div", { class: "note" }, n));
  parent.append(box);
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
    ? `<rect x="${(x(i) - 1.5).toFixed(1)}" y="${H - 14}" width="3" height="8" fill="#f85149"/>` : "").join("");
  const wrap = el("div", { class: "spark" });
  wrap.innerHTML =
    `<svg viewBox="0 0 ${W} ${H}" width="100%" style="height:auto;display:block" font-size="9">` +
    `<polyline points="${line}" fill="none" stroke="#58a6ff" stroke-width="1.6"/>${marks}` +
    `<text x="${pad}" y="${H - 3}" fill="#7d8697">0s</text>` +
    `<text x="${W - pad}" y="${H - 3}" fill="#7d8697" text-anchor="end">${Math.round(tl.span_s || 0)}s</text>` +
    `<text x="${pad}" y="9" fill="#7d8697">peak p95 ${Math.round(max)}ms</text></svg>`;
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
    $("#pat-title").textContent = `API token: ${name}`;
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
  if (rep.repros) {
    body.append(el("div", { class: "kv" },
      `repros: ${rep.repros.total} total, ${rep.repros.running} running`));
  }
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
  return j.status === "running" ? `${dur} and counting` : dur;
}
async function openJobs() {
  let rows;
  try { rows = (await api("/api/jobs")).jobs; }
  catch (e) { toast(e.message); return; }
  const box = $("#jobs-list");
  box.innerHTML = "";
  if (!rows.length) box.append(el("div", { class: "empty" }, "No jobs yet."));
  for (const j of rows) {
    box.append(el("button", {
      class: "jobrow", type: "button",
      onclick: () => { $("#jobs-dialog").close(); reopenJob(j); },
    },
      el("span", { class: "jstatus " + j.status }, j.status),
      el("span", { class: "jkind" }, jobTitle(j)),
      el("span", { class: "jage" }, jobAge(j))));
  }
  $("#jobs-dialog").showModal();
}
// since=0, so the retained events replay from the start; the terminal event is
// among them, which is what re-renders a finished job's result table.
function reopenJob(j) { streamJob(j.id, jobTitle(j), JOB_RESULT_RENDERER[j.kind]); }

// ---- create dialog ----------------------------------------------------------
let PRESETS = [];
async function openCreate() {
  try { PRESETS = (await api("/api/presets")).presets; } catch (e) { toast(e.message); return; }
  const sel = $("#preset-select");
  sel.innerHTML = "";
  for (const p of PRESETS) sel.append(el("option", { value: p.name }, p.name + (p.requires_license ? " (license)" : "")));
  sel.value = "default";
  renderPresetParams();
  $("#create-form").existing.value = "reuse";
  syncCreateSeed();
  $("#version-hint").textContent = "";
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
// One <select> drives which fields are relevant, because the three modes need
// completely different inputs and showing all of them at once invites the
// contradictions the API then has to reject (--tls-cert AND --acme-email, etc).
const HTTPS_MODE_HINT = {
  "": "",
  local: "A certificate signed by rc-repro's own CA. Works offline, no domain, no rate"
       + " limits. Browsers warn until `rc-repro trust-ca` has been run once. A phone"
       + " cannot use it — use Let's Encrypt for mobile.",
  acme: "Traefik obtains a real, publicly trusted certificate. Needs a domain you"
      + " control. Getting a certificate and being reachable at the name are separate"
      + " things — the job log says so if the name has no DNS record or the workspace"
      + " is bound to loopback.",
  own: "Paths are read on the machine running rc-repro, not uploaded from the browser.",
};

function syncHttpsFields() {
  const mode = $("#https-mode").value;
  $("#https-mode-hint").textContent = HTTPS_MODE_HINT[mode] || "";
  const show = {
    "https-local": mode === "local",
    "https-acme": mode === "acme",
    "https-own": mode === "own",
    // Both Let's Encrypt and a supplied certificate are tied to a hostname.
    "https-domain": mode === "acme" || mode === "own",
  };
  for (const [cls, on] of Object.entries(show)) {
    for (const el of document.querySelectorAll("." + cls)) el.hidden = !on;
  }
}

function applyHttpsToRequest(f, req) {
  const mode = f.https_mode ? f.https_mode.value : "";
  if (!mode) return true;
  req.https = true;
  const val = (k) => (f[k] && f[k].value.trim()) || "";
  if (mode === "local") {
    if (val("tls_san")) req.tls_san = val("tls_san");
    return true;
  }
  if (!val("domain")) { toast("HTTPS: a domain is required for this mode"); return false; }
  req.domain = val("domain");
  if (mode === "own") {
    if (!val("tls_cert") || !val("tls_key")) {
      toast("HTTPS: both the certificate and the key path are required"); return false;
    }
    req.tls_cert = val("tls_cert");
    req.tls_key = val("tls_key");
    return true;
  }
  // acme. Email may be blank when it is remembered in config; the challenge, the
  // DNS provider and whether a public bind is needed are all derived server-side,
  // so the form does not ask for them.
  if (val("acme_email")) req.acme_email = val("acme_email");
  req.acme_staging = !!(f.acme_staging && f.acme_staging.checked);
  return true;
}

async function submitCreate() {
  const f = $("#create-form");
  const req = {
    version: f.version.value.trim(),
    preset: f.preset.value,
    port: f.port.value ? parseInt(f.port.value, 10) : 0,
    monitor: f.monitor.checked, seed: f.seed.checked, wait: f.wait.checked,
    seed_profile: f.seed_profile.value,
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

// ---- wiring -----------------------------------------------------------------
$("#btn-new").addEventListener("click", openCreate);
$("#btn-refresh").addEventListener("click", loadRepros);
$("#btn-prune").addEventListener("click", doPrune);
$("#filter").addEventListener("input", (e) => { view.filter = e.target.value.trim().toLowerCase(); render(); });
$("#status-filter").addEventListener("change", (e) => { view.status = e.target.value; render(); });
$("#sort-by").addEventListener("change", (e) => { view.sort = e.target.value; render(); });
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
$("#import-cancel").addEventListener("click", () => $("#import-dialog").close());
$("#import-preview").addEventListener("click", (e) => { e.preventDefault(); previewImport(); });
$("#import-apply").addEventListener("click", applyImport);
$("#perf-cancel").addEventListener("click", () => $("#perf-dialog").close());
$("#perf-submit").addEventListener("click", submitPerf);
$("#btn-bench").addEventListener("click", openBench);
$("#btn-jobs").addEventListener("click", openJobs);
$("#jobs-close").addEventListener("click", () => $("#jobs-dialog").close());
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
