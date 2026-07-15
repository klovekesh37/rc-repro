"""Shared Keycloak scaffolding for the IdP presets (`saml`, `oidc`).

Both presets run the same Keycloak image with a generated realm import; only
the realm's client (SAML vs OIDC) and the published port differ. Bump the
Keycloak version here, once.
"""

from __future__ import annotations

import json

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
