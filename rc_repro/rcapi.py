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


def login(
    root_url: str,
    user: str = config.ADMIN_USERNAME,
    password: str = config.ADMIN_PASSWORD,
    timeout: float = 10.0,
) -> Auth:
    """Log in and return an Auth (token + user id)."""
    resp = requests.post(
        f"{root_url.rstrip('/')}/api/v1/login",
        json={"user": user, "password": password},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json().get("data", {})
    token = data.get("authToken")
    user_id = data.get("userId")
    if not token or not user_id:
        raise RuntimeError(f"login did not return a token: {resp.text[:200]}")
    return Auth(token=token, user_id=user_id)


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
