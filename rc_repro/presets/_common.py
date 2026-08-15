"""Shared helpers for dynamic preset builders: `--set KEY=VALUE` params arrive
as raw strings, so every builder needs the same small coercions."""

from __future__ import annotations


def truthy_param(params: dict, key: str, default: bool = False) -> bool:
    """Read a boolean --set param ("1"/"true"/"yes"/"on", case-insensitive)."""
    val = params.get(key)
    if val is None or str(val).strip() == "":   # absent or `--set key=` -> default
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def int_param(params: dict, key: str, default: int) -> int:
    """Read an integer --set param; empty/absent -> default. A non-numeric
    value raises a ValueError the CLI shows verbatim (not a traceback)."""
    val = params.get(key)
    if val is None or val == "":
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        raise ValueError(f"--set {key}={val!r} expects a whole number") from None


def str_param(params: dict, key: str, default: str) -> str:
    """Read a string --set param; empty/absent -> default."""
    val = params.get(key)
    if val is None or str(val) == "":
        return default
    return str(val)


def _k8s_manifests(*, name: str, image: str, ports: list[tuple[int, int]],
                   env: dict | None = None, args: list | None = None,
                   files: dict | None = None, mounts: list | None = None,
                   ui: dict | None = None, readiness: dict | None = None,
                   workspace: str = "__RC_REPRO_NAME__") -> str:
    """One Deployment + one Service per exposed port, from the same shape every
    scenario adapter needs. Written once here rather than five times.

    `ui` maps a Service name to the host port it should be published on; the
    lifecycle reads `rc-repro.io/ui-port` and forwards it. `rc-repro.io/ui-deployment`
    names the workload when the Service is not called after it -- MinIO serves an API
    and a console from one Deployment, and a label value cannot contain a comma.
    """
    import yaml

    app = f"rc-repro-{name}"
    labels = {"app": app, "app.kubernetes.io/managed-by": "rc-repro",
              "rc-repro.io/component": name, "rc-repro.io/repro": workspace}
    docs = []
    if files:
        docs.append({"apiVersion": "v1", "kind": "ConfigMap",
                     "metadata": {"name": f"{name}-files", "labels": labels},
                     "data": dict(files)})
    container = {
        "name": name, "image": image,
        "ports": [{"containerPort": c} for _, c in ports],
    }
    if env:
        container["env"] = [{"name": k, "value": str(v)} for k, v in env.items()]
    if args:
        container["args"] = [str(a) for a in args]
    # A readiness probe is what makes "ready endpoint" mean anything. Without one a
    # pod is Ready as soon as its container starts, and every consumer downstream --
    # the Service's endpoints, the port-forward, post_ready -- trusts a signal that
    # has not been earned.
    if readiness:
        container["readinessProbe"] = readiness
    pod = {"containers": [container]}
    if mounts:
        container["volumeMounts"] = mounts
        pod["volumes"] = [{"name": "files",
                           "configMap": {"name": f"{name}-files"}}]
    docs.append({
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "labels": labels},
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app": app}},
                 "template": {"metadata": {"labels": labels}, "spec": pod}},
    })
    for svc_name, (host, container_port) in (ui or {}).items():
        svc_labels = dict(labels)
        svc_labels["rc-repro.io/ui-port"] = str(host)
        if svc_name != name:
            svc_labels["rc-repro.io/ui-deployment"] = name
        docs.append({
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": svc_name, "labels": svc_labels},
            "spec": {"selector": {"app": app},
                     "ports": [{"name": "http", "port": host,
                                "targetPort": container_port}]},
        })
    return "---\n".join(yaml.safe_dump(d, sort_keys=False) for d in docs)
