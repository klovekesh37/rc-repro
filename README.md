# rc-repro

Spin up a **version-matched Rocket.Chat reproduction environment** in one command.

Give it a Rocket.Chat version; rc-repro resolves the compatible MongoDB, generates
a Docker Compose stack, boots it, and (with a preset) wires up backing services
like LDAP or SAML — so you can reproduce a customer's issue on their *exact*
version in minutes instead of hand-building it.

```bash
rc-repro up --version 8.5.1 --wait          # a clean RC 8.5.1 + matching Mongo
rc-repro up --version 6.5.3 --preset ldap   # RC 6.5.3 + seeded OpenLDAP
rc-repro up --version 8.4.1 --preset saml   # RC 8.4.1 + a Keycloak SAML IdP
```

## Quickstart

```bash
pipx install git+https://github.com/klovekesh37/rc-repro   # needs Docker + Python 3.11+
rc-repro doctor                                           # confirm Docker/ports/disk are OK
rc-repro up --version 8.5.1 --name test --wait            # boots, waits until serving
# open the printed URL, log in as admin / admin123
rc-repro down --name test --volumes                       # clean up
```

---

## Why

Reproducing a support ticket usually means: figure out which MongoDB the customer's
version needs, hand-write a compose file, wire the replica set, set env vars, click
through the setup wizard, and — for LDAP/SAML tickets — stand up a whole directory
or identity provider. That's 20 minutes to an afternoon per ticket, done slightly
differently by every analyst.

rc-repro does the *deciding* (what to run) so Docker can do the *running*. It's the
fast path for **version-, config-, auth-, API-, and SSO-class** tickets.

---

## Requirements

- **Docker Desktop** (or Docker Engine) with `docker compose` v2 — must be running.
- **Python 3.11+**.
- Internet (for pulling images and the live version lookup; `--offline` skips the latter).

> On Apple Silicon, only the Bitnami MongoDB image (used for MongoDB < 8, i.e.
> RC < 8) is amd64-only and runs under emulation — those boots are a bit slower.
> Everything else (Rocket.Chat, official MongoDB 8+, OpenLDAP, Keycloak) is
> arm64-native.

## Install

```bash
pipx install git+https://github.com/klovekesh37/rc-repro        # recommended
# or, for local development:
git clone https://github.com/klovekesh37/rc-repro.git && cd rc-repro
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Verify: `rc-repro doctor` (preflight — checks Docker, Compose, disk, ports, connectivity).

---

## Everyday use

```bash
# Reproduce a ticket on the customer's exact version, tagged to the ticket id
rc-repro up --version 7.4.1 --name acme-1234 --wait

# See what's running
rc-repro list

# Grab API access / hit the REST API (see "API testing" below)
rc-repro token --name acme-1234
rc-repro api   --name acme-1234 GET /api/v1/me

# Tail logs (attach to a ticket)
rc-repro logs --name acme-1234 -f

# Tear it down (keep data) / delete it entirely
rc-repro down --name acme-1234
rc-repro down --name acme-1234 --volumes
```

`--wait` blocks until Rocket.Chat is actually serving (and skips the setup wizard).
Every repro auto-creates an admin: **`admin` / `admin123`**.

### A persistent "daily driver"

You don't have to recreate an instance every time. Pin one and just start/stop it:

```bash
rc-repro up --version 8.5.1 --name main --pin    # once
rc-repro start        # each morning (acts on the pinned default)
rc-repro stop         # each evening — nothing lost, resumes in seconds
```

| Verb | What it does |
|------|--------------|
| `up` | create; if it already exists, bring it back up (data intact) |
| `stop` / `start` | pause / resume (containers kept) |
| `down` | remove containers, **keep** data + record (shows as `down`) |
| `down --volumes` | delete data and forget the repro |
| `prune` | remove all `down` repros (skips pinned & running) |

---

## Presets

A preset turns a bare RC into a scenario. `rc-repro presets` lists them.

| Preset | Brings up | Reproduces |
|--------|-----------|------------|
| `default` | RC + Mongo, admin auto-created | anything |
| `airgapped` | RC with Cloud endpoints stubbed out | offline / disconnected-workspace behaviour |
| `ldap` | OpenLDAP seeded with users + a group | LDAP auth / sync tickets |
| `saml` | a real Keycloak IdP (SAML realm + users) | SAML SSO login |

**Parameters** (some presets accept them via `--set`):

```bash
rc-repro up --version 8.5.1 --preset ldap --set users=5        # 5 LDAP users
rc-repro up --version 8.5.1 --preset ldap --set users=130000   # scale/perf repro
rc-repro up --version 8.5.1 --preset saml --set users=20       # 20 Keycloak users
```

For `ldap` and `saml`, log in as **`user1` / `user1`** (…`userN`/`userN`).

> **Keycloak console** (SAML preset): `http://localhost:8081` (`admin`/`admin`).
> Your test users live in the **`rcrepro`** realm — the console opens on `master`,
> so switch the realm dropdown (top-left) or open
> `http://localhost:8081/admin/master/console/#/rcrepro/users`. rc-repro prints this
> after `up` and in `rc-repro info`.

