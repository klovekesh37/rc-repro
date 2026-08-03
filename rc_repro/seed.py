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
from rc_repro.errors import ValidationError
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


@dataclass(frozen=True)
class Plan:
    users: int
    channels: int
    messages: int      # per channel
    dms: int
    rich: bool         # threads + reactions
    # Optional for positional compatibility with callers that constructed Plan
    # directly before the public Seed Dataset contract existed.
    profile: str = ""

    @property
    def user_names(self) -> list[str]:
        return [username(i) for i in range(self.users)]

    @property
    def channel_names(self) -> list[str]:
        return [channel_name(i) for i in range(self.channels)]

    @property
    def group_names(self) -> list[str]:
        return list(_GROUP_NAMES) if self.rich and self.user_names else []

    @property
    def dm_pairs(self) -> list[tuple[str, str]]:
        """Stable identities for the requested direct-message rooms."""
        names = self.user_names
        if len(names) < 2:
            return []
        pairs: list[tuple[str, str]] = []
        # Walk increasing gaps so the common profile keeps adjacent, readable
        # pairs while never producing the same unordered room twice.
        for gap in range(1, len(names)):
            for start in range(len(names) - gap):
                pairs.append((names[start], names[start + gap]))
                if len(pairs) == self.dms:
                    return pairs
        return pairs

    @staticmethod
    def _thread_count(messages: int, rich: bool) -> int:
        # One deterministic thread reply every fifth base message. Reactions are
        # not messages and therefore never affect this count.
        return (messages + 4) // 5 if rich and messages else 0

    def message_targets(self) -> list[tuple[str, int]]:
        """Return the rooms and base message counts owned by this seed."""
        targets = [(name, self.messages) for name in self.channel_names]
        if self.rich and self.user_names:
            targets.extend((name, max(3, self.messages // 3))
                            for name in self.group_names)
        targets.append(("general", self.messages))
        return targets

    def expected_counts(self) -> dict[str, int]:
        targets = self.message_targets()
        base = sum(count for _, count in targets)
        threads = sum(self._thread_count(count, self.rich) for _, count in targets)
        dms = len(self.dm_pairs)
        return {
            "users": self.users,
            "channels": self.channels,
            "groups": len(self.group_names),
            "messages": base + threads,
            "dm_messages": dms,
            "dms": dms,
            "thread_replies": threads,
        }

    def expected_rooms(self) -> dict[str, int]:
        """Return each planned room and its total message count."""
        return {
            name: count + self._thread_count(count, self.rich)
            for name, count in self.message_targets()
        }

    def as_dict(self) -> dict:
        """Stable, JSON-safe representation of the resolved Seed Dataset plan."""
        return {
            "profile": self.profile or "custom",
            # ``messages`` retains the documented per-channel value;
            # ``messages_per_channel`` makes the meaning explicit to agents.
            "users": self.users,
            "channels": self.channels,
            "messages": self.messages,
            "messages_per_channel": self.messages,
            # The resolved count can be lower than the profile request when a
            # caller overrides users below the minimum needed for a pair.
            "dms": len(self.dm_pairs),
            "requested_dms": self.dms,
            "rich": self.rich,
            "identities": {
                "users": self.user_names,
                "channels": self.channel_names,
                "groups": self.group_names,
                "dm_pairs": [list(pair) for pair in self.dm_pairs],
            },
            "targets": {
                "messages": {name: count for name, count in self.message_targets()},
            },
            "expected": self.expected_counts(),
        }


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
        profile=profile,
    )


def username(i: int) -> str:
    base = _FIRST_NAMES[i % len(_FIRST_NAMES)]
    grp = i // len(_FIRST_NAMES)
    return base if grp == 0 else f"{base}{grp + 1}"


def channel_name(i: int) -> str:
    base = _CHANNEL_NAMES[i % len(_CHANNEL_NAMES)]
    grp = i // len(_CHANNEL_NAMES)
    return base if grp == 0 else f"{base}-{grp + 1}"


class SeedVerificationError(ValidationError):
    """A seed wrote data but could not prove it matches its resolved plan."""

    def __init__(self, message: str, result: dict) -> None:
        super().__init__(message)
        self.result = result


def seed(root_url, admin: rcapi.Auth, plan: Plan, log=lambda m: None) -> dict:
    """Seed the repro and verify the resolved plan through REST readback.

    Write responses are useful progress evidence but are not proof of final
    state: a successful request can still leave zero visible messages.  The
    returned record keeps the plan, write observations, API readback, and
    verification decision separate.
    """
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
    # Prior values (None = the read failed). "Unreadable" is NOT "was on": the
    # old rule restored ON for both, so a single transient 500 — likely, since
    # seeding starts the moment RC answers — turned email-2FA on for the ldap /
    # saml / oidc / livechat presets, which deliberately switch it OFF because
    # their users have no mailbox here. Only touch a value we actually observed,
    # and only put back what we actually changed.
    email_2fa_prev = rcapi.get_setting(root_url, admin, config.ADMIN_PASSWORD, email_2fa)
    limiter_was_off = rcapi.get_setting(root_url, admin, config.ADMIN_PASSWORD, rate_limiter) is False
    email_2fa_changed = False
    _authorship_warning = (
        "seeded users may not be loginable, so messages will be authored by admin")
    if email_2fa_prev is None:
        log(f"  ⚠ could not read the email-2FA setting — leaving it alone; {_authorship_warning}")
    elif email_2fa_prev:
        email_2fa_changed = _set(email_2fa, False)
        if not email_2fa_changed:
            # Previously silent: the bool was discarded, so a failed disable
            # surfaced only as "0 usable as authors" further down.
            log(f"  ⚠ could not disable email-2FA — {_authorship_warning}")
    if not limiter_was_off and not _set(rate_limiter, False):
        log("  ⚠ could not disable the API rate limiter — seed rates may be throttled")

    try:
        result = _seed_body(root_url, admin_hdr, plan, post, log)
        # Keep the setting-restore helper usable in isolation: callers and tests
        # may replace the write phase with a lightweight stub that has no dataset
        # observations to verify. Real seed runs always return ``actual`` and
        # therefore take the REST readback path below.
        if not isinstance(result, dict) or "actual" not in result:
            return result
        participants = result.pop("_participants", {})
        result["readback"] = readback(
            root_url, admin, plan, get=session.get, fallback=result.get("actual"),
            participants=participants,
        )
        result["verification"] = verify_plan(plan, result["readback"])
        if not result["verification"]["ok"]:
            raise SeedVerificationError(
                f"seed verification failed: {_verification_message(result['verification'])}",
                result,
            )
        return result
    finally:
        if not limiter_was_off:
            _set(rate_limiter, True)
        if email_2fa_changed:
            _set(email_2fa, True)


def _message_id(resp) -> str | None:
    """The posted message's `_id`, or None.

    `.ok` is true for any 2xx, and a front proxy (Traefik fronts the
    multi-instance preset) can answer 200 with an HTML error page — a raw
    JSONDecodeError there would abort the entire seed mid-way.
    """
    try:
        return (resp.json().get("message") or {}).get("_id")
    except (ValueError, AttributeError):
        return None


def _seed_body(root_url, admin_hdr: dict, plan: Plan, post, log) -> dict:
    """Create content and keep requested versus successful writes separate."""
    durs = {"users": 0.0, "channels": 0.0, "messages": 0.0, "dms": 0.0}
    msg = Timings()   # primary chat.postMessage latencies
    logs: list[str] = []
    targets = plan.message_targets()
    attempted = {
        "users": plan.users,
        "channels": plan.channels + len(plan.group_names),
        "messages": sum(count for _, count in targets),
        "thread_replies": sum(plan._thread_count(count, plan.rich)
                               for _, count in targets),
        "dm_messages": len(plan.dm_pairs),
        "dms": len(plan.dm_pairs),
    }
    actual = {
        "users": 0, "channels": 0, "groups": 0, "messages": 0,
        "thread_replies": 0, "dm_messages": 0, "dms": 0,
    }

    def say(message: str) -> None:
        logs.append(message)
        log(message)

    def ok(response) -> bool:
        return response is not None and bool(getattr(response, "ok", False))

    def timed(bucket: str, path: str, headers: dict, payload: dict):
        t = time.monotonic()
        response = post(path, headers, payload)
        durs[bucket] += time.monotonic() - t
        return response

    # 1. Users (idempotent: an existing user just gets logged into).
    tokens: dict[str, rcapi.Auth] = {}
    names = plan.user_names
    created_names: set[str] = set()
    _t = time.monotonic()
    for un in names:
        response = post("/api/v1/users.create", admin_hdr, {
            "name": un.capitalize(), "username": un, "email": f"{un}@example.com",
            "password": un, "verified": True, "requirePasswordChange": False,
            "joinDefaultChannels": True,
        })
        if ok(response):
            created_names.add(un)
        try:
            tokens[un] = rcapi.login(root_url, un, un)
        except Exception:  # noqa: BLE001 - fall back to admin authorship
            pass
    durs["users"] = time.monotonic() - _t
    actual["users"] = len(created_names | set(tokens))
    say(f"users: {actual['users']} ({len(tokens)} usable as authors)")

    def hdr_for(members: list[str]) -> dict:
        authors = [tokens[u] for u in members if u in tokens]
        return ({**random.choice(authors).headers(), "Content-Type": "application/json"}
                if authors else admin_hdr)

    def post_messages(channel_ref: str, members: list[str], count: int) -> int:
        n = 0
        for index in range(count):
            t = time.monotonic()
            response = post("/api/v1/chat.postMessage", hdr_for(members),
                            {"channel": channel_ref, "text": random.choice(_MESSAGES)})
            dt = time.monotonic() - t
            durs["messages"] += dt
            if not ok(response):
                continue
            msg.add(dt * 1000)
            n += 1
            actual["messages"] += 1
            # Rich profiles are deterministic now, so the plan can predict the
            # exact number of thread replies instead of reporting an estimate.
            if plan.rich and index % 5 == 0:
                mid = _message_id(response)
                if mid:
                    tr = timed("messages", "/api/v1/chat.postMessage", hdr_for(members),
                               {"channel": channel_ref, "text": random.choice(_MESSAGES),
                                "tmid": mid})
                    if ok(tr):
                        n += 1
                        actual["messages"] += 1
                        actual["thread_replies"] += 1
                    timed("messages", "/api/v1/chat.react", hdr_for(members),
                          {"messageId": mid,
                           "emoji": random.choice([":+1:", ":tada:", ":eyes:"])})
        return n

    # 2. Public channels with a random member subset.
    for cn in plan.channel_names:
        members = (random.sample(names, k=min(len(names), random.randint(3, 8)))
                   if names else [])
        if ok(timed("channels", "/api/v1/channels.create", admin_hdr,
                    {"name": cn, "members": members})):
            actual["channels"] += 1
        post_messages(f"#{cn}", members, plan.messages)
    say(f"channels: {actual['channels']}")

    # 3. A couple of private groups (rich profiles).
    if plan.rich and names:
        for gn in plan.group_names:
            members = random.sample(names, k=min(len(names), 4))
            gr = timed("channels", "/api/v1/groups.create", admin_hdr,
                       {"name": gn, "members": members})
            # On a re-seed the group already exists, groups.create fails, and this
            # run's freshly sampled members are not in it — their posts then 400
            # (RC can't auto-join a private group). Only author into a group this
            # run actually created.
            if gr is None or not gr.ok:
                continue
            actual["groups"] += 1
            post_messages(f"#{gn}", members, max(3, plan.messages // 3))

    # 4. Messages into the default GENERAL channel (everyone is a member).
    post_messages("general", names, plan.messages)

    # 5. Direct messages between the deterministic pairs in the plan.
    dms = 0
    for u1, u2 in plan.dm_pairs:
        hdr = ({**tokens[u1].headers(), "Content-Type": "application/json"}
               if u1 in tokens else admin_hdr)
        room = timed("dms", "/api/v1/im.create", hdr, {"username": u2})
        if not ok(room):
            continue
        actual["dms"] += 1
        dm_message = timed(
            "dms", "/api/v1/chat.postMessage", hdr,
            {"channel": f"@{u2}", "text": random.choice(_MESSAGES)},
        )
        if ok(dm_message):
            actual["dm_messages"] += 1
            dms += 1
    say(f"messages: {actual['messages']} (attempted {attempted['messages']})  DMs: {dms}")

    return {
        "plan": plan.as_dict(),
        "users": actual["users"], "channels": actual["channels"],
        "messages": actual["messages"], "dms": actual["dms"],
        "attempted": attempted, "actual": actual, "logs": logs,
        # Kept private and removed by seed() before the result crosses a public
        # interface. These tokens let readback inspect user-owned DM rooms.
        "_participants": tokens,
        "durations": durs, "latency": msg.summary(), "latency_hist": msg.histogram(),
    }


def _verification_message(verification: dict) -> str:
    """Format all failed verification dimensions without leaking credentials."""
    details = [
        f"{key}: expected {value['expected']}, read back {value['actual']}"
        for key, value in sorted((verification.get("mismatches") or {}).items())
    ]
    details.extend(
        f"{key} identities differ"
        for key in sorted(verification.get("identity_mismatches") or {})
    )
    details.extend(
        f"{key} room differs"
        for key in sorted(verification.get("room_mismatches") or {})
    )
    unavailable = verification.get("unavailable") or []
    if unavailable:
        details.append("unavailable: " + ", ".join(sorted(unavailable)))
    return "; ".join(details) or "no exact readback was available"


def _payload_items(payload: dict | None, *keys: str) -> list[dict] | None:
    """Extract list responses across RC's legacy and current envelopes."""
    if not isinstance(payload, dict):
        return None
    candidates = [payload]
    for parent in ("data", "update"):
        child = payload.get(parent)
        if isinstance(child, dict):
            candidates.append(child)
    for candidate in candidates:
        for key in keys:
            value = candidate.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                return [value]
    return None


def _room_messages(room: dict | None) -> int | None:
    if not isinstance(room, dict):
        return None
    for key in ("msgs", "messageCount", "totalMessages"):
        value = room.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def readback(root_url: str, admin: rcapi.Auth, plan: Plan, *, get=None,
             fallback: dict | None = None,
             participants: dict[str, rcapi.Auth] | None = None) -> dict:
    """Read planned identities and room counters through the REST interface."""
    if get is None:
        session = requests.Session()
        get = session.get
    headers = admin.headers()
    fallback = fallback or {}
    participants = participants or {}
    unavailable: list[str] = []

    def fetch(path: str, params: dict | None = None,
              auth: rcapi.Auth | None = None) -> dict | None:
        try:
            response = get(
                f"{root_url.rstrip('/')}{path}",
                headers=auth.headers() if auth else headers,
                params=params or {}, timeout=30,
            )
            if response is None or not getattr(response, "ok", False):
                return None
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except (requests.RequestException, ValueError, TypeError):
            return None

    limit = max(100, plan.users + plan.channels + len(plan.group_names) + len(plan.dm_pairs) + 20)
    users_payload = fetch("/api/v1/users.list", {"count": limit, "offset": 0})
    user_rows = _payload_items(users_payload, "users")
    if user_rows is None:
        unavailable.append("users")
        user_names: list[str] = []
    else:
        wanted = set(plan.user_names)
        user_names = sorted({row.get("username", "") for row in user_rows} & wanted)

    channels_payload = fetch("/api/v1/channels.list", {"count": limit, "offset": 0})
    channel_rows = _payload_items(channels_payload, "channels")
    if channel_rows is None:
        unavailable.append("channels")
        channel_names: list[str] = []
    else:
        wanted = set(plan.channel_names)
        channel_names = sorted({row.get("name", "") for row in channel_rows} & wanted)

    group_rows: list[dict] | None = []
    if plan.group_names:
        groups_payload = fetch("/api/v1/groups.list", {"count": limit, "offset": 0})
        group_rows = _payload_items(groups_payload, "groups")
        if group_rows is None:
            unavailable.append("groups")
            group_names: list[str] = []
        else:
            wanted = set(plan.group_names)
            group_names = sorted({row.get("name", "") for row in group_rows} & wanted)
    else:
        group_names = []

    rows_by_name = {
        row.get("name"): row for row in (channel_rows or []) + (group_rows or [])
        if row.get("name")
    }
    messages_by_room: dict[str, int] = {}
    for room_name, expected in plan.message_targets():
        count = _room_messages(rows_by_name.get(room_name))
        if count is None:
            kind = "groups" if room_name in plan.group_names else "channels"
            detail = fetch(f"/api/v1/{kind}.info", {"roomName": room_name})
            detail_rows = _payload_items(detail, "channel", "group", "room")
            count = _room_messages(detail_rows[0] if detail_rows else None)
        if count is None:
            kind = "groups" if room_name in plan.group_names else "channels"
            history = fetch(
                f"/api/v1/{kind}.history",
                {"roomName": room_name, "count": max(1000, expected + 100)},
            )
            history_rows = _payload_items(history, "messages")
            if history_rows is not None:
                count = len(history_rows)
        if count is None:
            unavailable.append(f"messages:{room_name}")
            count = int(fallback.get("messages", 0)) if room_name == "general" else 0
        messages_by_room[room_name] = count

    im_payload = fetch("/api/v1/im.list", {"count": limit, "offset": 0})
    im_rows = _payload_items(im_payload, "ims", "rooms")
    planned_pairs = {tuple(sorted(pair)) for pair in plan.dm_pairs}
    dm_rows: dict[tuple[str, str], dict] = {}

    def add_dm_rows(rows: list[dict] | None) -> None:
        for row in rows or []:
            names = tuple(sorted({name for name in row.get("usernames") or []
                                  if name in plan.user_names}))
            if len(names) == 2 and names in planned_pairs:
                dm_rows.setdefault(names, row)

    add_dm_rows(im_rows)
    # Admins do not necessarily see user-authored DMs. Read the missing pairs as
    # one of their participants instead of turning a normal seed into an
    # unverifiable success merely because of that visibility boundary.
    for pair in sorted(planned_pairs):
        if pair in dm_rows:
            continue
        participant = participants.get(pair[0]) or participants.get(pair[1])
        if participant is None:
            continue
        payload = fetch("/api/v1/im.list", {"count": limit, "offset": 0}, participant)
        add_dm_rows(_payload_items(payload, "ims", "rooms"))

    if dm_rows or not planned_pairs:
        dm_pairs = [list(pair) for pair in sorted(dm_rows)]
        dm_count = len(dm_rows)
        dm_messages = sum(_room_messages(row) or 0 for row in dm_rows.values())
    else:
        # Keep write observations in the record, but make the inability to verify
        # the requested DMs explicit rather than presenting them as exact.
        unavailable.append("dms")
        dm_count = int(fallback.get("dms", 0))
        dm_messages = int(fallback.get("dm_messages", 0))
        dm_pairs = []

    total_messages = sum(messages_by_room.values())
    base_messages = sum(count for _, count in plan.message_targets())
    return {
        "users": len(user_names),
        "channels": len(channel_names),
        "groups": len(group_names),
        "messages": total_messages,
        "dm_messages": dm_messages,
        "dms": dm_count,
        "thread_replies": max(0, total_messages - base_messages),
        "usernames": user_names,
        "channel_names": channel_names,
        "group_names": group_names,
        "dm_pairs": dm_pairs,
        "messages_by_room": messages_by_room,
        "unavailable": sorted(set(unavailable)),
        "complete": not unavailable,
        "source": "api" if not unavailable else "api+write-responses",
    }


def verify_plan(plan: Plan, observed: dict) -> dict:
    """Compare readback counts with the resolved plan without hiding mismatches."""
    expected = plan.expected_counts()
    actual = {key: observed.get(key) for key in expected}
    mismatches = {
        key: {"expected": expected[key], "actual": actual[key]}
        for key in expected
        if actual[key] is not None and actual[key] != expected[key]
    }
    expected_identities = {
        "usernames": plan.user_names,
        "channel_names": plan.channel_names,
        "group_names": plan.group_names,
        "dm_pairs": [list(pair) for pair in plan.dm_pairs],
    }
    identity_mismatches = {}
    unavailable = list(observed.get("unavailable", []))
    for key, wanted in expected_identities.items():
        if key not in observed:
            unavailable.append(key)
            continue
        got = observed.get(key) or []
        if sorted(got) != sorted(wanted):
            identity_mismatches[key] = {"expected": wanted, "actual": got}
    expected_rooms = plan.expected_rooms()
    room_mismatches = {}
    if "messages_by_room" in observed:
        room_data = observed.get("messages_by_room") or {}
        for name, wanted in expected_rooms.items():
            if name not in room_data:
                unavailable.append(f"messages:{name}")
            elif room_data[name] != wanted:
                room_mismatches[name] = {"expected": wanted, "actual": room_data[name]}
    else:
        unavailable.extend(f"messages:{name}" for name in expected_rooms)
    for key in expected:
        if actual[key] is None:
            unavailable.append(key)
    return {
        "ok": not mismatches and not identity_mismatches and not room_mismatches and not unavailable,
        "exact": not unavailable,
        "expected": expected,
        "actual": actual,
        "mismatches": mismatches,
        "identity_mismatches": identity_mismatches,
        "room_mismatches": room_mismatches,
        "unavailable": sorted(set(unavailable)),
    }
