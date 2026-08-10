# rc-repro

**Reproduce a customer's Rocket.Chat issue on their *exact* version, in minutes.**
One command boots the right Rocket.Chat paired with a compatible MongoDB, plus
optional backing services (LDAP, SAML/OIDC, email, S3, multi-instance) and sample
data — instead of hand-building a compose stack for every ticket.

```bash
rc-repro up --version 8.5.1 --name TICKET-1234 --wait   # boot it
# → open the printed URL, log in as admin / admin123
rc-repro down --name TICKET-1234 --volumes              # bin it when done
```

Prefer a browser? [`rc-repro serve`](#web-gui) does all of the above.

## Contents

- [Getting started](#getting-started) — prerequisites, install, first repro
- [Everyday use](#everyday-use) — the lifecycle
- [Web GUI](#web-gui) — `serve`, accounts, and reaching it from somewhere else
- [Scenarios](#scenarios) — presets and monitoring
- [HTTPS](#https) — a real name, or a local certificate
- [Environment variables](#environment-variables) — settings vs. env vars
- [Data & performance](#data--performance) — seed, import, backup, upgrade, load test
- [API testing](#api-testing)
- [Reference](#reference) — commands, version resolution, state, development

Every command has `--help`, and it is written to be read — this file is the map,
`--help` is the detail.

---

# Getting started

## Prerequisites

- **Docker** with `docker compose` v2 — running.
- **Python 3.11+**
- Internet access, to pull images and look up version compatibility.

`rc-repro doctor` checks all of it and names anything that would fail a boot.

<details>
<summary><b>Apple Silicon, Podman, and Docker Hub rate limits</b></summary>

**Apple Silicon:** only the Bitnami MongoDB image (MongoDB < 8, i.e. RC < 8) is
amd64-only and runs emulated, so those boots are slower. Everything else is native.

**Podman** works via the `docker.sock` helper (`podman-mac-helper`). Two traps:

- **Kernel ≥ 6.19 cannot run MongoDB 8.0**
  ([SERVER-121912](https://jira.mongodb.org/browse/SERVER-121912)). `doctor`
  warns when it sees this; use an engine on kernel < 6.19 until MongoDB ships a fix.
- **Docker Hub anonymous pull limits** (`registry.rocket.chat` counts against Hub
  too) — run `docker login`.

`up` names either cause directly rather than reporting a bare compose failure.
</details>

## Install

```bash
pipx install 'rc-repro[gui] @ git+https://github.com/klovekesh37/rc-repro'
rc-repro doctor
```

The `gui` extra adds the few dependencies [`serve`](#web-gui) needs; drop it for a
CLI-only install. In a checkout: `pip install -e '.[gui]'`.

To update: `pipx reinstall rc-repro` (`upgrade` only moves when the version was
bumped). In a checkout: `git pull && pip install -e '.[gui]'`.

## Your first repro

```bash
rc-repro up --version 8.5.1 --name test --wait
# open the printed URL — admin / admin123
rc-repro down --name test --volumes
```

- `--wait` blocks until Rocket.Chat answers and skips the setup wizard. First boot
  pulls images and can take a few minutes.
- `--name` is optional (derived from the version) — use a **ticket id** so `list`
  maps repros to your work.
- Every repro creates the same admin: **`admin` / `admin123`**.

> **Local by default.** Published ports bind `127.0.0.1`. `--bind 0.0.0.0` exposes Rocket.Chat **and every sidecar**
> (Keycloak, Mailpit, MinIO — all with known credentials) to your whole network.
> This is a reproduction tool, never a deployment.

---

# Everyday use

```bash
rc-repro list                     # version, port, state, URL
rc-repro info   --name test       # URL, credentials, snippets, preset notes
rc-repro logs   --name test -f    # tail
rc-repro ready  --name test       # block until serving
```

A repro is long-lived — pause and resume it rather than recreating it:

| Command | Effect |
|---|---|
| `up` | create, or bring an existing one back up (data intact) |
| `stop` / `start` | pause / resume — containers kept, seconds |
| `down` | remove containers, **keep** data and the record |
| `down --volumes` | delete the data and forget it (confirms first) |
| `prune` | remove every `down` repro (skips pinned and running; confirms first) |

Both data-deleting commands prompt, and `prune` lists what it will delete. `--yes`
skips the prompt in scripts.

**A daily driver:** `rc-repro up -v 8.5.1 --name main --pin` once, then `rc-repro
start` / `rc-repro stop`. Commands with no `--name` act on the pinned repro (or
whatever `rc-repro use <name>` last set).

---

# Web GUI

`rc-repro serve` is the same engine as the CLI with a browser in front — useful
when you would rather click than type, and the way a team shares one box.

## On your own machine

```bash
rc-repro serve                    # http://localhost:7070/
```

It binds **loopback only** — the GUI can create and delete
containers and their volumes and mint admin tokens, so the port is docker control.
Binding anything else needs a deliberate flag, and rc-repro refuses the unsafe
combinations rather than warning about them.

**The first account.** With no accounts, a loopback `serve` prints a one-time link:

```
http://localhost:7070/setup#k=Xf9…
```

Open the **whole** URL including the part after `#`. A fragment never reaches the
server, so the key cannot appear in an access log, a proxy log or a `Referer`
header. The page asks for a name and a password and creates the first **admin**.

Prefer the terminal? `rc-repro users add <name>` first, and `serve` shows a normal
sign-in page instead.

## Accounts

```bash
rc-repro users                    # who exists, their roles and dates — never hashes
rc-repro users add alice          # GENERATES the password and prints it once
rc-repro users add alice --ask-password
rc-repro users role alice admin   # admin | member | readonly
rc-repro users passwd alice       # new password, ends alice's sessions
rc-repro users remove alice       # and every session it minted
```

The **first** account is an admin; every one after it is a `member` until you say otherwise.

The password is generated rather than typed unless --ask-password; and a generated one is ~96 bits where a typed one clears a
12-character minimum with scrypt.
Stored in `~/.rc-repro/users`, mode `0600`, hashed with `hashlib.scrypt`.

**Three roles, not a permission system:**

| Role | May |
|---|---|
| `admin` | everything, including managing people |
| `member` | everyday use — create, seed, load-test, tear down |
| `readonly` | look, but not touch — and **not** read logs or env vars, which carry LDAP bind passwords and OAuth client secrets |

A `member` may set `--rc-image`, `--reg-token` and `--bind`:

```bash
rc-repro config set gui.create_policy admin      # only admins may set those three
rc-repro config set gui.destroy_policy anyone    # members may delete anyone's workspace
rc-repro config set bind_host 0.0.0.0            # one decision instead of per-create
```

Sessions are server-side and revocable: sign-out ends one, changing or removing an
account ends every session it minted, and each person can see their own from the
account menu. 

## Reaching it from somewhere else

Signing in over plain http puts the password on the wire once and the session
cookie on every request after it. `serve` therefore **refuses** to bind a
reachable interface over plain http unless you say what is protecting it. Pick the
row that matches where TLS actually ends:

| Where TLS ends | Command |
|---|---|
| Nowhere — just you, on this machine | `rc-repro serve` |
| rc-repro does it, on a name that points here | `rc-repro serve --domain rc.example.com --email you@example.com` |
| A TLS proxy on **this** box | `rc-repro serve --bind 127.0.0.1 --allow-host rc.example.com` |
| A proxy **elsewhere** — a lab, Codespaces, ngrok, a load balancer | `rc-repro serve --bind 0.0.0.0 --allow-host rc.example.com --trust-proxy 10.0.0.1` |
| Nothing, and you accept it | `rc-repro serve --bind 0.0.0.0 --allow-host <name> --insecure` |

Two flags do the work, and they answer different questions:

- **`--allow-host`** is a **DNS-rebinding guard**: whatever you type in the address
  bar has to be named here, or the request is a 403. Repeatable; `*` accepts any
  host. This is the one people miss when a proxy forwards its own `Host` and every request 403s.
- **`--trust-proxy <address-or-CIDR>`** says *this peer terminates TLS, believe its
  `X-Forwarded-Proto`/`-For`*. Without it rc-repro cannot tell the browser's hop is
  https, so it will not mark the session cookie `Secure` and the sign-in page warns
  about a connection that is actually encrypted. Name the proxy's address, not
  `0.0.0.0`, unless nothing else can reach the port.

> Run `rc-repro users add <name>` first. The one-time setup link is offered on a loopback bind only.


Everything the GUI does is a CLI command underneath, and every action is recorded
against the account that took it and appended to `~/.rc-repro/audit.log` — a file. Read it with `rc-repro audit`.

## Your own workspaces

With accounts, derived names are namespaced by owner, so two people can run the
same version:

```
$ rc-repro list
NAME                 OWNER        RC        MONGO   PORT   STATE      URL
*alice-rc8-5-1       alice        8.5.1     8.0     3000   running    …
 bob-rc8-5-1         bob          8.5.1     8.0     3001   running    …
```

## Keeping it running

```bash
rc-repro serve --domain rc.example.com --email you@example.com --print-service
```

Prints a **systemd unit** and how to install it, plus a `nohup` fallback — and
writes nothing, so you can read it first. systemd restarts on crash, starts on
boot, and gives you `journalctl -u rc-repro -f`. `nohup` survives logout and
nothing else.

---

# Scenarios

A preset turns a bare Rocket.Chat into a scenario. `rc-repro presets` lists them;
the GUI's **Scenarios** page shows the same thing with each one's setup steps.

```bash
rc-repro up --version 8.5.1 --preset ldap
```

| Preset | Brings up | Reproduces |
|---|---|---|
| `default` | RC + Mongo | anything |
| `airgapped` | Cloud endpoints stubbed out | offline / disconnected workspace |
| `ldap` | OpenLDAP with users and a group | LDAP auth and sync |
| `saml` | a real Keycloak IdP (SAML realm) | SAML SSO |
| `oidc` | a real Keycloak IdP (OpenID Connect) | OIDC / OAuth SSO |
| `email` | Mailpit wired to RC's SMTP | invites, resets, verification, 2FA codes |
| `s3_minio` | MinIO as the upload backend | uploads, previews, presigned URLs |
| `livechat` | Omnichannel, an available agent, a demo site embedding the widget | widget load, CORS, routing, agent availability |
| `multi-instance` | N instances + load balancer + NATS, one Mongo | scaling, cross-instance real-time |

For `ldap`, `saml` and `oidc`, log in as **`user1` / `user1`**.

```bash
rc-repro up -v 8.5.1 --preset ldap --set users=130000        # scale repro
rc-repro up -v 8.5.1 --preset multi-instance --set instances=3
rc-repro up -v 8.5.1 --preset email --seed --wait            # verified users + Mailpit
rc-repro up -v 8.5.1 --preset s3_minio --set presigned=true  # real presigned URLs
rc-repro up -v 8.5.1 --preset livechat --wait
```

**`oidc` needs one host entry** — `127.0.0.1  keycloak` in `/etc/hosts`, because the
login URL must resolve the same way for your browser and RC's backend. `up` prints
it, `rc-repro info` reprints it, and the GUI shows it as a blocking setup step.
Everything else a preset needs is in its notes: consoles, credentials, realms.

<details>
<summary><b>Custom presets and Enterprise licences</b></summary>

Drop a YAML file in `~/.rc-repro/presets/<name>.yaml` (it overrides a built-in of
the same name). **Treat preset files as code** — they can run arbitrary containers
and mount files.

```yaml
name: my-scenario
description: What this reproduces.
env:
  OVERWRITE_SETTING_Some_Setting: "true"
services:
  my-sidecar: { image: some/image:tag }
depends_on: [my-sidecar]
```

For an EE licence pass a cloud **registration token** — the workspace
self-registers on first boot and Rocket.Chat Cloud syncs the licence down:

```bash
rc-repro up -v 8.5.1 --reg-token <token> --wait
```

Put `reg_token:` in `~/.rc-repro/config.yaml` or export `RC_REPRO_REG_TOKEN` to
stop retyping it.
</details>

## Monitoring

Prometheus + Grafana + Loki on top of *any* repro — an add-on, not a preset, so it
layers onto whatever you chose:

```bash
rc-repro up -v 8.5.1 --monitor
rc-repro monitor --name test           # or attach later, without restarting RC
rc-repro monitor --name test --off
```

**Grafana** on `http://localhost:5050` (`admin`/`admin`, anonymous view on) with the
official *Rocket.Chat Metrics*, MongoDB and Node Exporter dashboards provisioned;
**Prometheus** on `:9090`; container logs shipped to **Loki** and readable in the
*Rocket.Chat Logs* dashboard. The log collector is scoped to this repro's compose
project, so it never ingests your others.

---

# HTTPS

**A workspace never terminates TLS.** One Traefik — the **edge** — holds `:80` and
`:443` and serves every name on the machine. It starts by itself with the first
workspace that needs one.

```bash
# A domain name, with a Let's Encrypt certificate
rc-repro up -v 8.5.1 --domain t1234.example.com --email you@example.com

# No domain, no internet: rc-repro's own CA
rc-repro trust-ca                       # once
rc-repro up -v 8.5.1 --https            # https://<name>.rcrepro.localhost
```

The email is remembered (`rc-repro config set acme.email …`). Rocket.Chat's
`ROOT_URL` becomes the https URL, which is what makes OAuth/SAML redirects.

**shared fate is real**: while the edge is down, every https name on the box is
unreachable, which is why `doctor` calls a stopped edge a failure rather than a
warning.

```bash
rc-repro edge status     # running? which names? can it reach each one?
rc-repro edge stop       # frees :80 and :443
```

`status` prints each route beside whether the edge is actually attached to that
workspace — a route it cannot reach answers **502** rather than erroring, and
nothing else would tell you.

<details>
<summary><b>dns-01 — the way round all of that, and what unlocks a wildcard</b></summary>

Not a flag. Put your provider's credentials in `~/.rc-repro/acme/dns.env` and
rc-repro uses dns-01 automatically:

```bash
mkdir -p ~/.rc-repro/acme && chmod 700 ~/.rc-repro/acme
printf 'CF_DNS_API_TOKEN=%s\n' "$TOKEN" > ~/.rc-repro/acme/dns.env
chmod 600 ~/.rc-repro/acme/dns.env
```

---

# Environment variables

```bash
rc-repro env                                        # the effective env, secrets masked
rc-repro env --setting Message_AllowEditing=false   # a Rocket.Chat SETTING
rc-repro env --set MY_FLAG=true                     # a raw env var
rc-repro env --unset ALLOW_UNSAFE_QUERY_AND_FIELDS_API_PARAMS
```

Most Rocket.Chat configuration is a **setting** (what you would change in the admin
UI), not a plain env var — and a setting only takes effect from the environment
with the `OVERWRITE_SETTING_` prefix. The bare form failing *quietly* is the trap:

| Env var | Effect on `Accounts_ShowFormLogin` |
|---|---|
| `Accounts_ShowFormLogin=false` | **none** — accepted by docker, silently ignored |
| `OVERWRITE_SETTING_Accounts_ShowFormLogin=false` | applied |

So **`--setting Id=value` adds the prefix for you** — prefer it for anything you
would change in Admin → Settings. `--set` is for real env vars. If you use `--set`
with a name that *is* a setting on this workspace, rc-repro says so and gives you
the prefixed form; it asks the workspace, so it is right for whatever version is
running. The GUI's Env-vars tab does the same and keeps the two kinds apart.

`OVERWRITE_SETTING_*` is **asserted at every boot, not locked** — the API and admin
UI can still change it while the container runs, and the next restart puts the env
value back.

Applying a change rewrites the compose file and recreates **only** the Rocket.Chat
container: MongoDB keeps running, its volume is untouched, no image is pulled, open
sessions reconnect. `--no-restart` writes it for the next `up`.

Overrides persist in the repro's metadata, so `up --force` keeps them. Precedence is
base → preset → yours. Load-bearing keys (`MONGO_URL`, `PORT`, `ROOT_URL`,
`TRANSPORTER`) are allowed — reproducing a broken configuration is the point — but
each prints what it will break.

---

# Data & performance

## Sample data

A fresh repro is empty. `--seed` creates realistic users, channels, DMs and
messages, authored across the users rather than all by admin:

```bash
rc-repro up -v 8.5.1 --name test --seed --seed-profile standard
rc-repro seed --name test --profile large
rc-repro seed --name test --users 30 --channels 10 --messages 40
```

| Profile | Users | Channels | Msgs/channel | DMs | Threads & reactions |
|---|---|---|---|---|---|
| `small` (default) | 5 | 3 | 5 | 2 | no |
| `standard` | 20 | 8 | 20 | 5 | yes |
| `large` | 100 | 20 | 100 | 20 | yes |

Seed users are `alice`, `bob`, … with password = username. Email-2FA and the rate
limiter are disabled while seeding and **restored afterwards**.

**Scale repros** ("50k users", "a room with 800k messages won't load") want
`--scale`, which bulk-inserts straight into MongoDB at tens of thousands per second:

```bash
rc-repro seed --name test --scale users=50000,messages=800000@general
rc-repro seed --name test --clear-scale        # removes exactly what --scale added
```

Bulk documents bypass RC's hooks: bulk users are **credential-less** and messages
fire no notifications or threading. Use `--scale` for scale behaviour and the REST
seed for feature behaviour.

## support dump configuration

```bash
rc-repro config-import ./support-dump/8.5.0-settings.json --name test --dry-run
rc-repro config-import ./8.5.0-settings.json --name test --only Livechat,LDAP
```

Applies the settings from the suport dump. Deliberately skips **redacted
secrets** (listing them so you can set them by hand) and **identity settings** that
would break or pollute a local repro (`Site_Url`, `Enterprise_License`, `Assets_*`,
cloud registration).

## Backup, restore, upgrade

```bash
rc-repro backup --label "before upgrade"    # → ~/.rc-repro/backups/*.rcbak
rc-repro restore <bundle> --new             # rebuild a whole workspace from it
rc-repro upgrade --to 8.6.1                 # backs up first; --rollback undoes it
```

A backup is a **bundle**, not a bare dump: the database plus the version, preset and
parameters that produced it — which is what lets one file rebuild a workspace on a
colleague's machine. Rocket.Chat is stopped for both operations (`mongodump` is not
point-in-time consistent across collections; `--live` skips it) and the target
database is dropped first, so you never end up with a hybrid. Restoring newer data
into an older workspace is refused; older into newer needs `--allow-upgrade`.

**Not included:** sidecar data — MinIO objects, Keycloak realms, LDAP entries,
Mailpit mail.

`upgrade` needs the repro **running**, because the migrations happen when RC boots.
An upgrade that also needs a new MongoDB *major* is refused rather than attempted —
majors have to be stepped one at a time with a `featureCompatibilityVersion` bump
between each, and doing it in one move is how data is lost.

## Load testing and capacity

```bash
rc-repro loadtest --name test --scenario journey --vus 50 --duration 60s
rc-repro loadtest --name test --scenario custom --endpoint "GET /api/v1/channels.list?count=100"
rc-repro loadtest --name test --scenario journey --save before-fix
rc-repro loadtest --name test --scenario journey --compare before-fix
rc-repro capacity --name test --slo "p95=300ms,error=2%"
rc-repro benchmark --versions 8.4.1,8.5.1,8.6.0 --report
```

Real concurrent load through [k6](https://k6.io), run as a throwaway container on
the repro's own network, so it works with loopback-only binds.

- **Scenarios:** `journey` (a full session per iteration, each step timed so you see
  *which* one is slow), `messages`, `login`, `read`, `mixed`, `webhook`, `badbot`,
  and `custom` — any endpoint you name.
- **Shapes:** constant `--vus`, `--ramp 10:200`, or `--spike 10:100`, which reports
  how long p95 took to recover after load dropped. Long runs with `--stats` report
  the RAM slope per hour — the soak-test leak signal.
- **Their hardware:** `--constrain "rc=2cpu/2g,mongo=1cpu/1g"` caps services for the
  run (live, no restart, restored after).
- **`--slo p95=300ms,error=1%`** exits non-zero, so it drops into CI.
- **Diagnosis, on by default:** RC's event-loop lag, MongoDB's slow queries with
  their plans (flagging **COLLSCAN**), and a p95 timeline pinning *when* errors
  started — ending in a plain-language verdict.

`capacity` doubles VUs until the SLO breaks, bisects the boundary, and says why it
broke. `benchmark` boots several versions, runs the identical workload against each
and prints the deltas — the regression check that only a version-matched tool can do.

> **VUs are k6 workers, not Rocket.Chat accounts.** Write scenarios leave real
> messages in `#general`; `read` and `login` add nothing.

---

# API testing

```bash
rc-repro token --name test                     # -H auth headers
rc-repro api   --name test GET /api/v1/me
rc-repro api   --name test --pat POST /api/v1/users.update -d '{"userId":"ID","data":{"name":"X"}}'
rc-repro api   --name test --2fa POST /api/v1/settings/<id> -d '{"value":true}'
```

`--pat` mirrors a customer's Personal Access Token (with "Ignore 2FA"); `--2fa` gets
past a 2FA-guarded admin endpoint. The GUI has the same thing as a console, with the
response pretty-printed.

---

# Reference

## Commands

| Command | Purpose |
|---|---|
| `up` | create and start a version-matched repro |
| `start` / `stop` / `restart` / `down` | lifecycle; `down --volumes` also deletes data |
| `list` / `info` / `logs` / `ready` | what exists, where it is, what it is doing |
| `use` / `config` | the default repro; remembered settings |
| `env` | show or change env vars, recreating RC to apply |
| `seed` | sample users/channels/messages; `--scale` for bulk Mongo prefill |
| `config-import` | apply a customer's exported settings |
| `backup` / `backups` / `restore` | bundle a repro's database, list bundles, load one back |
| `upgrade` | move a running repro to another version; `--rollback` |
| `stats` / `benchmark` / `loadtest` / `capacity` | measure it |
| `monitor` | attach/detach Prometheus + Grafana |
| `token` / `api` / `pat` | REST auth and calls |
| `presets` / `versions` | what scenarios exist; the resolved MongoDB pairing |
| `serve` / `users` / `audit` | the [web GUI](#web-gui), its accounts, and who did what |
| `chown` | hand a workspace over to somebody else, with the record to match |
| `edge` / `trust-ca` / `tls-status` | the shared Traefik, the local CA, what is really served |
| `doctor` | preflight: Docker, Compose, kernel, Hub auth, disk, memory, ports |
| `prune` | delete every `down` repro |

`rc-repro <command> --help` for flags.

## How version → MongoDB resolution works

`rc-repro up --version X` (and `rc-repro versions X`) queries
`releases.rocket.chat/<version>/info` — Rocket.Chat's own per-release compatibility
data — and picks the highest supported MongoDB. Offline, or for a release predating
that data, it falls back to the shipped `versions.yaml` (`--offline` forces this).

The image follows the resolved Mongo version: **≥ 8** →
`mongodb/mongodb-community-server` with a fix-permission and one-shot init container
(matching the official compose); **< 8** → `bitnamilegacy/mongodb`.
`MONGO_OPLOG_URL` is emitted only for RC < 8.

## Where state lives

```
~/.rc-repro/                  # override with RC_REPRO_HOME
├── config.yaml               # default repro, reg_token, bind_host, gui policies
├── users                     # GUI accounts — 0600, scrypt
├── sessions                  # server-side sessions — 0600
├── audit.log                 # who ran what
├── presets/                  # your custom presets
├── backups/  reports/  loadtests/
├── acme/                     # ACME state; dns.env, issued.json
├── edge/                     # the shared edge and one file per route
└── repros/<name>/
    ├── docker-compose.yml    # generated — re-run `up` rather than editing
    └── repro.json
```

Config can also come from the environment, which wins over `config.yaml`:
`RC_REPRO_HOME`, `RC_REPRO_REG_TOKEN`, `RC_REPRO_RC_IMAGE`, `RC_REPRO_BIND_HOST`
(the `--bind` flag wins over both).

## Development

```bash
git clone https://github.com/klovekesh37/rc-repro.git && cd rc-repro
pip install -e ".[dev,gui,browser]"
pytest                         # no Docker needed
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the layout and how to add a preset.
