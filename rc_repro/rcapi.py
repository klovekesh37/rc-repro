"""Minimal Rocket.Chat REST client: readiness polling, login, and calls.

Kept dependency-light (requests only). Readiness uses the public /api/info
endpoint; auth uses /api/v1/login with the auto-provisioned admin.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass

import requests

from rc_repro import config


class NotReady(RuntimeError):
    pass


@dataclass
class Auth:
    token: str
    user_id: str

    def headers(self) -> dict[str, str]:
        return {"X-Auth-Token": self.token, "X-User-Id": self.user_id}


def api_info(root_url: str, timeout: float = 5.0) -> dict | None:
    """GET /api/info (public). Returns the JSON dict, or None if not serving."""
    try:
        resp = requests.get(f"{root_url.rstrip('/')}/api/info", timeout=timeout)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def wait_ready(
    root_url: str,
    *,
    timeout: float = 300.0,
    interval: float = 3.0,
    is_alive=None,
    on_tick=None,
) -> dict:
    """Block until RC serves /api/info, returning its payload.

    `is_alive()` (optional) is checked each tick; if it returns False we fail
    fast rather than poll a crashed container for the full timeout.
    `on_tick(elapsed)` (optional) is called each poll for progress display.
    """
    deadline = time.monotonic() + timeout
    while True:
        info = api_info(root_url)
        if info is not None:
            return info
        if is_alive is not None and not is_alive():
            raise NotReady("Rocket.Chat container is not running (check `logs`)")
        if time.monotonic() >= deadline:
            raise NotReady(f"Rocket.Chat did not become ready within {int(timeout)}s")
        if on_tick is not None:
            on_tick(timeout - (deadline - time.monotonic()))
        time.sleep(interval)


def _auth_from(resp) -> Auth:
    data = resp.json().get("data", {})
    token = data.get("authToken")
    user_id = data.get("userId")
    if not token or not user_id:
        raise RuntimeError(f"login did not return a token: {resp.text[:200]}")
    return Auth(token=token, user_id=user_id)


def login(
    root_url: str,
    user: str = config.ADMIN_USERNAME,
    password: str = config.ADMIN_PASSWORD,
    timeout: float = 10.0,
    mailpit_url: str | None = None,
) -> Auth:
    """Log in and return an Auth (token + user id).

    When the workspace has email-2FA enabled (the `email` preset does, by
    default), a plain login is rejected with `totp-required`. In that case we
    retry with the password-fallback 2FA method, and if that's not accepted and
    a Mailpit URL is known (meta.extra["mailpit_url"]), we trigger the code
    email, read the 6-digit code from Mailpit's API, and retry with it — so
    rc-repro's own admin calls keep working while OTP stays on for browsers.
    """
    base = f"{root_url.rstrip('/')}/api/v1"
    creds = {"user": user, "password": password}

    resp = requests.post(f"{base}/login", json=creds, timeout=timeout)
    if resp.status_code == 200:
        return _auth_from(resp)
    if "totp-required" not in resp.text:
        resp.raise_for_status()

    # 2FA challenge. Try the password-fallback method first (no email needed).
    resp2 = requests.post(
        f"{base}/login", json=creds, timeout=timeout,
        headers=password_2fa_headers(password),
    )
    if resp2.status_code == 200:
        return _auth_from(resp2)

    if not mailpit_url:
        raise RuntimeError(
            "login requires an email-2FA code and no Mailpit URL is configured "
            f"(is this the `email` preset?): {resp.text[:200]}"
        )

    # Ask RC to (re)send the code, then fish it out of Mailpit. Filter by the
    # recipient so a code for another user (Mailpit is a catch-all inbox for
    # every address) is never picked up by mistake, and snapshot the inbox first
    # so a leftover code from a previous login isn't mistaken for the fresh one.
    to_email = user if "@" in user else (
        config.ADMIN_EMAIL if user == config.ADMIN_USERNAME else None
    )
    baseline = newest_mail_stamp(mailpit_url, to_email=to_email)
    requests.post(
        f"{base}/users.2fa.sendEmailCode",
        json={"emailOrUsername": user}, timeout=timeout,
    )
    code = fetch_email_otp(mailpit_url, to_email=to_email, after=baseline)
    if not code:
        raise RuntimeError("email-2FA code did not arrive in Mailpit within the timeout")
    resp3 = requests.post(
        f"{base}/login", json=creds, timeout=timeout,
        headers={"x-2fa-code": code, "x-2fa-method": "email"},
    )
    resp3.raise_for_status()
    return _auth_from(resp3)


def _extract_otp(text: str) -> str | None:
    """First standalone 6-digit code in an email body, or None."""
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", text or "")
    return m.group(1) if m else None


def _addressed_to(item: dict, to_email: str | None) -> bool:
    if not to_email:
        return True
    addrs = [a.get("Address", "") for a in item.get("To") or []]
    return to_email.lower() in (a.lower() for a in addrs)


def newest_mail_stamp(mailpit_url: str, to_email: str | None = None) -> str:
    """The `Created` timestamp of the newest message (optionally addressed to
    `to_email`), or "" if none. Snapshot this BEFORE triggering a new code so
    fetch_email_otp can ignore stale codes still sitting in the catch-all inbox."""
    base = mailpit_url.rstrip("/")
    try:
        r = requests.get(f"{base}/api/v1/messages", params={"limit": 10}, timeout=5)
        if r.status_code == 200:
            for item in r.json().get("messages") or []:   # newest first
                if _addressed_to(item, to_email):
                    return item.get("Created") or ""
    except (requests.RequestException, ValueError):
        pass
    return ""


def fetch_email_otp(
    mailpit_url: str, to_email: str | None = None,
    timeout: float = 30.0, interval: float = 1.5, after: str = "",
) -> str | None:
    """Poll Mailpit's API for the newest message (optionally only those addressed
    to `to_email` — Mailpit is a catch-all inbox for every user) and extract a
    6-digit code. `after` (a `newest_mail_stamp` snapshot) skips any message not
    newer than it, so a code left over from a previous login is never reused.
    Returns None if no code shows up within `timeout`.
    """
    base = mailpit_url.rstrip("/")
    deadline = time.monotonic() + timeout
    while True:
        try:
            listing = requests.get(f"{base}/api/v1/messages", params={"limit": 10}, timeout=5)
            if listing.status_code == 200:
                for item in listing.json().get("messages") or []:   # newest first
                    mid = item.get("ID")
                    if not mid or not _addressed_to(item, to_email):
                        continue
                    if after and (item.get("Created") or "") <= after:
                        continue   # stale — predates the code we just requested
                    msg = requests.get(f"{base}/api/v1/message/{mid}", timeout=5)
                    if msg.status_code != 200:
                        continue
                    body = msg.json()
                    code = _extract_otp(body.get("Text") or "") or _extract_otp(body.get("HTML") or "")
                    if code:
                        return code
        except (requests.RequestException, ValueError):
            pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def complete_setup_wizard(root_url: str, auth: Auth, password: str, timeout: float = 15.0) -> bool:
    """Mark the setup wizard completed so the repro is usable immediately.

    INITIAL_USER creates the admin but leaves the wizard at 'in_progress' (the
    cloud-registration step), which beats OVERWRITE_SETTING_Show_Setup_Wizard.
    Best-effort; never raises.
    """
    return set_setting(root_url, auth, password, "Show_Setup_Wizard", "completed", timeout=timeout)


def password_2fa_headers(password: str) -> dict[str, str]:
    """Headers that satisfy RC's 'password' 2FA method for guarded endpoints."""
    return {
        "x-2fa-code": hashlib.sha256(password.encode()).hexdigest(),
        "x-2fa-method": "password",
    }


