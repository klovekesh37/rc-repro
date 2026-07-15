"""Dynamic `saml` preset: a real Keycloak IdP that Rocket.Chat authenticates
against over SAML.

The Keycloak realm is generated in code (a minimal realm with just our SAML
client + N test users) rather than shipping a bloated full export — Keycloak
regenerates all the default clients/flows/scopes on import anyway.

Parameters (via `--set`):
  users   number of test users to generate (default 5). Each userN has password
          userN and email userN@example.com — log in as user1 / user1.

Cert handling: Keycloak generates its signing key on realm import, so rc-repro
fetches the cert at runtime (post_ready) and applies it to RC — no manual
exchange. The IdP is published on host port 8081 (browser-reachable).
"""

from __future__ import annotations

from rc_repro import config
from rc_repro.presets import Preset, _common, _keycloak

_KC_PORT = config.PRESET_PORTS["saml"][0]
_KC_REALM = _keycloak.REALM
_KC_CLIENT = "rc-repro-saml-sp"  # must equal RC's SAML issuer


def _saml_mapper(name: str, user_property: str) -> dict:
    return {
        "name": name,
        "protocol": "saml",
        "protocolMapper": "saml-user-property-mapper",
        "consentRequired": False,
        "config": {
            "user.attribute": user_property,
            "attribute.nameformat": "Basic",
            "attribute.name": name,
        },
    }


def _client() -> dict:
    return {
        "clientId": _KC_CLIENT,
        "protocol": "saml",
        "enabled": True,
        # Trailing wildcard: matches whatever host port rc-repro assigns to RC.
        "redirectUris": ["http://localhost*"],
        "frontchannelLogout": True,
        "attributes": {
            "saml.assertion.signature": "true",   # sign the assertion
            "saml.server.signature": "true",       # sign the response
            "saml.client.signature": "false",      # RC doesn't sign its requests
            "saml.force.post.binding": "true",
            "saml.authnstatement": "true",
            "saml_name_id_format": "email",
            "saml.signature.algorithm": "RSA_SHA256",
        },
        "protocolMappers": [
            _saml_mapper("email", "email"),
            _saml_mapper("username", "username"),
            _saml_mapper("firstName", "firstName"),
            _saml_mapper("lastName", "lastName"),
        ],
    }


def build(params: dict) -> Preset:
    users = _common.int_param(params, "users", 5)
    saml_ep = f"http://localhost:{_KC_PORT}/realms/{_KC_REALM}/protocol/saml"
    descriptor = f"{saml_ep}/descriptor"

    services = {
        "keycloak": _keycloak.service("./saml/keycloak-realm.json", _KC_PORT),
    }
    env = {
        "OVERWRITE_SETTING_SAML_Custom_Default": "true",
        "OVERWRITE_SETTING_SAML_Custom_Default_provider": "keycloak",
        "OVERWRITE_SETTING_SAML_Custom_Default_entry_point": saml_ep,
        # Keycloak serves SSO and SLO from the same endpoint. We logout locally
        # by default, but set this correctly so the config is coherent and real
        # SLO works if logout_behaviour is switched to "SAML".
        "OVERWRITE_SETTING_SAML_Custom_Default_idp_slo_redirect_url": saml_ep,
        "OVERWRITE_SETTING_SAML_Custom_Default_logout_behaviour": "Local",
        "OVERWRITE_SETTING_SAML_Custom_Default_issuer": _KC_CLIENT,
        "OVERWRITE_SETTING_SAML_Custom_Default_button_label_text": "Keycloak SSO",
        "OVERWRITE_SETTING_SAML_Custom_Default_default_user_role": "user",
        "OVERWRITE_SETTING_SAML_Custom_Default_name_overwrite": "false",
        "OVERWRITE_SETTING_SAML_Custom_Default_mail_overwrite": "false",
        "OVERWRITE_SETTING_SAML_Custom_Default_user_data_fieldmap": (
            '{"username":"username","email":"email","name":"firstName"}'
        ),
        "OVERWRITE_SETTING_Accounts_TwoFactorAuthentication_By_Email_Enabled": "false",
    }
    return Preset(
        name="saml",
        description=(
            f"Keycloak IdP with a generated SAML realm ({users} users user1..user"
            f"{users}, password=username). Click 'Keycloak SSO', log in as "
            "user1/user1. Keycloak admin at http://localhost:8081 (admin/admin, "
            "realm 'rcrepro')."
        ),
        env=env,
        services=services,
        depends_on=["keycloak"],
        requires_license=False,
        source="built-in (dynamic)",
        files=[("saml/keycloak-realm.json", _keycloak.realm_json([_client()], users))],
        params_help={"users": "number of Keycloak test users (default 5)"},
        ports=list(config.PRESET_PORTS["saml"]),
        notes=[
            f"Keycloak admin console: http://localhost:{_KC_PORT}  (admin / admin)",
            f"  Your SAML users live in the '{_KC_REALM}' realm, NOT 'master' (the",
            "  default view). Switch the realm dropdown (top-left) to "
            f"'{_KC_REALM}', or open Users directly:",
            f"    http://localhost:{_KC_PORT}/admin/master/console/#/{_KC_REALM}/users",
            "  ('temporary admin' banner on master is normal Keycloak dev mode — ignore it)",
        ],
        post_ready=[
            {
                "action": "saml_idp_cert",
                "descriptor_url": descriptor,
                "setting": "SAML_Custom_Default_cert",
                "enable_setting": "SAML_Custom_Default",
            },
            {"action": "keycloak_master_ssl_off", "service": "keycloak"},
        ],
    )
