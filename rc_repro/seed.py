"""Populate a repro with realistic content via the Rocket.Chat REST API.

Seeds as the admin, but authors messages as the created users (logging in as each,
since we set their passwords) for realistic multi-author rooms. Email-2FA is
disabled first so those logins aren't blocked, and the API rate limiter is toggled
off during seeding, then restored.

Two properties this module is built around. Both were missing, and each was worth
having on its own; together they are what makes a seeded workspace a REPRODUCTION
rather than a pile of plausible content.

**A plan is a manifest, not a set of counts.** Every room is named, every membership
decided and every message counted before the first HTTP call. Nothing is chosen at
random: members come from a rotating window over the user list, authors round-robin
through a room's members, message text is indexed. Two runs of the same profile
produce the same workspace, so a customer's ticket and your reproduction can be
compared -- and so the result can be READ BACK and checked against what was asked
for, which a plan of counts cannot support.

**Every kind of room Rocket.Chat has.** Public and private channels, public and
private teams, channels inside a team of either visibility, discussions hung off a
parent room (with and without a parent message), direct messages, threads inside any
of them, and `general`. Support tickets are rarely about a public channel: they are
about a discussion inside a private team channel, or a thread nobody can see. A
seeder that only makes public channels cannot reproduce them.

Messages go through `chat.sendMessage` with an explicit `rid`, which is the one call
shape that works for every room kind above -- and the only one that can create a
THREAD. `chat.postMessage` with a `tmid` answers 400, so every thread reply this
module attempted before now was silently lost; the rooms had reactions and no
threads while the profile advertised both.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

from rc_repro import config, rcapi
from rc_repro.services import journal
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
_PRIVATE_NAMES = ["leadership", "private-project", "security", "hiring"]
_TEAM_NAMES = ["platform", "customer-success", "infrastructure"]
_TEAM_CHANNEL_NAMES = ["planning", "standup", "incidents", "releases"]
_DISCUSSION_NAMES = [
    "customer escalation 4821", "rollback plan", "release checklist",
    "post-mortem draft", "capacity for Q3", "onboarding questions",
]
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
_REPLIES = [
    "Good catch — looking now.",
    "Same here, reproduced on 8.5.1.",
    "Fixed in the branch, pushing shortly.",
    "Can you share the log line?",
    "Agreed, let's do that.",
]
_EMOJI = [":+1:", ":tada:", ":eyes:", ":rocket:"]

#: One message in every N gets a thread reply. A fixed stride rather than a
#: probability, so the plan can state how many replies there will be -- and so the
#: readback can tell "the thread was not created" from "the dice said no".
THREAD_EVERY = 5

#: And one in every N gets a reaction, on the profiles that ask for them.
REACT_EVERY = 3

#: What a discussion created FROM a message starts with, before anyone posts into
#: it: Rocket.Chat copies the parent message in as the discussion's anchor -- it
#: comes back with an empty `msg` and no `t`, so a message listing counts it as
#: content -- and the `reply` we send alongside becomes the first real line.
#:
#: MEASURED, on live 7.4.1 and 8.5.1, not assumed: the first guess was one, and the
#: room held two. It is stated here rather than absorbed into a fudge factor because
#: a plan that cannot say what a room will contain is the thing this module exists
#: to stop being.
#:
#: 6.x was deliberately not measured -- it is out of support, and a floor nobody
#: runs is a claim nobody can maintain. If the number ever differs on a future
#: release the room reads as holding MORE than planned, which is reported and not a
#: fault, so an unmeasured version degrades quietly rather than failing.
ANCHOR_MESSAGES = 2

# --- the room kinds -----------------------------------------------------------
#: Every shape of room Rocket.Chat has, because every one of them is something a
#: customer files a ticket about. The kind decides which endpoint creates it and
#: which endpoint reads it back; nothing else in this module branches on it.
CHANNEL = "channel"                              # public, standalone
PRIVATE = "private"                              # private, standalone
TEAM = "team"                                    # a public team's main room
TEAM_PRIVATE = "team-private"                    # a private team's main room
TEAM_CHANNEL = "team-channel"                    # public channel inside a team
TEAM_CHANNEL_PRIVATE = "team-channel-private"    # private channel inside a team
DISCUSSION = "discussion"                        # a child room of a parent room
DM = "dm"
GENERAL = "general"                              # the room every workspace ships

#: Kinds whose rooms a discussion may hang off. `general` is excluded on purpose:
#: it is the one room a reader will already be looking at, and burying the seeded
#: discussions there makes them hard to find rather than easy.
_DISCUSSABLE = (CHANNEL, PRIVATE, TEAM, TEAM_PRIVATE, TEAM_CHANNEL,
                TEAM_CHANNEL_PRIVATE)

#: Which kinds are private, for the readback (`groups.*` vs `channels.*`) and for
#: anything that wants to say so on screen.
_PRIVATE_KINDS = (PRIVATE, TEAM_PRIVATE, TEAM_CHANNEL_PRIVATE)


@dataclass(frozen=True)
class RoomSpec:
    """One room, fully decided before anything is created."""

    kind: str
    name: str
    members: tuple[str, ...]
    messages: int
    #: Indexes of this room's messages that get one threaded reply each. A tuple
    #: rather than a count, so the readback knows WHICH message to look under.
    threads: tuple[int, ...] = ()
    #: Indexes of this room's messages that get a reaction. Planned rather than
    #: decided while posting, for the same reason as `threads`: a count nobody can
    #: predict is a count nobody can check, and reactions were the one part of a
    #: `rich` profile that did work -- so they were also the part with no evidence.
    reactions: tuple[int, ...] = ()
    #: A team's name for a team channel; the parent room's name for a discussion.
    parent: str = ""
    #: For a discussion: the index of the parent message it hangs off, or -1 for a
    #: discussion created against the room rather than against a message. Both are
    #: real paths in the product and they produce different system messages.
    from_message: int = -1

    @property
    def private(self) -> bool:
        return self.kind in _PRIVATE_KINDS

    @property
    def replies(self) -> int:
        return len(self.threads)

    @property
    def reacted(self) -> int:
        return len(self.reactions)

    @property
    def total_messages(self) -> int:
        """Messages that will exist in the room: the base ones, the replies, and the
        opening line of a discussion that was created from a message.

        A threaded reply IS a message in the room -- `channels.messages` returns it
        with a `tmid` -- so a count that omits it does not match what the server
        reports, which is exactly the mismatch a verifier would report as a fault.

        A discussion opened from a message is the same trap one level down: it
        arrives holding `ANCHOR_MESSAGES` before anyone posts into it, and leaving
        those out made every message-anchored discussion report more than planned on
        a perfectly good seed.
        """
        return (self.messages + self.replies
                + (ANCHOR_MESSAGES if self.from_message >= 0 else 0))


@dataclass(frozen=True)
class Shape:
    """How much of each kind a profile asks for. Turned into rooms by `_build`."""

    users: int
    channels: int
    private: int
    teams: int
    team_channels: int      # per team
    discussions: int
    dms: int
    messages: int           # per room, before replies
    reactions: bool


#: `small` is deliberately not a scaled-down `standard`: it still contains ONE of
#: every kind, because the point of the small profile is to have something of each
#: to click through, and a profile that omits teams cannot demonstrate a team bug.
PROFILES: dict[str, Shape] = {
    "small": Shape(users=5, channels=2, private=1, teams=1, team_channels=2,
                   discussions=2, dms=2, messages=5, reactions=False),
    "standard": Shape(users=20, channels=5, private=2, teams=2, team_channels=2,
                      discussions=3, dms=5, messages=20, reactions=True),
    "large": Shape(users=100, channels=12, private=4, teams=3, team_channels=2,
                   discussions=6, dms=20, messages=100, reactions=True),
}


@dataclass(frozen=True)
class Plan:
    """The manifest: what will exist when seeding finishes."""

    profile: str
    users: int
    #: Standalone public channels. Kept as a top-level field because both
    #: front-ends print it in the "seeding N users, N channels" line.
    channels: int
    messages: int           # per room, before replies
    reactions: bool
    rooms: tuple[RoomSpec, ...] = ()

    @property
    def dms(self) -> int:
        return sum(1 for r in self.rooms if r.kind == DM)

    @property
    def rich(self) -> bool:
        """Kept for callers that ask whether this profile does threads/reactions."""
        return self.reactions

    @property
    def total_messages(self) -> int:
        return sum(r.total_messages for r in self.rooms)

    @property
    def total_replies(self) -> int:
        return sum(r.replies for r in self.rooms)

    def by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rooms:
            out[r.kind] = out.get(r.kind, 0) + 1
        return out

    def named(self, name: str) -> RoomSpec | None:
        return next((r for r in self.rooms if r.name == name), None)


def username(i: int) -> str:
    base = _FIRST_NAMES[i % len(_FIRST_NAMES)]
    grp = i // len(_FIRST_NAMES)
    return base if grp == 0 else f"{base}{grp + 1}"


def _pool_name(pool: list[str], i: int, sep: str = "-") -> str:
    base = pool[i % len(pool)]
    grp = i // len(pool)
    return base if grp == 0 else f"{base}{sep}{grp + 1}"


def channel_name(i: int) -> str:
    return _pool_name(_CHANNEL_NAMES, i)


def private_name(i: int) -> str:
    return _pool_name(_PRIVATE_NAMES, i)


def team_name(i: int) -> str:
    return _pool_name(_TEAM_NAMES, i)


def team_channel_name(team_i: int, i: int) -> str:
    """Prefixed with the team, because a channel name is unique workspace-wide.

    Two teams both wanting `planning` is the common case, and the second create
    would fail with a name clash rather than producing the second team's channel.
    """
    return f"{team_name(team_i)}-{_pool_name(_TEAM_CHANNEL_NAMES, i)}"


def _members(names: list[str], index: int, size: int) -> tuple[str, ...]:
    """A rotating window over the user list: deterministic, and varied per room.

    `random.sample` gave a different membership on every run, which is why nothing
    downstream could state what a room would contain. The stride of 3 keeps
    consecutive rooms from having identical membership without needing randomness.
    """
    if not names:
        return ()
    size = max(1, min(size, len(names)))
    return tuple(names[(index * 3 + j) % len(names)] for j in range(size))


def _threads(messages: int, every: int = THREAD_EVERY) -> tuple[int, ...]:
    return tuple(range(0, messages, every)) if messages else ()


def dm_pair(names: list[str], i: int) -> tuple[str, str]:
    """The i-th DM pair, walking increasing gaps so no pair repeats early.

    `random.sample(names, 2)` drew with replacement across DMs, so the same pair
    could be picked twice -- and the second `im.create` returns the SAME room, so a
    plan asking for five DM rooms could produce four.

    Walking `gap` gives `n * ((n - 1) // 2)` distinct pairs before any repeats. The
    bound is not `n * (n // 2)`: at `gap == n / 2` on an even user count the pair
    {a, a + gap} is the same unordered pair as {a + gap, a}, so that last band of
    gaps yields half as many. No profile comes near the limit -- `large` asks for 20
    DMs from 100 users, where the bound is 4,950 -- but a caller passing --users 4
    with 20 DMs would, and it should get repeats rather than a wrong promise.
    """
    n = len(names)
    # The gap wraps within 1..n-1 so it can never be a multiple of n, because a gap
    # of 0 pairs a user WITH THEMSELVES -- `im.create` accepts that and makes a
    # self-DM, which is a real Rocket.Chat object and not what a plan asking for a
    # conversation between two people meant. It appeared the moment `--users 2` was
    # passed with the default five DMs.
    gap = 1 + (i // n) % (n - 1)
    a = i % n
    return names[a], names[(a + gap) % n]


def _build(shape: Shape, names: list[str]) -> tuple[RoomSpec, ...]:
    """Turn a shape into the concrete list of rooms, in CREATION order.

    Order matters and is part of the manifest: a team exists before its channels,
    and every discussion's parent exists before the discussion. Sorting this list
    later would break seeding, so nothing does.
    """
    rooms: list[RoomSpec] = []
    idx = 0

    def add(kind: str, name: str, size: int, messages: int, **kw) -> RoomSpec:
        nonlocal idx
        spec = RoomSpec(kind=kind, name=name, members=_members(names, idx, size),
                        messages=messages, threads=_threads(messages),
                        reactions=_threads(messages, REACT_EVERY) if shape.reactions else (),
                        **kw)
        rooms.append(spec)
        idx += 1
        return spec

    for i in range(shape.channels):
        add(CHANNEL, channel_name(i), 4, shape.messages)
    for i in range(shape.private):
        # Fewer messages: a private room's value here is that it EXISTS and is
        # invisible to a non-member, not that it is busy.
        add(PRIVATE, private_name(i), 3, max(3, shape.messages // 2))
    for t in range(shape.teams):
        # Alternating, so any profile with two or more teams has one of each. A
        # private team is a different object from a private channel -- its main
        # room is type `p` AND it owns other rooms -- and only one of the two is
        # exercised by making every team public.
        tname = team_name(t)
        add(TEAM if t % 2 == 0 else TEAM_PRIVATE, tname, 4, max(3, shape.messages // 2))
        for c in range(shape.team_channels):
            add(TEAM_CHANNEL if c % 2 == 0 else TEAM_CHANNEL_PRIVATE,
                team_channel_name(t, c), 3, max(3, shape.messages // 2), parent=tname)
    # Everyone is already in `general`, so it is the one room where the whole user
    # list authors.
    rooms.append(RoomSpec(
        GENERAL, "general", tuple(names), shape.messages, _threads(shape.messages),
        reactions=_threads(shape.messages, REACT_EVERY) if shape.reactions else ()))

    parents = [r for r in rooms if r.kind in _DISCUSSABLE]
    for d in range(shape.discussions):
        if not parents:
            break
        parent = parents[d % len(parents)]
        # Alternating: half are opened FROM a message (which is how a support
        # engineer usually makes one, and which leaves a different system message
        # in the parent), half against the room itself.
        add(DISCUSSION, _pool_name(_DISCUSSION_NAMES, d, sep=" "), 3,
            max(2, shape.messages // 4),
            parent=parent.name, from_message=0 if d % 2 == 0 else -1)

    # Capped at the number of DISTINCT pairs: `im.create` for a pair that already has
    # a room returns that same room, so asking for five DMs from two users produced
    # five records pointing at one or two rooms -- and the verification then reported
    # rooms as short that were simply the same room counted twice.
    n = len(names)
    for i in range(min(shape.dms, n * (n - 1) // 2)):
        if n < 2:
            break
        a, b = dm_pair(names, i)
        msgs = max(2, shape.messages // 5)
        rooms.append(RoomSpec(
            DM, f"{a}~{b}", (a, b), msgs, _threads(msgs),
            reactions=_threads(msgs, REACT_EVERY) if shape.reactions else ()))
    return tuple(rooms)


def plan_from(profile: str, users=None, channels=None, messages=None) -> Plan:
    if profile not in PROFILES:
        raise ValueError(
            f"unknown seed profile {profile!r} (want {' | '.join(PROFILES)})"
        )
    base = PROFILES[profile]
    shape = Shape(
        users=base.users if users is None else max(0, users),
        channels=base.channels if channels is None else max(0, channels),
        private=base.private, teams=base.teams, team_channels=base.team_channels,
        discussions=base.discussions, dms=base.dms,
        messages=base.messages if messages is None else max(0, messages),
        reactions=base.reactions,
    )
    names = [username(i) for i in range(shape.users)]
    return Plan(profile=profile, users=shape.users, channels=shape.channels,
                messages=shape.messages, reactions=shape.reactions,
                rooms=_build(shape, names))


def seed(root_url, admin: rcapi.Auth, plan: Plan, log=lambda m: None, workspace: str = "",
         tokens_out: dict | None = None) -> dict:
    """Seed the repro. `log(msg)` is called with progress lines.

    `tokens_out`, when given, is filled with the per-user sessions this created, for
    a readback that has to look at a DM. An out-parameter rather than a key in the
    result: the result is written into `repro.json` and rendered in a browser, and a
    session token has no business in either.
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
    # "UNREADABLE" IS NOT "WAS ON" -- the rule the email-2FA lines above state in as
    # many words, and the limiter line twelve lines down did not follow. `... is False`
    # makes an unreadable setting indistinguishable from one that was on, so a single
    # transient 500 (likely: seeding starts the moment RC answers) made this disable the
    # limiter and then force it ON in the `finally`, on a workspace that may have had it
    # off deliberately. Only touch what was actually observed.
    limiter_prev = rcapi.get_setting(root_url, admin, config.ADMIN_PASSWORD, rate_limiter)
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
    # JOURNALLED, both of them. The `finally` below is the only thing that puts these
    # back, and it is exactly the block a SIGKILL, an OOM or a `systemctl restart` does
    # not run. A GUI seed of the `large` profile is minutes long against `jobs.drain`'s
    # 25 seconds, so this is a routine interruption, not an exotic one -- and what
    # survives is a workspace with the rate limiter AND email-2FA off, silently
    # different from the one somebody is about to measure, with nothing on disk for
    # `doctor` or `serve`'s recovery to find. `workspace` is optional so the pure
    # seed path stays callable without a name; every real caller passes it.
    limiter_note = email_2fa_note = ""
    if limiter_prev is True:
        if workspace:
            limiter_note = journal.record(journal.RATE_LIMITER_OFF, workspace)
        if not _set(rate_limiter, False):
            journal.clear(limiter_note)
            limiter_note = ""
            log("  ⚠ could not disable the API rate limiter — seed rates may be throttled")
    if email_2fa_changed and workspace:
        email_2fa_note = journal.record(journal.EMAIL_2FA_OFF, workspace)

    try:
        return _seed_body(root_url, admin_hdr, plan, post, log, tokens_out)
    finally:
        if limiter_prev is True:
            if _set(rate_limiter, True):
                journal.clear(limiter_note)
        if email_2fa_changed:
            if _set(email_2fa, True):
                journal.clear(email_2fa_note)


