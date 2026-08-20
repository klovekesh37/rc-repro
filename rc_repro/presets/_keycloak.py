"""Shared Keycloak scaffolding for the IdP presets (`saml`, `oidc`).

Both presets run the same Keycloak image with a generated realm import; only
the realm's client (SAML vs OIDC) and the published port differ. Bump the
Keycloak version here, once.
"""

from __future__ import annotations

import json

UI_PORT_LABEL = "rc-repro.io/ui-port"


def manifests(realm: str, host_port: int, http_port: int | None = None,
              *, workspace: str = "__RC_REPRO_NAME__") -> str:
    """The same Keycloak, as native Kubernetes resources.

    Deliberately NOT a Scenario with two adapters like `ldap`. Keycloak's intent
    survives the crossing untouched: the realm JSON is byte-identical, the settings
    Rocket.Chat needs are identical, and the post_ready actions are REST calls that
    do not care. Only the container's packaging differs, so `saml` and `oidc` carry
    BOTH renderings on one Preset and each runtime reads the half it understands.

    THE HOSTNAME TRANSFERS, which is the part worth stating. `keycloak:<port>` has
    to resolve identically from Rocket.Chat's backend and from the browser, because
    OIDC uses one URL for the authorize redirect AND the token exchange. On Compose
    that is the project network plus a `127.0.0.1 keycloak` hosts entry. Here it is
    a Service literally named `keycloak` plus the same hosts entry, with the port
    forwarded to the same number. The gotcha and its workaround are unchanged, which
    is the whole point of publishing on the registry's port.
    """
    import yaml

    container_port = http_port or 8080
    labels = {
        "app": "rc-repro-keycloak",
        "app.kubernetes.io/managed-by": "rc-repro",
        "rc-repro.io/component": "keycloak",
        "rc-repro.io/repro": workspace,
        UI_PORT_LABEL: str(host_port),
    }
    env = [{"name": "KC_BOOTSTRAP_ADMIN_USERNAME", "value": "admin"},
           {"name": "KC_BOOTSTRAP_ADMIN_PASSWORD", "value": "admin"},
           # See service() for why: without it, anything terminating TLS in front
           # serves an https console that requests http assets, and the browser
           # blocks every one.
           {"name": "KC_PROXY_HEADERS", "value": "xforwarded"}]
    if http_port:
        env.append({"name": "KC_HTTP_PORT", "value": str(http_port)})
    config_map = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "keycloak-realm", "labels": labels},
        "data": {"rcrepro-realm.json": realm},
    }
    deployment = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "keycloak", "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": labels["app"]}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {"containers": [{
                    "name": "keycloak",
                    "image": KC_IMAGE,
                    "args": ["start-dev", "--import-realm"],
                    "env": env,
                    "ports": [{"containerPort": container_port}],
                    # subPath, so the ConfigMap lands as a FILE next to Keycloak's
                    # own import directory rather than replacing the directory.
                    "volumeMounts": [{
                        "name": "realm",
                        "mountPath": "/opt/keycloak/data/import/rcrepro-realm.json",
                        "subPath": "rcrepro-realm.json",
                    }],
                }],
                "volumes": [{"name": "realm",
                             "configMap": {"name": "keycloak-realm"}}]},
            },
        },
    }
    service = {
        "apiVersion": "v1", "kind": "Service",
        # `keycloak`, so RC's pod resolves exactly the hostname the realm and the
        # settings already name.
        "metadata": {"name": "keycloak", "labels": labels},
        "spec": {"selector": {"app": labels["app"]},
                 "ports": [{"name": "http", "port": host_port,
                            "targetPort": container_port}]},
    }
    return "---\n".join(yaml.safe_dump(d, sort_keys=False)
                        for d in (config_map, deployment, service))


KC_IMAGE = "quay.io/keycloak/keycloak:26.0"
REALM = "rcrepro"
_DOMAIN = "example.com"


def service(realm_mount: str, host_port: int, http_port: int | None = None) -> dict:
    """The Keycloak compose service.

    `http_port` makes Keycloak listen on that port INSIDE the container too
    (KC_HTTP_PORT) — the oidc preset needs the same port inside and out so one
    `keycloak:<port>` URL works from both the browser and RC's backend.
    Without it, Keycloak listens on its default 8080.
    """
    env = {
        "KC_BOOTSTRAP_ADMIN_USERNAME": "admin",
        "KC_BOOTSTRAP_ADMIN_PASSWORD": "admin",
        # Believe X-Forwarded-Proto/-Host, so the URLs Keycloak GENERATES match the
        # scheme and host the browser actually used.
        #
        # Keycloak builds absolute URLs for its own assets, its issuer and every
        # OIDC endpoint. Without this it derives them from the connection it can
        # see -- plain http on a published port -- so reaching the admin console
        # through anything that terminates TLS in front (an iximiuz lab forward,
        # Codespaces, ngrok, a corporate proxy) serves an https PAGE that asks for
        # http RESOURCES, and the browser blocks every one of them:
        #
        #   Mixed Content: The page at 'https://<host>/admin/master/console/' was
        #   loaded over HTTPS, but requested an insecure resource
        #   'http://<host>/resources/master/admin/en'. This request has been blocked.
        #
        # The console renders blank or half-dead, and nothing in the container log
        # says why -- the failure is entirely in the browser.
        #
        # Measured against quay.io/keycloak/keycloak:26.0, one request carrying
        # `X-Forwarded-Proto: https, X-Forwarded-Host: lab.example.com`:
        #
        #   without : issuer http://127.0.0.1:18085/realms/master
        #   with    : issuer https://lab.example.com/realms/master
        #
        # With no proxy in front there are no such headers and nothing changes --
        # also measured, because a fix that alters the ordinary laptop case would
        # be trading one bug for another. It does mean a client on the host can
        # influence generated URLs by sending the header itself; on a throwaway IdP
        # running admin/admin that is not a boundary anyone was relying on.
        #
        # KC_PROXY_HEADERS, not the old KC_PROXY: `proxy` was deprecated in 24 and
        # REMOVED in 26, which is the version above.
        "KC_PROXY_HEADERS": "xforwarded",
    }
    container_port = 8080
    if http_port:
        env["KC_HTTP_PORT"] = str(http_port)
        container_port = http_port
    return {
        "image": KC_IMAGE,
        "command": ["start-dev", "--import-realm"],
        "environment": env,
        "volumes": [f"{realm_mount}:/opt/keycloak/data/import/rcrepro-realm.json:ro"],
        "ports": [f"{host_port}:{container_port}"],
        "restart": "unless-stopped",
    }


def users(count: int) -> list[dict]:
    """Test users user1..userN, password = username, verified emails."""
    return [
        {
            "username": f"user{i}",
            "enabled": True,
            "emailVerified": True,
            "email": f"user{i}@{_DOMAIN}",
            "firstName": "User",
            "lastName": str(i),
            "credentials": [{"type": "password", "value": f"user{i}", "temporary": False}],
        }
        for i in range(1, count + 1)
    ]


def realm_json(clients: list[dict], user_count: int) -> str:
    """A minimal realm export: just our client(s) + test users. Keycloak
    regenerates all default clients/flows/scopes on import anyway."""
    realm = {
        "realm": REALM,
        "enabled": True,
        "sslRequired": "none",  # allow HTTP (reached via docker port-forward)
        "clients": clients,
        "users": users(user_count),
    }
    return json.dumps(realm, indent=2)
