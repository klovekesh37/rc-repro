"""A secret-safe, backend-neutral record of what was deployed and how it behaved.

The point of evidence is that someone can attach it to a support case and a reader
can trust it. Two properties follow from that, and neither is optional:

* **Secret-safe.** The root URL is reduced to its origin, and no token, licence, or
  password appears anywhere. A record that leaks a credential cannot be attached to
  anything, which makes it worthless.
* **Backend-neutral.** The `repro` block is byte-for-byte the same shape for Docker
  and Kubernetes, and `runtime` keeps one shape with different contents. A consumer
  reads it without branching on topology.

`licensed` and `retention` are recorded because a conclusion drawn from a run
depends on them. An unlicensed microservices repro may look healthy while not
behaving as licensed, so citing it as proof without that caveat is the actual harm
the field prevents. The cleanup handle is a literal command rather than a
description, so a human can paste it months later without knowing the topology.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from rc_repro import runner
from rc_repro.services import lifecycle

#: The rendered deployment artifact, whichever topology produced it. Evidence
#: hashes whichever exists, so reproducibility works the same either way.
_ARTIFACTS = ("docker-compose.yml", "values.yaml")


def safe_origin(url: str) -> str:
    """Reduce a URL to scheme://host[:port], dropping any path, query, or userinfo.

    Credentials in a URL are the classic accidental leak, and a path can carry a
    ticket id or customer name. The origin is all evidence needs.
    """
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except (UnicodeError, ValueError):
        return "REDACTED"
    if parsed.scheme not in ("http", "https") or not host:
        return "REDACTED"
    if ":" in host:                      # IPv6 literal
        host = f"[{host}]"
    port = parsed.port
    return f"{parsed.scheme}://{host}:{port}" if port else f"{parsed.scheme}://{host}"


def _artifact(name: str) -> tuple[str, str]:
    """(filename, sha256) of the rendered artifact, or ("", "") if absent."""
    ws = runner.workspace(name)
    for candidate in _ARTIFACTS:
        path = ws / candidate
        if path.exists():
            return candidate, hashlib.sha256(path.read_bytes()).hexdigest()
    return "", ""


def _topology(meta) -> str:
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    return extra.get("topology", "compose") or "compose"


def _cleanup_command(name: str, topology: str) -> str:
    """The exact command that removes this repro, valid on any machine holding it.

    A literal string rather than a structured descriptor, so an agent relays
    something a human can paste and a human needs no knowledge of the topology.
    """
    return f"rc-repro down --name {name} --volumes --yes"


def _ownership(meta, topology: str) -> dict:
    """How rc-repro knows this is its own, stated so a teardown decision is
    auditable after the fact. A name prefix is not ownership."""
    if topology == "kubernetes":
        extra = meta.extra if isinstance(meta.extra, dict) else {}
        return {"proof": "label", "managed_by": "rc-repro",
                "namespace": extra.get("k8s_namespace", "")}
    return {"proof": "record", "managed_by": "rc-repro", "project": meta.project}


def _licensed(meta) -> dict:
    """Whether a licence was supplied. Never the value, only whether and from where.

    The chart does not validate a licence, so microservices can come up present but
    not functioning as licensed. Recording this is what stops an unlicensed run being
    cited as proof.
    """
    try:
        preset = lifecycle.presets.load(meta.preset)
        required = bool(getattr(preset, "requires_license", False))
    except Exception:  # noqa: BLE001 - a missing preset must not break evidence
        required = False
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    supplied = bool(extra.get("reg_token_supplied"))
    return {"required": required, "supplied": supplied,
            "source": "reg_token" if supplied else None}


#: Only these reasons may appear on a retained run. Anything else is refused so
#: agents cannot invent a soft justification for leaving state behind.
_RETAIN_REASONS = frozenset({"persisted preference", "explicit task"})


def resolve_retention(*, retained: bool | None = None,
                      reason: str | None = None,
                      preferences: dict | None = None) -> dict:
    """Decide retain vs teardown from explicit args and persisted preference.

    Missing or malformed ``retain_runs`` falls back to teardown. A retained run
    must name either a persisted preference or an explicit task.
    """
    prefs = preferences if preferences is not None else {}
    pref_raw = prefs.get("retain_runs") if isinstance(prefs, dict) else None
    pref_retain = pref_raw is True  # malformed/missing => False

    if retained is True:
        # Unknown freeform reasons collapse to explicit task so the closed set
        # of reasons cannot grow through caller prose.
        why = (reason or "").strip() or ("persisted preference" if pref_retain
                                         else "explicit task")
        if why not in _RETAIN_REASONS:
            why = "explicit task"
        return {"retained": True, "reason": why}
    if retained is False:
        return {"retained": False, "reason": None}
    # Default: preference only when it is exactly True; else teardown.
    if pref_retain:
        return {"retained": True, "reason": "persisted preference"}
    return {"retained": False, "reason": None}


def record(name: str, *, retained: bool | None = None,
           reason: str | None = None) -> dict:
    """Build the evidence record for a repro."""
    target = lifecycle.resolve_name(name)
    meta = runner.read_meta(target)
    topology = _topology(meta)
    artifact, digest = _artifact(target)

    if topology == "kubernetes":
        from rc_repro.services import k8s
        services = k8s.pods(target)
        state = k8s.aggregate_state(services)
        extra = meta.extra if isinstance(meta.extra, dict) else {}
        engine = {"kind_cluster": k8s.CLUSTER_NAME,
                  "kubectl_context": extra.get("k8s_context", ""),
                  "chart": k8s.CHART,
                  "chart_version": extra.get("chart_version", "")}
        engine["port_forward"] = k8s.forward_state(meta)
    else:
        services = [{"service": c.get("service", ""), "state": c.get("state", ""),
                     "status": c.get("status", "")}
                    for c in runner.container_details(target)]
        state = lifecycle._pretty_state(runner.rc_state(target)) if runner.docker_available() else "unknown"
        engine = {"docker_version": runner.docker_server_version(),
                  "compose_version": runner.compose_version()}

    try:
        from rc_repro.services import onboarding
        prefs = onboarding.state().get("preferences") or {}
    except Exception:  # noqa: BLE001 - evidence must not fail on a bad config
        prefs = {}
    retention = resolve_retention(retained=retained, reason=reason,
                                  preferences=prefs)

    return {
        # Identical shape on both topologies, so a consumer never branches here.
        "repro": {
            "name": meta.name,
            "preset": meta.preset,
            "topology": topology,
            "rc_version": meta.rc_version,
            "rc_image": meta.rc_image,
            "mongo_tag": meta.mongo_tag,
            "mongo_flavor": meta.mongo_flavor,
            # Origin only: never a path, query, or userinfo.
            "root_url": safe_origin(meta.root_url),
            "host_port": meta.host_port,
            "version_source": meta.version_source,
            "created_at": meta.created_at,
        },
        "runtime": {"state": state, "services": services, "engine": engine},
        "artifact": {"name": artifact, "sha256": digest},
        "ownership": _ownership(meta, topology),
        "license": _licensed(meta),
        "retention": {
            "retained": retention["retained"],
            "reason": retention["reason"],
            "cleanup": _cleanup_command(meta.name, topology),
            "owner": "rc-repro",
            "created_at": meta.created_at,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def write_bundle(name: str, dest: str | Path, payload: dict) -> dict:
    """Write a bundle: the record plus logs and the rendered artifact.

    Opt-in rather than the default, because the single JSON document is the machine
    contract and a directory is what a human attaches to a case.
    """
    import json

    out = Path(dest).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True),
                                       encoding="utf-8")
    written = ["manifest.json"]

    artifact = payload.get("artifact", {}).get("name") or ""
    if artifact:
        src = runner.workspace(name) / artifact
        if src.exists():
            (out / artifact).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            written.append(artifact)

    logs_dir = out / "logs"
    logs_dir.mkdir(exist_ok=True)
    try:
        if payload["repro"]["topology"] == "kubernetes":
            from rc_repro.services import k8s
            # One file per pod: a single concatenated log is unreadable when nine
            # components are interleaved, and the failing one is what a reader wants.
            for pod, text in k8s.collect_logs(name).items():
                (logs_dir / f"{pod}.log").write_text(text, encoding="utf-8")
                written.append(f"logs/{pod}.log")
        else:
            text = runner.ps(name)
            if text:
                (logs_dir / "services.txt").write_text(text, encoding="utf-8")
                written.append("logs/services.txt")
    except Exception:  # noqa: BLE001 - a bundle must not fail on a missing engine
        pass
    return {"path": str(out), "files": sorted(written)}
