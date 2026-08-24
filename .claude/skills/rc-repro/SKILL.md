---
name: rc-repro
description: Use when you need a real, version-matched Rocket.Chat instance to reproduce reported behaviour, check whether a bug is version-specific, or collect evidence for a support case. Creates disposable environments and tears them down.
---

# rc-repro

rc-repro boots a version-matched Rocket.Chat paired with a compatible MongoDB, on
Docker Compose or Kubernetes, with optional backing services (LDAP, SAML/OIDC,
email, S3, multi-instance) and sample data. It is for reproducing a customer's
issue on their exact version, then binning it.

## When to reach for it

- A report says "broken in 8.6.1" and you need that exact version running to see
  whether it reproduces.
- You need to know whether behaviour is version-specific: boot two versions and
  compare. Each gets its own name and port, so they coexist.
- A ticket is about a private team channel, a discussion or a thread, and you need
  a workspace that actually contains one.

Do **not** reach for it to inspect a customer's live workspace, or when the
question is about documented behaviour rather than observed behaviour.

## Three standing rules

1. **Read `rc-repro capabilities` first.** It is JSON, needs no container engine,
   and is the authority for *this build* on which commands exist, which accept
   `--json`, which stream progress, what the error codes are and what each exit
   code means. Do not assume a flag exists because it did in another version.
2. **Branch on the exit code and `error.code`, never on prose.** Messages get
   reworded between releases; codes do not.
   - `1` internal — an unhandled failure. There is still an envelope on stdout;
     the traceback is on stderr. Retrying unchanged will not help.
   - `2` usage — fix the call; retrying unchanged will not help.
   - `3` preflight — the environment is not usable. Run `rc-repro doctor --json`.
   - `4` not found — no such workspace.
   - `5` not ready — still unknown, you may poll again.
   - `7` create failed — known dead. Stop; do not retry in a loop.
   - `8` conflict — the name or port is taken. Pick another.

   `capabilities.exit_codes` is the whole table. It also lists `6` (gate), which
   nothing in this build raises; treat it as reserved rather than as a gap.
3. **Never import rc-repro's Python internals and never scrape human output.**
   Use `--json` on the commands that have it — `capabilities.commands[].json` says
   which, and it is a minority, not all of them. For the rest (`seed`, `logs`,
   `token`, `env`, `backup`, `restore`, `upgrade`, `prune`, …) the **exit code** is
   the machine-readable part, and every command sets it from the one table above.
   stdout is the document; anything on stderr is for a person.

## The output contract

Every `--json` reply is one envelope:

```json
{"schema":"rc-repro.info.v1","contract":1,"rc_repro_version":"0.76.2",
 "generated_at":"...","ok":true,"data":{...},"warnings":[],"error":null}
```

`up` and `ready` **stream**: NDJSON `rc-repro.event.v1` progress lines first, then
exactly one envelope as the **last line**. So read to end of stream and parse the last
line — which is the right way to read EVERY reply, streaming or not. A failure is the
same envelope with `ok: false` and `error.code` set; there is always exactly one,
including when the failure happened before any work started, and including when the
failure was an unhandled one (its traceback goes to stderr, never stdout).

Only those two stream. `capabilities.commands[].streams` is derived from the build
and is the authority — `down` in particular emits the envelope alone, despite being
the other long-running command.

`contract` is the wire generation — if you do not recognise it, refuse rather than
guess. `schema` versions each payload separately. New keys may appear in `data` or
`warnings`; ignore ones you do not know.

## Recipes

### Reproduce a report at a specific version

```
rc-repro capabilities                        # what this build can do
rc-repro doctor --json                       # is this machine usable
rc-repro up --version 8.6.1 --name TICKET-1234 --wait --json
rc-repro info --name TICKET-1234 --json      # URL, admin login, links, pods
```

`up --wait` blocks until Rocket.Chat serves. Without it, poll `rc-repro ready
--name <n> --json`, which exits 5 while it is still coming up and 0 once it serves.

### Boot the backing service the ticket is about

```
rc-repro up --version 8.6.1 --name TICKET-1234 --preset ldap --wait --json
rc-repro up --version 8.6.1 --name TICKET-1235 --preset ldap --set users=130000 --wait --json
```

