# `--seed` — design draft (for review)

> **Status:** DRAFT — not built. Review, edit, and add/remove anything. Open
> questions are at the bottom; that's where your input matters most.

## 1. Goal

Every repro today is an **empty** Rocket.Chat — one admin, no rooms, no
messages. Many tickets can't be reproduced in an empty box. `--seed` populates a
repro with realistic content (users, channels, DMs, messages, threads) so it
*looks and behaves like a real workspace* seconds after boot.

## 2. Why (use cases)

- **"Messages not syncing / search / notifications"** — need existing rooms + message history to reproduce.
- **UI / performance with data** — sidebar with many channels, long message lists, mentions.
- **Permissions / roles** — multiple users with different roles to test access.
- **Admin features** — user management, moderation, retention need populated data.
- **Demos** — a believable workspace for showing a fix.

Not a replacement for LDAP-scale user counts (that's the `ldap` preset's LDIF), nor
for restoring a customer DB (out of scope). This is *content realism*.

## 3. How you'd use it

Two entry points — a standalone command (the engine) and sugar on `up`:

```bash
# seed an already-running repro (re-runnable)
rc-repro seed --name acme-1234 --profile standard

# seed at creation time (implies --wait; seeds once RC is ready)
rc-repro up --version 8.5.1 --name acme-1234 --seed
rc-repro up --version 8.5.1 --seed --profile large

# fine-grained counts (override a profile)
rc-repro seed --name acme-1234 --users 30 --channels 10 --messages 40
```

### Profiles (presets of counts)

| Profile | Users | Channels | Msgs/channel | DMs | Threads/Reactions |
|---------|-------|----------|--------------|-----|-------------------|
| `small` (default) | 5 | 3 | 5 | 2 | no |
| `standard` | 20 | 8 | 20 | 5 | yes |
| `large` | 100 | 20 | 100 | 20 | yes |

`--users/--channels/--messages` override the chosen profile's numbers.

## 4. What gets seeded

| Item | How | Notes |
|------|-----|-------|
| **Users** | `POST /api/v1/users.create` (verified=true, known password) | e.g. `alice`, `bob`, … or `seed_userN` (see Q1). Password known so we can post as them. |
| **Public channels** | `POST /api/v1/channels.create` with `members` | realistic names (`dev`, `support`, `random`, …) + numbered filler |
| **Private groups** | `POST /api/v1/groups.create` | a couple, for permission testing |
| **DMs** | `POST /api/v1/im.create` | between random seeded users |
| **Messages** | `POST /api/v1/chat.postMessage` | posted **as** the seeded users (see Q2) into channels + `GENERAL` |
| **Threads** | `chat.postMessage` with `tmid` | standard/large only |
| **Reactions** | `POST /api/v1/chat.react` | standard/large only |

Message text comes from a small canned pool of support-flavoured lines, cycled
(no external lorem dependency).

## 5. How it works (mechanism)

1. Ensure RC is ready + get the **admin** auth (we already do this for the wizard/cert steps).
2. **Create users** as admin. Cache each user's login token (log in once with the known password) so messages can be authored *by them* → realistic multi-author rooms.
3. **Create channels/groups** (admin), adding seeded users as members.
4. **Create DMs** between random pairs.
5. **Post messages** round-robin: for each room, post N messages cycling through its members as authors (falls back to admin if a login fails).
6. **Threads/reactions** (standard/large) on a subset of messages.
7. Print a summary: `seeded 20 users, 8 channels, 160 messages`.

Runs via the existing `rcapi` REST client. All endpoints above are **not**
2FA-guarded (verified with `users.create` earlier); if any turn out to be, we
already have `password_2fa_headers()`.

### Where it hooks in the code

- New module **`rc_repro/seed.py`**: `seed(root_url, admin_auth, plan) -> Summary`.
- New CLI command **`seed`** (calls it on a ready repro).
- **`up --seed`** sets `wait=True` and calls `seed()` at the end of `_do_ready`
  (same place the SAML cert step runs), so it works with no extra flags.

## 6. Limits & edge cases

- **API rate limiter** — RC throttles endpoints (`API_Enable_Rate_Limiter`). Large seeds could hit it. Plan: temporarily disable it via `set_setting` during seeding, re-enable after (or add small delays). See Q6.
- **Timestamps** — `chat.postMessage` stamps *now*; REST can't backdate. Historical-date repros would need a Mongo insert (out of scope for v1). See Q5.
- **Idempotency / re-seed** — `users.create` fails if the user exists. Plan: skip-if-exists and continue, so re-running just tops up. `--reset` (delete prior seeded content) is possible but fiddly (identifying "seeded" rooms) — probably later.
- **Scale** — REST seeding is fine for hundreds; not 130k users (use `ldap` for that). Document the practical ceiling (~a few hundred users / few thousand messages).
- **Name collisions** — `ldap`/`saml` presets already create `user1…userN`. Seed users must not clash — use different names (see Q1).
- **Non-default presets** — seeding an `ldap`/`saml` repro is fine (adds local content alongside the directory users), but the *authors* are local seed users, not LDAP ones.

## 7. Open questions (your call — edit inline)

1. **User naming:** realistic (`alice`, `bob`, `carol`…) or `seed_userN`? Realistic reads better in demos; numbered scales further. (Must avoid the `ldap`/`saml` `userN` names.)
2. **Message authorship:** post **as each user** (realistic multi-author rooms, but N extra logins) vs **all as admin** (simpler/faster, less realistic)? Leaning: as-users, cache tokens.
3. **Profiles vs pure params:** keep the `small/standard/large` profiles + overrides, or just raw `--users/--channels/--messages`?
4. **Entry point:** both `rc-repro seed` *and* `up --seed`, or only one?
5. **Backdated message timestamps:** worth the extra complexity (direct Mongo writes) for time-based repros, or accept "all messages are recent" in v1?
6. **Rate limiter:** auto-disable `API_Enable_Rate_Limiter` during seeding (fast) vs just throttle our calls (safe, slower)?
7. **Default profile on bare `--seed`:** `small`? And should `up --seed` default to `small`?
8. **Extra content types:** do we want files/attachments, pinned messages, mentions, user statuses, custom roles/teams in v1 — or keep to users/channels/DMs/messages/threads/reactions?

## 8. Out of scope (v1)

- Restoring a real customer DB dump.
- Huge user counts (use `ldap`).
- Historical/backdated data at scale.
- Marketplace apps / integrations content.