def generate_pat(
    root_url: str,
    auth: Auth,
    password: str,
    token_name: str = "rc-repro",
    bypass_2fa: bool = True,
    timeout: float = 15.0,
) -> str:
    """Create (or regenerate) a Personal Access Token and return the token string.

    Mirrors a token created in the UI with "Ignore Two Factor Authentication"
    (bypass_2fa=True). Generating a PAT is itself a 2FA-guarded action, satisfied
    here with the password method.
    """
    base = f"{root_url.rstrip('/')}/api/v1"
    hdr = {**auth.headers(), "Content-Type": "application/json", **password_2fa_headers(password)}
    r = requests.post(
        f"{base}/users.generatePersonalAccessToken",
        headers=hdr,
        json={"tokenName": token_name, "bypassTwoFactor": bypass_2fa},
        timeout=timeout,
    )
    j = r.json()
    if j.get("success") and j.get("token"):
        return j["token"]
    # Already exists → regenerate it (also 2FA-guarded).
    r2 = requests.post(
        f"{base}/users.regeneratePersonalAccessToken",
        headers=hdr,
        json={"tokenName": token_name},
        timeout=timeout,
    )
    j2 = r2.json()
    if j2.get("success") and j2.get("token"):
        return j2["token"]
    raise RuntimeError(f"could not create PAT: {r.text[:200]}")


