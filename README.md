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

Prefer a UI? `rc-repro serve` opens a local web dashboard for everything below
(create, seed, config-import, load-test, monitoring). See [Web GUI](#web-gui).

## Contents

- [Getting started](#getting-started) — prerequisites, install, your first repro
- [Everyday use](#everyday-use) — commands & lifecycle
- [Web GUI](#web-gui) — `rc-repro serve`, a local dashboard
- [Scenarios](#scenarios) — presets (LDAP, SAML, email, …) & monitoring
- [HTTPS](#https) — a domain and an email, or a local certificate
- [Environment variables](#environment-variables) — `rc-repro env`, change settings on a running repro
- [Data & performance](#data--performance) — sample data, data-scale prefill, config import, backup/restore, upgrade testing, benchmarking, load testing

Then, when you need them:

- [Shared server](#shared-server) — accounts, who did what, one Traefik for every HTTPS name
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

> **Podman / non-Docker engines:** rc-repro talks to any Docker-compatible API, so
> Podman works via the `docker.sock` helper (`podman-mac-helper`). Two known traps:
> - **Kernel ≥ 6.19 can't run MongoDB 8.0** ([SERVER-121912](https://jira.mongodb.org/browse/SERVER-121912)),
>   which recent RC versions require — common on fresh Podman machines / Fedora
>   CoreOS. `rc-repro doctor` warns when it detects this; use an engine on kernel
>   < 6.19 until MongoDB ships a fix.
> - **Docker Hub anonymous pull-rate limits** (`registry.rocket.chat` counts against
>   Hub too) — run `docker login` (Hub username + a Personal Access Token).
>
> When a boot fails from either cause, `up` now names it directly instead of a bare
> "`docker compose up` failed".

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

> **Web GUI (optional):** the `rc-repro serve` dashboard needs a couple of extra
> deps (the `gui` extra). With pipx use the PEP 508 `name[extra] @ URL` form:
>
> ```bash
> pipx install 'rc-repro[gui] @ git+https://github.com/klovekesh37/rc-repro'
> # already installed? add the extra by reinstalling:
> pipx install --force 'rc-repro[gui] @ git+https://github.com/klovekesh37/rc-repro'
> ```
>
> In a venv: `pip install -e '.[gui]'`. The core CLI stays dependency-light without it.

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

# Web GUI

`rc-repro serve` — a local, browser-based dashboard over the same engine as the CLI — useful when
you'd rather click than type. Needs the `gui` extra (see [Install](#install)).

```bash
rc-repro serve            # prints a http://localhost:7070/?t=... URL and opens it
rc-repro serve --port 8080 --no-open
```

It binds **loopback only** by default and prints a one-time session token in the
URL (repros run weak fixed credentials, so the control plane must not be exposed
to your network — `--bind 0.0.0.0` requires an explicit opt-in and warns).

Sharing one `serve` with a team? Add accounts and it asks for a login instead of
handing everyone the same token, records who ran what, and can serve itself and
every workspace over HTTPS on one port — see [Shared server](#shared-server).

What you can do from it:

- **Dashboard** — every repro as a card (version, port, state, uptime/health),
  with filter / status / sort. Click a card for a **detail panel**: Overview
  (RC/Mongo/port/uptime/health), **Logs / Containers / Env-vars** tabs, clickable
  **links** to RC and preset sidecars (MinIO, Keycloak, Mailpit, Grafana), a live
  **CPU/Mem chart**, and a copyable local URL.
- **Create** a repro (with an Advanced section for `--reg-token`, `--mongo`,
  `--rc-image`, `--bind`, pin/offline/no-pull, and an **HTTPS** section — see
  [HTTPS](#https)), **seed** (profile / bulk `--scale` / clear),
  **config-import** (upload a support-dump `*-settings.json` → preview the plan →
  apply), attach/detach **monitoring**, and run the **perf** suite (load test with
  an embedded k6 Grafana dashboard, capacity, benchmark).
- **Per-repro actions** — bring a `down`ed repro back up, start/stop/restart,
  make it the default, mint an **API token** (PAT), an **API call** console
  (`rc-repro api` with the response pretty-printed), **Check TLS**, and a
  **doctor** preflight behind the Docker badge.
- **Activity** — long jobs keep running if you close the dialog, and are
  reachable again from the jobs list; a crash-looping repro is called out. With
  [accounts](#shared-server), each job also shows **who** ran it.

Long operations stream live progress in the browser. Everything the GUI does is
also a CLI command — same code underneath.

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
- **Logs → Loki**: an OpenTelemetry collector tails **this repro's** containers and
  ships their logs to Loki, queryable in Grafana (Explore → Loki, e.g.
  `{k8s_namespace_name="rcrepro-<name>"}`). The collector is scoped to the repro's
  compose project, so it never ingests your other repros.

Attach or detach on an **already-running** repro (RC is not restarted — metrics are
enabled live via the API):

```bash
rc-repro monitor --name test            # attach Prometheus + Grafana
rc-repro monitor --name test --off      # detach them
```

Config mirrors the official [`RocketChat/rocketchat-compose`](https://github.com/RocketChat/rocketchat-compose)
monitoring stack (file-SD Prometheus + provisioned Grafana).

---

# HTTPS

**Two ways, one command each.** Both are served by the shared
[edge](#https-for-the-whole-box-the-edge) — one Traefik for the machine, started
automatically — so you can run as many as you like at once.

```bash
# A real name, with a Let's Encrypt certificate
rc-repro up -v 8.5.1 --domain chat.example.com --email me@example.com

# No domain, no internet: rc-repro's own CA
rc-repro trust-ca                       # once
rc-repro up -v 8.5.1 --https            # https://<name>.rcrepro.localhost
```

The email is remembered, so later runs need only the domain:

```bash
rc-repro config set acme.email me@example.com     # once
rc-repro up -v 8.5.1 --domain chat.example.com
```

Rocket.Chat's `ROOT_URL` becomes the https URL — which is what makes OAuth/SAML
redirects, `Secure` cookies, mixed-content cases and the mobile app behave like a
customer's workspace.

`--domain` takes the same two inputs the [official Rocket.Chat
compose](https://docs.rocket.chat/docs/deploy-with-docker-docker-compose#3c-configure-domain-and-reverse-proxy)
calls `DOMAIN` and `LETSENCRYPT_EMAIL`, and `--https` is a flag rather than a
preset — `--preset` takes one value, so a `tls` preset could never combine with
`oidc`, `saml`, `livechat` or `multi-instance`, which are exactly the ones that
need HTTPS.

## Before it can work

**You provide** a domain that already resolves to this host, with **TCP/443
reachable from the internet**. An A record, a CNAME, a public IP, a port-forward —
however you arrange it is yours to manage.

**rc-repro provides** the certificate and serves on it. It does not check your DNS
or your routing: it cannot see a tunnel or a firewall from inside the process, and
guessing was the biggest source of confusion in this path.

Let's Encrypt validates by **connecting to your domain on 443**. So the routing has
to work *before* a certificate can be issued — and if something else on the path
terminates 443 (a proxying DNS provider like Cloudflare's orange cloud, or a
managed lab or PaaS front end), the challenge never reaches this host and Traefik
keeps serving its own self-signed certificate. `rc-repro tls-status --name X`
makes a real TLS connection and tells you what is actually being served.

**dns-01 is the way round all of that**, because it proves control with a TXT
record and needs no inbound access at all.

<details>
<summary><b>Using dns-01 (also what unlocks a wildcard certificate)</b></summary>

Not a flag — put your provider's credentials in `~/.rc-repro/acme/dns.env` and
rc-repro uses dns-01 automatically:

```bash
mkdir -p ~/.rc-repro/acme && chmod 700 ~/.rc-repro/acme
printf 'CF_DNS_API_TOKEN=%s\n' "$TOKEN" > ~/.rc-repro/acme/dns.env
chmod 600 ~/.rc-repro/acme/dns.env

rc-repro up -v 8.5.1 --domain chat.example.com --email me@example.com
```

The provider is inferred from the variable names — `CF_*` → cloudflare, `AWS_*` →
route53, `DO_AUTH_TOKEN` → digitalocean, `HETZNER_*`, `AZURE_*`, and so on. Any of
[lego's ~100 providers](https://go-acme.github.io/lego/dns/) works; name one
explicitly with `rc-repro config set acme.dns_provider <name>` if the variables do
not identify it.

Credentials are read from that file and mounted as an `env_file` — never passed on
a command line, where `ps` would show them to every user on the box. The values are
never printed.

The same token is what lets the edge obtain **one `*.example.com` certificate**
instead of one per workspace name — see
[Certificates](#certificates).

</details>

## Rehearse against staging first

Let's Encrypt allows **5 certificates per identical hostname per 7 days** and **5
failed validations per hostname per hour**, which is easy to burn while getting
DNS right:

```bash
rc-repro config set acme.staging true
```

A browser warning on a staging certificate **is** the success signal. Staging uses
separate storage, so switching back really re-issues. ACME state lives in
`~/.rc-repro/acme/` **outside** the workspace, so `down --volumes` can never force
a re-issue.

## Good to know

- **`up --wait` only proves Rocket.Chat booted.** The certificate is requested
  afterwards, in the background. `rc-repro tls-status --name X` is the check.
- **Internal calls still use http.** `rc-repro api`, seeding and the load tests
  talk to the workspace's plain port, so they need no CA. `rc-repro info` shows
  both URLs.
- **Workspaces stay bound to loopback**, including `--domain` ones. Only the edge
  is published, and it is the only thing that needs to reach them.
- **`--https` phones don't work.** Browsers trust the local CA after `trust-ca`;
  a phone has neither the CA nor a way to resolve `.localhost`.
- **⚠ Repros run fixed weak credentials (`admin`/`admin123`).** Do not leave a
  publicly reachable `--domain` workspace running: the hostname appears in public
  certificate-transparency logs within minutes of issuance.

---

# Environment variables

`rc-repro env` — Rocket.Chat is configured largely through environment variables, and a repro often
needs one the preset does not set — an `OVERWRITE_SETTING_*`, a feature flag, or
something a customer has in their own deployment.

```bash
rc-repro env                                        # list the effective env (secrets masked)
rc-repro env --setting Message_AllowEditing=false   # a Rocket.Chat SETTING
rc-repro env --set MY_FLAG=true                     # a raw env var
rc-repro env --unset ALLOW_UNSAFE_QUERY_AND_FIELDS_API_PARAMS
rc-repro up -v 8.6.1 --setting Log_Level=2 --env MY_FLAG=true   # or at creation
```

## Settings vs. env vars — `--setting` and the `OVERWRITE_SETTING_` prefix

Most Rocket.Chat configuration is a **setting** (what you would change in the admin
UI), not a plain env var. A setting only takes effect from the environment when it
carries the `OVERWRITE_SETTING_` prefix:

| Env var | Effect on the `Accounts_ShowFormLogin` setting |
|---|---|
| `Accounts_ShowFormLogin=false` | **none** — accepted by docker, silently ignored |
| `OVERWRITE_SETTING_Accounts_ShowFormLogin=false` | applied |

The bare form failing quietly is the trap, so:

- **`--setting Id=value` adds the prefix for you.** Prefer it for anything you would
  otherwise change in Admin → Settings.
- **`--set` is for real env vars** (`MONGO_URL`, `NODE_ENV`, a feature flag).
- If you use `--set` with a name that *is* a setting on this workspace, rc-repro
  says so and tells you the prefixed form. It asks the workspace which names are
  settings, so it is right for whatever version is running.

`OVERWRITE_SETTING_*` is **asserted at every boot, not locked**: the REST API and the
admin UI can still change the value while the container runs, and the next restart
puts the env value back.

The GUI's **Env-vars** tab does the same, and keeps the two kinds apart: the add row
has a **Rocket.Chat setting / Plain env var** selector, settings are listed by their
bare id with a `setting` tag (rather than a wall of `OVERWRITE_SETTING_*`), rows you
set are marked `*`, and each has a **remove**. The prefix is added server-side, so the
browser and the CLI cannot disagree about it.

## How it works, and what it costs

**An env var cannot be changed inside a running container** — the environment is
fixed when the container is created. So applying a change rewrites the compose file
and recreates the **Rocket.Chat** container only:

- **MongoDB keeps running and its volume is untouched** — no data is lost.
- No image pull, so it takes seconds.
- Rocket.Chat restarts, so open sessions reconnect.

Use `--no-restart` to write the change and apply it at the next `up`.

## Gotchas

- **Overrides persist.** They are stored in the repro's metadata, not just the
  generated compose file, so `up --force` keeps them. (Editing the compose file by
  hand does *not* survive a rebuild.)
- **`--unset` removes a key entirely**, including a preset or base default. Blanking
  one to `""` is not the same thing — Rocket.Chat treats an empty value as set.
- **Precedence:** base defaults → preset env → your overrides. Yours win.
- **All instances.** With `multi-instance`, the change is applied to every
  `rocketchat-N`.
- **Load-bearing keys** (`MONGO_URL`, `PORT`, `ROOT_URL`, `TRANSPORTER`) are allowed —
  reproducing a broken configuration is the point — but each prints a warning saying
  what it will break.
- Values are masked by key name when listed, the same as the Env-vars tab.

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
seeding an `email` repro leaves its 2FA setting on).

### Data-scale prefill (`--scale`)

The REST seed above is realistic but slow — a round-trip per message. For
*scale* repros (*"50k users"*, *"a room with 800k messages won't load"*) use
`--scale`, which bulk-inserts straight into MongoDB (tens of thousands/sec):

```bash
rc-repro seed --name test --scale users=50000                       # 50k users in seconds
rc-repro seed --name test --scale messages=800000@general           # fill a room
rc-repro seed --name test --scale users=50000,messages=800000@team-chat
rc-repro seed --name test --clear-scale                             # remove everything --scale added
```

Every scale document is tagged, so `--clear-scale` removes exactly what was
inserted (and fixes the room's message counter) without touching your REST-seeded
or real content. The target room (`messages=N@room`) must already exist — use
`general` or a REST-seeded channel.

**Trade-off:** bulk documents bypass RC's application hooks — bulk users are
**credential-less** (not loginable; log in as a REST-seeded user or `admin` to
browse them) and messages fire no notifications/mentions/threading. Use `--scale`
for scale/perf behaviour, the REST seed for feature behaviour.

## Config import (`config-import`)

Reproduce a *customer's* configuration, not a default one. Point `config-import`
at the `*-settings.json` from a support dump and it applies the settings the
customer actually changed:

```bash
rc-repro config-import ./support-dump/8.5.0-settings.json --name test
rc-repro config-import ./8.5.0-settings.json --name test --dry-run     # preview the plan
rc-repro config-import ./8.5.0-settings.json --name test --only Livechat,LDAP
```

It imports only settings that differ from their default, and deliberately
**skips**:

- **redacted secrets** (`XXXXXXXX` in the dump — SMTP/OAuth/token passwords); it
  lists them so you can set them by hand,
- **identity/environment settings** that would break or pollute a local repro —
  `Site_Url`, `Enterprise_License`, `Assets_*`, deployment fingerprint, cloud
  registration.

Custom OAuth providers (e.g. Entra ID) are **pre-created** automatically so their
settings apply. `--dry-run` prints the full apply/skip plan without changing
anything. Some settings need `rc-repro restart` to fully take effect; a few
Enterprise/module-gated settings will be reported as rejected on an unlicensed
repro (they apply once a license is present).

## Backup & restore (`backup`, `restore`)

A backup is a **bundle**, not a bare dump: the Rocket.Chat database plus the
version, preset and parameters that produced it. That is what lets a single file
rebuild a whole workspace — on your machine or a colleague's.

```bash
rc-repro backup --label "before upgrade"     # -> ~/.rc-repro/backups/<name>-<stamp>.rcbak
rc-repro backups                             # what you have, newest first
rc-repro restore <bundle>                    # in place — same repro
rc-repro restore <bundle> --new              # build a fresh workspace from it
rc-repro restore <bundle> --name other       # into a different existing repro
```

Built on `mongodump --archive` / `mongorestore --archive` — the procedure the
[official docs](https://docs.rocket.chat/docs/deploy-with-docker-docker-compose#back-up-and-restore-mongodb-data-on-docker)
describe — with some deliberate differences:

- **Rocket.Chat is stopped** for both operations (MongoDB keeps running).
  `mongodump` is not point-in-time consistent across collections, so a dump taken
  while RC writes can catch a half-finished state. `--live` skips this.
- **The target database is dropped first.** `mongorestore --drop` alone only drops
  the collections it is about to restore, so anything present in the target but
  absent from the bundle would survive and leave you with a hybrid.
- **`--db` and `--gzip`**, so `admin`/`local` never travel and bundles stay small.

Compatibility is checked before anything is touched: restoring **newer data into
an older workspace is refused** (Rocket.Chat does not migrate a database
backwards), and older-into-newer needs `--allow-upgrade` because RC will migrate
it on boot.

**Not included:** sidecar data — MinIO objects, Keycloak realms, LDAP entries,
Mailpit mail. Restoring an `s3_minio` bundle gives you a database that references
uploads whose objects are gone; the restore says so.

## Upgrade testing (`upgrade`)

Rocket.Chat runs its database migrations on boot, so "it broke after we upgraded"
is only reproducible with real data going to a real new version:

```bash
rc-repro upgrade --to 8.6.1 --dry-run    # what would happen
rc-repro upgrade --to 8.6.1              # backs up first, then upgrades
rc-repro upgrade --rollback              # undo, from that automatic backup
```

The repro has to be **running** — the pre-upgrade backup needs MongoDB up, and the
migrations only happen when Rocket.Chat boots. A stopped repro is refused with the
command to start it. The GUI applies the same rule: Upgrade only appears on a
running workspace.

A pre-upgrade backup is taken automatically (`--no-backup` opts out, and then
there is nothing to roll back to). If the upgrade fails, it is rolled back to the
previous version and data unless you pass `--no-rollback`.

**Refused rather than attempted:** an upgrade that also needs a new MongoDB
*major* (e.g. RC 8.2+ moving you to MongoDB 8.0). Majors have to be stepped one at
a time with a `featureCompatibilityVersion` bump between each; doing that in one
move is how data is lost, so rc-repro names the path and stops.

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
rc-repro loadtest --name test --scenario journey --vus 50 --duration 60s     # full user sessions
rc-repro loadtest --name test --scenario mixed --ramp 10:200 --duration 2m --stats --report
rc-repro loadtest --name test --scenario messages --slo p95=300ms,error=1%,rps=100   # CI gate

# Hit the customer's *actual* slow call under load:
rc-repro loadtest --name test --scenario custom --endpoint "GET /api/v1/channels.list?count=100" --vus 20
rc-repro loadtest --name test --scenario custom --endpoint "POST /api/v1/chat.postMessage" --body '{"channel":"#general","text":"hi"}'

# Before/after — did the fix/setting change actually help?
rc-repro loadtest --name test --scenario journey --save before-fix
rc-repro loadtest --name test --scenario journey --compare before-fix

# Customer-sized hardware — what does *their* 2-CPU/2GB box handle?
rc-repro loadtest --name test --scenario journey --constrain "rc=2cpu/2g,mongo=1cpu/1g" --compare before-fix
```
```
+- loadtest journey (50 VUs / 60s, 10 users) ---------+
|  throughput  229 req/s   (1850 requests)            |
|  latency     p50 7ms  p90 60ms  p95 66ms  p99 88ms  |
|  errors      0.00%   checks 100% ok                 |
|  responses   2xx 1850                               |
+-----------------------------------------------------+
Per-step latency:
  step      count       p50     p95     p99
  login       370      60ms    86ms   111ms
  open        370       6ms    17ms    26ms
  post        370      24ms    61ms    77ms
vs baseline 'before-fix' (journey, saved 2026-07-18):
  p95                   66ms -> 190ms       +188%   <- regression
  step post p95         61ms -> 180ms       +195%   <- regression
```

**Scenarios:** **`journey`** (a full user session per iteration — login → rooms →
open → post → sync, **each step timed** so you see *which one* is slow), `messages`
(write path), `login` (auth), `read` (`channels.history`), `mixed` (60/30/10 blend
with per-endpoint latency), **`webhook`** (an incoming-webhook storm — the
integration auto-created for the run), **`badbot`** (a badly-written script: tight
unpaginated polling), and **`custom`** — any endpoint you name with
`--endpoint "METHOD /path"` (`--body` for POST/PUT/PATCH).

**Load shapes:** constant `--vus`, `--ramp 10:200`, or **`--spike 10:100`** —
base load for a third, a sharp spike for a third, then a recovery window; the
result reports **how long p95 took to recover** after load dropped (or that it
didn't). Long runs with `--stats` also report the **RAM slope per hour** (the
soak-test leak signal).

**Watch it live:** with the monitoring add-on attached, `--live` streams k6's
metrics into the same Prometheus — client-side load and RC server metrics on
one Grafana timeline (`http://localhost:5050` → Explore → `k6_*`).

**Real users:** load is spread across seeded users (`alice`, `bob`, … — `--users N`,
default 10) so it carries real per-user identity, permissions and subscriptions;
if the repro isn't seeded it falls back to the admin token with a warning
(`--users 0` forces admin-only; `custom` always uses the admin PAT).

**Before/after:** `--save LABEL` stores a run (`~/.rc-repro/loadtests/`);
`--compare LABEL` diffs the current run against it — per-metric and per-step
deltas, regressions flagged. The report also embeds a **workspace snapshot**
(version, instances, users/rooms/messages in the DB) so results are comparable
evidence, and `--json` prints a machine-readable result for CI.

**Customer-sized hardware:** `--constrain "rc=2cpu/2g,mongo=1cpu/1g"` caps
services for the duration of the test (live `docker update`, no restart —
restored after), so results reflect the *customer's* box, not your laptop.
`rc` covers every RC instance, `mongo` is MongoDB, or name any compose service;
each takes a CPU count (`0.5cpu`) and/or memory cap (`512m`, `2g`). ⚠ A memory
cap below the service's current usage can OOM-kill it — which *is* how an
undersized box behaves, but expect errors in that run.

**Diagnosis (on by default; `--no-diag` to skip):** every run also collects the
*server side* of the story and ends with a plain-language **verdict**:

- **RC internals** — RC's own Prometheus metrics sampled during the run
  (enabled/restored automatically): **event-loop lag**, the Node saturation
  signal — once the loop lags, every request queues behind it.
- **Slow MongoDB queries** — Mongo's profiler is armed for the run (`--slowms`,
  default 100ms) and the slowest queries are read back with their plan, flagging
  **COLLSCAN** (missing index) — the single most useful line in a perf ticket.
- **Latency over time** — an ASCII p95 timeline that shows degradation and pins
  *when* errors started, not just how many.

```
Verdict:
  - RC event loop saturated: lag peaked at 2.29s on rocketchat - the Node process
      is the bottleneck; more CPU or more instances (multi-instance preset) will help.
  - MongoDB ran 12 collection scan(s) (COLLSCAN) among the profiled slow queries -
      likely a missing index.
```

## Capacity finder (`capacity`)

How many concurrent users does this workspace (or *this customer's hardware*)
actually sustain? `capacity` doubles VUs until your SLO breaks, bisects to the
boundary, and tells you why it broke:

```bash
rc-repro capacity --name test --scenario journey --slo "p95=300ms,error=2%"
rc-repro capacity --name test --constrain "rc=2cpu/2g" --report   # on their box
```
```
   4 VUs             112.8 req/s   p95   111ms   err  0.00%   PASS
   8 VUs             130.8 req/s   p95   166ms   err  0.00%   PASS
  16 VUs             137.5 req/s   p95   260ms   err  0.00%   PASS
  32 VUs             148.9 req/s   p95   547ms   err  0.00%   FAIL (p95 <= 300ms)
  24 VUs (bisect)    140.3 req/s   p95   432ms   err  0.00%   FAIL
  20 VUs (bisect)    138.8 req/s   p95   348ms   err  0.00%   FAIL

Capacity: ~16 concurrent VUs (holds at 16, breaks at 20)
  why it broke: at 20 VUs the RC event loop saturated (lag peaked at 815ms)
```

Tune the search with `--start` (first VU step, default 10), `--max` (stop
doubling past this, default 640), and `--step-duration` (how long each step
runs, default 20s); it also takes `--scenario`, `--users`, `--constrain`,
`--report` and `--json` like `loadtest`.

The **responses** line breaks failures down by class — `429` (rate-limited), `4xx`
(client error), `5xx` (server error) — so you can tell *"slow **and** crashing"*
from *"just being throttled"* at a glance.

> **What "VUs" are:** virtual users are k6's concurrent workers — **not** Rocket.Chat
> accounts (none are created or deleted). Write scenarios (`journey`, `messages`,
> `mixed`, a custom `POST`) leave real messages in `#general`; `read`/`login`/custom
> `GET` add nothing.

k6 runs as a throwaway container **on the repro's own docker network**, so it hits
the internal service address (works even with loopback-only binds and
multi-instance repros, which it targets through Traefik). The rate limiter is
disabled for the run, then restored. With `--slo`, the command **exits non-zero**
if any rule fails, so it drops straight into CI. `--stats` adds the CPU/RAM cost;
`--report` writes a shareable markdown report.

[k6]: https://k6.io

---

# Shared server

Everything above assumes one person on a laptop. Put `rc-repro serve` on a box the
team shares and three things break at once:

- the session token is **one secret handed to everybody**, and it changes on every
  restart, so a shared URL dies whenever the service does;
- nothing can answer **"who tore down TICKET-1234?"**;
- two people running `up -v 8.5.1` both get `rc8-5-1`, and **the second silently
  reuses the first one's data**.

Accounts fix all three. Everything here is opt-in: until someone runs `users add`,
rc-repro behaves exactly as it always has.

## Accounts

```bash
rc-repro users add alice        # GENERATES the password and shows it once
rc-repro users add alice --ask-password   # type one instead
rc-repro users list             # names and dates — never hashes
rc-repro users passwd alice     # generates a new one, ends alice's sessions
rc-repro users remove alice
```

The password is generated, not typed, for the same two reasons the GUI has always
minted them. An admin who types a colleague's password also knows it, which makes
every audit line signed with that name deniable. And a generated one is ~96 bits,
where a typed one clears the 12-character minimum and is otherwise whatever
somebody thought of. Either way it is never an *argument* — `ps` shows command
lines to every user on the machine.

Stored in `~/.rc-repro/users`, mode `0600`, hashed with `hashlib.scrypt` from the
standard library (no new dependency).

Once **any** account exists, `rc-repro serve` asks the browser to sign in. Ten
failed attempts from one address in sixty seconds and the login refuses to spend
any more CPU on that address; there is deliberately no per-*account* lockout,
because anyone who can reach the port could then lock out a named colleague, and
names are not secret here. Every failure is recorded instead:

```bash
rc-repro audit --kind signin     # who was guessed at, from where, and when
```

An unknown username performs exactly the same hash derivation as a known one, so
response time cannot be used to enumerate who exists.

> **Basic Auth sends the password on every request**, so `serve` refuses to do it
> over plain http on a network-reachable interface. Three ways forward, and it
> prints all three: give it a real name with `--domain` (below), keep it on
> loopback behind a TLS proxy on the same box, or — if TLS terminates upstream at
> a proxy, lab or load balancer — pass `--insecure`, which means "plain http is
> fine on this hop", not "no login".

## Who did what

Every job records the account that ran it — shown in the GUI's **Activity** list,
and appended to `~/.rc-repro/audit.log`:

```
2026-08-07T09:12:44+00:00<TAB>alice<TAB>up<TAB>alice-rc8-5-1
```

A file rather than only stdout, because on a shared box "who did this" has to
survive a restart. In token mode the actor is empty and the GUI hides the column
entirely — a shared secret genuinely cannot say who acted, and *"unknown"* would
be worse than nothing.

**The CLI's idea of who you are is not authenticated.** It uses your login name if
it matches an account, or `RC_REPRO_USER`. That is enough for attribution among
colleagues; it is not a security boundary, and the audit log mixes a verified GUI
identity with an asserted CLI one.

## Your own workspaces

With accounts, derived names are namespaced by owner, so two people can run the
same version:

```
$ rc-repro list
NAME                 OWNER        RC        MONGO   PORT   STATE      URL
*alice-rc8-5-1       alice        8.5.1     8.0     3000   running    …
 bob-rc8-5-1         bob          8.5.1     8.0     3001   running    …
```

The OWNER column appears only when a workspace has one, so single-user output —
and anything parsing it — is unchanged.

**Everyone can see and act on everything.** Support engineers hand tickets over
and cover for each other; hiding a colleague's workspace would make the tool worse
at its job. The guardrail is that destroying someone else's data names them first,
in the CLI prompt and the GUI confirm — and still says so under `--yes`, where
there is no prompt to read but there is a log to read afterwards.

## HTTPS for the whole box: the edge

**A workspace never terminates TLS.** One Traefik — the **edge** — holds `:80`
and `:443` and serves every name on the machine. Nothing to set up:

```bash
rc-repro up -v 8.5.1 --domain t1234.support.example.com --email ops@example.com
rc-repro up -v 8.6.1 --domain t5678.support.example.com     # and another, and another
```

The edge starts by itself with the first workspace that needs a name. There is no
`--edge` flag, no ordering to get right, and `--domain` means exactly what it
always did.

Add **one wildcard DNS record** (`*.support.example.com` → the box) and no
workspace ever needs a DNS record again.

**`--https` works the same way**, with rc-repro's own CA instead of Let's
Encrypt, so it needs no domain at all:

```bash
rc-repro trust-ca                                  # once
rc-repro up -v 8.5.1 --https --name TICKET-1234    # https://ticket-1234.rcrepro.localhost
```

Ten of those used to consume ten ports and give ten different URLs. Now every
name answers on 443.

### What that buys, and what it costs

- **HTTPS is a runtime property.** A workspace's compose file is identical
  whether it serves https or not, so gaining or losing a name writes or deletes a
  route file — **no container is ever rebuilt for it.** Workspaces from an older
  rc-repro are moved across the same way: their Traefik container is removed and
  the edge attaches to them live, with Rocket.Chat and MongoDB never stopping.
- **Workspaces still cannot reach each other.** Each keeps the private network
  compose gives it and the edge joins *those*, rather than everything sharing one.
- **The GUI is just another name.** `serve --domain support.example.com` adds a
  route for it; whether the GUI has a public name and whether a workspace can
  serve HTTPS are unrelated questions.
- **Shared fate**, and it is real: while the edge is down, every https name on the
  box is unreachable. It only routes, it restarts unless stopped, and `doctor`
  calls it a failure rather than a warning.

### Seeing it

It is **not a workspace** — never in `list`, and `prune`/`down` cannot touch it.
It answers for itself instead:

```bash
rc-repro edge status      # running? which names? can it reach each one?
rc-repro edge stop        # frees :80 and :443 for something else
rc-repro edge start
```

`status` prints each route beside whether the edge is actually attached to that
workspace, because a route it cannot reach answers **502** rather than erroring —
the one failure nothing else would tell you about.

### Certificates

Each name gets its own by default, against Let's Encrypt's limit of 50 per
registered domain per 7 days; rc-repro counts the names it caused and warns as you
approach it. Put a DNS API token in `~/.rc-repro/acme/dns.env` and the edge
obtains a single `*.support.example.com` instead, and no workspace ever costs a
certificate again. No flag — a wildcard can only be issued over dns-01, so the
token *is* the choice.

A wildcard covers exactly one label: `t1234.support.example.com` is covered,
`a.b.support.example.com` is not and gets its own.

> **Let's Encrypt has to reach this machine on :443.** Behind a platform that
> terminates 443 itself — some managed labs and PaaS front ends do — the challenge
> never arrives and Traefik keeps serving its own self-signed certificate.
> `rc-repro edge status` and `tls-status` show what is really being served. dns-01
> (the token above) is the way out, because it is validated by a DNS record rather
> than an inbound connection.

## Keeping it running

`serve` runs in the foreground. To keep it up:

```bash
rc-repro serve --domain support.example.com --email ops@example.com --print-service
```

That prints a **systemd unit** and the commands to install it, plus the
`nohup … &` fallback — and writes nothing, so you can read it before anything
runs. systemd is the real answer: it restarts on crash, starts on boot, and gives
you `systemctl status` and `journalctl -u rc-repro -f`. `nohup` survives logout
and **nothing else** — no restart, no reboot, no log rotation, no status.

Restart-on-crash is only useful *because* accounts exist. With the session token,
every restart minted a new one and killed every bookmark.

## What this is not

- **No roles.** Everyone who can sign in can do everything. The users file leaves
  room for a role column, so `readonly` can be added without a migration.
- **No MFA, and no clean logout** — browsers cache Basic credentials until the tab
  closes. Revoking is `users remove`. SSO in front is the upgrade if those matter.
- **Every workspace still runs `admin`/`admin123`.** The GUI login is the only
  boundary; the workspaces behind it are not individually protected.
- **Shared fate.** The edge going down takes every https name with it. It only
  routes, so it is small, it restarts unless stopped, and `doctor` calls it a
  failure rather than a warning.

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
| `config` | read/write remembered settings (`config set acme.email …`) |
| `env` | show or change a repro's Rocket.Chat env vars, recreating RC to apply (`--set`, `--unset`, `--no-restart`) |
| `list` | all repros: version, port, state, URL |
| `info` | URL, admin creds, snippets, preset notes |
| `token` / `api` / `pat` | REST auth + calls |
| `seed` | populate a repro with sample users/channels/messages (`--stats` for CPU/RAM cost; `--scale` for bulk Mongo data-scale prefill) |
| `config-import` | apply a customer's exported settings (support-dump `*-settings.json`) to a repro; `--dry-run`, `--only` |
| `backup` / `backups` | dump a repro's database into a restorable bundle; list the bundles you have |
| `restore` | load a bundle back — in place, into another repro, or `--new` to rebuild the whole workspace |
| `upgrade` | move a **running** repro to another RC version and let it migrate; `--rollback` undoes it |
| `stats` | sample a repro's container CPU/RAM (`--for N`, or `--watch` live) |
| `benchmark` | boot several versions, run identical seed workload, compare (regression check) |
| `loadtest` | drive concurrent HTTP load with k6 as real seeded users; per-step latency, SLO gate, `--save`/`--compare` baselines, `--spike`, `--live` |
| `capacity` | double VUs until the SLO breaks, bisect the boundary — "handles ~N concurrent" + why it broke |
| `monitor` | attach/detach Prometheus + Grafana on a running repro |
| `trust-ca` | install rc-repro's local CA so `--https` repros are trusted (`--uninstall`, `--show`) |
| `tls-status` | report the certificate a `--domain`/`--https` repro is actually serving — see [HTTPS](#https) |
| `users` | GUI accounts for a [shared server](#shared-server) (`list`/`add`/`passwd`/`remove`) |
| `serve` | launch the [web GUI](#web-gui) (needs `pip install 'rc-repro[gui]'`); `--domain`/`--email` give it a public name on the [edge](#https-for-the-whole-box-the-edge), `--print-service` shows how to keep it running |
| `edge` | the shared Traefik serving every HTTPS name (`status`/`start`/`stop`/`restart`) |
| `logs` | tail a repro's logs |
| `presets` | list available presets |
| `versions <X.Y.Z>` | show the resolved MongoDB pairing (without launching) |
| `doctor` | preflight checks (Docker, Compose, engine kernel, Hub auth, disk, ports, connectivity) |
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
├── loadtests/                # saved loadtest baselines (--save / --compare)
├── users                     # GUI accounts, 0600, scrypt — see Shared server
├── audit.log                 # who ran what: timestamp, actor, kind, target
├── acme/                     # ACME state; dns.env (DNS token), issued.json (cert tally)
├── edge/                     # the shared edge's compose project + one file per route
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