def _json(resp) -> dict:
    """A response's JSON body, or {}.

    `.ok` is true for any 2xx, and a front proxy (Traefik fronts the multi-instance
    preset) can answer 200 with an HTML error page — a raw JSONDecodeError there
    would abort the entire seed mid-way.
    """
    try:
        return resp.json() or {}
    except (ValueError, AttributeError):
        return {}


def _message_id(resp) -> str | None:
    """The posted message's `_id`, or None."""
    return (_json(resp).get("message") or {}).get("_id")


@dataclass
class _Made:
    """What actually got created, alongside the spec that asked for it."""

    spec: RoomSpec
    rid: str = ""
    team_id: str = ""
    reused: bool = False
    messages: int = 0
    replies: int = 0
    reactions: int = 0
    failed: str = ""
    message_ids: list[str] = field(default_factory=list)


def _seed_body(root_url, admin_hdr: dict, plan: Plan, post, log,
               tokens_out: dict | None = None) -> dict:
    """Create everything the plan names; split out so seed() can guarantee setting
    restoration in a finally. Times each phase and collects per-message latency for
    the seed timing breakdown."""
    durs = {"users": 0.0, "channels": 0.0, "messages": 0.0, "dms": 0.0}
    msg = Timings()   # primary chat.sendMessage latencies

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
    if tokens_out is not None:
        tokens_out.update(tokens)
    log(f"users: {len(names)} ({len(tokens)} usable as authors)")

    def hdr_for(members: tuple[str, ...], turn: int) -> dict:
        """The author of one message: the members, round-robin.

        Round-robin rather than a random pick, for the same reason as everything
        else here -- but it is also better content: a random pick can leave a
        member who never speaks, while this guarantees every member of a room
        authors in it as soon as there are as many messages as members.
        """
        authors = [tokens[u] for u in members if u in tokens]
        if not authors:
            return admin_hdr
        return {**authors[turn % len(authors)].headers(),
                "Content-Type": "application/json"}

    made: dict[str, _Made] = {}

    def _room_id(body: dict) -> str:
        for key in ("channel", "group", "room", "discussion"):
            rid = (body.get(key) or {}).get("_id")
            if rid:
                return rid
        return ""

    def _lookup(spec: RoomSpec) -> str:
        """The id of a room that already exists, so a re-seed adds to it.

        A second `rc-repro seed` used to abandon a private group it had not created
        this run (its freshly-sampled members were not in it, so every post 400'd).
        With a deterministic manifest the membership is the same on every run, so
        the room can simply be reused -- and the readback reports the totals, which
        is a better answer than silently doing less.
        """
        path = "/api/v1/groups.info" if spec.private else "/api/v1/channels.info"
        try:
            r = requests.get(f"{root_url.rstrip('/')}{path}",
                             headers=admin_hdr, params={"roomName": spec.name}, timeout=30)
        except requests.RequestException:
            return ""
        return _room_id(_json(r)) if r.ok else ""

    # 2. Rooms, in the manifest's order (a team before its channels, a parent
    #    before its discussions). Every call goes through `timed`, so the buckets
    #    add up without an outer wrapper subtracting one from another.
    for spec in plan.rooms:
        rec = _Made(spec=spec)
        made[spec.name] = rec
        members = list(spec.members)
        if spec.kind == DISCUSSION:
            # NOT here. Half of them are meant to hang off a MESSAGE in the parent,
            # and at this point no message has been posted anywhere -- so the `pmid`
            # branch could never fire and every discussion came out attached to the
            # room instead. The whole variant was dead code, silently. They are
            # created in their own pass below, after the parents have said something.
            continue
        if spec.kind == GENERAL:
            # Not created -- every workspace ships it. Looked up, because messages
            # go by rid and a name would not reach a renamed default room.
            rec.rid = _lookup(spec)
            if not rec.rid:
                rec.failed = "general not found"
            continue
        if spec.kind == DM:
            owner = spec.members[0]
            hdr = ({**tokens[owner].headers(), "Content-Type": "application/json"}
                   if owner in tokens else admin_hdr)
            r = timed("dms", "/api/v1/im.create", hdr, {"username": spec.members[1]})
            # post() returns None only on a TRANSPORT error, so a 400/403 (revoked
            # create-d permission, Accounts_Direct_Message_Max_Users) still counted
            # as a DM and still fired a doomed postMessage. The reported count then
            # landed in the benchmark report as workload that was never created.
            if r is None or not r.ok:
                rec.failed = "im.create"
                continue
            rec.rid = _room_id(_json(r))
            continue
        if spec.kind in (TEAM, TEAM_PRIVATE):
            r = timed("channels", "/api/v1/teams.create", admin_hdr, {
                "name": spec.name, "type": 1 if spec.private else 0,
                "members": list(members)})
            body = _json(r)
            if r is not None and r.ok:
                rec.team_id = (body.get("team") or {}).get("_id", "")
                rec.rid = (body.get("team") or {}).get("roomId", "")
            else:
                rec.rid, rec.reused = _lookup(spec), True
                if not rec.rid:
                    rec.failed = "teams.create"
            continue
        # A plain channel or group, standalone or inside a team. `extraData.teamId`
        # puts it in the team in ONE call -- `teams.createRoom` does not exist on
        # 8.5.1 (404), and create-then-`teams.addRooms` is two round trips for the
        # same result.
        path = "/api/v1/groups.create" if spec.private else "/api/v1/channels.create"
        payload: dict = {"name": spec.name, "members": list(members)}
        if spec.parent:
            team = made.get(spec.parent)
            if team and team.team_id:
                payload["extraData"] = {"teamId": team.team_id}
        r = timed("channels", path, admin_hdr, payload)
        if r is not None and r.ok:
            rec.rid = _room_id(_json(r))
        else:
            rec.rid, rec.reused = _lookup(spec), True
            if not rec.rid:
                rec.failed = "create"

    # 3. Messages, threads and reactions. One call shape for every room kind:
    #    chat.sendMessage against an rid. A discussion's `name` is a generated slug
    #    rather than its title, so addressing rooms by `#name` -- as this used to --
    #    cannot reach one at all.
    def fill(ordinal: int, rec: _Made) -> None:
        spec = rec.spec
        if not rec.rid:
            return
        bucket = "dms" if spec.kind == DM else "messages"
        for i in range(spec.messages):
            # Stride 7 against a pool of 16 is coprime, so a room walks the whole
            # pool before repeating; the room's ordinal offsets it so two rooms do
            # not open with the same line. The first cut multiplied by the count of
            # messages ALREADY posted, which made the effective stride 8 -- half the
            # pool size -- and every room said "Hey team, any update on this?" three
            # times out of five.
            body = {"rid": rec.rid,
                    "msg": _MESSAGES[(ordinal * 5 + i * 7) % len(_MESSAGES)]}
            t = time.monotonic()
            r = post("/api/v1/chat.sendMessage", hdr_for(spec.members, i), {"message": body})
            dt = time.monotonic() - t
            durs[bucket] += dt
            if r is None or not r.ok:
                continue
            msg.add(dt * 1000)
            rec.messages += 1
            mid = _message_id(r)
            if mid:
                rec.message_ids.append(mid)
            if i in spec.threads and mid:
                # THE THREAD. `chat.postMessage` with a `tmid` answers 400, so every
                # reply this module used to attempt was lost and no thread was ever
                # created -- while `rich` profiles advertised them. `chat.sendMessage`
                # takes the whole message object, `tmid` included, and is what the
                # client itself uses.
                tr = timed(bucket, "/api/v1/chat.sendMessage", hdr_for(spec.members, i + 1),
                           {"message": {"rid": rec.rid, "tmid": mid,
                                        "msg": _REPLIES[i % len(_REPLIES)]}})
                if tr is not None and tr.ok:
                    rec.replies += 1
            if mid and i in spec.reactions:
                rr = timed(bucket, "/api/v1/chat.react", hdr_for(spec.members, i),
                           {"messageId": mid, "emoji": _EMOJI[i % len(_EMOJI)]})
                if rr is not None and rr.ok:
                    rec.reactions += 1

    ordinals = {name: n for n, name in enumerate(made)}
    for name, rec in made.items():
        if rec.spec.kind != DISCUSSION:
            fill(ordinals[name], rec)

    # 4. Discussions LAST, so the half that hang off a message have one to hang off.
    for name, rec in made.items():
        spec = rec.spec
        if spec.kind != DISCUSSION:
            continue
        parent = made.get(spec.parent)
        if not parent or not parent.rid:
            rec.failed = "no parent room"
            continue
        payload: dict = {"prid": parent.rid, "t_name": spec.name,
                         "users": list(spec.members)}
        if 0 <= spec.from_message < len(parent.message_ids):
            # Opened FROM a message, which is how a support engineer usually makes
            # one, and which leaves a different system message in the parent.
            payload["pmid"] = parent.message_ids[spec.from_message]
            payload["reply"] = _REPLIES[spec.from_message % len(_REPLIES)]
        r = timed("channels", "/api/v1/rooms.createDiscussion", admin_hdr, payload)
        if r is None or not r.ok:
            rec.failed = "rooms.createDiscussion"
            continue
        rec.rid = _room_id(_json(r))
        fill(ordinals[name], rec)

    return _result(plan, made, names, durs, msg, log)


