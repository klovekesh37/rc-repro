# Contributing to rc-repro

Short guide for teammates working on the tool itself. For *using* rc-repro, see
[README.md](README.md).

## Dev setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                 # pure-logic tests — no Docker needed, run in seconds
```

CI runs `pytest` on every push/PR (see `.github/workflows/ci.yml`).

## Project layout

| Module | Responsibility |
|--------|----------------|
| `rc_repro/cli.py` | Typer commands, orchestration, `up`/`ready` flow, preflight |
| `rc_repro/versions.py` | RC version → MongoDB pairing (live lookup + fallback map) |
| `rc_repro/presets/` | preset package: `__init__.py` holds the `Preset` dataclass + loader |
| `rc_repro/presets/_common.py` | shared `--set` param helpers (`truthy_param`/`int_param`/`str_param`) |
| `rc_repro/presets/_keycloak.py` | shared Keycloak scaffolding reused by `saml` + `oidc` |
| `rc_repro/presets/ldap.py`, `saml.py`, `oidc.py`, `email.py`, `s3_minio.py`, `livechat.py` | generate the LDAP / Keycloak (SAML & OIDC) / Mailpit / MinIO / Omnichannel scenarios |
| `rc_repro/presets/multi_instance.py` | generate the multi-instance (Traefik + NATS) scenario |
| `rc_repro/compose.py` | build the docker-compose document (incl. cloning RC into N instances, port binding) |
| `rc_repro/runner.py` | on-disk state, `docker compose` invocations, host-port allocation |
| `rc_repro/rcapi.py` | minimal Rocket.Chat REST client (readiness, auth, 2FA/OTP, settings) |
| `rc_repro/seed.py` | populate a repro with sample users/channels/messages via REST |
| `rc_repro/monitoring.py` | `--monitor` add-on: Prometheus + Grafana services/config, attachable to any repro |
| `rc_repro/ui.py` | terminal output helpers (`ok`/`warn`/`fail`/`note`/`die`) |
| `rc_repro/config.py` | paths, constants, `PRESET_PORTS` registry, env-var overrides, persisted config |
| `rc_repro/__init__.py` | `__version__` (single-sourced from `pyproject.toml` via `importlib.metadata`) |
| `rc_repro/data/` | shipped version map, static preset YAML, monitoring dashboard JSON |

## Adding a preset

**Static preset** (env / extra services only) — add a YAML file to
`rc_repro/data/presets/<name>.yaml`:

```yaml
name: my-scenario
description: What this reproduces.
env:                       # merged into the rocketchat service
  OVERWRITE_SETTING_Some_Setting: "true"
services:                  # optional extra compose services
  my-sidecar: { image: some/image:tag }
depends_on: [my-sidecar]
```

**Dynamic preset** (needs generated files, params, or post-boot steps — like
`ldap`/`saml`) — write a `build(params) -> Preset` function in a new
`rc_repro/presets/<name>.py` and register it in
`presets._dynamic_builders()` (in `rc_repro/presets/__init__.py`).

- Read `--set` params via `_common.truthy_param` / `int_param` / `str_param` so
  bad values raise a clean error instead of a traceback.
- Reuse `_keycloak` if your preset needs a Keycloak IdP.

Useful `Preset` fields:
- `files` — generated files written to the workspace (e.g. an LDIF or realm JSON).
- `params_help` — one line per `--set` key, shown by `rc-repro presets`.
- `post_ready` — actions run once RC is serving; add a handler in
  `cli._POST_READY_ACTIONS` and key it by the action's `"action"` string.
- `notes` — tips printed after `up` / by `info`.
- `ports` — host ports the preset's side services publish (see below).
- `volumes` — named volumes merged into the compose top-level `volumes:` (any
  volume a preset service mounts must be declared here or compose rejects it).
- `extra` — arbitrary metadata copied into `repro.json` (e.g. the email preset
  stores `mailpit_url` so `rcapi.login` can fetch OTP codes).

**Side-service ports.** If your preset publishes host ports, add them to
`config.PRESET_PORTS` and set `ports=list(config.PRESET_PORTS["<name>"])` on the
`Preset`. That keeps allocation collision-free (a second same-preset repro is
rejected up front) and picks a port that doesn't clash with other presets. All
published ports are bound to `127.0.0.1` automatically — don't hard-code a bind
host in the service.

Anyone can also drop a preset in `~/.rc-repro/presets/<name>.yaml` locally — a
user file overrides a built-in or dynamic preset of the same name. Static YAML
supports `env`, `services`, `rocketchat`, `depends_on`, `notes`, `params_help`,
`instances`, `entry_service`, `extra`, `volumes`, `ports` (but not `files` /
`post_ready`, which are code-only).

## Conventions

- Add/adjust a test in `tests/test_core.py` for any new resolution/preset/compose
  logic (these run without Docker; each test gets an isolated `RC_REPRO_HOME` via
  `tests/conftest.py`).
- Keep Docker interaction in `runner.py` and REST interaction in `rcapi.py`.
- Use `ui.ok/warn/fail`/`_err` for status output rather than raw `typer.secho`.
- Check `docker compose` return codes — lifecycle commands must fail loudly, not
  print a false success.
- Verify user-facing changes with a real `up` where practical — several bugs in
  this tool only surfaced by actually running/clicking through a repro.

## Maintainer

Maintained by the Rocket.Chat support team.
Issues and requests: https://github.com/klovekesh37/rc-repro/issues