`capabilities.presets` lists what this build has: `ldap`, `saml`, `oidc`, `email`,
`s3_minio`, `livechat`, `airgapped`, `default`. A preset starts the side service,
wires Rocket.Chat to it, and applies the settings on first boot — so the feature is
already on when you arrive: an `ldap` workspace has users to log in as, an `email`
one catches every message Rocket.Chat sends instead of mailing it, `airgapped` has no
route to Rocket.Chat Cloud.

`--set key=value` tunes one, and `rc-repro presets` prints each preset's own params
with their defaults. Two worth knowing: `--set users=130000` on `ldap` is the
directory-scale case, and `--set idp_host=<host>` on `saml`/`oidc` is what makes the
IdP reachable when the browser is not on this machine.

Three constraints, each of which is a refusal rather than a surprise:

- `--preset` takes a **name from that list, never a path**.
- **One preset per workspace.** A preset is all-or-nothing, not additive — boot two
  workspaces rather than looking for a way to combine two scenarios.
- A preset cannot be combined with `--deployment multi-instance` on Docker; the
  refusal says so instead of silently dropping one of them. `--preset multi-instance`
  is the old spelling of `--deployment multi-instance` — it still works, and warns.

### Put realistic content in it

```
rc-repro up --version 8.6.1 --name t1 --wait --seed --seed-profile standard --verify-seed
```

Seeding is planned before it runs and checked afterwards. The result carries
`planned` (what the manifest asked for), the per-room readback, and
`verification.ok`. **Treat a false or missing `verification.ok` as unproven** — do
not infer success from a 2xx write or an attempted message count. `--verify-seed`
makes a mismatch fail the command; without it the check still runs and is recorded
in the workspace's `repro.json`.

A profile contains every kind of room, because tickets are rarely about a public
channel: public and private channels, public and private teams, channels of either
visibility inside a team, discussions (some anchored to a parent message), direct
messages, threads and reactions. `standard` is 22 rooms, 287 messages, 48 threads across 20 users.

In the verification, `faults` means something asked for is not there. Rooms holding
**more** than planned are reported separately and are not faults — seeding only ever
adds, so that is a re-seed or a preset that posts on boot. Rooms that could not be
read are a gap in the check, not a defect in the workspace.

### Choose where and how it runs

Three axes, and `capabilities.runtimes` lists the legal combinations:

```
rc-repro up --version 8.6.1 --runtime kubernetes --deployment microservices --replicas 2 ...
```

`--runtime` is `docker` (default) or `kubernetes`; `--deployment` is `monolith` or
`multi-instance` on Docker, `microservices` or `monolith` on Kubernetes. An illegal
pair is refused with `VALIDATION_FAILED` before anything is created.

### Which cluster it runs in

If `kind` is installed, rc-repro creates and manages its own cluster (`rc-repro-local`;
`prune` deletes it once no rc-repro namespace is left). If it is not, rc-repro uses the
cluster `kubectl` already points at and never removes it. With both present, kind wins.
Provisioning is the only step that differs.

`rc-repro doctor --json` is the authority on which applies and on what the cluster
provides -- `storage`, `ingress`, `loadbalancer`, `metrics`. Do not infer a capability
from a distribution name: a stock k3s has all four, a stock kind has storage only, so
`stats` refuses on one and not the other.

### What Kubernetes refuses, and why

These are refusals, not gaps — the refusal names the reason and the alternative:

- `--https` / `--domain`: rc-repro does not create an Ingress for a Kubernetes
  workspace yet. Reach it on `http://localhost:<port>` via its port-forward, or use
  `--runtime docker` when the case needs TLS. Do NOT tell a user their cluster is
  missing an ingress controller or cert-manager -- a stock k3s has Traefik as the
  default class and needs neither.
- `loadtest` / `capacity`: a Kubernetes workspace is reached through a
  `kubectl port-forward`, a single userspace relay that saturates long before
  Rocket.Chat does — the numbers would measure the forward. Run these against a
  Compose workspace on the same version.
- `env --set`: an environment variable is a helm value there. The refusal hands
  over the `helm upgrade` that changes one.
- `stats`: needs metrics-server in the cluster; the refusal says how to install it.

