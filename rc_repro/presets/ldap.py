"""Dynamic `ldap` preset: an OpenLDAP directory seeded with N users + a group,
with Rocket.Chat wired to authenticate against it.

Parameters (via `--set`):
  users   number of LDAP users to generate (default 5). Set high (e.g. 130000)
          to reproduce LDAP-sync scale/performance issues.
  domain  LDAP domain (default example.com) -> base DN dc=example,dc=com.

Every user `userN` has password `userN`, so you can log in immediately as
`user1` / `user1`. The local admin (admin/admin123) still works via
LDAP_Login_Fallback, so rc-repro's own API/token calls keep functioning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import yaml

from rc_repro.presets import Preset, _common
from rc_repro import config
from rc_repro.presets.scenario import Scenario

#: phpLDAPadmin's host port, from the ONE registry both runtimes read. Compose
#: publishes it directly; Kubernetes declares it on the Service (see UI_PORT_LABEL)
#: and the lifecycle forwards it. Same number either way, so a ticket's instructions
#: do not depend on where the workspace happens to run.
_UI_PORT = config.PRESET_PORTS["ldap"][0]

#: Multi-arch (amd64/arm/arm64), and the standard pairing with osixia/openldap.
_UI_IMAGE = "osixia/phpldapadmin:0.9.0"

#: A Service wearing this label is published on the host port it names. One
#: convention for every scenario, so the four presets still to be adapted need no
#: new mechanism -- and no second place to declare a port that could disagree with
#: config.PRESET_PORTS.
UI_PORT_LABEL = "rc-repro.io/ui-port"

# osixia imports custom LDIF from here on first boot (with `--copy-service`).
_BOOTSTRAP_PATH = "/container/service/slapd/assets/config/bootstrap/ldif/custom/50-rc-users.ldif"
_GROUP_CN = "rc-users"
_GID = "5000"
_PARAMS_HELP = {
    "users": "number of LDAP users to generate (default 5; try 130000 for scale)",
    "domain": "LDAP domain (default example.com)",
}


@dataclass(frozen=True)
class LDAPIntent:
    """Deployment-neutral LDAP intent shared by each native adapter."""

    users: int
    domain: str
    base_dn: str
    admin_password: str = "admin"

    def as_params(self) -> dict:
        return {"users": self.users, "domain": self.domain, "base_dn": self.base_dn}


def _ldif(base_dn: str, domain: str, users: int) -> str:
    blocks = [
        f"dn: ou=Users,{base_dn}\nobjectClass: organizationalUnit\nou: Users\n",
        f"dn: ou=groups,{base_dn}\nobjectClass: organizationalUnit\nou: groups\n",
    ]
    for i in range(1, users + 1):
        uid = f"user{i}"
        blocks.append(
            f"dn: uid={uid},ou=Users,{base_dn}\n"
            "objectClass: inetOrgPerson\n"
            "objectClass: posixAccount\n"
            f"cn: User {i}\n"
            f"sn: {i}\n"
            f"uid: {uid}\n"
            f"uidNumber: {1000 + i}\n"
            f"gidNumber: {_GID}\n"
            f"homeDirectory: /home/{uid}\n"
            f"mail: {uid}@{domain}\n"
            f"userPassword: {uid}\n"
        )
    members = "".join(f"memberUid: user{i}\n" for i in range(1, users + 1))
    blocks.append(
        f"dn: cn={_GROUP_CN},ou=groups,{base_dn}\n"
        "objectClass: top\n"
        "objectClass: posixGroup\n"
        f"cn: {_GROUP_CN}\n"
        f"gidNumber: {_GID}\n"
        f"{members}"
    )
    return "\n".join(blocks) + "\n"


def _intent(params: Mapping[str, str]) -> LDAPIntent:
    users = _common.int_param(params, "users", 5)
    domain = _common.str_param(params, "domain", "example.com")
    base_dn = ",".join(f"dc={part}" for part in domain.split("."))
    return LDAPIntent(users=users, domain=domain, base_dn=base_dn)


def _env(intent: LDAPIntent, *, host: str = "openldap") -> dict[str, str]:
    """Rocket.Chat LDAP settings shared by Compose and Kubernetes."""
    admin_pw = intent.admin_password
    return {
        "OVERWRITE_SETTING_LDAP_Enable": "true",
        # Generic OpenLDAP, NOT Active Directory. Without this, RC defaults to
        # server type "ad" and searches sAMAccountName instead of our uid field,
        # so every LDAP login fails with "User not found".
        "OVERWRITE_SETTING_LDAP_Server_Type": "",
        "OVERWRITE_SETTING_LDAP_Host": host,
        "OVERWRITE_SETTING_LDAP_Port": "389",
        "OVERWRITE_SETTING_LDAP_BaseDN": intent.base_dn,
        "OVERWRITE_SETTING_LDAP_Authentication": "true",
        "OVERWRITE_SETTING_LDAP_Authentication_UserDN": f"cn=admin,{intent.base_dn}",
        "OVERWRITE_SETTING_LDAP_Authentication_Password": admin_pw,
        "OVERWRITE_SETTING_LDAP_User_Search_Filter": "(objectclass=inetOrgPerson)",
        "OVERWRITE_SETTING_LDAP_User_Search_Field": "uid",
        "OVERWRITE_SETTING_LDAP_User_Search_Scope": "sub",
        "OVERWRITE_SETTING_LDAP_Unique_Identifier_Field": "uid",
        # Keep local login working so admin/admin123 (and rc-repro's API) still work.
        "OVERWRITE_SETTING_LDAP_Login_Fallback": "true",
        # No SMTP in the repro, so email-2FA on a new LDAP user's first login
        # would block it with an OTP that goes nowhere. Turn it off so you can
        # actually log in as user1/user1.
        "OVERWRITE_SETTING_Accounts_TwoFactorAuthentication_By_Email_Enabled": "false",
    }


def _compose(intent: LDAPIntent) -> Preset:
    ldif = _ldif(intent.base_dn, intent.domain, intent.users)
    services = {
        "openldap": {
            "image": "osixia/openldap:1.5.0",  # multi-arch (amd64/arm64) — no platform pin
            "command": ["--copy-service"],  # required to import custom bootstrap LDIF
            "environment": {
                "LDAP_ORGANISATION": "RC Repro",
                "LDAP_DOMAIN": intent.domain,
                "LDAP_BASE_DN": intent.base_dn,
                "LDAP_ADMIN_PASSWORD": intent.admin_password,
            },
            "volumes": [f"./ldap/50-rc-users.ldif:{_BOOTSTRAP_PATH}:ro"],
            "restart": "unless-stopped",
        },
        # A directory you cannot look at is hard to reproduce against: "is the user
        # there, and what attributes does it have" is the first question of nearly
        # every LDAP ticket, and answering it meant `docker exec ... ldapsearch`.
        "phpldapadmin": {
            "image": _UI_IMAGE,
            "environment": {
                "PHPLDAPADMIN_LDAP_HOSTS": "openldap",
                # The directory is on a private network with a fixed throwaway
                # password; HTTPS here would only add a certificate warning.
                "PHPLDAPADMIN_HTTPS": "false",
            },
            "ports": [f"{_UI_PORT}:80"],
            "depends_on": ["openldap"],
            "restart": "unless-stopped",
        },
    }
    return Preset(
        name="ldap",
        description=(
            f"OpenLDAP (osixia) seeded with {intent.users} user(s) + group "
            f"'{_GROUP_CN}'; RC wired for LDAP login. Log in as user1 / user1."
        ),
        env=_env(intent),
        services=services,
        depends_on=["openldap"],
        requires_license=False,
        source="built-in (dynamic)",
        files=[("ldap/50-rc-users.ldif", ldif)],
        # Declared so port allocation and the capacity preflight can SEE it --
        # `test_preset_ports_match_registry` is the invariant, and it caught this
        # being missing the moment phpLDAPadmin gained a port.
        ports=list(config.PRESET_PORTS["ldap"]),
        notes=[
            f"phpLDAPadmin: http://localhost:{_UI_PORT}",
            f"    log in with DN  cn=admin,{intent.base_dn}  /  {intent.admin_password}",
            f"    Rocket.Chat users are user1..user{intent.users}, password same as name",
        ],
        params_help=dict(_PARAMS_HELP),
        scenario="ldap",
        scenario_params=intent.as_params(),
    )


def _kubernetes_manifest(intent: LDAPIntent) -> str:
    """Render the small OpenLDAP workload used by the Kubernetes adapter."""
    labels = {
        "app": "rc-repro-openldap",
        "app.kubernetes.io/managed-by": "rc-repro",
        "rc-repro.io/component": "ldap",
        # The Kubernetes lifecycle substitutes the namespace-local repro name.
        "rc-repro.io/repro": "__RC_REPRO_NAME__",
    }
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "openldap-bootstrap", "labels": labels},
        "data": {"50-rc-users.ldif": _ldif(intent.base_dn, intent.domain, intent.users)},
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "openldap", "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": labels["app"]}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [{
                        "name": "openldap",
                        "image": "osixia/openldap:1.5.0",
                        "args": ["--copy-service"],
                        "env": [
                            {"name": "LDAP_ORGANISATION", "value": "RC Repro"},
                            {"name": "LDAP_DOMAIN", "value": intent.domain},
                            {"name": "LDAP_BASE_DN", "value": intent.base_dn},
                            {"name": "LDAP_ADMIN_PASSWORD", "value": intent.admin_password},
                        ],
                        "ports": [{"name": "ldap", "containerPort": 389}],
                        # slapd binds 389 only after the bootstrap LDIF is imported,
                        # and without this the pod is Ready the instant the container
                        # starts -- so the Service publishes an endpoint that is not
                        # serving, and Rocket.Chat starts against an LDAP that is not
                        # answering. That is a first-login failure nobody would
                        # connect back to here. tcpSocket rather than httpGet: LDAP
                        # is not HTTP, and slapd binding the port IS the milestone.
                        "readinessProbe": {
                            "tcpSocket": {"port": 389},
                            "initialDelaySeconds": 5,
                            "periodSeconds": 3,
                            "failureThreshold": 40,
                        },
                        "volumeMounts": [{
                            "name": "bootstrap",
                            "mountPath": _BOOTSTRAP_PATH,
                            "subPath": "50-rc-users.ldif",
                        }],
                    }],
                    "volumes": [{
                        "name": "bootstrap",
                        "configMap": {"name": "openldap-bootstrap"},
                    }],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "openldap", "labels": labels},
        "spec": {
            "selector": {"app": labels["app"]},
            "ports": [{"name": "ldap", "port": 389, "targetPort": 389}],
        },
    }
    # phpLDAPadmin, and the label that gets it published. A Service carrying
    # `rc-repro.io/ui-port` is forwarded to that host port by the Kubernetes
    # lifecycle -- the declaration lives on the Service that defines the thing,
    # rather than in a second registry the adapter would have to be kept in step
    # with. The NUMBER still comes from config.PRESET_PORTS, so both runtimes
    # publish the same one.
    ui_labels = dict(labels)
    ui_labels["app"] = "rc-repro-phpldapadmin"
    ui_labels[UI_PORT_LABEL] = str(_UI_PORT)
    ui_deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "phpldapadmin", "labels": ui_labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": ui_labels["app"]}},
            "template": {
                "metadata": {"labels": ui_labels},
                "spec": {
                    "containers": [{
                        "name": "phpldapadmin",
                        "image": _UI_IMAGE,
                        "env": [
                            {"name": "PHPLDAPADMIN_LDAP_HOSTS", "value": "openldap"},
                            {"name": "PHPLDAPADMIN_HTTPS", "value": "false"},
                        ],
                        "ports": [{"containerPort": 80}],
                        "readinessProbe": {
                            "httpGet": {"path": "/", "port": 80},
                            "initialDelaySeconds": 3,
                            "periodSeconds": 3,
                            "failureThreshold": 30,
                        },
                    }],
                },
            },
        },
    }
    ui_service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "phpldapadmin", "labels": ui_labels},
        "spec": {
            "selector": {"app": ui_labels["app"]},
            "ports": [{"name": "http", "port": 80, "targetPort": 80}],
        },
    }
    return "---\n".join(yaml.safe_dump(doc, sort_keys=False) for doc in (
        config_map, deployment, service, ui_deployment, ui_service))


def _kubernetes(intent: LDAPIntent) -> Preset:
    env = _env(intent)
    return Preset(
        name="ldap",
        description=(
            f"OpenLDAP (osixia) seeded with {intent.users} user(s) + group "
            f"'{_GROUP_CN}'; RC wired for LDAP login. Log in as user1 / user1."
        ),
        notes=[
            f"phpLDAPadmin: http://localhost:{_UI_PORT}",
            f"    log in with DN  cn=admin,{intent.base_dn}  /  {intent.admin_password}",
            f"    Rocket.Chat users are user1..user{intent.users}, password same as name",
        ],
        topology="kubernetes",
        # Declared here for the same reason the Compose adapter declares it, and it
        # was missing: phpLDAPadmin is published on the FIXED host port 8082 on this
        # runtime too (as a port-forward rather than a compose mapping), but with an
        # empty `ports` list `check_sidecar_ports` returns immediately and
        # `sidecar_ports` is never recorded. So a second Kubernetes LDAP workspace got
        # no pre-flight refusal and its forward simply failed to bind 8082 -- where
        # Compose refuses up front and names the workspace holding the port.
        #
        # test_preset_ports_match_registry did not catch it because it resolves
        # presets through `presets.load`, which is the DOCKER adapter.
        ports=list(config.PRESET_PORTS["ldap"]),
        # The scenario changes the backing service and Rocket.Chat settings; it
        # does not make the microservices deployment cease to be an Enterprise
        # topology.  Preserve that deployment-level requirement so lifecycle
        # warnings and onboarding next-command guidance remain truthful.
        requires_license=True,
        source="built-in (scenario)",
        env=env,
        params_help=dict(_PARAMS_HELP),
        scenario="ldap",
        scenario_params=intent.as_params(),
        kubernetes_manifests=[_kubernetes_manifest(intent)],
    )


_SCENARIO = Scenario(
    name="ldap",
    params_help=_PARAMS_HELP,
    resolve_intent=_intent,
    # Keyed by RUNTIME, in services/topology.py's words. See the note on
    # Preset.topology for why there is only one set of words for this.
    adapters={"docker": _compose, "kubernetes": _kubernetes},
)


def scenario() -> Scenario:
    return _SCENARIO


def build(params: dict) -> Preset:
    """Legacy `ldap` loader entry point; resolve through the Compose adapter."""
    return _SCENARIO.resolve(params, "docker")
