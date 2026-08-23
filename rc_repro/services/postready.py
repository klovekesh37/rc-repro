"""Post-ready preset self-configuration, moved out of the CLI so both front-ends
run it identically. Each handler reports via `emit` instead of printing.

Behaviour is unchanged from the CLI's former `_pr_*` handlers; only the output
mechanism differs (Event objects rather than typer.echo/ui.warn).
"""

from __future__ import annotations

import time

from rc_repro import config, rcapi, runner
from rc_repro.services.events import Emit, info, warn


#: How long to wait for Keycloak to serve its SAML descriptor. Passed INTO
#: `fetch_saml_idp_cert`, which owns the retrying -- not layered on top of it.
#: Generous because on Kubernetes the image is pulled before the realm is imported.
IDP_CERT_DEADLINE = 180.0


def _exec_in(meta: runner.Metadata, service: str, argv: list[str]) -> int:
    """Run a command in a preset's side service, on either runtime.

    A post_ready action is a preset configuring ITSELF, and presets now exist on
    both runtimes -- so the one call that reaches into a container had to stop being
    a Compose call.
    """
    from rc_repro.services import topology
    if topology.of_meta(meta) != topology.KUBERNETES:
        return runner.compose_exec(meta.name, service, argv)
    from rc_repro.services import k8s
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    context = str(extra.get("context") or k8s.CONTEXT)
    namespace = str(extra.get("namespace") or k8s.namespace_for(meta.name))
    pod = k8s.run(["kubectl", "--context", context, "-n", namespace, "get", "pod",
                   "-l", f"app=rc-repro-{service}", "-o",
                   "jsonpath={.items[0].metadata.name}"],
                  own=k8s.is_ours(context))
    target = (pod.stdout or "").strip()
    if not target:
        return 1
    res = k8s.run(["kubectl", "--context", context, "-n", namespace, "exec", target,
                   "--", *argv], timeout=k8s.APPLY_TIMEOUT, own=k8s.is_ours(context))
    return res.returncode


def _pr_saml_idp_cert(meta: runner.Metadata, auth: rcapi.Auth, action: dict, emit: Emit) -> bool:
    info(emit, "fetching IdP cert (Keycloak first boot can take ~30s)...", phase="post_ready")
    # ONE call. `fetch_saml_idp_cert` already retries to its own deadline -- its
    # docstring says "Retries until the IdP (e.g. Keycloak, which boots slowly) is
    # serving" -- and v0.54.1 wrapped it in a second 20-attempt loop without reading
    # that. 20 x 90s is thirty minutes of a create that looks hung, which is how the
    # operational audit found it: the SAML workspace sat in "still waiting for the
    # IdP to serve its descriptor" for half an hour.
    #
    # The deadline is raised instead, and only here: Keycloak on Kubernetes has to
    # pull its image before it can boot, which the Compose default did not allow for.
    cert = rcapi.fetch_saml_idp_cert(action["descriptor_url"],
                                     timeout=IDP_CERT_DEADLINE)
    if cert and rcapi.set_setting(meta.root_url, auth, config.ADMIN_PASSWORD, action["setting"], cert):
        enable = action.get("enable_setting")
        if enable:
            rcapi.set_setting(meta.root_url, auth, config.ADMIN_PASSWORD, enable, False)
            time.sleep(1)
            rcapi.set_setting(meta.root_url, auth, config.ADMIN_PASSWORD, enable, True)
        info(emit, "IdP cert applied; SAML login button registered.", phase="post_ready")
        return True
    warn(emit, "could not fetch/apply IdP cert (is the IdP up?)", phase="post_ready")
    return False


def _pr_keycloak_master_ssl_off(meta: runner.Metadata, auth: rcapi.Auth, action: dict, emit: Emit) -> bool:
    svc = action.get("service", "keycloak")
    port = action.get("port", 8080)
    kcadm = "/opt/keycloak/bin/kcadm.sh"
    script = (
        f'{kcadm} config credentials --server http://localhost:{port} '
        f'--realm master --user admin --password admin >/dev/null && '
        f'{kcadm} update realms/master -s sslRequired=NONE'
    )
    # Through whichever runtime owns this workspace. `runner.compose_exec` on a
    # Kubernetes workspace answers "no configuration file provided: not found" --
    # docker compose's own words for "there is no compose project here" -- printed
    # raw, in the middle of a create that then said it succeeded.
    if _exec_in(meta, svc, ["bash", "-lc", script]) == 0:
        info(emit, "Keycloak admin console enabled over HTTP.", phase="post_ready")
        return True
    warn(emit, "could not relax Keycloak master-realm sslRequired "
               "(is Keycloak up yet?) - the admin console may reject HTTP",
         phase="post_ready")
    return False


