# rc-repro

**Reproduce a customer's Rocket.Chat issue on their *exact* version in minutes.**
One command spins up the right Rocket.Chat version paired with a compatible
MongoDB, plus optional backing services (LDAP, SAML/OIDC, email, S3, multi-instance)
and sample data — instead of hand-building a compose stack for every ticket.

```bash
rc-repro up --version 8.5.1 --name TICKET-1234 --wait   # boot it
# → open the printed URL, log in as admin / admin123
rc-repro down --name TICKET-1234 --volumes              # bin it when done
```

## Contents

- [Getting started](#getting-started) — prerequisites, install, your first repro
- [Everyday use](#everyday-use) — commands & lifecycle
- [Scenarios](#scenarios) — presets (LDAP, SAML, email, …) & monitoring
- [Data & performance](#data--performance) — sample data, benchmarking, load testing
- [API testing](#api-testing)
- [Reference](#reference) — command list, version resolution, state, development

---

# Getting started

## Prerequisites

- **Docker Desktop** (or Docker Engine) with `docker compose` v2 — **must be running**.
- **Python 3.11+**.
- Internet access (to pull images and look up version compatibility).

> **Apple Silicon note:** only the Bitnami MongoDB image (used for MongoDB < 8,
> i.e. RC < 8) is amd64-only and runs under emulation, so those boots are slower.
> Everything else (Rocket.Chat, official MongoDB 8+, OpenLDAP, Keycloak) is native.

## Install

**Recommended — with [pipx](https://pipx.pypa.io)** (isolated, adds `rc-repro` to your PATH):

```bash
pipx install git+https://github.com/klovekesh37/rc-repro
```

**Alternative — a plain virtualenv:**

```bash
git clone https://github.com/klovekesh37/rc-repro.git && cd rc-repro
python3 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip     # editable installs need pip >= 21.3
pip install -e .
```

Then confirm your machine is ready — `doctor` checks Docker, Compose, disk,
connectivity and ports. Fix any ✗ before continuing:

```bash
rc-repro doctor
```

<details>
<summary><b>Updating to the latest version</b></summary>

```bash
pipx reinstall rc-repro          # always re-pulls the latest from GitHub
# or, once a new release version is published:
pipx upgrade rc-repro
```

Use **`reinstall`** to be sure — `pipx upgrade` only picks up changes when the
package version was bumped. (Venv installs: `git pull && pip install -e .`.)
</details>

## Your first repro

```bash
# 1. Create a repro on a specific version and wait until it's actually serving
rc-repro up --version 8.5.1 --name test --wait

# 2. Open the printed URL (http://localhost:<port>) and log in:
#      username: admin    password: admin123

# 3. When you're done, remove it
rc-repro down --name test --volumes
```

That's the whole loop: **`up` → use it → `down`**.

- `--wait` blocks until Rocket.Chat responds (first boot pulls images and can take a few minutes), and skips the setup wizard so you land straight in.
- `--name` is optional (a name is derived from the version); use a **ticket id** so `list` maps repros to your work.
- Every repro auto-creates the same admin: **`admin` / `admin123`**.

> **Local-only by default:** all published ports bind to `127.0.0.1`, so repros
> (which run these well-known credentials) aren't reachable from your network.
> MongoDB and NATS are never published at all (stricter than the official compose).
> ⚠ `--bind 0.0.0.0` exposes RC **and every sidecar** (Keycloak, Mailpit, MinIO —
> all with known credentials) to your whole network. Treat it as dangerous:
> trusted networks only, take the repro down when done. rc-repro is a local
> reproduction tool — never a production or internet-facing deployment.

---

# Everyday use

```bash
rc-repro list                     # all repros: version, port, state, URL
rc-repro info   --name test       # URL, admin creds, handy snippets
rc-repro logs   --name test -f    # tail logs (attach to a ticket)
rc-repro ready  --name test       # block until it's serving (if you didn't use --wait)
```

**Lifecycle** — a repro is a long-lived thing you pause/resume, not something you recreate:

| Command | Effect |
|---|---|
| `up` | create; if it already exists, bring it back up (data intact) |
| `stop` / `start` | pause / resume — containers kept, resumes in seconds |
| `down` | remove containers, **keep** data + record (shows as `down`) |
| `down --volumes` | delete the data and forget the repro (asks to confirm) |
| `prune` | remove all `down` repros (skips pinned & running; asks to confirm) |

> The two data-deleting commands (`down --volumes`, `prune`) prompt for
> confirmation — `prune` lists exactly what it will delete first. Pass `--yes`
> (`-y`) to skip the prompt in scripts/CI.

**Persistent "daily driver"** — pin one and just start/stop it:

```bash
rc-repro up --version 8.5.1 --name main --pin    # once
rc-repro start        # each morning (acts on the pinned default)
rc-repro stop         # each evening — nothing lost
```

Once a repro is pinned (or set with `rc-repro use <name>`), commands with no
`--name` act on it: `rc-repro start`, `rc-repro logs -f`, etc.

---

# Scenarios

## Presets

A preset turns a bare RC into a scenario. See them all with `rc-repro presets`;
launch one with `--preset`:

```bash
rc-repro up --version 8.5.1 --preset ldap
```

| Preset | Brings up | Reproduces |
|--------|-----------|------------|
| `default` | RC + Mongo, admin auto-created | anything |
| `airgapped` | RC with Cloud endpoints stubbed out | offline / disconnected-workspace behaviour |
| `ldap` | OpenLDAP seeded with users + a group | LDAP auth / sync tickets |
| `saml` | a real Keycloak IdP (SAML realm + users) | SAML SSO login |
| `oidc` | a real Keycloak IdP (OpenID Connect + users) | OIDC / OAuth SSO login |
| `email` | Mailpit mailcatcher wired to RC's SMTP | email flows: invites, password reset, verification, 2FA codes |
| `s3_minio` | MinIO object storage as RC's file upload backend | S3 storage tickets: uploads, previews, presigned URLs |
| `livechat` | Omnichannel + an available agent + a demo website embedding the widget | Livechat widget load / CORS / routing / agent-availability |
| `multi-instance` | N RC instances + Traefik load balancer + NATS, one shared Mongo | horizontal scaling / cross-instance real-time |

For `ldap`, `saml` and `oidc`, log in as **`user1` / `user1`** (…`userN` / `userN`).

**Common `--set` parameters:**

```bash
rc-repro up --version 8.5.1 --preset ldap --set users=5        # 5 LDAP users
rc-repro up --version 8.5.1 --preset ldap --set users=130000   # scale/perf repro
rc-repro up --version 8.5.1 --preset saml --set users=20       # 20 Keycloak users
rc-repro up --version 8.5.1 --preset multi-instance --set instances=3   # 3 instances behind a load balancer
rc-repro up --version 8.5.1 --preset email --seed --wait                # Mailpit + verified sample users
rc-repro up --version 8.5.1 --preset email --set verification=true      # require signup email verification
rc-repro up --version 8.5.1 --preset s3_minio                           # files stored in MinIO instead of GridFS
rc-repro up --version 8.5.1 --preset s3_minio --set presigned=true      # real presigned URLs (needs hosts entry)
rc-repro up --version 8.5.1 --preset livechat --wait                    # Omnichannel + widget on a demo site
```

Per-preset details (credentials, URLs, gotchas) are below — expand the one you need:

<details>
<summary><b><code>email</code></b> — Mailpit mailcatcher + email-2FA</summary>

Captures every email RC sends (nothing leaves your machine) in Mailpit at
`http://localhost:8025` — one **catch-all inbox for all users**; check the To:
column. Covers invites, password resets, verification and notification mail out
of the box. **Email-2FA is enabled globally**, but RC only applies it to users
with a *verified* email: seeded users are verified, so `alice` / `alice` gets the
full OTP flow (code lands in Mailpit); `admin` isn't, so it logs in plain until
you verify it (Admin → Users → admin → Verified). rc-repro's own
`token`/`api`/`seed` calls fetch a required code from Mailpit automatically, so
they keep working either way. `--set verification=true` also makes new signups
verify their address first.
</details>

<details>
<summary><b><code>livechat</code></b> — Omnichannel + widget on a demo site</summary>

Enables Omnichannel, makes `admin` an available agent, creates a **`support`
department** with the agent(s) assigned, and serves a demo "customer website" at
`http://localhost:8090` that embeds the Livechat widget **cross-origin** (the real
setup where widget-load/CORS/CSP tickets happen). The widget frames RC, so the
preset also drops RC's `X-Frame-Options: sameorigin`
(`Iframe_Restrict_Access=false`) — otherwise the browser refuses to display it.
Open the site, start a chat as a visitor, answer it in RC's Omnichannel area (the
built-in same-origin widget is also at `<repro-url>/livechat`).

`--set agents=N` (all assigned to the department), `--set department=false`,
`--set registration=true`. **Business hours and canned responses are Enterprise
features** — pass `--reg-token` to set them up, otherwise they're skipped.
</details>

<details>
<summary><b><code>s3_minio</code></b> — MinIO object storage backend</summary>

Stores RC's file uploads in a MinIO bucket (auto-created) instead of GridFS —
browse it at `http://localhost:9001` (`rcrepro` / `rcrepro-secret`). By default
downloads are proxied through RC so everything works with zero setup.
`--set presigned=true` switches to real presigned MinIO URLs — add
`127.0.0.1  minio` to `/etc/hosts` (printed on `up`); removing that line
reproduces the classic "uploads work but previews/downloads break" ticket.
</details>

<details>
<summary><b><code>multi-instance</code></b> — N instances behind Traefik + NATS</summary>

Runs N Rocket.Chat instances behind Traefik on one URL, sharing one MongoDB and
coordinating over NATS. Confirm the mesh with
`rc-repro api --name <name> GET /api/v1/instances.get` (lists every connected
instance). Traefik load-balances via its file provider — a generated
`traefik/dynamic.yml` listing the `rocketchat-1..N` backends (no Docker socket
needed).
</details>

<details>
<summary><b><code>saml</code> / <code>oidc</code></b> — Keycloak console & host entry</summary>

**`oidc` needs one host entry.** OIDC's login URL must resolve the same way for
your browser and RC's backend, so add `127.0.0.1  keycloak` to `/etc/hosts`
(rc-repro prints this on `up`). Then log in via "Keycloak (OIDC)". The OIDC
Keycloak console is at `http://localhost:8085` (`admin`/`admin`, realm `rcrepro`).

**Keycloak console** (`saml` preset): `http://localhost:8081` (`admin`/`admin`).
The console opens on the **`master`** realm, but your SAML users live in the
**`rcrepro`** realm — switch the realm dropdown (top-left), or open Users directly:
`http://localhost:8081/admin/master/console/#/rcrepro/users`.
(`rc-repro info` prints this too.)
</details>

<details>
<summary><b>Custom / team presets & Enterprise licenses</b></summary>

**Custom / team presets** — drop a YAML file in `~/.rc-repro/presets/<name>.yaml`
(overrides a built-in of the same name). **Treat preset files as code**: they can
run arbitrary containers and mount files — only use presets you trust.

```yaml
name: my-scenario
description: What this reproduces.
env:                       # merged into the rocketchat service
  OVERWRITE_SETTING_Some_Setting: "true"
services:                  # optional extra compose services
  my-sidecar: { image: some/image:tag }
depends_on: [my-sidecar]
```

**Enterprise (EE) license** — pass a cloud **registration token**: the workspace
self-registers on first boot and Rocket.Chat Cloud syncs its license down
(needs internet; get the token from the cloud console):

```bash
rc-repro up --version 8.5.1 --reg-token <your-token> --wait
```

To avoid retyping it, put `reg_token: <your-token>` in `~/.rc-repro/config.yaml`
or export `RC_REPRO_REG_TOKEN` — every new repro then registers automatically.
</details>

## Monitoring (`--monitor`)

Add **Prometheus + Grafana** with Rocket.Chat metrics on top of *any* repro — it's
an add-on, not a preset, so it layers onto whatever preset you chose (including
`multi-instance`, where Prometheus scrapes every instance):

```bash
rc-repro up --version 8.5.1 --monitor                                   # any repro + monitoring
rc-repro up --version 8.5.1 --preset multi-instance --set instances=3 --monitor
```

- **Grafana**: `http://localhost:5050` (`admin`/`admin`, anonymous view enabled) —
  the official **"Rocket.Chat Metrics"** dashboard is auto-provisioned.
- **Prometheus**: `http://localhost:9090` (Status → Targets shows RC up).

Attach or detach on an **already-running** repro (RC is not restarted — metrics are
enabled live via the API):

```bash
rc-repro monitor --name test            # attach Prometheus + Grafana
rc-repro monitor --name test --off      # detach them
```

Config mirrors the official [`RocketChat/rocketchat-compose`](https://github.com/RocketChat/rocketchat-compose)
monitoring stack (file-SD Prometheus + provisioned Grafana).

---

# Data & performance

## Sample data (`--seed`)

A fresh repro is empty. Tickets about message sync, search, notifications, UI
with data, or permissions need a populated workspace — `--seed` creates realistic
users, channels, DMs and messages (authored across the users, not just admin).

```bash
rc-repro up --version 8.5.1 --name test --seed                 # small (default)
rc-repro up --version 8.5.1 --name test --seed --seed-profile standard
rc-repro seed --name test --profile large                      # seed an existing repro
rc-repro seed --name test --users 30 --channels 10 --messages 40   # custom counts
```

| Profile | Users | Channels | Msgs/channel | DMs | Threads/reactions |
|---------|-------|----------|--------------|-----|-------------------|
| `small` (default) | 5 | 3 | 5 | 2 | no |
| `standard` | 20 | 8 | 20 | 5 | yes |
| `large` | 100 | 20 | 100 | 20 | yes |

`seed` reports a **timing breakdown** (time + rate per phase, message-latency
p50/p95/p99); add `--stats` for the CPU/RAM cost. `rc-repro api` prints each
call's latency (`HTTP 200 [admin] in 11ms`).

Seed users are `alice`, `bob`, … (password = username). While seeding, email-2FA
and the API rate limiter are temporarily disabled so it can log in as each user
and post at volume — both are **restored to their prior values afterward** (so
seeding an `email` repro leaves its 2FA setting on). For huge *user* counts use
the `ldap` preset instead.

## Version comparison (`benchmark`)

Boot several versions, run the **identical** seed workload against each, and
compare — a performance-regression check unique to rc-repro because it's
version-matched:

```bash
rc-repro benchmark --versions 8.4.1,8.5.1,8.6.0 --seed-profile standard --report
```
```
VERSION  MONGO           BOOT   SEED  msg/s  p95    RC CPU  MongoCPU  RC RAM
8.4.1    8.0 (official)  10.2s  5.6s  19.6   121ms  62%     31%       1400MB
8.5.1    8.0 (official)  10.8s  5.8s  19.0   118ms  64%     33%       1450MB
8.6.0    8.0 (official)  11.1s  9.2s  11.0   340ms  91%     48%       1600MB   <- regression: seed +59%, p95 +188%
```

Runs are sequential on the same host; the deltas between versions are the signal.
`--report` writes a shareable markdown table for a ticket
(default `~/.rc-repro/reports/`, or `--report-path`).

## Load testing (`loadtest`)

Drive real concurrent HTTP load with [k6] and gate the result against an SLO:

```bash
rc-repro loadtest --name test --scenario messages --vus 50 --duration 60s
rc-repro loadtest --name test --scenario mixed --ramp 10:200 --duration 2m --stats --report
rc-repro loadtest --name test --scenario messages --slo p95=300ms,error=1%,rps=100   # CI gate

# Hit the customer's *actual* slow call under load:
rc-repro loadtest --name test --scenario custom --endpoint "GET /api/v1/channels.list?count=100" --vus 20
rc-repro loadtest --name test --scenario custom --endpoint "POST /api/v1/chat.postMessage" --body '{"channel":"#general","text":"hi"}'
```
```
+- loadtest custom GET /api/v1/channels.list?count=100 (20 VUs / 30s) -+
|  throughput  42.0 req/s   (1260 requests)                            |
|  latency     p50 340ms  p90 720ms  p95 980ms  p99 1400ms             |
|  errors      8.10%   checks 92% ok                                   |
|  responses   2xx 1158 · 429 61 · 5xx 41                              |
+---------------------------------------------------------------------+
  ✓ p95 <= 1000ms  (actual 980ms)    SLO gate: PASS
```

**Scenarios:** `messages` (write path), `login` (auth), `read`
(`channels.history`), `mixed` (60% read / 30% post / 10% login), and **`custom`**
— any endpoint you name with `--endpoint "METHOD /path"` (`--body` for
POST/PUT/PATCH), so you can reproduce the exact call a customer reports as slow.

The **responses** line breaks failures down by class — `429` (rate-limited), `4xx`
(client error), `5xx` (server error) — so you can tell *"slow **and** crashing"*
from *"just being throttled"* at a glance.

> **What "VUs" are:** virtual users are k6's concurrent workers — **not** Rocket.Chat
> accounts. All VUs authenticate as the one admin token loadtest mints; none are
> created or deleted. Note the *write* scenarios (`messages`, `mixed`, a custom
> `POST`) leave real messages in `#general`; `read`/`login`/custom `GET` add nothing.

k6 runs as a throwaway container **on the repro's own docker network**, so it hits
the internal service address (works even with loopback-only binds and
multi-instance repros, which it targets through Traefik). Load is offered via a
bypass-2FA PAT — exactly how a customer script hits the API — and the rate limiter
is disabled for the run, then restored. With `--slo`, the command **exits
non-zero** if any rule fails, so it drops straight into CI. `--stats` adds the
CPU/RAM cost; `--report` writes a shareable markdown report.

[k6]: https://k6.io

---

# API testing

Auth is set up so you can hit the REST API immediately:

```bash
rc-repro token --name test                     # prints -H auth headers
rc-repro api   --name test GET /api/v1/me
rc-repro api   --name test POST /api/v1/users.create -d '{"name":"Bob","username":"bob","email":"b@x.com","password":"p"}'

# Mirror a customer's Personal Access Token (with "Ignore 2FA"):
rc-repro api --name test --pat  POST /api/v1/users.update -d '{"userId":"ID","data":{"name":"X"}}'
# Get past a 2FA-guarded admin endpoint:
rc-repro api --name test --2fa  POST /api/v1/settings/<id> -d '{"value":true}'
```

---

# Reference

## Command list

| Command | Purpose |
|---|---|
| `up` | create & start a version-matched repro |
| `ready` | block until RC is serving (`/api/info`) |
| `start` / `stop` / `restart` | lifecycle without recreating |
| `down` | remove containers (`--volumes` also deletes data + record; confirms first, `--yes` to skip) |
| `use <name>` | set the default repro for name-less commands |
| `list` | all repros: version, port, state, URL |
| `info` | URL, admin creds, snippets, preset notes |
| `token` / `api` / `pat` | REST auth + calls |
| `seed` | populate a repro with sample users/channels/messages (`--stats` for CPU/RAM cost) |
| `stats` | sample a repro's container CPU/RAM (`--for N`, or `--watch` live) |
| `benchmark` | boot several versions, run identical seed workload, compare (regression check) |
| `loadtest` | drive concurrent HTTP load with k6, check against an SLO (`--slo`, non-zero exit on breach) |
| `monitor` | attach/detach Prometheus + Grafana on a running repro |
| `logs` | tail a repro's logs |
| `presets` | list available presets |
| `versions <X.Y.Z>` | show the resolved MongoDB pairing (without launching) |
| `doctor` | preflight checks (Docker, Compose, disk, ports, connectivity) |
| `prune` | delete all `down` repros (confirms first, `--yes` to skip) |

Run `rc-repro <command> --help` for flags.

## How version → MongoDB resolution works

`rc-repro up --version X` (and `rc-repro versions X`) resolves the MongoDB pairing:

1. **Live:** queries `releases.rocket.chat/<version>/info` (Rocket.Chat's own
   per-release compatibility data) and picks the highest supported MongoDB.
2. **Fallback:** if offline or the release predates that data, uses the shipped
   `rc_repro/data/versions.yaml` map (`--offline` forces this path).

The MongoDB image is chosen by the resolved **Mongo** version: **Mongo ≥ 8** →
`mongodb/mongodb-community-server` + a fix-permission and a one-shot init
container (matching the official `RocketChat/rocketchat-compose`); **Mongo < 8** →
`bitnamilegacy/mongodb` (auto-inits the replica set). `MONGO_OPLOG_URL` is emitted
only for RC < 8 (deprecated in 8.x).

## Where state lives

```
~/.rc-repro/                  # override with RC_REPRO_HOME
├── config.yaml               # default_repro, optional reg_token / rc_image
├── presets/                  # your custom/team presets
├── reports/                  # benchmark & loadtest markdown reports
└── repros/<name>/
    ├── docker-compose.yml     # generated — don't hand-edit; re-run `up`
    ├── repro.json             # metadata
    └── …                      # preset-generated files (LDIF, realm JSON, …)
```

Config values can also come from the environment (env wins over `config.yaml`) —
handy for CI/scripts: `RC_REPRO_HOME`, `RC_REPRO_REG_TOKEN`, `RC_REPRO_RC_IMAGE`,
`RC_REPRO_BIND_HOST` (default `127.0.0.1`; the `--bind` flag wins over both).

## Development

```bash
git clone https://github.com/klovekesh37/rc-repro.git && cd rc-repro
python -m pip install --upgrade pip
pip install -e ".[dev]"
pytest                         # pure-logic tests — no Docker needed
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the project layout and how to add a
preset.
