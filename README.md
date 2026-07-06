# rc-repro

Spin up a **version-matched Rocket.Chat reproduction environment in one command** —
the right Rocket.Chat version paired with a compatible MongoDB, plus optional
backing services (LDAP, SAML) and sample data. Reproduce a customer's issue on
their *exact* version in minutes instead of hand-building a compose stack.


## 1. Prerequisites

- **Docker Desktop** (or Docker Engine) with `docker compose` v2 — **must be running**.
- **Python 3.11+**.
- Internet access (to pull images and look up version compatibility).

> **Apple Silicon note:** only the Bitnami MongoDB image (used for MongoDB < 8,
> i.e. RC < 8) is amd64-only and runs under emulation, so those boots are slower.
> Everything else (Rocket.Chat, official MongoDB 8+, OpenLDAP, Keycloak) is native.

## 2. Install

**Recommended — with [pipx](https://pipx.pypa.io) (isolated, adds `rc-repro` to your PATH):**

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

Then confirm your machine is ready:

```bash
rc-repro doctor
```

`doctor` checks Docker, Compose, disk, connectivity and ports. Fix any ✗ before continuing.

## 3. Your first repro

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

## 4. Everyday commands

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
| `down --volumes` | delete the data and forget the repro |
| `prune` | remove all `down` repros (skips pinned & running) |

**Persistent "daily driver"** — pin one and just start/stop it:

```bash
rc-repro up --version 8.5.1 --name main --pin    # once
rc-repro start        # each morning (acts on the pinned default)
rc-repro stop         # each evening — nothing lost
```

Once a repro is pinned (or set with `rc-repro use <name>`), commands with no
`--name` act on it: `rc-repro start`, `rc-repro logs -f`, etc.

## 5. Presets

A preset turns a bare RC into a scenario. See them with `rc-repro presets`.

| Preset | Brings up | Reproduces |
|--------|-----------|------------|
| `default` | RC + Mongo, admin auto-created | anything |
| `airgapped` | RC with Cloud endpoints stubbed out | offline / disconnected-workspace behaviour |
| `ldap` | OpenLDAP seeded with users + a group | LDAP auth / sync tickets |
| `saml` | a real Keycloak IdP (SAML realm + users) | SAML SSO login |

```bash
rc-repro up --version 8.5.1 --preset ldap
```

**Parameters** (some presets accept `--set`):

```bash
rc-repro up --version 8.5.1 --preset ldap --set users=5        # 5 LDAP users
rc-repro up --version 8.5.1 --preset ldap --set users=130000   # scale/perf repro
rc-repro up --version 8.5.1 --preset saml --set users=20       # 20 Keycloak users
```

For `ldap` and `saml`, log in as **`user1` / `user1`** (…`userN` / `userN`).

> **Keycloak console** (`saml` preset): `http://localhost:8081` (`admin`/`admin`).
> The console opens on the **`master`** realm, but your SAML users live in the
> **`rcrepro`** realm — switch the realm dropdown (top-left), or open Users directly:
> `http://localhost:8081/admin/master/console/#/rcrepro/users`.
> (`rc-repro info` prints this too.)

**Custom / team presets** — drop a YAML file in `~/.rc-repro/presets/<name>.yaml`
(overrides a built-in of the same name):

```yaml
name: my-scenario
description: What this reproduces.
env:                       # merged into the rocketchat service
  OVERWRITE_SETTING_Some_Setting: "true"
services:                  # optional extra compose services
  my-sidecar: { image: some/image:tag }
depends_on: [my-sidecar]
```

## 6. Sample data (`--seed`)

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

Seed users are `alice`, `bob`, … (password = username). Seeding disables email-2FA
and briefly the API rate limiter so it can log in as each user and post at volume.
For huge *user* counts use the `ldap` preset instead. (Design: [`docs/seed-design.md`](docs/seed-design.md).)

## 7. API testing

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

## 8. Command reference

| Command | Purpose |
|---|---|
| `up` | create & start a version-matched repro |
| `ready` | block until RC is serving (`/api/info`) |
| `start` / `stop` / `restart` | lifecycle without recreating |
| `down` | remove containers (`--volumes` also deletes data + record) |
| `use <name>` | set the default repro for name-less commands |
| `list` | all repros: version, port, state, URL |
| `info` | URL, admin creds, snippets, preset notes |
| `token` / `api` / `pat` | REST auth + calls |
| `seed` | populate a repro with sample users/channels/messages |
| `logs` | tail a repro's logs |
| `presets` | list available presets |
| `versions <X.Y.Z>` | show the resolved MongoDB pairing (without launching) |
| `doctor` | preflight checks (Docker, Compose, disk, ports, connectivity) |
| `prune` | delete all `down` repros |

Run `rc-repro <command> --help` for flags.

## 9. How version → MongoDB resolution works

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

## 10. Where state lives

```
~/.rc-repro/                  # override with RC_REPRO_HOME
├── config.yaml               # default_repro, optional reg_token / rc_image
├── presets/                  # your custom/team presets
└── repros/<name>/
    ├── docker-compose.yml     # generated — don't hand-edit; re-run `up`
    ├── repro.json             # metadata
    └── …                      # preset-generated files (LDIF, realm JSON, …)
```

## 11. Development

```bash
git clone https://github.com/klovekesh37/rc-repro.git && cd rc-repro
python -m pip install --upgrade pip
pip install -e ".[dev]"
pytest                         # pure-logic tests — no Docker needed
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the project layout and how to add a
preset.