`logs`, `env` (read), `upgrade`, `backup`, `restore`, `seed`, `monitor`, `api`,
`pat` and `token` all work on both runtimes.

### Collect what a case needs

`rc-repro info --name <n> --json` is the record to attach: version, MongoDB pairing,
runtime and deployment, links, workspace path, and — on Kubernetes — the namespace
and the pods with the reason any of them cannot start. Credential values in the env
listing are masked, including passwords embedded in a MongoDB URL.

The admin login it reports (`admin` / `admin123`) is a fixed local sandbox
credential, not a secret. A registration token is a real licence: never paste one
into a case, and never pass one you were not given for this purpose.

### Tear it down

```
rc-repro down --name <n> --volumes --yes --json
```

**Tear down by default.** A workspace costs about 1.1 GB on Compose and more on
Kubernetes, `up` refuses when the box lacks headroom, and seven concurrent stacks
have OOM-killed a 10 GB host. Keep one only when the task says so, and say so when
you do.

`--volumes` deletes the data and the record and is irreversible; with `--json` it
**requires `--yes`**, because there is nobody to prompt. Without `--volumes` the
data is kept and `rc-repro up --version <same> --name <same>` brings it back.

`down --volumes` can also **refuse**: exit `5`, a warning naming the reason, and the
record deliberately kept — so `list` still showing the workspace afterwards is
correct, not a contradiction. Here `5` means "it is still there, read the warning",
not "poll again"; retrying the identical command is not the fix. The usual cause is a
Kubernetes namespace that will not finish terminating.

On Kubernetes, `down` leaves the shared cluster running on purpose — it is shared by
every workspace. `rc-repro prune` reclaims it **if rc-repro created it**, and refuses
while any rc-repro-owned namespace remains; a cluster you supplied is never deleted.

`down --volumes` also removes the shared MongoDB operator and the shared
Prometheus/Grafana stack, once no other workspace still needs them.

## When the envelope is not enough

Every invocation also appends to `$RC_REPRO_HOME/logs/rc-repro.log` (by default
`~/.rc-repro/logs/rc-repro.log`) — one JSON object per line, mode 0600, with
credentials dropped rather than masked. Each record carries a `run` id constant for
one invocation, so

```
grep '"run":"<id>"' ~/.rc-repro/logs/rc-repro.log
```

is that command from `start` to `end`, including work a `serve` job did on another
thread. `actor` is empty until this machine has accounts (`rc-repro users add`) —
that is attribution being off, not an unrecorded caller.

Reach for it when the envelope's `error.code` tells you *what* failed and you
need *when* and *in what order* — a create that died three phases in looks identical
in the envelope whichever phase it was. `RC_REPRO_LOG_MAX_MB=0` turns it off.

This file is for reading, not for parsing as a contract: it has no `schema` and no
`contract` field, and it is not covered by the guarantees above. Branch on the
envelope; read the log to understand.

## Things that will bite you

- **First run is slow.** Images have to be pulled. Do not treat the first duration
  as typical or kill it early.
- **`up` can refuse for capacity.** That is a preflight, not a hiccup: it means the
  box does not have room. Free something rather than retrying.
- **Version pairs matter.** rc-repro resolves the right MongoDB for a Rocket.Chat
  version. Do not override it unless you know why.
- **Kubernetes costs more.** The chart is well over a Compose workspace, and where
  `kind` is installed the first Kubernetes workspace also creates the cluster
  (~600 MB more). Against an existing k3s that step is skipped.
- **Everything binds `127.0.0.1`** unless told otherwise, because repros ship fixed
  weak credentials. Widening that is a decision for a human on a trusted network.

## If this skill is stale

`capabilities.skill.current` is false when a copy that exists does not match the
build. `capabilities.skill.hosts` names each one and its scope:

- `project` — the checkout's own `.claude/skills/rc-repro/SKILL.md`. This is the one
  in play when you are working inside the repository, and it travels with the branch.
- `user` — `~/.claude/skills/` and `~/.agents/skills/`, for a machine with no
  checkout. `rc-repro skill install` writes these; it refuses to overwrite a copy
  you have edited unless you pass `--force`.

Prefer `capabilities` over this file wherever the two disagree. That one is
generated from the build; this is prose, and prose goes stale.
