# rc-repro — Low-Level Design (Draft v0.3)

A CLI that launches a **version-matched Rocket.Chat reproduction environment**
in one command — the exact Rocket.Chat image a customer runs, paired with a
compatible MongoDB, plus any backing services a scenario needs (LDAP, SMTP,
reverse proxy, SAML) — so **any support analyst** can reproduce a reported issue
in seconds instead of hand-building it.

- **Language (planned):** Python 3.11+
- **Runtime target (v1):** Docker + `docker compose` (Helm/microservices later)
- **Audience:** the whole support team, on Mac / Windows / Linux
- **Status:** BUILT. This document is the original design/rationale; for current
  usage and the exact command/preset set, see **[README.md](README.md)** (the
  source of truth). Implemented presets: `default`, `airgapped`, `ldap`, `saml`
  (Keycloak). Some ideas explored below (mock-saml, a separate reverse-proxy
  preset, microservices/Helm backends) were deferred or superseded — the README
  reflects what actually ships.

> **What changed in v0.3** (grounded in the team's real `~/RC-Test` workspaces):
> mongo **flavor by RC version** (bitnami-legacy auto-init < 8, official + init
> container ≥ 8); `MONGO_OPLOG_URL` only for RC < 8 (deprecated in 8.x); RC image
> default `registry.rocket.chat/...`; corrected admin-provision env
> (`INITIAL_USER` + `ADMIN_*`) and API flag; presets realigned to real components
> (osixia+phpldapadmin, Keycloak, docker-mailserver/Mailpit); **hybrid networking**
> (localhost default, `--proxy` → managed Traefik + shared net + federation);
> uploads persisted; `snapshot`/`restore` of own fixtures added to the later list.
>
> **v0.2** (team-wide feedback): preflight/collisions, presets inject **backing
> services** via **dict-merge** compose, per-repro volumes + fresh/reuse,
> `info`/`token`, `--reg-token`, `prune`, persistent-workspace `start`/`stop`/`--pin`.

---

## 1. Why this exists (and why not just docker)

Reproducing a customer issue today means manually finding the right MongoDB
version, hand-writing `docker-compose.yml`, wiring the replica set, setting env,
clicking through the setup wizard — and for LDAP/SAML issues, standing up a
whole directory/IdP. It's 20 minutes to an afternoon per ticket, done wrong
half the time, done differently by every analyst.

`rc-repro` earns its place **only** on what docker will never do for you:

| Task | Plain `docker compose` | `rc-repro` |
|---|---|---|
| "RC 6.5.3 needs which Mongo?" | google the matrix, hand-edit | resolved from RC's own release API |
| Compose + replica-set init + env | copy-paste-tweak every time | generated |
| Reproduce an **LDAP** issue | ~an afternoon wiring OpenLDAP + 10 settings | `--preset ldap` — seeded users, ready |
| **Proxy/subpath/TLS** ticket | can't do it on localhost | `--preset reverse-proxy` |
| "What's everyone running?" | cryptic `docker ps` names | `list` by ticket / version / URL |
| Team reproduces **identically** | "works on my machine" | same shipped presets |

**Design rule:** *thin where thinness is fine (executing docker), smart where it
matters (deciding what to run).* If it ever degrades into a docker-flag
passthrough, it has failed.

**Metric to quote to management:** time-to-repro from ~20 min (simple) / hours
(LDAP-class) down to ~1 min of human effort × repros/week × team size.

---

## 2. CLI surface

```
rc-repro up --version <X.Y.Z> [--preset NAME] [--name NAME] [--pin] [--wait] [flags]
rc-repro ready  [--name NAME]       # block until RC serves (polls /api/info)
rc-repro api    [--name NAME] <METHOD> <PATH> [--data JSON]   # authed REST call
rc-repro start  [--name NAME]       # resume a stopped repro (seconds, no rebuild)
rc-repro stop   [--name NAME]       # pause a repro, keep containers + data
rc-repro restart[--name NAME]
rc-repro use    <NAME>              # set the default repro for name-less commands
rc-repro list                       # all repros: version, port, status, URL, ticket, pinned
rc-repro info   [--name NAME]       # URL + admin creds + curl snippet
rc-repro token  [--name NAME]       # mint an API auth token (X-Auth-Token/X-User-Id)
rc-repro logs   [--name NAME] [-f]  # tail logs (attachable to a ticket)
rc-repro down   [--name NAME] [--volumes]
rc-repro presets                    # list available presets
rc-repro versions <X.Y.Z>           # show resolved MongoDB/shell/oplog for a version
rc-repro doctor                     # preflight: docker up? ports free? disk ok?
rc-repro prune                      # remove stopped repros (never pinned) + dangling
```

Commands with `[--name NAME]` fall back to the **default repro** (set by
`--pin` or `use`) when `--name` is omitted — so a daily workspace is just
`rc-repro start` / `rc-repro stop`.

### `up` flags

| Flag          | Default                    | Purpose                                                   |
|---------------|----------------------------|-----------------------------------------------------------|
| `--version`   | *(required)*               | RC version to reproduce, e.g. `6.5.3`                     |
| `--preset`    | `default`                  | Scenario bundle to apply (see §6)                         |
| `--name`      | derived (`rc6-5-3`)        | Handle for this repro — **use the ticket id**             |
| `--port`      | first free ≥ 3000          | Host port for Rocket.Chat                                 |
| `--root-url`  | `http://localhost:<port>`  | Override `ROOT_URL`                                       |
| `--proxy`     | `false`                    | Front with managed Traefik → `https://<name>.rc.localhost` + shared net (§7.2) |
| `--rc-image`  | `rocketchat/rocket.chat`   | Override the RC image repo                                |
| `--mongo`     | resolved automatically     | Force a MongoDB tag (escape hatch)                        |
| `--reg-token` | from config / none         | Cloud registration token → workspace syncs its EE license |
| `--pin`       | `false`                    | Mark persistent: becomes the default, protected from `prune` |
| `--fresh`     | `false`                    | Wipe this repro's volume and start clean                  |
| `--offline`   | `false`                    | Skip the live version lookup                              |
| `--no-pull`   | `false`                    | Don't pull images first                                   |
| `--force`     | `false`                    | Overwrite an existing repro of the same name             |

### Example flows

```bash
# Reproduce exactly what the customer reported, tagged to the ticket
rc-repro up --version 7.4.1 --name acme-1234

# Reproduce an LDAP login problem — directory + seeded users come up too
rc-repro up --version 6.5.3 --preset ldap --name bcorp-5678
#  → log in as alice / alicepass (an LDAP user), no manual wiring

# Reproduce a proxy/subpath issue localhost can't show
rc-repro up --version 7.4.1 --preset reverse-proxy

# See everything, grab API access, tear down
rc-repro list
rc-repro token --name acme-1234
rc-repro down --name acme-1234 --volumes
```

---

## 3. Architecture

```
                         ┌─────────────────────────────────────────────┐
                         │                   CLI (Typer)                │
                         │  up·down·list·info·token·logs·presets·       │
                         │  versions·doctor·prune                       │
                         └─────────────────────────────────────────────┘
                                            │
   ┌───────────┬───────────┬───────────┬───┴───────┬───────────┬────────────┐
   ▼           ▼           ▼           ▼           ▼           ▼            ▼
┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐  ┌───────────┐
│versions│ │presets │ │compose │ │ runner │ │preflight│ │  rcapi │  │  config   │
│resolver│ │loader  │ │builder │ │(docker+│ │(checks) │ │(REST:  │  │(paths/    │
│        │ │(+svcs) │ │(merge) │ │ state) │ │         │ │ token) │  │ defaults) │
└────────┘ └────────┘ └────────┘ └────────┘ └────────┘ └────────┘  └───────────┘
     │                     ▲          │
     ▼                     │          ▼
 releases.rocket.chat  preset frags  ~/.rc-repro/repros/<name>/
 + versions.yaml       (services)    docker-compose.yml + repro.json
                                          │
                                          ▼  subprocess
                                     `docker compose ...`
```

**Responsibilities (one line each):**
- **versions** — RC version → (MongoDB tag, shell, oplog?), live + fallback.
- **presets** — name → `{env, services, rocketchat-patch, depends_on}` bundle.
- **compose** — base dict + preset fragments → merged `docker-compose.yml`.
- **runner** — on-disk state; shells out to `docker compose`; reads status.
- **preflight** — docker running? port free? disk ok? collisions?
- **rcapi** — small REST client for `token` (login → auth headers) and health.
- **config** — home dir, defaults, stored `reg_token`, image repos.
- **cli** — parse, orchestrate, human-readable output.

---

## 4. Python module layout

```
rc-repro/
├── pyproject.toml            # packaging + `rc-repro` console entry point
├── README.md
├── DESIGN.md                 # this file
└── rc_repro/
    ├── __init__.py
    ├── __main__.py           # python -m rc_repro → cli.app()
    ├── cli.py                # Typer app; one function per command
    ├── config.py             # Paths, defaults, RC_REPRO_HOME, stored reg_token
    ├── versions.py           # Resolver (live lookup + fallback map)
    ├── presets.py            # Preset dataclass + loader (built-in + user)
    ├── compose.py            # Spec + dict-merge builder → yaml
    ├── runner.py             # docker subprocess wrappers + metadata I/O
    ├── preflight.py          # doctor checks + up preflight
    ├── rcapi.py              # minimal Rocket.Chat REST client (token/health)
    └── data/
        ├── versions.yaml     # curated fallback map
        └── presets/
            ├── default.yaml
            ├── open-signup.yaml
            ├── ldap.yaml
            ├── smtp.yaml
            ├── reverse-proxy.yaml
            └── saml.yaml
```

**Deps:** `typer` (CLI), `pyyaml` (config), `requests` (live lookup + `token`),
`packaging` (semver compare). Docker via `subprocess` — no docker SDK. All data
files shipped via `importlib.resources`.

---

## 5. Version resolution (unchanged core, still the heart)

Two-tier: authoritative live lookup + offline fallback.

### 5.1 Live (authoritative)
```
GET https://releases.rocket.chat/<version>/info
→ { "compatibleMongoVersions": ["5.0","6.0","7.0"], "nodeVersion": ..., "lts": true }
```
1. GET (≈5s timeout). 2. Pick the **highest** `compatibleMongoVersions` as the
Mongo tag. 3. `mongo_flavor` = `bitnami-legacy` if RC major < 8 else `official`
(see §7.1). 4. `mongo_shell` = `mongosh` if Mongo major ≥ 5 else `mongo` (only
the official flavor uses an init shell). 5. `oplog` = `True` for RC major < 8 →
emit `MONGO_OPLOG_URL`; omit it for RC ≥ 8 (deprecated/ignored — 8.x uses change
streams). 6. Any failure → fallback map.

### 5.2 Fallback map (`data/versions.yaml`, verified against the endpoint)

| RC constraint    | Mongo | flavor         | shell   | oplog | note                    |
|------------------|-------|----------------|---------|-------|-------------------------|
| `>=8.2.0`        | 8.0   | official       | mongosh | no    | RC 8.2+ → Mongo 8.0     |
| `>=8.0.0 <8.2.0` | 8.2   | official       | mongosh | no    | RC 8.0/8.1 → Mongo 8.2  |
| `>=7.0.0 <8.0.0` | 7.0   | bitnami-legacy | —       | yes   | 5.0/6.0/7.0 supported   |
| `>=6.0.0 <7.0.0` | 6.0   | bitnami-legacy | —       | yes   | 4.4/5.0/6.0 supported   |
| `>=5.0.0 <6.0.0` | 5.0   | bitnami-legacy | —       | yes   | 4.2/4.4/5.0 supported   |
| `>=4.0.0 <5.0.0` | 4.4   | bitnami-legacy | —       | yes   | needs MONGO_OPLOG_URL   |
| `>=3.0.0 <4.0.0` | 4.0   | bitnami-legacy | —       | yes   | needs MONGO_OPLOG_URL   |
| `<3.0.0`         | 3.6   | bitnami-legacy | —       | yes   | legacy, best-effort     |

(`shell` only applies to the `official` flavor's init container; bitnami-legacy
auto-inits the replica set, so no shell is invoked.)

> **Mongo flavor rule (from the team's real composes):** RC major **< 8** uses
> `bitnamilegacy/mongodb:<tag>` (auto-inits the replica set via
> `MONGODB_REPLICA_SET_MODE=primary`, no init container); RC major **≥ 8** uses
> official `docker.io/mongo:<tag>` + a one-shot init container (§7.1). Docker
> Hub `mongo:` has every tag `3.6`→`8.2`; bitnamilegacy covers the older line.

```python
@dataclass
class Resolved:
    rc_version: str; rc_image: str; mongo_tag: str
    mongo_flavor: str      # "bitnami-legacy" (RC<8) | "official" (RC>=8)
    mongo_shell: str; oplog: bool; source: str; note: str
    # oplog == RC major < 8 → emit MONGO_OPLOG_URL (deprecated in 8.x).

def resolve(version: str, *, offline: bool = False) -> Resolved: ...
```

Default RC image is `registry.rocket.chat/rocketchat/rocket.chat` (what the team
uses); `--rc-image rocketchat/rocket.chat` switches to Docker Hub for very old
tags.

---

## 6. Presets — now with backing services (the big change)

A preset is a **scenario**, not just env. It can contribute: env vars merged
into RC, **extra services** (LDAP/SMTP/etc.), an optional raw patch to the RC
service (ports/labels for proxy scenarios), and `depends_on` so RC waits for
them.

### 6.1 Schema (`data/presets/*.yaml`)

```yaml
name: ldap
description: Rocket.Chat wired to a seeded OpenLDAP directory. Log in as alice/alicepass.
requires_license: false          # informational; EE-gated scenarios set true
env:                             # shorthand → merged into rocketchat.environment
  OVERWRITE_SETTING_LDAP_Enable: "true"
  OVERWRITE_SETTING_LDAP_Host: openldap
  OVERWRITE_SETTING_LDAP_Port: "1389"
  OVERWRITE_SETTING_LDAP_BaseDN: "dc=example,dc=org"
  OVERWRITE_SETTING_LDAP_Authentication: "true"
  OVERWRITE_SETTING_LDAP_Authentication_UserDN: "cn=admin,dc=example,dc=org"
  OVERWRITE_SETTING_LDAP_Authentication_Password: "adminpassword"
  OVERWRITE_SETTING_LDAP_User_Search_Field: "uid"
services:                        # extra compose services, merged in verbatim
  openldap:
    image: bitnami/openldap:2.6
    environment:
      LDAP_ROOT: "dc=example,dc=org"
      LDAP_ADMIN_USERNAME: admin
      LDAP_ADMIN_PASSWORD: adminpassword
      LDAP_USERS: "alice,bob"
      LDAP_PASSWORDS: "alicepass,bobpass"
    restart: unless-stopped
depends_on: [openldap]           # RC waits for these too
rocketchat: {}                   # optional raw patch merged into the RC service
```

`env` is sugar for `rocketchat.environment`; `rocketchat:` is the general escape
hatch (the **reverse-proxy** preset uses it to drop the published port and add
Traefik labels, and sets `ROOT_URL` to the proxied domain/subpath).

### 6.2 Shipped presets — aligned to the team's actual workspaces

**v1 (build these):**

| Preset    | Brings up (real components used today)                    | Reproduces                        |
|-----------|-----------------------------------------------------------|-----------------------------------|
| `default` | RC + Mongo, admin auto-created, wizard skipped            | anything                          |
| `ldap`    | `osixia/openldap` + `osixia/phpldapadmin`, seeded LDIF    | LDAP auth/sync tickets            |
| `saml`    | `quay.io/keycloak/keycloak` (realm import + IdP certs)    | SAML SSO login                    |
| `email`   | `docker-mailserver` + `roundcube` webmail                 | email/invite/reset; inspect mail  |

> `email` ships a **lighter `--variant mailpit`** option (single Mailpit
> container) for quick send-only checks, vs the full mailserver+Roundcube for
> IMAP/SMTP round-trip realism.

**Later (seams designed now, not built in v1):**

| Preset / feature      | From workspace | Notes                                             |
|-----------------------|----------------|---------------------------------------------------|
| `open-signup`         | —              | trivial env-only; easy add                        |
| `airgapped`           | `airgapped`    | `OVERWRITE_SETTING_Cloud_Url/Client_Id/Secret`    |
| `clamav`              | `clamAV`       | `clamav/clamav` sidecar                           |
| `reverse-proxy`/TLS   | `Perm`         | folded into the `--proxy` model (§7.2)            |
| microservices         | `multiInst`    | NATS + monitoring; `--deploy microservices`       |
| k8s / Helm            | `k3s-micro`    | Helm/ArgoCD; `--backend helm`                     |

Basic SAML/LDAP login is Community; **advanced** sync (roles/groups) is EE → use
`--reg-token` (§9).

### 6.3 Precedence & extensibility
`~/.rc-repro/presets/<name>.yaml` **overrides** the built-in of the same name.
This is how analysts tweak a scenario or add team-specific ones; later a shared
team preset git repo can seed this dir.

```python
@dataclass
class Preset:
    name: str; description: str
    env: dict[str, str]
    services: dict[str, dict]
    rocketchat: dict            # raw patch for the RC service
    depends_on: list[str]
    requires_license: bool
    source: str                 # "built-in" | file path

def load(name, user_dir) -> Preset: ...
def list_presets(user_dir) -> list[Preset]: ...
```

---

## 7. Compose building (dict-merge, not template)

Because presets inject services, `compose.py` builds a Python dict and
`yaml.dump`s it — deep-merging preset fragments — instead of rendering a fixed
template.

```python
@dataclass
class Spec:
    project_name: str; rc_image: str; rc_tag: str
    mongo_tag: str; mongo_shell: str; oplog: bool
    root_url: str; host_port: int
    reg_token: str | None
    preset: Preset

def build(spec: Spec) -> dict: ...      # returns the compose document
def to_yaml(doc: dict) -> str: ...
```

Algorithm:
1. Start from the **base** dict: `mongodb`, `mongo-init`, `rocketchat`,
   `volumes: {mongodb_data}` (see §7.1).
2. Set RC env: `ROOT_URL`, `PORT`, `DEPLOY_METHOD=docker`, `DEPLOY_PLATFORM`,
   `MONGO_URL`, `MONGO_OPLOG_URL` (RC < 8 only; deprecated in 8.x), `REG_TOKEN` (if set),
   `ALLOW_UNSAFE_QUERY_AND_FIELDS_API_PARAMS=true`, and the admin-provision vars
   `INITIAL_USER=yes` + `ADMIN_USERNAME/ADMIN_NAME/ADMIN_EMAIL/ADMIN_PASS`.
   Pick the mongo service block by `mongo_flavor` (§7.1).
3. **Deep-merge** `preset.services` into `services`.
4. Merge `preset.env` into `services.rocketchat.environment`.
5. Deep-merge `preset.rocketchat` patch into `services.rocketchat`.
6. Extend `services.rocketchat.depends_on` with `preset.depends_on`
   (each `{condition: service_started}`).

### 7.1 Base services (two mongo flavors)

The RC service is identical either way; only the mongo block differs.

**Common RC service:**
```yaml
name: rcrepro-<name>
services:
  rocketchat:
    image: registry.rocket.chat/rocketchat/rocket.chat:<rc_tag>
    restart: always
    environment:
      ROOT_URL: <root_url>              # http://localhost:<port>  (or proxied, §7.2)
      PORT: "<container_port>"
      DEPLOY_METHOD: docker
      DEPLOY_PLATFORM: ""
      MONGO_URL: "mongodb://mongodb:27017/rocketchat?replicaSet=rs0"
      # MONGO_OPLOG_URL only when RC < 8:
      #   mongodb://mongodb:27017/local?replicaSet=rs0
      REG_TOKEN: "<reg_token or empty>"
      ALLOW_UNSAFE_QUERY_AND_FIELDS_API_PARAMS: "true"
      INITIAL_USER: "yes"
      ADMIN_USERNAME: admin
      ADMIN_NAME: Admin
      ADMIN_EMAIL: admin@example.com
      ADMIN_PASS: admin123
      # + preset env (OVERWRITE_SETTING_* etc.)
    depends_on: [ mongodb ]             # + mongo-init for the official flavor
    ports: ["<host_port>:<container_port>"]   # dropped when --proxy (§7.2)
    volumes:
      - uploads:/app/uploads            # persisted so files can be inspected
volumes:
  mongodb_data: { driver: local }
  uploads: { driver: local }
```

**Flavor A — `bitnami-legacy` (RC < 8): auto-inits, no init container.**
```yaml
  mongodb:
    image: docker.io/bitnamilegacy/mongodb:<tag>
    restart: always
    volumes: [ "mongodb_data:/bitnami/mongodb" ]
    environment:
      MONGODB_REPLICA_SET_MODE: primary          # ← auto rs.initiate()
      MONGODB_REPLICA_SET_NAME: rs0
      MONGODB_ADVERTISED_HOSTNAME: mongodb
      ALLOW_EMPTY_PASSWORD: "yes"
```

**Flavor B — `official` (RC ≥ 8): mongo + one-shot init container.**
```yaml
  mongodb:
    image: docker.io/mongo:<tag>
    command: ["mongod","--replSet","rs0","--bind_ip_all"]
    volumes: [ "mongodb_data:/data/db" ]
    healthcheck:
      test: ["CMD-SHELL","<shell> --quiet --eval 'db.adminCommand({ping:1}).ok' | grep -q 1 || exit 1"]
      interval: 10s; timeout: 10s; retries: 30; start_period: 20s
  mongo-init:
    image: docker.io/mongo:<tag>
    depends_on: { mongodb: { condition: service_healthy } }
    restart: "no"
    entrypoint: [<shell>, --host, mongodb:27017, --quiet, --eval,
      "try { rs.status() } catch (e) { rs.initiate({_id:'rs0',members:[{_id:0,host:'mongodb:27017'}]}) }"]
```

**Why the replica set:** RC requires MongoDB as a replica set. Bitnami's
`REPLICA_SET_MODE=primary` initiates it automatically (why the team's RC ≤ 7
composes need no init step); the official image doesn't, so flavor B adds a
one-shot `mongo-init` that RC waits on.

### 7.2 Networking: localhost default, `--proxy` opt-in

Two modes, matching how the team works vs. the simplest thing that boots.

**Default — plain localhost (zero setup, cross-platform):**
- RC published on `<host_port>` (auto-picked from 3000+); `ROOT_URL=http://localhost:<port>`.
- No Traefik, no hosts file, no cert trust. This is the fast path.

**`--proxy` — managed Traefik + shared network (matches `Perm`/`openldap`):**
- rc-repro manages one shared Traefik + a shared docker network as **infra**
  (`rc-repro proxy up` / lifecycle), created on first `--proxy` use.
- The repro drops its published port and instead gets Traefik labels + joins the
  shared network; `ROOT_URL=https://<name>.rc.localhost` (Chrome resolves
  `*.rc.localhost` → 127.0.0.1; no `/etc/hosts` edits).
- TLS via a local CA Traefik serves (self-signed; `doctor` explains trusting it).
- Because all `--proxy` repros share one network, **instances can reach each
  other → federation testing** (replacing the manual external `rc_federation_net`).

The preset `rocketchat:` patch (§6.1) is how proxy labels get applied; the
deferred `reverse-proxy`/TLS scenarios collapse into this one flag.

---

## 8. State, volumes & the fresh/reuse rule

Root `~/.rc-repro` (override `RC_REPRO_HOME`). Each repro is isolated.

```
~/.rc-repro/
├── config.yaml                  # optional: default reg_token, rc_image, etc.
├── presets/                     # user/team presets (override built-ins)
└── repros/
    └── acme-1234/
        ├── docker-compose.yml
        └── repro.json           # metadata (name, ticket, version, mongo, port, preset, created)
```

**Volumes are per-repro** (compose project `rcrepro-<name>` → volume
`rcrepro-<name>_mongodb_data`), so repros never see each other's data.

**Fresh vs reuse rule:**
- **New name → fresh** (no volume exists yet).
- **Re-`up` an existing name → reuse its volume** (keep the state you built).
- `--fresh` → `down -v` then recreate (clean slate on demand).
- `down` removes containers but **keeps the volume and the record** (shows as
  `down` in `list`) so `up` can bring the exact repro back with its data.
- `down --volumes` deletes the data **and forgets the repro** (removed from
  `list`) — there's nothing left to restore.
- `prune` sweeps every `down` leftover (kept volumes + records), skipping pinned
  and running/`stopped` repros.

This satisfies both "give me a clean box" and "don't nuke the broken state I
spent an hour reproducing."

### 8.1 Two modes: ephemeral repros vs a persistent workspace

Creating a fresh instance per ticket is right for reproduction, but exhausting
for everyday poking. So a repro is a **long-lived object you create once and
pause/resume** — not something you must recreate each session.

| Verb        | docker equivalent            | Effect                                            |
|-------------|------------------------------|---------------------------------------------------|
| `up`        | `compose up -d` (idempotent) | Create; if it already exists & is stopped, **start** it |
| `stop`      | `compose stop`               | Pause: containers + data kept, resume in seconds  |
| `start`     | `compose start`              | Resume without rebuild / re-pull / re-provision   |
| `restart`   | `compose restart`            | Bounce it                                          |
| `down`      | `compose down`               | Remove containers (volume kept unless `--volumes`)|

**Ephemeral (per-ticket):**
```bash
rc-repro up --version 7.4.1 --name acme-1234    # reproduce
rc-repro down --name acme-1234 --volumes         # done, wipe
```

**Persistent (your daily driver) — create once, then just start/stop:**
```bash
rc-repro up --version 8.2.0 --name main --pin    # ONE time
rc-repro start                                    # each morning (acts on default)
rc-repro stop                                     # each evening — nothing lost
```

**Pinning & the default repro:**
- `--pin` marks a repro persistent → it is set as the **default**, and `prune`
  will never remove it.
- Commands with `[--name]` omitted act on the default (`use <name>` switches it).
- `config.yaml` stores `default_repro`.
- **`up` is idempotent:** on an existing, stopped repro it just `start`s it (no
  recreate) unless `--fresh` is given. So a mistyped repeat `up` never nukes
  your workspace.

---

## 9. Enterprise license (cloud registration)

Analysts get EE by **registering the workspace to Rocket.Chat Cloud**, which
then **syncs the assigned license down**. rc-repro plumbs the registration
token:

- `--reg-token <token>` (or `reg_token:` in `~/.rc-repro/config.yaml`) sets
  `REG_TOKEN` on the RC service → the workspace self-registers on first boot →
  cloud syncs its license automatically.
- Requires internet; the token comes from the cloud console.
- Presets that need EE set `requires_license: true` and `up` prints a hint if no
  token is configured.
- Alternative (documented, no automation): register manually in Admin → Cloud.

---

## 10. API testing helpers (`info`, `token`)

An env is useless for API repros unless auth is instant.
- Presets auto-create admin (creds known) and skip the wizard.
- `rc-repro info --name X` → URL, admin creds, and a ready `curl` snippet.
- `rc-repro token --name X` → `rcapi` POSTs `/api/v1/login`, prints
  `-H "X-Auth-Token: …" -H "X-User-Id: …"` so you're hitting the REST API in
  seconds. (Waits for RC health first.)

---

## 10b. Readiness (`ready` / `--wait`)

RC's first boot is minutes (image pull → replica-set init → migrations → HTTP
up), so "container started" ≠ "serving". `ready` polls `GET <root_url>/api/info`
(public, returns the version) every ~3s:

- container not running → *starting*; connection refused → *booting*; HTTP 200 →
  **ready**; container `exited` → **fail fast** + print last log lines.
- Default timeout ~300s; exit 0 on ready, non-zero on timeout/crash.
- Because `/api/info` returns the version, `ready` also asserts the running
  container really is the requested version.
- `up --wait` runs this after start; `token`/`api` call it implicitly first.

## 11. Preflight & `doctor`

`doctor` runs standalone; a subset runs automatically before `up`:
- **Docker running?** `docker info` → else "start Docker Desktop", no stack trace.
- **Port free?** check known repros + try to bind + scan `docker ps` published ports.
- **Name collision?** existing repro → reuse / `--force`.
- **Same version already up** under another name? → inform, offer second on next port.
- **Stray RC containers** (`docker ps --filter ancestor=rocketchat/rocket.chat`) → warn.
- **Disk** headroom for a ~1.5 GB image pull → warn if low.

---

## 12. Step-by-step: `up`

```
1. Validate --version is real semver.
2. preflight: docker up? port free? name/version collisions? (warn/stop as needed)
3. resolve(version, offline) -> Resolved (+ apply --rc-image/--mongo overrides).
4. presets.load(preset) -> Preset.
5. name = --name or derived; exists & not --force -> error; --fresh -> mark wipe.
6. port = --port or first free >= 3000 (skip used).
7. spec = Spec(...); doc = compose.build(spec); yaml = to_yaml(doc).
8. runner.write(name, yaml, metadata).
9. print plan (RC image, mongo+source, preset+services, URL, creds).
10. if --fresh: runner.down(name, volumes=True).
11. runner.up(name, pull=not --no-pull).   # docker compose pull + up -d
12. print success + URL + creds + `info`/`token`/`logs` hints.
```

docker invocations (cwd = repro workspace): `up` → `pull` + `up -d`; `down` →
`down [-v]`; `logs` → `logs [-f]`; `list` → `ps --format …` per repro.

---

## 13. Team, portability & hygiene

- **Cross-platform:** Python + `subprocess docker compose` works on Mac/Win/Linux;
  no shell-isms. Paths via `pathlib`.
- **Standardization:** identical shipped presets → a repro is one copy-pasteable
  command in a ticket. Personal/team overrides in `~/.rc-repro/presets`.
- **Distribution:** `pipx install git+<repo>`; console entry point `rc-repro`.
- **Disk hygiene:** RC images ~1.5 GB, volumes accumulate. `down --volumes`,
  `prune` (stopped repros + dangling volumes/images), disk warning in `doctor`.
- **Ticket evidence:** `logs --name X > bundle.log` to attach to Zoho.

---

## 14. Scope

**v1:** `up/start/stop/restart/use/down/list/info/token/logs/presets/versions/
doctor/prune`; version-matched compose repros with **flavor-by-version** mongo
(bitnami-legacy < 8, official ≥ 8); ephemeral **and** pinned persistent
workspaces (create-once, start/stop); presets `default, ldap, saml, email`;
localhost by default with `--proxy` (managed Traefik + shared net); live version
resolution + fallback; EE via `--reg-token`; per-repro volumes (incl. uploads)
with fresh/reuse.

**Later (seams designed now):** `open-signup`, `airgapped`, `clamav` presets;
`snapshot`/`restore` of a repro's own fixtures (mongodump + uploads, replacing
`backup.sh`); `--seed` sample data via REST; **microservices** (`--deploy
microservices` + NATS + monitoring, from `multiInst`); **Helm/k8s** backend
(`--backend helm`, from `k3s-micro`) — `versions`/`presets` stay
backend-agnostic, only `compose`/`runner` gain a `helm` sibling; shared team
preset git repo.

**Explicitly out of scope:** restoring a *customer's* MongoDB dump (customers
don't share their databases; `snapshot`/`restore` is for the analyst's own
prepared states only).

---

## 15. Open questions before building

1. **CLI framework:** Typer (recommended) vs Click.
2. **`saml` via Keycloak:** ship a pre-built realm export + IdP cert (from your
   `7.0.0_P/rc-keycl.xml` + `saml.crt/pem`) so login works out of the box, vs
   requiring manual realm setup. Recommend shipping the realm import.
3. **`email` default variant:** full `docker-mailserver` + Roundcube (IMAP/SMTP
   round-trip, matches `Perm`) vs lighter Mailpit as the default with mailserver
   as `--variant full`. Recommend Mailpit default, mailserver opt-in.
4. **bitnami-legacy longevity:** it's the deprecated image line — acceptable for
   RC < 8 repros now, but we should watch for it disappearing and be ready to
   pin a mirror or fall back to official `mongo:` for those versions too.
5. **Team preset sharing:** file-copy for v1, git URL in `config.yaml` later.
```