def _result(plan: Plan, made: dict[str, _Made], names: list[str],
            durs: dict, msg: Timings, log) -> dict:
    """The seed summary. The legacy keys keep their meaning; the rest are new."""
    rooms = [r for r in made.values()]
    created = [r for r in rooms if r.rid and not r.failed]
    total_msgs = sum(r.messages for r in rooms)
    total_replies = sum(r.replies for r in rooms)
    dms = sum(1 for r in created if r.spec.kind == DM)
    by_kind: dict[str, int] = {}
    for r in created:
        by_kind[r.spec.kind] = by_kind.get(r.spec.kind, 0) + 1
    failed = [(r.spec.name, r.failed) for r in rooms if r.failed]
    if failed:
        log(f"  ⚠ {len(failed)} room(s) not created: "
            + ", ".join(f"{n} ({why})" for n, why in failed[:4]))
    log(f"messages: {total_msgs + total_replies} ({total_replies} in threads)  "
        f"rooms: {len(created)}  DMs: {dms}")
    return {
        # Legacy keys, unchanged in meaning: both front-ends and the benchmark
        # report read these.
        "users": len(names),
        "channels": sum(1 for r in created if r.spec.kind == CHANNEL),
        "messages": total_msgs + total_replies,
        "dms": dms,
        "durations": durs, "latency": msg.summary(), "latency_hist": msg.histogram(),
        # The manifest's own vocabulary, for the readback and for anything that
        # wants to say what is in there.
        "profile": plan.profile,
        "rooms": by_kind,
        "rooms_total": len(created),
        "threads": total_replies,
        "reactions": sum(r.reactions for r in rooms),
        "reused": sum(1 for r in created if r.reused),
        "failed": [{"name": n, "why": w} for n, w in failed],
        "planned": {
            "rooms": plan.by_kind(),
            "rooms_total": len(plan.rooms),
            "messages": plan.total_messages,
            "threads": plan.total_replies,
            "users": plan.users,
        },
        "created": [{"kind": r.spec.kind, "name": r.spec.name, "rid": r.rid,
                     "parent": r.spec.parent, "private": r.spec.private,
                     "messages": r.messages, "replies": r.replies,
                     "reused": r.reused}
                    for r in created],
    }


