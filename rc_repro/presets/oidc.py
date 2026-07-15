"""Dynamic `oidc` preset: a Keycloak OpenID Connect IdP that Rocket.Chat logs in
against via its Custom OAuth provider.

Reuses the shared Keycloak scaffolding (_keycloak), but ships an OIDC client
instead of a SAML one. OIDC authenticates with a client id + a fixed client
secret (no signing certs), so there's no runtime cert fetch.

THE ONE GOTCHA (see docs/oidc-design.md §5): OIDC's `url` is used by BOTH the
browser (authorize) and RC's backend (token/userinfo). We use a single shared
hostname `keycloak:8080` — RC's backend resolves it over the compose network; the
browser resolves it via a one-line hosts entry `127.0.0.1  keycloak` (rc-repro
prints it). Keycloak is published on host port 8080 (SAML uses 8081, so the two
presets don't collide).
"""

from __future__ import annotations

from rc_repro import config
from rc_repro.presets import Preset, _common, _keycloak

_KC_PORT = config.PRESET_PORTS["oidc"][0]   # distinct from SAML's, so both presets can run at once
                                            # (8080 is too commonly occupied — e.g. other local Keycloaks)
_KC_REALM = _keycloak.REALM
_KC_HOST = "keycloak"    # resolvable from RC (compose DNS) AND the browser (/etc/hosts)
_CLIENT_ID = "rc-oidc"
_CLIENT_SECRET = "rc-oidc-secret"
_PROVIDER = "keycloak"   # RC Custom OAuth name -> settings key + /_oauth/keycloak callback


def _oidc_client() -> dict:
    return {
        "clientId": _CLIENT_ID,
        "protocol": "openid-connect",
        "enabled": True,
        "publicClient": False,              # confidential -> uses the client secret
        "secret": _CLIENT_SECRET,           # pinned so RC's config matches on import
        "standardFlowEnabled": True,        # authorization-code flow
        "directAccessGrantsEnabled": True,
        "redirectUris": ["http://localhost*", f"http://{_KC_HOST}:{_KC_PORT}/*"],
        "webOrigins": ["*"],
        # Keycloak's built-in openid/profile/email scopes already emit
        # preferred_username / email / name / sub — no custom mappers needed.
    }


def build(params: dict) -> Preset:
    users = _common.int_param(params, "users", 5)
    realm_base = f"http://{_KC_HOST}:{_KC_PORT}/realms/{_KC_REALM}"
    setting = f"Accounts_OAuth_Custom-{_PROVIDER.capitalize()}"   # Accounts_OAuth_Custom-Keycloak

    services = {
        # http_port: listen on the same port we publish, so the single
        # `keycloak:8085` URL works from the browser (via /etc/hosts) and RC's
        # backend (via the compose network) identically.
        "keycloak": _keycloak.service(
            "./oidc/keycloak-realm.json", _KC_PORT, http_port=_KC_PORT
        ),
    }

    # A custom OAuth provider's settings don't exist until it's created via the
    # API, so these can't be OVERWRITE_SETTING env — rc-repro creates the provider
    # and sets these on `ready` (post_ready). Values are full setting ids.
    oauth_settings = {
        setting: True,
        f"{setting}-url": realm_base,
        f"{setting}-authorize_path": "/protocol/openid-connect/auth",
        f"{setting}-token_path": "/protocol/openid-connect/token",
        f"{setting}-identity_path": "/protocol/openid-connect/userinfo",
        f"{setting}-token_sent_via": "header",
        f"{setting}-scope": "openid profile email",
        f"{setting}-id": _CLIENT_ID,
        f"{setting}-secret": _CLIENT_SECRET,
        f"{setting}-login_style": "popup",
        f"{setting}-username_field": "preferred_username",
        f"{setting}-email_field": "email",
        f"{setting}-name_field": "name",
        f"{setting}-merge_users": True,
        f"{setting}-show_button": True,
        f"{setting}-button_label_text": "Keycloak (OIDC)",
        f"{setting}-button_color": "#1d74f5",
        f"{setting}-button_label_color": "#FFFFFF",
    }

    env = {
        # This one IS a real existing setting, so OVERWRITE works at boot.
        # No SMTP in the repro -> don't block first login on email-2FA.
        "OVERWRITE_SETTING_Accounts_TwoFactorAuthentication_By_Email_Enabled": "false",
    }

    return Preset(
        name="oidc",
        description=(
            f"Keycloak OIDC IdP (OpenID Connect) with {users} users user1..user"
            f"{users} (password=username). RC logs in via Custom OAuth (popup). "
            "Click 'Keycloak (OIDC)', sign in as user1/user1. Requires a hosts entry "
            "(see the note printed on `up`)."
        ),
        env=env,
        services=services,
        depends_on=["keycloak"],
        requires_license=False,
        source="built-in (dynamic)",
        files=[("oidc/keycloak-realm.json", _keycloak.realm_json([_oidc_client()], users))],
        params_help={"users": "number of Keycloak test users (default 5)"},
        ports=list(config.PRESET_PORTS["oidc"]),
        post_ready=[
            {"action": "keycloak_master_ssl_off", "service": "keycloak", "port": _KC_PORT},
            # Create the Custom OAuth provider and configure it (can't be done via
            # env, since the provider's settings don't exist until it's created).
            {"action": "create_oauth_provider", "name": _PROVIDER.capitalize(), "settings": oauth_settings},
        ],
        notes=[
            "OIDC needs one host entry so your browser can reach Keycloak at the",
            "same URL RC's backend uses. Add this line to /etc/hosts (needs sudo):",
            "    127.0.0.1  keycloak",
            f"Then log in via 'Keycloak (OIDC)' as user1 / user1.",
            f"Keycloak admin console: http://{_KC_HOST}:{_KC_PORT}  (admin/admin, realm '{_KC_REALM}').",
        ],
    )
