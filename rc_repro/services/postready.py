"""Post-ready preset self-configuration, moved out of the CLI so both front-ends
run it identically. Each handler reports via `emit` instead of printing.

Behaviour is unchanged from the CLI's former `_pr_*` handlers; only the output
mechanism differs (Event objects rather than typer.echo/ui.warn).
"""

from __future__ import annotations

import time

from rc_repro import config, rcapi, runner
from rc_repro.services.events import Emit, info, warn


def _pr_saml_idp_cert(meta: runner.Metadata, auth: rcapi.Auth, action: dict, emit: Emit) -> None:
    info(emit, "fetching IdP cert (Keycloak first boot can take ~30s)...", phase="post_ready")
    cert = rcapi.fetch_saml_idp_cert(action["descriptor_url"])
    if cert and rcapi.set_setting(meta.root_url, auth, config.ADMIN_PASSWORD, action["setting"], cert):
        enable = action.get("enable_setting")
        if enable:
            rcapi.set_setting(meta.root_url, auth, config.ADMIN_PASSWORD, enable, False)
            time.sleep(1)
            rcapi.set_setting(meta.root_url, auth, config.ADMIN_PASSWORD, enable, True)
        info(emit, "IdP cert applied; SAML login button registered.", phase="post_ready")
    else:
        warn(emit, "could not fetch/apply IdP cert (is the IdP up?)", phase="post_ready")


def _pr_keycloak_master_ssl_off(meta: runner.Metadata, auth: rcapi.Auth, action: dict, emit: Emit) -> None:
    svc = action.get("service", "keycloak")
    port = action.get("port", 8080)
    kcadm = "/opt/keycloak/bin/kcadm.sh"
    script = (
        f'{kcadm} config credentials --server http://localhost:{port} '
        f'--realm master --user admin --password admin >/dev/null && '
        f'{kcadm} update realms/master -s sslRequired=NONE'
    )
    if runner.compose_exec(meta.name, svc, ["bash", "-lc", script]) == 0:
        info(emit, "Keycloak admin console enabled over HTTP.", phase="post_ready")
    else:
        warn(emit, "could not relax Keycloak master-realm sslRequired "
                   "(is Keycloak up yet?) - the admin console may reject HTTP", phase="post_ready")


def _pr_create_oauth_provider(meta: runner.Metadata, auth: rcapi.Auth, action: dict, emit: Emit) -> None:
    if rcapi.add_oauth_service(meta.root_url, auth, config.ADMIN_PASSWORD, action["name"]):
        for sid, val in action["settings"].items():
            rcapi.set_setting(meta.root_url, auth, config.ADMIN_PASSWORD, sid, val)
        info(emit, "OIDC provider created; login button registered.", phase="post_ready")
    else:
        warn(emit, "could not create the OAuth provider", phase="post_ready")


def _pr_livechat_setup(meta: runner.Metadata, auth: rcapi.Auth, action: dict, emit: Emit) -> None:
    url, pw = meta.root_url, config.ADMIN_PASSWORD
    agents = [{"agentId": auth.user_id, "username": config.ADMIN_USERNAME}]
    rcapi.add_livechat_agent(url, auth, pw, config.ADMIN_USERNAME)
    for i in range(2, int(action.get("agents", 1)) + 1):
        u = f"agent{i}"
        rcapi.create_user(url, auth, pw, u)
        rcapi.add_livechat_agent(url, auth, pw, u)
        uid = rcapi.get_user_id(url, auth, u)
        if uid:
            agents.append({"agentId": uid, "username": u})
    available = rcapi.set_livechat_available(url, auth, pw)

    dept, dept_ok = action.get("department"), False
    if dept:
        dept_id = rcapi.ensure_livechat_department(url, auth, pw, dept)
        if dept_id:
            dept_ok = rcapi.assign_livechat_agents(url, auth, pw, dept_id, agents)

    canned = rcapi.save_canned_response(url, auth, pw, "hello",
                                        "Hi! Thanks for reaching out - how can I help?")
    if available:
        summary = f"Omnichannel: {len(agents)} agent(s) available"
        if dept_ok:
            summary += f", '{dept}' department created + assigned"
        info(emit, summary + " - log into RC to go online.", phase="post_ready")
    else:
        warn(emit, "set up the Omnichannel agent manually (Admin -> Omnichannel -> Agents)", phase="post_ready")
    if not canned:
        info(emit, "(canned responses & business hours are Enterprise features - pass "
                   "--reg-token to enable, else set them up manually)", phase="post_ready")


_POST_READY_ACTIONS = {
    "saml_idp_cert": _pr_saml_idp_cert,
    "keycloak_master_ssl_off": _pr_keycloak_master_ssl_off,
    "create_oauth_provider": _pr_create_oauth_provider,
    "livechat_setup": _pr_livechat_setup,
}


def run_post_ready(meta: runner.Metadata, auth, emit: Emit) -> None:
    actions = meta.extra.get("post_ready", []) if isinstance(meta.extra, dict) else []
    if auth is None:
        if actions:
            warn(emit, "preset self-config skipped - could not log in as admin; "
                       f"re-run once reachable: rc-repro ready --name {meta.name}", phase="post_ready")
        return
    for action in actions:
        handler = _POST_READY_ACTIONS.get(action.get("action"))
        if handler:
            handler(meta, auth, action, emit)