# --- readback -----------------------------------------------------------------
#
# Seeding reports what it ASKED for. Reading it back reports what is there, and the
# two are not the same thing: a create can answer 200 and leave nothing, a message
# can be accepted and dropped, and until this existed the only symptom was a number
# in a report that nobody could check. `messages: ~62` -- the tilde was the tool
# admitting it did not know.
#
# Two rules this makes, both learned from a version of it that failed a healthy
# workspace:
#
# 1. "I could not read it" is NOT "it is wrong". A single 500 on `channels.list`
#    must not fail an otherwise perfect seed, so unreadable rooms are reported in
#    their own list and never counted as faults.
# 2. System messages are not content. A room that gained a discussion holds a
#    `discussion-created` message, and the room's own `msgs` counter includes it --
#    so counting either would report a mismatch for a workspace that is exactly
#    right. Only messages with no `t` are content.

#: `get()`'s answer for "the server says this room does not exist", as opposed to
#: "the server could not be asked". One is a fault and the other is a gap in the
#: check, and they must never be the same value.
ABSENT = "<absent>"


#: A message with one of these is the SERVER talking about the room, not a person
#: talking in it. Rather than list them (there are dozens -- `uj`, `au`, `ru`,
#: `room_changed_topic`, `discussion-created`, ...), anything carrying a `t` at all
#: is treated as a system message, which is what the client does to decide whether
#: to render it as a grey line.
def _is_content(message: dict) -> bool:
    return not message.get("t")


