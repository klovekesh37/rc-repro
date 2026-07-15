"""Dynamic `email` preset: a Mailpit mailcatcher wired to Rocket.Chat's SMTP.

Mailpit is a sink, not a relay: it accepts all outgoing mail on SMTP :1025 and
shows it in a web UI — nothing leaves the machine, so you can read a password
reset link, invite, verification mail or 2FA code seconds after RC sends it,
for ANY user, without owning a real mailbox.

2FA is NOT forced on — enable it manually when a ticket needs it (Admin →
Accounts → Two Factor Authentication, or per-user in My Account). Codes land in
Mailpit like all other mail, and if email-2FA is enabled, rc-repro's own admin
logins keep working: the preset records Mailpit's URL in the repro metadata
(Preset.extra) and rcapi.login fetches the code from Mailpit automatically.

Pairs well with --seed: seeded users (alice/alice, …) have verified emails —
ready for 2FA opt-in and for receiving notification mail.

Parameters (via `--set`):
  verification   require email verification on new accounts (default false).
"""

from __future__ import annotations

from rc_repro import config
from rc_repro.presets import Preset, _common

_MAILPIT_WEB_PORT = config.PRESET_PORTS["email"][0]   # web UI + API; SMTP :1025 stays compose-internal
_SMTP_PORT = 1025
_FROM = "rocketchat@example.com"


def build(params: dict) -> Preset:
    verification = _common.truthy_param(params, "verification")
    mailpit_url = f"http://localhost:{_MAILPIT_WEB_PORT}"

    services = {
        "mailpit": {
            # Multi-arch (amd64/arm64) — native on Apple Silicon.
            "image": "docker.io/axllent/mailpit:v1.20",
            "restart": "unless-stopped",
            "environment": {
                # Accept any/plain SMTP so RC connects without credentials.
                "MP_SMTP_AUTH_ACCEPT_ANY": "1",
                "MP_SMTP_AUTH_ALLOW_INSECURE": "1",
            },
            "ports": [f"{_MAILPIT_WEB_PORT}:8025"],
        }
    }

    env = {
        # Wire RC's SMTP at Mailpit — plain SMTP, no TLS, no auth.
        "OVERWRITE_SETTING_SMTP_Host": "mailpit",
        "OVERWRITE_SETTING_SMTP_Port": str(_SMTP_PORT),
        "OVERWRITE_SETTING_SMTP_Protocol": "smtp",
        "OVERWRITE_SETTING_SMTP_IgnoreTLS": "true",
        "OVERWRITE_SETTING_SMTP_Username": "",
        "OVERWRITE_SETTING_SMTP_Password": "",
        "OVERWRITE_SETTING_From_Email": _FROM,
        "OVERWRITE_SETTING_Accounts_TwoFactorAuthentication_By_Email_Enabled": "true"
    }
    if verification:
        env["OVERWRITE_SETTING_Accounts_EmailVerification"] = "true"

    return Preset(
        name="email",
        description=(
            "Mailpit mailcatcher wired to RC's SMTP. Every email RC sends — "
            "password resets, invites, verification, notification and 2FA mail — "
            f"is captured at {mailpit_url} instead of leaving the machine. "
            "Pair with --seed for users with verified emails."
        ),
        env=env,
        services=services,
        depends_on=["mailpit"],
        requires_license=False,
        source="built-in (dynamic)",
        params_help={
            "verification": "require email verification on new accounts (default false)",
        },
        extra={config.EXTRA_MAILPIT_URL: mailpit_url},
        ports=list(config.PRESET_PORTS["email"]),
        notes=[
            f"Mailpit (EVERY email RC sends, for ALL users, lands here): {mailpit_url}",
            "  — one shared inbox; tell users apart by the To: column.",
            "Quick SMTP check: Admin → Email → 'Send test email', then open Mailpit.",
            "Need the email-2FA flow? Enable it in Admin → Accounts → Two Factor",
            "  Authentication — codes arrive in Mailpit, and rc-repro's own calls",
            "  fetch the admin code automatically so token/api/seed keep working.",
            "Seeded users (--seed) are created with verified emails, ready for 2FA.",
        ],
    )