def get_setting(root_url: str, auth: Auth, password: str, setting_id: str, timeout: float = 15.0):
    """Read a Rocket.Chat setting's current value, or None if unavailable."""
    headers = {**auth.headers(), **password_2fa_headers(password)}
    try:
        resp = requests.get(
            f"{root_url.rstrip('/')}/api/v1/settings/{setting_id}",
            headers=headers, timeout=timeout,
        )
        if resp.status_code == 200 and resp.json().get("success") is True:
            return resp.json().get("value")
    except (requests.RequestException, ValueError):
        pass
    return None


def set_setting(root_url: str, auth: Auth, password: str, setting_id: str, value, timeout: float = 15.0) -> bool:
    """PATCH a Rocket.Chat setting (2FA-guarded; satisfied via password method)."""
    headers = {**auth.headers(), "Content-Type": "application/json", **password_2fa_headers(password)}
    try:
        resp = requests.post(
            f"{root_url.rstrip('/')}/api/v1/settings/{setting_id}",
            headers=headers, json={"value": value}, timeout=timeout,
        )
        return resp.status_code == 200 and resp.json().get("success") is True
    except (requests.RequestException, ValueError):
        return False


def add_oauth_service(root_url: str, auth: Auth, password: str, name: str, timeout: float = 15.0) -> bool:
    """Create a Custom OAuth provider via RC's `addOAuthService` method.

    A custom provider's `Accounts_OAuth_Custom-<name>-*` settings don't exist
    until it's created, so OVERWRITE_SETTING env can't configure it — this
    creates the provider (and its settings) so they can then be set via the API.
    """
    headers = {**auth.headers(), "Content-Type": "application/json", **password_2fa_headers(password)}
    msg = json.dumps({"msg": "method", "id": "1", "method": "addOAuthService", "params": [name]})
    try:
        resp = requests.post(
            f"{root_url.rstrip('/')}/api/v1/method.call/addOAuthService",
            headers=headers, json={"message": msg}, timeout=timeout,
        )
        return resp.status_code == 200 and resp.json().get("success") is True
    except (requests.RequestException, ValueError):
        return False


def _method_call(root_url: str, auth: Auth, password: str, method: str, params: list, timeout: float = 15.0) -> bool:
    """Invoke a Meteor method over REST (method.call). Returns True on success."""
    headers = {**auth.headers(), "Content-Type": "application/json", **password_2fa_headers(password)}
    msg = json.dumps({"msg": "method", "id": "1", "method": method, "params": params})
    try:
        resp = requests.post(
            f"{root_url.rstrip('/')}/api/v1/method.call/{method}",
            headers=headers, json={"message": msg}, timeout=timeout,
        )
        return resp.status_code == 200 and resp.json().get("success") is True
    except (requests.RequestException, ValueError):
        return False


def create_user(root_url: str, auth: Auth, password: str, username: str, timeout: float = 15.0) -> bool:
    """Create a verified user (idempotent-ish: an existing username just fails)."""
    headers = {**auth.headers(), "Content-Type": "application/json", **password_2fa_headers(password)}
    try:
        resp = requests.post(
            f"{root_url.rstrip('/')}/api/v1/users.create", headers=headers,
            json={"name": username.capitalize(), "username": username,
                  "email": f"{username}@example.com", "password": username,
                  "verified": True, "requirePasswordChange": False},
            timeout=timeout,
        )
        return resp.status_code == 200 and resp.json().get("success") is True
    except (requests.RequestException, ValueError):
        return False


def add_livechat_agent(root_url: str, auth: Auth, password: str, username: str, timeout: float = 15.0) -> bool:
    """Make `username` an Omnichannel agent (assigns the livechat-agent role)."""
    headers = {**auth.headers(), "Content-Type": "application/json", **password_2fa_headers(password)}
    try:
        resp = requests.post(
            f"{root_url.rstrip('/')}/api/v1/livechat/users/agent",
            headers=headers, json={"username": username}, timeout=timeout,
        )
        return resp.ok
    except (requests.RequestException, ValueError):
        return False


def get_user_id(root_url: str, auth: Auth, username: str, timeout: float = 15.0) -> str | None:
    """Resolve a username to its _id via users.info, or None."""
    try:
        r = requests.get(
            f"{root_url.rstrip('/')}/api/v1/users.info",
            params={"username": username}, headers=auth.headers(), timeout=timeout,
        )
        if r.ok:
            return (r.json().get("user") or {}).get("_id")
    except (requests.RequestException, ValueError):
        pass
    return None