def _pr_create_oauth_provider(meta: runner.Metadata, auth: rcapi.Auth, action: dict, emit: Emit) -> bool:
    """Create RC's Custom OAuth provider, then CONFIGURE it -- and report either.

    Both halves used to be unreportable. The handler returned None on every path,
    and `run_post_ready` collects a failure only on an explicit False, so the oidc
    preset's one self-config action could not appear in the "only partly configured"
    summary that exists precisely for this -- the warning printed, the summary said
    nothing, and `up` exited 0.

    Worse, every `set_setting` result was discarded. `add_oauth_service` creating the
    provider is not the same as the provider being usable: the settings carry the
    realm URL, the client id and the secret, and a provider created WITHOUT them
    renders a login button that points nowhere. That is the failure a support
    engineer sees as "I click the OIDC button and the page is blank" -- the button is
    real, the endpoint behind it is empty, and nothing in the create said so.
    """
    if not rcapi.add_oauth_service(meta.root_url, auth, config.ADMIN_PASSWORD, action["name"]):
        warn(emit, "could not create the OAuth provider", phase="post_ready")
        return False
    settings = dict(action["settings"])
    # The provider's settings are created BY the method above, and RC's settings are
    # cached -- so the first PATCH can land before the id exists. Retried rather than
    # slept past: a fixed sleep is either too short on a loaded box or wasted on a
    # fast one, and this is the window in which a wrong answer looks like success.
    failed = _apply_settings(meta, auth, settings, emit)
    for _ in range(3):
        if not failed:
            break
        time.sleep(2)
        failed = _apply_settings(meta, auth, {k: settings[k] for k in failed}, emit)
    if failed:
        warn(emit, f"the OAuth provider exists but {len(failed)} of its "
                   f"{len(settings)} settings did not apply: "
                   f"{', '.join(sorted(failed)[:4])}"
                   f"{'…' if len(failed) > 4 else ''}. Its login button will render "
                   f"and lead nowhere.", phase="post_ready")
        return False
    info(emit, "OIDC provider created and configured; login button registered.",
         phase="post_ready")
    return True


def _apply_settings(meta: runner.Metadata, auth: rcapi.Auth, settings: dict,
                    emit: Emit) -> list[str]:
    """Apply each setting; return the ids that did not take."""
    return [sid for sid, val in settings.items()
            if not rcapi.set_setting(meta.root_url, auth, config.ADMIN_PASSWORD,
                                     sid, val)]


def _pr_livechat_setup(meta: runner.Metadata, auth: rcapi.Auth, action: dict, emit: Emit) -> bool:
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
    if not canned:
        info(emit, "(canned responses & business hours are Enterprise features - pass "
                   "--reg-token to enable, else set them up manually)", phase="post_ready")
    # REPORTED, like every other handler. This returned None on both paths, so a
    # livechat workspace whose agent could not be made available printed a warning
    # and was still counted as fully configured -- the same hole the oidc action had.
    # `canned` is deliberately NOT part of the verdict: it fails on a workspace with
    # no licence, which is the normal case and not a fault.
    if not available:
        warn(emit, "set up the Omnichannel agent manually (Admin -> Omnichannel -> Agents)", phase="post_ready")
        return False
    summary = f"Omnichannel: {len(agents)} agent(s) available"
    if dept_ok:
        summary += f", '{dept}' department created + assigned"
    info(emit, summary + " - log into RC to go online.", phase="post_ready")
    return True


_POST_READY_ACTIONS = {
    "saml_idp_cert": _pr_saml_idp_cert,
    "keycloak_master_ssl_off": _pr_keycloak_master_ssl_off,
    "create_oauth_provider": _pr_create_oauth_provider,
    "livechat_setup": _pr_livechat_setup,
}


def run_post_ready(meta: runner.Metadata, auth, emit: Emit) -> list[str]:
    actions = meta.extra.get("post_ready", []) if isinstance(meta.extra, dict) else []
    if auth is None:
        if actions:
            warn(emit, "preset self-config skipped - could not log in as admin; "
                       f"re-run once reachable: rc-repro ready --name {meta.name}", phase="post_ready")
        return
    # COLLECTED, not just warned past. A create that printed "could not fetch/apply
    # IdP cert" and then "starting" left the reader to work out for themselves
    # whether SAML actually worked -- the deployment succeeded and the CONFIGURATION
    # did not, and those are different answers to different questions.
    failed = []
    for action in actions:
        name = action.get("action")
        handler = _POST_READY_ACTIONS.get(name)
        if not handler:
            continue
        if handler(meta, auth, action, emit) is False:
            failed.append(str(name))
    if failed:
        warn(emit, f"the workspace is RUNNING but its {meta.preset!r} preset is only "
                   f"partly configured — these did not complete: "
                   f"{', '.join(failed)}", phase="post_ready")
        warn(emit, f"    Rocket.Chat itself is fine. Re-run the preset's "
                   f"self-configuration with: rc-repro ready --name {meta.name}",
             phase="post_ready")
    return failed
