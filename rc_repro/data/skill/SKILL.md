---
name: rc-repro
description: Use when you need a real, version-matched Rocket.Chat instance to reproduce reported behaviour, check whether a bug is version-specific, or collect evidence for a support case. Creates disposable environments and tears them down.
---

# rc-repro

rc-repro creates disposable, version-matched Rocket.Chat environments for
reproducing behaviour and collecting evidence.

## When to reach for it

- A report says "broken in 8.6.1" and you need that exact version running to see
  whether it reproduces.
- You need to know whether behaviour is version-specific: run two versions and
  compare.
- A support case needs evidence of what was deployed and how it behaved.
- You need a throwaway instance with a particular integration in place (LDAP,
  SAML, email, S3) rather than building one by hand.

Do **not** reach for it to inspect a customer's live workspace, or when the
question is about documented behaviour rather than observed behaviour.

## Three standing rules

1. **Read `rc-repro capabilities` first.** It is JSON, needs no container engine,
   and is the authority for *this build* on which commands exist, which accept
   `--json`, which stream progress, and what the error and exit codes mean. Do not
   assume a flag exists because it once did.
2. **Branch on exit codes and `error.code`, never on prose.** Messages get
   reworded; codes do not. In particular: exit 6 means a human decision is
   required, so stop and relay `error.gate.approve_with` verbatim rather than
   trying to satisfy it yourself. Exit 5 means "not ready yet, may poll"; exit 7
   means "known dead, stop".
3. **Never import rc-repro's Python internals and never scrape human output.**
   Use the documented commands with `--json`. Anything on stderr is for humans.

## Recipes

### Reproduce a report at a specific version

```
rc-repro capabilities                       # what can this build do
rc-repro doctor                             # is the environment usable
rc-repro up --version <X.Y.Z> --json        # NDJSON progress, then one envelope
rc-repro ready --name <name> --json         # block until it serves
rc-repro info --name <name> --json          # URL and admin credentials
```

Then drive the workspace to the reported steps. The final line of a streaming
command is always the result envelope; the lines before it are progress events.

### Check whether behaviour is version-specific

Create two repros at different versions and compare. Each gets its own name and
port, so they coexist.

### Collect evidence for a case

Capture the repro's record and state before tearing it down. Evidence is
secret-safe by design: the root URL is reduced to its origin and no tokens appear
in it. Never paste a registration token or licence into a case; reference that one
was used instead.

### Clean up

```
rc-repro down --name <name> --volumes --yes --json
```

Tear down by default. Keeping a repro costs disk and leaves state behind, so retain
one only when asked, and when you do, report the exact command that removes it.
The Kubernetes preset's shared Kind cluster deliberately stays warm after `down`.
Use `rc-repro prune --yes` only when the task also calls for reclaiming that empty
cluster; it refuses while an rc-repro-owned namespace remains.

## Things that will bite you

- **A preset may need a cluster.** `capabilities` reports `presets_by_topology`;
  anything outside `compose` needs Kubernetes tooling and far more memory and CPU.
  Check before offering it.
- **Version pairs matter.** rc-repro resolves the right MongoDB for a Rocket.Chat
  version. Do not override it unless you know why.
- **Not every host can run every version.** Some combinations are refused at
  preflight because they genuinely cannot work, not because they are slow. Read the
  error rather than retrying.
- **First run is slow.** Images have to be pulled. A later run on a warm host is
  much faster, so do not treat the first duration as typical or kill it early.

## Authority

rc-repro enforces its own boundaries through exit codes, not through whatever
permissions your host can express. Treat exit 6 as final: public exposure, an
unapproved cluster, new credentials, deleting something rc-repro does not own, and
retaining a run are human decisions. Relay the request, do not route around it.