def ensure_livechat_department(root_url: str, auth: Auth, password: str, name: str, timeout: float = 15.0) -> str | None:
    """Create (or find, if it already exists) an Omnichannel department; return
    its id. The create schema is strict — only these department fields, no
    agents (assign those separately via assign_livechat_agents)."""
    base = f"{root_url.rstrip('/')}/api/v1"
    hdr = {**auth.headers(), "Content-Type": "application/json", **password_2fa_headers(password)}
    body = {"department": {"enabled": True, "name": name, "email": f"{name}@example.com",
                           "showOnRegistration": True, "showOnOfflineForm": True}}
    try:
        r = requests.post(f"{base}/livechat/department", headers=hdr, json=body, timeout=timeout)
        b = r.json()
        if b.get("success"):
            return (b.get("department") or {}).get("_id")
        # Already exists (or strict-schema reject) — look it up by name.
        existing = requests.get(f"{base}/livechat/department", headers=hdr, timeout=timeout).json()
        for d in existing.get("departments", []):
            if d.get("name") == name:
                return d.get("_id")
    except (requests.RequestException, ValueError):
        pass
    return None


def assign_livechat_agents(root_url: str, auth: Auth, password: str, dept_id: str,
                           agents: list[dict], timeout: float = 15.0) -> bool:
    """Assign agents (list of {agentId, username}) to a department. Idempotent."""
    hdr = {**auth.headers(), "Content-Type": "application/json", **password_2fa_headers(password)}
    upsert = [{"agentId": a["agentId"], "username": a["username"], "count": 0, "order": 0} for a in agents]
    try:
        r = requests.post(
            f"{root_url.rstrip('/')}/api/v1/livechat/department/{dept_id}/agents",
            headers=hdr, json={"upsert": upsert, "remove": []}, timeout=timeout,
        )
        return r.ok
    except (requests.RequestException, ValueError):
        return False


def save_canned_response(root_url: str, auth: Auth, password: str, shortcut: str,
                         text: str, timeout: float = 15.0) -> bool:
    """Save a global canned response. This is an ENTERPRISE feature — returns
    False on Community (403), so callers treat it as best-effort."""
    hdr = {**auth.headers(), "Content-Type": "application/json", **password_2fa_headers(password)}
    try:
        r = requests.post(
            f"{root_url.rstrip('/')}/api/v1/canned-responses",
            headers=hdr, json={"shortcut": shortcut, "text": text, "scope": "global"},
            timeout=timeout,
        )
        return r.status_code == 200 and r.json().get("success") is True
    except (requests.RequestException, ValueError):
        return False


def set_livechat_available(root_url: str, auth: Auth, password: str, timeout: float = 15.0) -> bool:
    """Set the logged-in agent available for Omnichannel. Note: the workspace
    only shows as "online" to visitors once that agent also has a live presence
    (i.e. is logged into the RC UI) — this just flips the availability flag."""
    headers = {**auth.headers(), "Content-Type": "application/json", **password_2fa_headers(password)}
    try:
        resp = requests.post(
            f"{root_url.rstrip('/')}/api/v1/livechat/agent.status",
            headers=headers, json={"status": "available"}, timeout=timeout,
        )
        return resp.status_code == 200 and resp.json().get("success") is True
    except (requests.RequestException, ValueError):
        return False


def fetch_saml_idp_cert(descriptor_url: str, timeout: float = 90.0, interval: float = 3.0) -> str | None:
    """Fetch an IdP's signing cert from its SAML metadata descriptor.

    Retries until the IdP (e.g. Keycloak, which boots slowly) is serving.
    Returns the base64 DER cert body (single line, RC's idp cert format).
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            resp = requests.get(descriptor_url, timeout=10)
            if resp.status_code == 200:
                m = re.search(r"<[^>]*X509Certificate>([^<]+)<", resp.text)
                if m:
                    return "".join(m.group(1).split())
        except requests.RequestException:
            pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def call(
    root_url: str,
    method: str,
    path: str,
    auth: Auth | None = None,
    data: dict | None = None,
    extra_headers: dict | None = None,
    timeout: float = 30.0,
) -> tuple[int, str]:
    """Make an authed REST call. Returns (status_code, body_text)."""
    url = f"{root_url.rstrip('/')}/{path.lstrip('/')}"
    headers = auth.headers() if auth else {}
    if extra_headers:
        headers.update(extra_headers)
    resp = requests.request(method.upper(), url, headers=headers, json=data, timeout=timeout)
    return resp.status_code, resp.text