### Custom / team presets

Drop a YAML file in `~/.rc-repro/presets/<name>.yaml` to add or override a preset:

```yaml
name: my-scenario
description: What this reproduces.
env:                       # merged into the rocketchat service
  OVERWRITE_SETTING_Some_Setting: "true"
services:                  # optional extra compose services
  my-sidecar:
    image: some/image:tag
depends_on: [my-sidecar]
```

---

## API testing

For reproducing REST-API tickets (auth is set up so you can call endpoints instantly):

```bash
rc-repro token --name acme-1234                       # prints -H auth headers
rc-repro api   --name acme-1234 GET /api/v1/me
rc-repro api   --name acme-1234 POST /api/v1/users.create -d '{"name":"…", ...}'

# Mirror a customer's Personal Access Token (with "Ignore 2FA"):
rc-repro api --name acme-1234 --pat  POST /api/v1/users.update -d '{"userId":"…","data":{"name":"X"}}'
# Get past a 2FA-guarded admin endpoint:
rc-repro api --name acme-1234 --2fa  POST /api/v1/settings/<id> -d '{"value":true}'
```

---

## How version → MongoDB resolution works

`rc-repro up --version X` (and `rc-repro versions X`) resolves the MongoDB pairing:

1. **Live:** queries `releases.rocket.chat/<version>/info` (Rocket.Chat's own
   per-release compatibility data) and picks the highest supported MongoDB.
2. **Fallback:** if offline / the release predates that data, uses the shipped
   `rc_repro/data/versions.yaml` map.

The MongoDB image is chosen by the resolved **Mongo** version: **Mongo ≥ 8** →
official `mongo:` (multi-arch) + a one-shot init container; **Mongo < 8** →
`bitnamilegacy/mongodb` (auto-inits the replica set — Bitnami's public images were
deprecated and moved to the `bitnamilegacy` namespace). `MONGO_OPLOG_URL` is
emitted only for RC < 8 (deprecated in 8.x).

```bash
rc-repro versions 6.5.3     # show what would be used, without launching
```

---

## Where things live

```
~/.rc-repro/
├── config.yaml            # default_repro, optional reg_token / rc_image
├── presets/               # your custom/team presets
└── repros/<name>/
    ├── docker-compose.yml # generated — don't hand-edit; re-run `up`
    ├── repro.json         # metadata
    └── …                  # preset-generated files (e.g. LDIF, realm JSON)
```

Override the root with `RC_REPRO_HOME`.

---

## Commands

| | |
|---|---|
| `up` | create & start a version-matched repro |
| `ready` | block until RC is serving (`/api/info`) |
| `start` / `stop` / `restart` | lifecycle without recreating |
| `down` | remove containers (`--volumes` deletes data + record) |
| `use <name>` | set the default repro for name-less commands |
| `list` | all repros: version, port, state, URL |
| `info` | URL, admin creds, curl snippet, preset notes |
| `token` / `api` / `pat` | REST auth + calls |
| `logs` | tail a repro's logs |
| `presets` | list available presets |
| `versions <X.Y.Z>` | show the resolved MongoDB pairing |
| `doctor` | preflight checks (Docker, Compose, disk, ports, connectivity) |
| `prune` | delete all `down` repros |

Run `rc-repro <command> --help` for flags.

---

## Development

```bash
python -m pip install --upgrade pip   # editable installs need pip >= 21.3
pip install -e ".[dev]"
pytest                     # pure-logic tests (no Docker needed)
```

Architecture and design rationale are in [`DESIGN.md`](DESIGN.md). Module layout:

| Module | Responsibility |
|--------|----------------|
| `cli.py` | Typer commands, orchestration, output |
| `versions.py` | RC version → MongoDB pairing (live + fallback) |
| `presets.py` | load presets (static YAML + dynamic builders) |
| `ldap_preset.py` / `saml_preset.py` | generate the LDAP / Keycloak scenarios |
| `compose.py` | build the docker-compose document |
| `runner.py` | on-disk state + `docker compose` invocations |
| `rcapi.py` | minimal Rocket.Chat REST client (readiness, auth, settings) |
| `config.py` | paths, constants, persisted config |
```
