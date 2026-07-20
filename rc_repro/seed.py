"""Populate a repro with realistic content (users, channels, DMs, messages,
threads, reactions) via the Rocket.Chat REST API.

Seeds as the admin, but authors messages as the created users (logging in as
each, since we set their passwords) for realistic multi-author rooms. Email-2FA
is disabled first so those logins aren't blocked, and the API rate limiter is
toggled off during seeding, then restored.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

import requests

from rc_repro import config, rcapi
from rc_repro.perf import Timings

# Realistic pools; overflow gets a numeric suffix (e.g. alice, bob, …, alice2).
# NOTE: deliberately avoids `userN` names, which the ldap/saml presets use.
_FIRST_NAMES = [
    "alice", "bob", "carol", "dave", "erin", "frank", "grace", "heidi",
    "ivan", "judy", "mallory", "niaj", "olivia", "peggy", "quinn", "rupert",
    "sybil", "trent", "uma", "victor", "wendy", "xavier", "yvonne", "zack",
]
_CHANNEL_NAMES = [
    "team-chat", "dev", "support", "random", "announcements", "design",
    "qa", "ops", "product", "sales", "incidents", "watercooler",
]
_GROUP_NAMES = ["leadership", "private-project"]
_MESSAGES = [
    "Hey team, any update on this?",
    "Can someone review my PR when you get a chance?",
    "Deploying to staging now 🚀",
    "Heads up: maintenance window tonight at 10pm.",
    "Thanks, that fixed it!",
    "I'm seeing the same issue on my end.",
    "Let's sync about this in standup.",
    "Docs are updated, please take a look.",
    "Good morning! What's on the agenda today?",
    "Anyone free for a quick call?",
    "Ticket #4821 is resolved.",
    "Nice work everyone 👏",
    "Reminder: submit your timesheets.",
    "Rolling back the last change, it broke the build.",
    "LGTM ✅",
    "Where did we land on the pricing question?",
]


@dataclass
class Plan:
    users: int
    channels: int
    messages: int      # per channel
    dms: int
    rich: bool         # threads + reactions


PROFILES: dict[str, Plan] = {
    "small": Plan(users=5, channels=3, messages=5, dms=2, rich=False),
    "standard": Plan(users=20, channels=8, messages=20, dms=5, rich=True),
    "large": Plan(users=100, channels=20, messages=100, dms=20, rich=True),
}


def plan_from(profile: str, users=None, channels=None, messages=None) -> Plan:
    if profile not in PROFILES:
        raise ValueError(
            f"unknown seed profile {profile!r} (want {' | '.join(PROFILES)})"
        )
    base = PROFILES[profile]
    return Plan(
        users=base.users if users is None else max(0, users),
        channels=base.channels if channels is None else max(0, channels),
        messages=base.messages if messages is None else max(0, messages),
        dms=base.dms,
        rich=base.rich,
    )


def username(i: int) -> str:
    base = _FIRST_NAMES[i % len(_FIRST_NAMES)]
    grp = i // len(_FIRST_NAMES)
    return base if grp == 0 else f"{base}{grp + 1}"


def channel_name(i: int) -> str:
    base = _CHANNEL_NAMES[i % len(_CHANNEL_NAMES)]
    grp = i // len(_CHANNEL_NAMES)
    return base if grp == 0 else f"{base}-{grp + 1}"


def seed(root_url, admin: rcapi.Auth, plan: Plan, log=lambda m: None) -> dict:
    """Seed the repro. `log(msg)` is called with progress lines."""
    base = root_url.rstrip("/")
    session = requests.Session()
    admin_hdr = {**admin.headers(), "Content-Type": "application/json"}

    def post(path: str, headers: dict, payload: dict):
        try:
            return session.post(f"{base}{path}", headers=headers, json=payload, timeout=30)
        except requests.RequestException:
            return None

    def _set(setting_id: str, value) -> bool:
        return rcapi.set_setting(root_url, admin, config.ADMIN_PASSWORD, setting_id, value)

    # Make seeding possible/fast: new users' logins aren't blocked by email-2FA,
    # and bulk calls aren't throttled. Both settings are restored to their PRIOR
    # values afterwards — in a finally, so a mid-seed crash can't leave the
    # workspace's security settings silently changed. ("Was off before" is only
    # honoured when we could actually read the setting; unknown -> restore on.)
    email_2fa = "Accounts_TwoFactorAuthentication_By_Email_Enabled"
    rate_limiter = "API_Enable_Rate_Limiter"
    # Prior values (None = couldn't read). Restore is keyed on the KNOWN-off
    # state only: an unreadable setting must never leave 2FA disabled, so unknown
    # (None) restores ON — matching the limiter's "unknown -> restore on" rule.
    email_2fa_prev = rcapi.get_setting(root_url, admin, config.ADMIN_PASSWORD, email_2fa)
    limiter_was_off = rcapi.get_setting(root_url, admin, config.ADMIN_PASSWORD, rate_limiter) is False
    _set(email_2fa, False)
    if not limiter_was_off and not _set(rate_limiter, False):
        log("  ⚠ could not disable the API rate limiter — seed rates may be throttled")

    try:
        return _seed_body(root_url, admin_hdr, plan, post, log)
    finally:
        if not limiter_was_off:
            _set(rate_limiter, True)
        if email_2fa_prev is not False:   # was on, or unknown -> restore on
            _set(email_2fa, True)


def _seed_body(root_url, admin_hdr: dict, plan: Plan, post, log) -> dict:
    """The actual content creation (users/channels/DMs/messages); split out so
    seed() can guarantee setting restoration in a finally. Times each phase and
    collects per-message latency for the seed timing breakdown."""
    durs = {"users": 0.0, "channels": 0.0, "messages": 0.0, "dms": 0.0}
    msg = Timings()   # primary chat.postMessage latencies

    def timed(bucket: str, path: str, headers: dict, payload: dict):
        t = time.monotonic()
        r = post(path, headers, payload)
        durs[bucket] += time.monotonic() - t
        return r

    # 1. Users (idempotent: an existing user just gets logged into).
    tokens: dict[str, rcapi.Auth] = {}
    names = [username(i) for i in range(plan.users)]
    _t = time.monotonic()
    for un in names:
        post("/api/v1/users.create", admin_hdr, {
            "name": un.capitalize(), "username": un, "email": f"{un}@example.com",
            "password": un, "verified": True, "requirePasswordChange": False,
            "joinDefaultChannels": True,
        })
        try:
            tokens[un] = rcapi.login(root_url, un, un)
        except Exception:  # noqa: BLE001 - fall back to admin authorship
            pass
    durs["users"] = time.monotonic() - _t
    log(f"users: {len(names)} ({len(tokens)} usable as authors)")

    def hdr_for(members: list[str]) -> dict:
        authors = [tokens[u] for u in members if u in tokens]
        return {**random.choice(authors).headers(), "Content-Type": "application/json"} if authors else admin_hdr

    def post_messages(channel_ref: str, members: list[str], count: int) -> int:
        n = 0
        for _ in range(count):
            t = time.monotonic()
            r = post("/api/v1/chat.postMessage", hdr_for(members),
                     {"channel": channel_ref, "text": random.choice(_MESSAGES)})
            dt = time.monotonic() - t
            durs["messages"] += dt
            if r is None or not r.ok:
                continue
            msg.add(dt * 1000)
            n += 1
            if plan.rich and random.random() < 0.2:
                mid = (r.json().get("message") or {}).get("_id")
                if mid:
                    # The thread reply is a real extra message — count it so the
                    # reported total isn't understated (the reaction is not).
                    tr = timed("messages", "/api/v1/chat.postMessage", hdr_for(members),
                               {"channel": channel_ref, "text": random.choice(_MESSAGES), "tmid": mid})
                    if tr is not None and tr.ok:
                        n += 1
                    timed("messages", "/api/v1/chat.react", hdr_for(members),
                          {"messageId": mid, "emoji": random.choice([":+1:", ":tada:", ":eyes:"])})
        return n

    # 2. Public channels with a random member subset.
    total_msgs = 0
    for i in range(plan.channels):
        cn = channel_name(i)
        members = random.sample(names, k=min(len(names), random.randint(3, 8))) if names else []
        timed("channels", "/api/v1/channels.create", admin_hdr, {"name": cn, "members": members})
        total_msgs += post_messages(f"#{cn}", members, plan.messages)
    log(f"channels: {plan.channels}")

    # 3. A couple of private groups (rich profiles).
    if plan.rich and names:
        for gn in _GROUP_NAMES:
            members = random.sample(names, k=min(len(names), 4))
            timed("channels", "/api/v1/groups.create", admin_hdr, {"name": gn, "members": members})
            total_msgs += post_messages(f"#{gn}", members, max(3, plan.messages // 3))

    # 4. Messages into the default GENERAL channel (everyone is a member).
    total_msgs += post_messages("general", names, plan.messages)

    # 5. Direct messages between random pairs.
    dms = 0
    for _ in range(plan.dms):
        if len(names) < 2:
            break
        u1, u2 = random.sample(names, 2)
        hdr = {**tokens[u1].headers(), "Content-Type": "application/json"} if u1 in tokens else admin_hdr
        if timed("dms", "/api/v1/im.create", hdr, {"username": u2}) is not None:
            timed("dms", "/api/v1/chat.postMessage", hdr, {"channel": f"@{u2}", "text": random.choice(_MESSAGES)})
            dms += 1
    log(f"messages: ~{total_msgs}  DMs: {dms}")

    return {
        "users": len(names), "channels": plan.channels, "messages": total_msgs, "dms": dms,
        "durations": durs, "latency": msg.summary(), "latency_hist": msg.histogram(),
    }
