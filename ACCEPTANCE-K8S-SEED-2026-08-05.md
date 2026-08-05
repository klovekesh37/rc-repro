# Kubernetes seed setup findings — 2026-08-05

This note records the observed VPS acceptance run for `first-repro`. It is
deliberately secret-free: the registration token and admin password are not
included, only the fact that a token was supplied.

## Run and environment

- Rocket.Chat `8.6.1`, `microservices` deployment, LDAP scenario.
- Shared Kind cluster `rc-repro-local`, context `kind-rc-repro-local`.
- rc-repro namespace `rc-repro-first-repro`.
- Official Helm chart `rocketchat/rocketchat` version `7.0.2`.
- Port-forward was `up`; the repro runtime was `running`.
- The Kind control-plane node was Ready on Kubernetes `v1.36.1`, amd64,
  kernel `6.8.0-117-generic`, with containerd `2.3.1`.
- All ten observed workload pods were Running and ready. The
  `rc-ddp-streamer` pod had one restart, which is worth preserving as runtime
  evidence but did not make the deployment unhealthy.
- The licence-required deployment had a registration token supplied. The value
  is intentionally absent here and from evidence output.

## Observed defects

### 1. Seed verification failed after a healthy deployment

Rocket.Chat became reachable and the base seed created 20 users, 8 channels,
5 DMs, and 192 ordinary messages. The persisted verification record found 212
messages against 232 expected and marked the run unverified. The run was kept so
the failed proof could be inspected without duplicating data.

### 2. Thread-reply writes were silently lost

The standard plan expected 40 thread replies. The writer's persisted `actual`
record reported 0 successful thread replies, without a per-reply error surfaced
in the CLI progress stream. A failed seed must explain which writes were
attempted, which succeeded, and why the remainder failed.

### 3. Readback misclassified room messages as thread replies

The REST readback reported 20 thread replies even though the writer recorded
none. It also treated the pre-existing `general` messages as if they were
replies, producing room-count mismatches across every rich room. This makes the
readback useful evidence of disagreement, but not trustworthy proof of reply
identity until the verifier uses explicit parent-message identity.

### 4. `up --json` leaked a traceback on seed failure

The failed seed ended with a Python traceback instead of the documented
structured `CREATE_FAILED` envelope. Agents and scripts therefore had to infer
the failure from terminal output even though the command was explicitly run in
JSON mode.

## Setup and access friction

### 5. Cluster inspection required manual plumbing

The useful state was split across `evidence`, `kind get clusters`, and several
`kubectl` calls. The operator had to extract the rc-repro-owned kubeconfig,
context, and namespace with shell variables before asking for nodes, pods, and
Services. `info` did not render this inventory even though the Kubernetes
section promised pod and port-forward visibility.

This checkout now provides:

```bash
rc-repro info --name first-repro
rc-repro inspect --name first-repro
rc-repro inspect --name first-repro --json
```

`inspect` is read-only, uses the isolated rc-repro kubeconfig, and keeps admin
credentials, registration tokens, and Secret contents out of its JSON record.

### 6. Safe case capture and private access were separate paths

The human `info` view intentionally includes the fixed local admin login, so it
is not safe to paste into a ticket or chat. The `evidence` view is safe but omits
some operator-oriented access detail. The new `inspect --json` path joins the
safe runtime/seed/retention proof with the loopback/SSH handoff without copying
credentials.

### 7. A failed create leaves a live retained repro to clean up deliberately

The failed seed did not mean the Kubernetes deployment was down. The retained
run remained live for diagnosis, and the evidence record supplied the explicit
cleanup command:

```bash
rc-repro down --name first-repro --volumes --yes
```

No retry or teardown was performed during this acceptance capture because either
action would have changed the evidence being investigated.

## Follow-up queue

1. Make seed writes return structured per-phase and per-reply failure details.
2. Make readback verify thread replies by explicit parent-message identity and
   preserve `empty`, `unavailable`, and incomplete coverage as distinct states.
3. Ensure every `--json` lifecycle failure ends in its documented envelope, never
   a traceback.
4. Keep the `info`/`inspect` access contract covered by real-cluster acceptance,
   including a dead port-forward and a partially readable Kubernetes API.
