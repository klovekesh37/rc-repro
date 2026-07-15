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

from rc_repro.presets import Preset, _common

# osixia imports custom LDIF from here on first boot (with `--copy-service`).
_BOOTSTRAP_PATH = "/container/service/slapd/assets/config/bootstrap/ldif/custom/50-rc-users.ldif"
_GROUP_CN = "rc-users"
_GID = "5000"


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


def build(params: dict) -> Preset:
    users = _common.int_param(params, "users", 5)
    domain = _common.str_param(params, "domain", "example.com")
    base_dn = ",".join(f"dc={part}" for part in domain.split("."))
    admin_pw = "admin"

    ldif = _ldif(base_dn, domain, users)

    services = {
        "openldap": {
            "image": "osixia/openldap:1.5.0",  # multi-arch (amd64/arm64) — no platform pin
            "command": ["--copy-service"],  # required to import custom bootstrap LDIF
            "environment": {
                "LDAP_ORGANISATION": "RC Repro",
                "LDAP_DOMAIN": domain,
                "LDAP_BASE_DN": base_dn,
                "LDAP_ADMIN_PASSWORD": admin_pw,
            },
            "volumes": [f"./ldap/50-rc-users.ldif:{_BOOTSTRAP_PATH}:ro"],
            "restart": "unless-stopped",
        }
    }

    env = {
        "OVERWRITE_SETTING_LDAP_Enable": "true",
        # Generic OpenLDAP, NOT Active Directory. Without this, RC defaults to
        # server type "ad" and searches sAMAccountName instead of our uid field,
        # so every LDAP login fails with "User not found".
        "OVERWRITE_SETTING_LDAP_Server_Type": "",
        "OVERWRITE_SETTING_LDAP_Host": "openldap",
        "OVERWRITE_SETTING_LDAP_Port": "389",
        "OVERWRITE_SETTING_LDAP_BaseDN": base_dn,
        "OVERWRITE_SETTING_LDAP_Authentication": "true",
        "OVERWRITE_SETTING_LDAP_Authentication_UserDN": f"cn=admin,{base_dn}",
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

    return Preset(
        name="ldap",
        description=(
            f"OpenLDAP (osixia) seeded with {users} user(s) + group "
            f"'{_GROUP_CN}'; RC wired for LDAP login. Log in as user1 / user1."
        ),
        env=env,
        services=services,
        depends_on=["openldap"],
        requires_license=False,
        source="built-in (dynamic)",
        files=[("ldap/50-rc-users.ldif", ldif)],
        params_help={
            "users": "number of LDAP users to generate (default 5; try 130000 for scale)",
            "domain": "LDAP domain (default example.com)",
        },
    )