@dataclass
class RoomFacts:
    """What the server says about one planned room."""

    spec: RoomSpec
    found: bool = False
    rid: str = ""
    messages: int = 0        # content messages, replies included
    replies: int = 0         # of those, the ones inside a thread
    reacted: int = 0         # of those, the ones carrying at least one reaction
    threads: int = 0         # parent messages that have a thread
    in_team: bool = True     # for a team channel: is it actually IN the team
    unreadable: str = ""     # why this room could not be checked at all


def readback(root_url, admin: rcapi.Auth, plan: Plan,
             login=rcapi.login, tokens: dict | None = None) -> dict:
    """Re-read the seeded workspace and report what is actually there.

    Reads as the ADMIN wherever the admin can see the room, and as a participant
    where it cannot: a direct message belongs to its two users and is invisible to
    everyone else, so a readback that only used the admin token would report every
    DM missing.

    `tokens` are the sessions the SEEDER already minted, and passing them is not an
    optimisation. Seeding turns email-2FA off for its own logins and turns it back
    on afterwards -- so by the time this runs, signing in as a user needs a code
    from a mailbox that does not exist, and every DM comes back "cannot sign in".
    On a workspace with 2FA off the fallback login works and this can be called on
    its own. `login` is injectable so a test does not need a server.
    """
    base = root_url.rstrip("/")
    session = requests.Session()
    hdr = {**admin.headers(), "Content-Type": "application/json"}
    cache: dict[str, dict] = {
        name: {**auth.headers(), "Content-Type": "application/json"}
        for name, auth in (tokens or {}).items()}

    def get(path: str, headers: dict, **params):
        """(body, why). `why` is "" on success and `ABSENT` when the room is gone.

        Rocket.Chat answers a lookup for a room that does not exist with HTTP 400
        and `errorType: error-room-not-found`, which is an ANSWER, not a failure to
        ask -- and the difference is the whole verdict. Treating it as "could not
        read" filed a deleted room under `unreadable`, where nothing counts it as a
        fault, so a workspace missing a room verified clean. Found live, by deleting
        one and watching the check pass.
        """
        try:
            r = session.get(f"{base}{path}", headers=headers, params=params, timeout=30)
        except requests.RequestException as exc:
            return None, str(exc)
        if not r.ok:
            body = _json(r)
            if r.status_code in (400, 404) and "not-found" in str(body.get("errorType", "")):
                return None, ABSENT
            return None, f"HTTP {r.status_code}"
        return _json(r), ""

    def as_user(name: str) -> dict | None:
        if name not in cache:
            try:
                cache[name] = {**login(root_url, name, name).headers(),
                               "Content-Type": "application/json"}
            except Exception:  # noqa: BLE001 - reported as unreadable, never a fault
                cache[name] = {}
        return cache[name] or None

    def count_messages(rid: str, path: str, headers: dict) -> tuple[int, int, int, str]:
        """(content messages, replies, reacted, why).

        All three come out of ONE listing: a reacted message carries a `reactions`
        object and a reply carries a `tmid`, so asking per message would be N calls
        for facts the room already handed over.
        """
        body, why = get(path, headers, roomId=rid, count=0)
        if body is None:
            return 0, 0, 0, why
        msgs = [m for m in (body.get("messages") or []) if _is_content(m)]
        return (len(msgs),
                sum(1 for m in msgs if m.get("tmid")),
                sum(1 for m in msgs if m.get("reactions")),
                "")

    facts: list[RoomFacts] = []
    rids: dict[str, str] = {}
    for spec in plan.rooms:
        f = RoomFacts(spec=spec)
        facts.append(f)
        if spec.kind == DM:
            headers = as_user(spec.members[0])
            if headers is None:
                f.unreadable = f"cannot sign in as {spec.members[0]}"
                continue
            body, why = get("/api/v1/im.list", headers, count=0)
            if body is None:
                f.unreadable = why
                continue
            for room in (body.get("ims") or body.get("update") or []):
                if spec.members[1] in (room.get("usernames") or []):
                    f.found, f.rid = True, room.get("_id", "")
                    break
            if f.rid:
                f.messages, f.replies, f.reacted, f.unreadable = count_messages(
                    f.rid, "/api/v1/im.messages", headers)
            continue
        if spec.kind == DISCUSSION:
            prid = rids.get(spec.parent)
            if not prid:
                # The parent's own row already says whether it is absent or merely
                # unreadable; repeating the guess here would double-count one fault.
                parent = next((x for x in facts if x.spec.name == spec.parent), None)
                if parent is not None and not parent.found and not parent.unreadable:
                    continue          # parent is absent -> this one is too, reported once
                f.unreadable = f"parent {spec.parent!r} not readable"
                continue
            body, why = get("/api/v1/chat.getDiscussions", hdr, roomId=prid, count=100)
            if body is None:
                if why != ABSENT:
                    f.unreadable = why
                continue
            # Matched on the DISPLAY name: `rooms.createDiscussion` gives the room a
            # generated slug for its `name` and puts the title in `fname`, so a
            # readback keyed on the name it was asked for finds nothing at all.
            for m in (body.get("messages") or []):
                if m.get("msg") == spec.name:
                    f.found, f.rid = True, m.get("drid", "")
                    break
            if f.rid:
                path = ("/api/v1/groups.messages" if spec.private
                        else "/api/v1/channels.messages")
                f.messages, f.replies, f.reacted, f.unreadable = count_messages(
                    f.rid, path, hdr)
            continue
        info_path = "/api/v1/groups.info" if spec.private else "/api/v1/channels.info"
        body, why = get(info_path, hdr, roomName=spec.name)
        if body is None:
            if why != ABSENT:
                f.unreadable = why
            continue
        room = body.get("channel") or body.get("group") or {}
        f.rid = room.get("_id", "")
        f.found = bool(f.rid)
        if not f.found:
            continue
        rids[spec.name] = f.rid
        msg_path = "/api/v1/groups.messages" if spec.private else "/api/v1/channels.messages"
        f.messages, f.replies, f.reacted, f.unreadable = count_messages(
            f.rid, msg_path, hdr)
        tl, why = get("/api/v1/chat.getThreadsList", hdr, rid=f.rid, count=100)
        if tl is not None:
            f.threads = int(tl.get("total") or 0)
        if spec.parent and spec.kind in (TEAM_CHANNEL, TEAM_CHANNEL_PRIVATE):
            # A channel can exist and NOT be in the team, which looks identical from
            # `channels.info` and is the whole difference between a team workspace
            # and a workspace with similarly-named channels.
            f.in_team = _in_team(get, hdr, spec.parent, spec.name)

    users, why = get("/api/v1/users.list", hdr, count=0)
    known = ({u.get("username") for u in (users.get("users") or [])}
             if users is not None else set())
    planned_users = [username(i) for i in range(plan.users)]
    return {
        "rooms": facts,
        "users_found": sorted(u for u in planned_users if u in known),
        "users_unreadable": why if users is None else "",
        "planned_users": planned_users,
    }


