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
| `rc_repro/cli.py` | Typer commands, orchestration, output |
| `rc_repro/versions.py` | RC version → MongoDB pairing (live lookup + fallback map) |
| `rc_repro/presets.py` | load presets (static YAML + dynamic builders) |
| `rc_repro/ldap_preset.py`, `saml_preset.py`, `oidc_preset.py` | generate the LDAP / Keycloak (SAML & OIDC) scenarios |
| `rc_repro/multi_instance_preset.py` | generate the multi-instance (Traefik + NATS) scenario |
| `rc_repro/compose.py` | build the docker-compose document (incl. cloning RC into N instances) |
| `rc_repro/runner.py` | on-disk state + `docker compose` invocations |
| `rc_repro/rcapi.py` | minimal Rocket.Chat REST client (readiness, auth, settings) |
| `rc_repro/seed.py` | populate a repro with sample users/channels/messages via REST |
| `rc_repro/config.py` | paths, constants, persisted config |
| `rc_repro/data/` | shipped version map + static preset YAML |

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
`rc_repro/<name>_preset.py` and register it in `presets._dynamic_builders()`.
Useful `Preset` fields: `files` (generated files written to the workspace),
`params_help` (for `--set`), `post_ready` (actions run once RC is serving),
`notes` (tips printed after `up` / in `info`).

Anyone can also drop a preset in `~/.rc-repro/presets/<name>.yaml` locally — a
user file overrides a built-in or dynamic preset of the same name.

## Conventions

- Add/adjust a test in `tests/test_core.py` for any new resolution/preset/compose
  logic (these run without Docker).
- Keep Docker interaction in `runner.py` and REST interaction in `rcapi.py`.
- Verify user-facing changes with a real `up` where practical — several bugs in
  this tool only surfaced by actually running/clicking through a repro.

## Maintainer

Maintained by the Rocket.Chat support team.
Issues and requests: https://github.com/klovekesh37/rc-repro/issues
