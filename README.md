# rc-repro

**Reproduce a customer's Rocket.Chat issue on their exact version, in minutes.**
One command boots that version paired with a compatible MongoDB — on Docker Compose
or on Kubernetes — with optional backing services (LDAP, SAML/OIDC, email, S3,
multi-instance) and realistic sample data.

```bash
rc-repro up --version 8.5.1 --name TICKET-1234 --wait   # boot it
rc-repro down --name TICKET-1234 --volumes              # bin it when done
```

Prefer a browser? [`rc-repro serve`](#13-web-gui) does everything the CLI does.

---

## Contents

**Start here** — [1 Install](#1-install) · [2 First workspace](#2-your-first-workspace) ·
[3 Where it runs](#3-where-it-runs) · [4 Lifecycle](#4-everyday-lifecycle)

**Fill it** — [5 Scenarios](#5-scenarios) · [6 Sample data](#6-sample-data) ·
[7 Settings and env vars](#7-settings-and-env-vars) · [8 Backup, restore, upgrade](#8-backup-restore-upgrade)

**Reach it and measure it** — [9 HTTPS](#9-https) · [10 Monitoring](#10-monitoring) ·
[11 Load testing](#11-load-testing-and-capacity) · [12 REST API](#12-rest-api)

**Share it and script it** — [13 Web GUI](#13-web-gui) · [14 For scripts and agents](#14-for-scripts-and-agents)

**[Reference](#reference)** — commands, exit codes, version pairing, state, development

Every command has `--help`, and it is written to be read: this file is the map.

---

## 1. Install

**You need:** Docker with `docker compose` v2, running · Python 3.11+ · internet, for
image pulls and version lookup. For `--runtime kubernetes` you also need
[kind](https://kind.sigs.k8s.io/), `kubectl` and `helm` on `PATH`.

### A. As a tool you use (recommended)

```bash
pipx install 'rc-repro[gui] @ git+https://github.com/klovekesh37/rc-repro'
rc-repro doctor
```

### B. In a virtualenv

```bash
git clone https://github.com/klovekesh37/rc-repro.git && cd rc-repro
python3 -m venv .venv
.venv/bin/pip install -e ".[gui]"
.venv/bin/rc-repro doctor
```

Run it as `.venv/bin/rc-repro …`, or `source .venv/bin/activate` first. A bare
`python`/`pytest` resolves outside the venv and will not be this build.

**Extras:** `[gui]` adds what [`serve`](#13-web-gui) needs (FastAPI, uvicorn) — drop it
for a CLI-only install. `[dev]` adds pytest and ruff, `[browser]` adds Playwright; for
development install `".[dev,gui,browser]"`.

`rc-repro doctor` is the preflight and names anything that would fail a boot: Docker,
Compose, kernel, Hub auth, disk, memory, ports.

### Updating

```bash
pipx reinstall rc-repro                 # tool install
git pull && .venv/bin/pip install -e .  # checkout
```

`rc-repro --version` reports the **installed** distribution, so in a checkout `git
pull` alone does not move it — re-run `pip install -e .` or the version keeps naming
the release you had before. On a remote box that one command is how you tell whether
a deploy landed.

<details>
<summary><b>Apple Silicon, Podman, and Docker Hub rate limits</b></summary>

**Apple Silicon:** only the Bitnami MongoDB image (MongoDB < 8, i.e. RC < 8) is
amd64-only and runs emulated, so those boots are slower. Everything else is native.

**Podman** works via the `docker.sock` helper (`podman-mac-helper`). Two traps:

- **Kernel ≥ 6.19 cannot run MongoDB 8.0**
  ([SERVER-121912](https://jira.mongodb.org/browse/SERVER-121912)). `doctor` warns
  when it sees this; use an engine on kernel < 6.19 until MongoDB ships a fix.
- **Docker Hub anonymous pull limits** (`registry.rocket.chat` counts against Hub
  too) — run `docker login`.

`up` names either cause directly rather than reporting a bare compose failure.
</details>

---

## 2. Your first workspace

```bash
rc-repro up --version 8.5.1 --name test --wait
rc-repro info --name test          # URL, login, links, what is in it
rc-repro down --name test --volumes
```

- `--wait` blocks until Rocket.Chat answers and skips the setup wizard. First boot
  pulls images and can take a few minutes — that duration is not typical.
- `--name` is optional (derived from the version); use a **ticket id** so `list` maps
  workspaces to your work.
- Every workspace gets the same admin: **`admin` / `admin123`**.

> **Local by default.** Published ports bind `127.0.0.1`. `--bind 0.0.0.0` exposes
> Rocket.Chat **and every sidecar** — Keycloak, Mailpit, MinIO, all with known
> credentials — to your whole network. This is a reproduction tool, never a deployment.

---

## 3. Where it runs

Two runtimes, and the choice decides what the other flags mean. `rc-repro
capabilities` lists the legal combinations for your build.

```bash
rc-repro up -v 8.5.1 --name t1                                     # Docker, monolith
rc-repro up -v 8.5.1 --name t2 --deployment multi-instance --replicas 3
rc-repro up -v 8.5.1 --name t3 --runtime kubernetes                # microservices
rc-repro up -v 8.5.1 --name t4 --runtime kubernetes --deployment monolith
```

| `--runtime` | `--deployment` | What you get |
|---|---|---|
| `docker` (default) | `monolith` (default) | RC + MongoDB, two containers |
| `docker` | `multi-instance` | N instances + load balancer + NATS, one MongoDB |
| `kubernetes` | `microservices` (default) | Helm chart, ~9 pods, real service split |
| `kubernetes` | `monolith` | Helm chart, ~5 pods |

**Kubernetes runs in a shared `kind` cluster** that rc-repro creates on first use and
every workspace after that reuses. Each workspace is a namespace
(`rc-repro-<name>`), reached on `http://localhost:<port>` through a `kubectl
port-forward` that rc-repro starts. rc-repro keeps **its own kubeconfig** under
`RC_REPRO_HOME`, so creating a cluster never moves your current context.

`kind` is therefore required to *create* one. Everything rc-repro then does inside a
namespace is plain Kubernetes and behaves the same on k3s, minikube, Docker Desktop or
a remote cluster — `doctor` probes whatever `kubectl` points at and reports it as
yours — but `up` does not yet target an existing cluster, and refuses with a preflight
error rather than guessing when `kind` is absent.

`--mongo-operator` manages MongoDB with the official community operator (adds SCRAM
auth, needs MongoDB 6.0+); without it MongoDB is a plain StatefulSet with no auth.

**What Kubernetes refuses, and why** — each refusal names the reason and the alternative:

| Refused | Because |
|---|---|
| `--https`, `--domain` | HTTPS needs an ingress controller and cert-manager, not the Traefik edge Compose uses |
| `--fresh` | it means *delete this workspace's data*, and this path keeps the PVC — use `down --volumes` |
| `loadtest`, `capacity` | the port-forward is one userspace relay; it saturates first, so the numbers would measure the forward |
| `env --set` | an environment variable is a helm value there; the refusal hands over the `helm upgrade` that changes one |
| `stats` | needs metrics-server in the cluster; the refusal says how to install it |

`logs`, `env` (read), `upgrade`, `backup`, `restore`, `seed`, `monitor`, `api`, `pat`
and `token` all work on both. `down` leaves the shared cluster running on purpose;
`rc-repro prune` reclaims it, and refuses while any rc-repro namespace remains.

---

## 4. Everyday lifecycle

```bash
rc-repro list                     # version, MongoDB, where it runs, port, state, URL
rc-repro info   --name test       # URL, credentials, links, notes, pods
rc-repro logs   --name test -f    # tail (pods on Kubernetes, containers on Compose)
rc-repro ready  --name test       # block until serving
```

A workspace is long-lived — pause and resume rather than recreating:

| Command | Effect |
|---|---|
| `up` | create, or bring an existing one back (data intact) |
| `stop` / `start` | pause / resume — seconds; scale-to-zero on Kubernetes |
| `down` | remove containers or the release, **keep** data and the record |
| `down --volumes` | delete the data and forget it (confirms first) |
| `prune` | remove every `down` workspace, skipping pinned and running ones |

Both data-deleting commands prompt and `prune` lists what it will delete; `--yes`
skips the prompt in scripts.

**A daily driver:** `rc-repro up -v 8.5.1 --name main --pin` once, then `start` /
`stop`. Commands with no `--name` act on the pinned workspace, or whatever `rc-repro
use <name>` last set.

> **Tear down by default.** A Compose workspace costs about 1.1 GB and a Kubernetes
> one more; `up` refuses when the box lacks headroom, and seven concurrent stacks have
> OOM-killed a 10 GB host.

---

## 5. Scenarios

A preset turns a bare Rocket.Chat into a scenario. All of them work on both runtimes.

```bash
rc-repro presets                              # what exists, and what each reproduces
rc-repro up -v 8.5.1 --preset ldap --wait
```

| Preset | Brings up | Reproduces |
|---|---|---|
| `default` | RC + MongoDB | anything |
| `airgapped` | cloud endpoints stubbed out | offline / disconnected workspace |
| `ldap` | OpenLDAP with users and a group | LDAP auth and sync |
| `saml` | a real Keycloak IdP (SAML realm) | SAML SSO |
| `oidc` | a real Keycloak IdP (OpenID Connect) | OIDC / OAuth SSO |
| `email` | Mailpit wired to RC's SMTP | invites, resets, verification, 2FA codes |
| `s3_minio` | MinIO as the upload backend | uploads, previews, presigned URLs |
| `livechat` | Omnichannel, an agent, a demo site with the widget | widget load, CORS, routing |
| `multi-instance` | N instances + load balancer + NATS | scaling, cross-instance real-time |

For `ldap`, `saml` and `oidc`, log in as **`user1` / `user1`**.

```bash
rc-repro up -v 8.5.1 --preset ldap --set users=130000        # scale a directory
rc-repro up -v 8.5.1 --preset email --seed --wait            # verified users + Mailpit
rc-repro up -v 8.5.1 --preset s3_minio --set presigned=true
```

**`oidc` needs one host entry** — `127.0.0.1  keycloak` in `/etc/hosts`, because the
login URL must resolve the same way for your browser and RC's backend. `up` prints
it, `info` reprints it, and the GUI shows it as a blocking setup step. Everything
else a preset needs — consoles, credentials, realms — is in its notes.

<details>
<summary><b>Custom presets and Enterprise licences</b></summary>

Drop a YAML file in `~/.rc-repro/presets/<name>.yaml`; it overrides a built-in of the
same name. **Treat preset files as code** — they can run arbitrary containers and
mount files.

```yaml
name: my-scenario
description: What this reproduces.
env:
  OVERWRITE_SETTING_Some_Setting: "true"
services:
  my-sidecar: { image: some/image:tag }
depends_on: [my-sidecar]
```

For an EE licence pass a cloud **registration token**; the workspace self-registers
on first boot and Rocket.Chat Cloud syncs the licence down. On Kubernetes it is
delivered through a Secret referenced by `valueFrom`, never as a values literal.

```bash
rc-repro up -v 8.5.1 --reg-token <token> --wait
```

Put `reg_token:` in `~/.rc-repro/config.yaml` or export `RC_REPRO_REG_TOKEN` to stop
retyping it. **A registration token is a real licence** — do not paste one into a
support case.
</details>

---

## 6. Sample data

A fresh workspace is empty. `--seed` plans a manifest, creates it over REST, then
**reads it back and checks it against the plan**.

```bash
rc-repro up -v 8.5.1 --name test --seed --seed-profile standard --verify-seed
rc-repro seed --name test --profile large
rc-repro seed --name test --users 30 --channels 10 --messages 40
```

| Profile | Users | Rooms | Messages | Contains |
|---|---|---|---|---|
| `small` (default) | 5 | 11 | 48 | every room kind, in miniature |
| `standard` | 20 | 22 | 287 | + more of each, 48 threads |
| `large` | 100 | 52 | 3006 | + volume |

Every profile contains **every kind of room**, because tickets are rarely about a
public channel: public and private channels, public and private teams, channels of
either visibility inside a team, discussions (some anchored to a parent message),
direct messages, threads and reactions.

Seed users are `alice`, `bob`, … with password = username. Email-2FA and the rate
limiter are disabled while seeding and **restored afterwards**.

**The readback is the point.** `verification.ok` says the rooms are really there —
treat a false or missing one as unproven rather than inferring success from a 2xx
write. `--verify-seed` makes a mismatch fail the command; without it the check still
runs and is recorded in the workspace's `repro.json`. Rooms holding *more* than
planned are reported separately and are not faults: seeding only ever adds.

**Scale workspaces** ("50k users", "a room with 800k messages won't load") want
`--scale`, which bulk-inserts straight into MongoDB:

```bash
rc-repro seed --name test --scale users=50000,messages=800000@general
rc-repro seed --name test --clear-scale        # removes exactly what --scale added
```

Bulk documents bypass RC's hooks: bulk users are credential-less and messages fire no
notifications. Use `--scale` for scale behaviour and the REST seed for feature
behaviour.

**A customer's settings**, from a support dump:

```bash
rc-repro config-import ./8.5.0-settings.json --name test --dry-run
rc-repro config-import ./8.5.0-settings.json --name test --only Livechat,LDAP
```

It skips **redacted secrets** (listing them so you can set them by hand) and
**identity settings** that would break or pollute a local workspace (`Site_Url`,
`Enterprise_License`, `Assets_*`, cloud registration).

---

## 7. Settings and env vars

```bash
rc-repro env                                        # effective env, secrets masked
rc-repro env --setting Message_AllowEditing=false   # a Rocket.Chat SETTING
rc-repro env --set MY_FLAG=true                     # a raw env var
rc-repro env --unset ALLOW_UNSAFE_QUERY_AND_FIELDS_API_PARAMS
```

Most Rocket.Chat configuration is a **setting** — what you would change in the admin
UI — and a setting only takes effect from the environment with the
`OVERWRITE_SETTING_` prefix. The bare form failing *quietly* is the trap:

| Env var | Effect on `Accounts_ShowFormLogin` |
|---|---|
| `Accounts_ShowFormLogin=false` | **none** — accepted by docker, silently ignored |
| `OVERWRITE_SETTING_Accounts_ShowFormLogin=false` | applied |

`--setting Id=value` adds the prefix for you; `--set` is for real env vars. Use a
setting's name with `--set` and rc-repro asks the running workspace, then hands you
the prefixed form.

`OVERWRITE_SETTING_*` is **asserted at every boot, not locked**: the API and admin UI
can still change it while the container runs, and the next restart puts it back.

Applying a change rewrites the compose file and recreates **only** the Rocket.Chat
container — MongoDB keeps running, no image is pulled. `--no-restart` writes it for
the next `up`. Overrides persist in the workspace's metadata, so `up --force` keeps
them; precedence is base → preset → yours. Load-bearing keys (`MONGO_URL`, `PORT`,
`ROOT_URL`, `TRANSPORTER`) are allowed — reproducing a broken configuration is the
point — but each prints what it will break.

---

## 8. Backup, restore, upgrade

```bash
rc-repro backup  --name test --label "before upgrade"   # → ~/.rc-repro/backups/*.rcbak
rc-repro backups                                        # what exists
rc-repro restore <bundle> --new                         # rebuild a whole workspace
rc-repro upgrade --name test --to 8.6.1                 # backs up first
rc-repro upgrade --name test --rollback
```

A backup is a **bundle**, not a bare dump: the database plus the version, preset and
parameters that produced it, which is what lets one file rebuild a workspace on a
colleague's machine. Rocket.Chat is stopped for both operations (`mongodump` is not
point-in-time consistent across collections; `--live` skips the stop) and the target
database is dropped first, so you never get a hybrid.

Restoring newer data into an older workspace is refused; older into newer needs
`--allow-upgrade`. **Not included:** sidecar data — MinIO objects, Keycloak realms,
LDAP entries, Mailpit mail.

`upgrade` needs the workspace **running**, because the migrations happen when RC
boots. An upgrade that also needs a new MongoDB *major* is refused rather than
attempted: majors have to be stepped one at a time with a
`featureCompatibilityVersion` bump between each.

---

## 9. HTTPS

**Compose only** — on Kubernetes both flags are refused (see [3](#3-where-it-runs)).

**A workspace never terminates TLS.** One Traefik — the **edge** — holds `:80` and
`:443` and serves every name on the machine, starting itself with the first workspace
that needs one.

```bash
# A real name, with a Let's Encrypt certificate
rc-repro up -v 8.5.1 --domain t1234.example.com --email you@example.com

# No domain, no internet: rc-repro's own CA
rc-repro trust-ca                       # once, per machine
rc-repro up -v 8.5.1 --https            # https://<name>.rcrepro.localhost
```

The email is remembered (`rc-repro config set acme.email …`). Rocket.Chat's
`ROOT_URL` becomes the https URL, which is what makes OAuth and SAML redirects work.

```bash
rc-repro edge status     # running? which names? can it reach each one?
rc-repro edge stop       # frees :80 and :443
rc-repro tls-status --name test
```

**Shared fate is real:** while the edge is down every https name on the box is
unreachable, which is why `doctor` calls a stopped edge a failure rather than a
warning. `edge status` prints each route beside whether the edge is actually attached
to that workspace — a route it cannot reach answers **502**, and nothing else would
tell you.

<details>
<summary><b>dns-01, and what unlocks a wildcard</b></summary>

Not a flag. Put your provider's credentials in `~/.rc-repro/acme/dns.env` and
rc-repro uses dns-01 automatically:

```bash
mkdir -p ~/.rc-repro/acme && chmod 700 ~/.rc-repro/acme
printf 'CF_DNS_API_TOKEN=%s\n' "$TOKEN" > ~/.rc-repro/acme/dns.env
chmod 600 ~/.rc-repro/acme/dns.env
```
</details>

---

## 10. Monitoring

Prometheus + Grafana + Loki on top of *any* workspace — an add-on, not a preset:

```bash
rc-repro up -v 8.5.1 --monitor
rc-repro monitor --name test           # or attach later, without restarting RC
rc-repro monitor --name test --off
```

**Grafana** on `http://localhost:5050` (`admin`/`admin`, anonymous view on) with the
official *Rocket.Chat Metrics*, MongoDB and Node Exporter dashboards provisioned;
**Prometheus** on `:9090`; container logs shipped to **Loki** and readable in the
*Rocket.Chat Logs* dashboard.

On Compose the log collector is scoped to that workspace's project, so it never
ingests your others. On Kubernetes one stack lives in `rc-repro-system` and is shared
by the cluster; `--off` leaves it up while any other workspace still wants it.

---

## 11. Load testing and capacity

**Compose only** — see [3](#3-where-it-runs) for why the Kubernetes numbers would be
wrong rather than missing.

```bash
rc-repro loadtest --name test --scenario journey --vus 50 --duration 60s
rc-repro loadtest --name test --scenario custom --endpoint "GET /api/v1/channels.list?count=100"
rc-repro loadtest --name test --scenario journey --save before-fix
rc-repro loadtest --name test --scenario journey --compare before-fix
rc-repro capacity --name test --slo "p95=300ms,error=2%"
rc-repro benchmark --versions 8.4.1,8.5.1,8.6.0 --report
```

Real concurrent load through [k6](https://k6.io), run as a throwaway container on the
workspace's own network, so it works with loopback-only binds.

- **Scenarios:** `journey` (a full session per iteration, each step timed),
  `messages`, `login`, `read`, `mixed`, `webhook`, `badbot`, and `custom`.
- **Shapes:** constant `--vus`, `--ramp 10:200`, or `--spike 10:100`, which reports
  how long p95 took to recover. Long runs with `--stats` report the RAM slope per
  hour — the soak-test leak signal.
- **Their hardware:** `--constrain "rc=2cpu/2g,mongo=1cpu/1g"` caps services for the
  run, live, and restores them after.
- **`--slo p95=300ms,error=1%`** exits non-zero, so it drops into CI.
- **Diagnosis, on by default:** RC's event-loop lag, MongoDB's slow queries with
  their plans (flagging **COLLSCAN**), and a p95 timeline pinning when errors
  started — ending in a plain-language verdict.

`capacity` doubles VUs until the SLO breaks, bisects the boundary and says why.
`benchmark` boots several versions, runs the identical workload against each and
prints the deltas — the regression check only a version-matched tool can do.

> **VUs are k6 workers, not Rocket.Chat accounts.** Write scenarios leave real
> messages in `#general`; `read` and `login` add nothing.

---

## 12. REST API

```bash
rc-repro token --name test                     # -H auth headers
rc-repro api   --name test GET /api/v1/me
rc-repro api   --name test --pat POST /api/v1/users.update -d '{"userId":"ID","data":{"name":"X"}}'
rc-repro api   --name test --2fa POST /api/v1/settings/<id> -d '{"value":true}'
rc-repro pat   --name test                     # mint a Personal Access Token
```

`--pat` mirrors a customer's Personal Access Token (with "Ignore 2FA"); `--2fa` gets
past a 2FA-guarded admin endpoint. The GUI has the same thing as a console with the
response pretty-printed.

---

## 13. Web GUI

`rc-repro serve` is the same service layer as the CLI with a browser in front — for
when you would rather click than type, and the way a team shares one box.

```bash
rc-repro serve                    # http://localhost:7070/
```

It binds **loopback only**: the GUI can create and delete workspaces and their
volumes and mint admin tokens, so the port is docker control. Binding anything else
takes a deliberate flag, and `serve` refuses the unsafe combinations rather than
warning about them.

**The first account.** With no accounts, a loopback `serve` prints a one-time link:

```
http://localhost:7070/setup#k=Xf9…
```

Open the **whole** URL including the part after `#` — a fragment never reaches the
server, so the key cannot appear in an access log, a proxy log or a `Referer` header.
The page creates the first **admin**. Prefer the terminal? `rc-repro users add
<name>` first, and `serve` shows a normal sign-in page.

### Accounts and roles

```bash
rc-repro users                    # who exists, roles, dates — never hashes
rc-repro users add alice          # GENERATES the password, prints it once
rc-repro users add alice --ask-password
rc-repro users role alice admin   # admin | member | readonly
rc-repro users passwd alice       # new password, ends alice's sessions
rc-repro users remove alice       # and every session it minted
```

| Role | May |
|---|---|
| `admin` | everything, including managing people |
| `member` | everyday use — create, seed, load-test, tear down |
| `readonly` | look, but not touch — and **not** read logs or env vars, which carry LDAP bind passwords and OAuth client secrets |

The first account is an admin; every one after it is a `member` until you say
otherwise. Passwords are generated (~96 bits) unless `--ask-password`, which enforces
a 12-character minimum; stored in `~/.rc-repro/users`, mode `0600`, hashed with
`hashlib.scrypt`. Sessions are server-side and revocable, so sign-out ends one and
changing or removing an account ends every session it minted.

A name is **folded into a DNS label**, because it becomes part of a workspace name:
`lucy.felix` and `Lucy_Felix` both create `lucy-felix`, and `users add` prints what it
made. Sign-in, `passwd`, `role` and `remove` accept either spelling.

```bash
rc-repro config set gui.create_policy admin      # only admins may set --rc-image/--reg-token/--bind
rc-repro config set gui.destroy_policy anyone    # members may delete anyone's workspace
rc-repro config set bind_host 0.0.0.0            # one decision instead of per-create
rc-repro audit                                   # who did what
rc-repro chown <name> --to bob                   # hand a workspace over
```

Every action is recorded against the account that took it in `~/.rc-repro/audit.log`.
With accounts, derived names are namespaced by owner, so two people can run the same
version side by side.

### Reaching it from somewhere else

Signing in over plain http puts the password on the wire once and the session cookie
on every request after. `serve` therefore **refuses** to bind a reachable interface
over plain http unless you say what is protecting it. Pick the row that matches where
TLS actually ends:

| Where TLS ends | Command |
|---|---|
| Nowhere — just you, on this machine | `rc-repro serve` |
| rc-repro does it, on a name that points here | `rc-repro serve --domain rc.example.com --email you@example.com` |
| A TLS proxy on **this** box | `rc-repro serve --bind 127.0.0.1 --allow-host rc.example.com` |
| A proxy **elsewhere** — a lab, Codespaces, ngrok, an LB | `rc-repro serve --bind 0.0.0.0 --allow-host rc.example.com --trust-proxy 10.0.0.1` |
| Nothing, and you accept it | add `--insecure` |

- **`--allow-host`** is a DNS-rebinding guard: whatever is in the address bar has to
  be named here or the request is a 403. Repeatable; `*` accepts anything. It wants a
  bare hostname, and corrects a scheme, port or trailing slash while telling you.
- **`--trust-proxy <address-or-CIDR>`** grants permission to believe that peer's
  `X-Forwarded-Proto`; without it the session cookie cannot be marked `Secure`.
  **A bare `0.0.0.0` is the single address `0.0.0.0/32`, which no peer ever has** —
  write `0.0.0.0/0` for anywhere, or better, the proxy's real address.
- `--insecure` is deliberately not in `serve --help`. It still works, and `serve`
  names it at the one moment it applies: when it refuses to put a password on the wire.

### Keeping it running

```bash
rc-repro serve --domain rc.example.com --email you@example.com --print-service
```

Prints a **systemd unit** and how to install it, plus a `nohup` fallback, and writes
nothing so you can read it first.

---

## 14. For scripts and agents

### `--json`

`up`, `ready`, `down`, `list`, `info`, `doctor`, `loadtest`, `capacity` and `skill`
take `--json`; `capabilities` is JSON already. Every reply is one envelope:

```json
{"schema":"rc-repro.info.v1","contract":1,"rc_repro_version":"0.68.1",
 "generated_at":"…","ok":true,"data":{…},"warnings":[],"error":null}
```

`up` and `ready` **stream**: NDJSON `rc-repro.event.v1` progress lines first, then
exactly one envelope as the **last line** — so read to end of stream and parse the
last line. stdout carries JSON and nothing else; human prose moves to stderr.

`contract` is the wire generation; if you do not recognise it, refuse rather than
guess. New keys may appear in `data` and `warnings` — ignore ones you do not know.

### `rc-repro capabilities`

JSON, needs no container engine, and is the authority for **your build** on which
commands exist, which take `--json`, which stream, the runtime combinations, the seed
profiles, the error codes and the exit codes. Read it instead of assuming a flag
exists because it did in another version.

### For coding agents

```bash
rc-repro skill install        # writes the agent skill file
rc-repro skill status
```

Installs a skill describing this build's contract for an agent to follow, and
`capabilities.skill.current` is false when the installed copy is stale.

---

## Reference

### Commands

| Group | Commands |
|---|---|
| lifecycle | `up` `start` `stop` `restart` `down` `prune` |
| inspect | `list` `info` `logs` `ready` `stats` |
| settings | `use` `config` `env` |
| data | `seed` `config-import` `backup` `backups` `restore` `upgrade` |
| performance (Compose only) | `benchmark` `loadtest` `capacity` |
| REST | `token` `api` `pat` |
| GUI + attribution | `serve` `users` `audit` `chown` |
| HTTPS (Compose only) | `edge` `trust-ca` `tls-status` |
| other | `monitor` `presets` `versions` `doctor` `capabilities` `skill` |

`rc-repro <command> --help` for flags.

### Exit codes

A **domain** failure exits with a code that says which kind it was, so a script can
tell "still coming up" from "fix your environment" without parsing prose.

| Code | Meaning | Do about it |
|---|---|---|
| 0 | success | — |
| 2 | usage / validation | fix the call; retrying unchanged will not help |
| 3 | preflight | the environment is not usable — run `doctor` |
| 4 | not found | no such workspace |
| 5 | not ready | **poll again** — still coming up |
| 7 | create failed | known dead; stop, do not retry in a loop |
| 8 | conflict | the name or port is taken; pick another |
| 1 | anything else | including argument errors caught before a command runs |

Only failures raised by the service layer as a typed error carry a code: a mistyped
flag or an out-of-range `--vus` still exits 1. Branch on `$? -ne 0` for "did it fail",
and on the table above where the command documents it. Code **6** (authority gate) is
reserved and nothing raises it yet.

### How version → MongoDB resolution works

`rc-repro up --version X` (and `rc-repro versions X`) queries
`releases.rocket.chat/<version>/info` — Rocket.Chat's own per-release compatibility
data — and picks the highest supported MongoDB. Offline, or for a release predating
that data, it falls back to the shipped `versions.yaml`; `--offline` forces the
fallback.

The image follows the resolved version: **≥ 8** →
`mongodb/mongodb-community-server` with a fix-permission and one-shot init container,
matching the official compose; **< 8** → `bitnamilegacy/mongodb`. `MONGO_OPLOG_URL`
is emitted only for RC < 8.

### Where state lives

```
~/.rc-repro/                  # override with RC_REPRO_HOME
├── config.yaml               # default workspace, reg_token, bind_host, gui policies
├── users                     # GUI accounts — 0600, scrypt
├── sessions                  # server-side sessions — 0600
├── audit.log                 # who ran what
├── presets/                  # your custom presets
├── backups/  reports/  loadtests/
├── acme/                     # ACME state; dns.env, issued.json
├── edge/                     # the shared edge and one file per route
├── clients/kubernetes/       # rc-repro's own kubeconfig
└── repros/<name>/
    ├── docker-compose.yml    # generated — re-run `up` rather than editing it
    └── repro.json            # the record: version, runtime, ports, seed, notes
```

Config can also come from the environment, which wins over `config.yaml`:
`RC_REPRO_HOME`, `RC_REPRO_REG_TOKEN`, `RC_REPRO_RC_IMAGE`, `RC_REPRO_BIND_HOST` (the
`--bind` flag wins over both).

### Development

```bash
git clone https://github.com/klovekesh37/rc-repro.git && cd rc-repro
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,gui,browser]"
.venv/bin/playwright install chromium
.venv/bin/ruff check .
.venv/bin/python -m pytest -q          # no Docker needed
```

Install all three extras. Without `[gui]` the HTTP and auth tests skip themselves,
and without `[browser]` plus chromium the browser tests do too — both report green
while testing nothing, which is how a broken form POST once shipped past 570 passing
HTTP tests.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the layout and how to add a preset.