def _in_team(get, hdr, team_name_: str, room_name: str) -> bool:
    body, _ = get("/api/v1/teams.info", hdr, teamName=team_name_)
    team_id = ((body or {}).get("teamInfo") or {}).get("_id")
    if not team_id:
        return False
    rooms, _ = get("/api/v1/teams.listRooms", hdr, teamId=team_id, count=100)
    return any(r.get("name") == room_name for r in ((rooms or {}).get("rooms") or []))


def verify(plan: Plan, facts: dict) -> dict:
    """Compare the manifest with what was read back.

    Returns findings, never raises: whether a mismatch should end the command is
    the CALLER's decision, and the default is that it should not. A verifier that
    fails a healthy workspace teaches people to pass the override by reflex, and
    then it is protecting nothing -- which is exactly what happened to the version
    of this that shipped with an exact-equality gate and a known-lossy seeder.
    """
    faults: list[dict] = []
    extra: list[dict] = []
    unreadable: list[dict] = []

    def fault(kind: str, name: str, want, got, detail: str = "") -> None:
        faults.append({"kind": kind, "room": name, "want": want, "got": got,
                       "detail": detail})

    def counted(kind: str, name: str, want: int, got: int) -> None:
        """A count that disagrees -- and WHICH WAY it disagrees decides everything.

        Seeding only ever adds, so `got > want` cannot be evidence that this run
        lost anything: the room already held content, which is what a second
        `rc-repro seed`, a preset that posts on boot, or a restored backup all look
        like. Reported, because a reader comparing against the manifest should know,
        but never as a fault -- gating on exact equality is what made the earlier
        version of this refuse a workspace that was working.

        `got < want` is the real signal: something was asked for and is not there.
        """
        if got < want:
            fault(kind, name, want, got)
        elif got > want:
            extra.append({"kind": kind, "room": name, "want": want, "got": got})

    missing_users = [u for u in facts["planned_users"] if u not in facts["users_found"]]
    if facts.get("users_unreadable"):
        unreadable.append({"what": "users", "why": facts["users_unreadable"]})
    elif missing_users:
        fault("users", "", len(facts["planned_users"]),
              len(facts["users_found"]), ", ".join(missing_users[:5]))

    for f in facts["rooms"]:
        spec = f.spec
        if f.unreadable:
            unreadable.append({"what": spec.name, "why": f.unreadable})
            continue
        if not f.found:
            fault("missing", spec.name, spec.kind, "absent")
            continue
        counted("messages", spec.name, spec.total_messages, f.messages)
        counted("threads", spec.name, spec.replies, f.replies)
        counted("reactions", spec.name, spec.reacted, f.reacted)
        if not f.in_team:
            fault("team", spec.name, f"in {spec.parent}", "not in the team")
        # A discussion's ANCHOR is checked by that message count, not separately: one
        # opened from a message arrives holding ANCHOR_MESSAGES it did not post, so a
        # `pmid` that silently stopped being sent makes the room come back two short
        # -- a fault, named by room. That is not a happy accident; it is why the
        # anchor is in the plan instead of being subtracted out. The variant was dead
        # code for a while (discussions were created before their parents had said
        # anything) and nothing noticed, which is what this now prevents.
    return {
        "ok": not faults,
        "faults": faults,
        "extra": extra,
        "unreadable": unreadable,
        "checked": len(facts["rooms"]),
        "rooms": [{"kind": f.spec.kind, "name": f.spec.name, "found": f.found,
                   "messages": f.messages, "want_messages": f.spec.total_messages,
                   "replies": f.replies, "want_replies": f.spec.replies,
                   "reacted": f.reacted, "want_reacted": f.spec.reacted,
                   "threads": f.threads, "in_team": f.in_team,
                   "unreadable": f.unreadable}
                  for f in facts["rooms"]],
    }
