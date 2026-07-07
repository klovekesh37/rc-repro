# `oidc` preset — design draft (for review)

> **Status:** DRAFT — not built. Review/edit. The one real design decision is in
> §5 (the browser-vs-backend URL problem); everything else is straightforward.

## 1. Goal

Reproduce **OpenID Connect (OIDC) SSO** logins — many customers use OIDC (via
Keycloak, Okta, Azure AD, Google, Auth0) rather than SAML. RC implements OIDC
through its **Custom OAuth** provider. This preset stands up a Keycloak OIDC
client + users and wires RC to log in against it — the OIDC counterpart to the
existing `saml` preset.

```bash
rc-repro up --version 8.5.1 --preset oidc
rc-repro up --version 8.5.1 --preset oidc --set users=20
# -> "Keycloak (OIDC)" login button; sign in as user1 / user1
```

## 2. Reuses the Keycloak we already have

The `saml` preset already generates a Keycloak realm (`rcrepro`) with users. `oidc`
reuses the same Keycloak image + realm-generation code (`saml_preset` user
generation), but the realm ships an **OpenID Connect client** instead of a SAML
client. Everything else (arm64-native Keycloak, `sslRequired=none`, master-realm
console fix, users `user1..userN`) carries over.

**OIDC is actually simpler than SAML in one way:** it authenticates with a
**client id + client secret** (a shared string), not signing certificates — so
there's **no runtime cert fetch**. We pin a fixed client secret in the realm and
set the same secret in RC. Deterministic, no exchange.

## 3. What it stands up

- **Keycloak** (same image), realm `rcrepro`, with:
  - an **OIDC client** `rc-oidc` (confidential, standard authorization-code flow,
    a **fixed client secret**, wildcard redirect URIs `http://localhost*`).
  - test users `user1..userN` (password = username) — reused from `saml`.
  - Keycloak's built-in `openid`/`profile`/`email` scopes already emit the claims
    RC needs (`preferred_username`, `email`, `name`, `sub`) — **no custom mappers**.
- **Rocket.Chat** configured with a Custom OAuth provider named `keycloak` (below).

## 4. RC OIDC (Custom OAuth) settings

Set via `OVERWRITE_SETTING_Accounts_OAuth_Custom-Keycloak-*`:

| Setting | Value |
|---|---|
| `Accounts_OAuth_Custom-Keycloak` | `true` (enable) |
| `…-url` | Keycloak realm base — **see §5** |
| `…-authorize_path` | `/protocol/openid-connect/auth` |
| `…-token_path` | `/protocol/openid-connect/token` |
| `…-identity_path` | `/protocol/openid-connect/userinfo` |
| `…-scope` | `openid profile email` |
| `…-id` | `rc-oidc` (client id) |
| `…-secret` | the fixed client secret |
| `…-token_sent_via` | `header` |
| `…-login_style` | `redirect` |
| `…-username_field` | `preferred_username` |
| `…-email_field` | `email` |
| `…-name_field` | `name` |
| `…-merge_users` | `true` |
| `…-button_label_text` | `Keycloak (OIDC)` |
| `…-button_color` / `…-button_label_color` | brand colours |

RC's callback (redirect URI) is `<ROOT_URL>/_oauth/keycloak` → matched by the
client's `http://localhost*` wildcard.

## 5. THE key problem: browser vs. backend reachability

OIDC uses `url` for **both**:
1. the **authorize** step — the *browser* is redirected to `<url>/…/auth`, and
2. the **token + userinfo** steps — *RC's backend (inside the container)* POSTs to
   `<url>/…/token` and `<url>/…/userinfo`.

So `url` must resolve to Keycloak **from both the host browser and the RC
container** — but `localhost:8081` means "the host" to the browser and "the RC
container itself" to RC's backend. That's the classic OIDC-in-Docker gotcha (SAML
avoided it because SAML is browser-only).

**Proposed solution (Approach A — recommended):** use a single shared hostname
`keycloak:8080` that resolves the same way from both sides:
- Publish Keycloak on host port **8080** (matching its internal 8080).
- RC's OIDC `url` = `http://keycloak:8080/realms/rcrepro`. RC's backend resolves
  `keycloak` via the compose network (→ the Keycloak container). ✅
- The **browser** resolves `keycloak` via a one-line hosts entry:
  `127.0.0.1  keycloak` in `/etc/hosts` → the published `keycloak:8080`. ✅
- Both hit Keycloak at the identical `http://keycloak:8080`, ports aligned.

rc-repro would print the exact hosts line in `up`/`info` notes, and `doctor` could
check whether `keycloak` resolves. The line needs `sudo`, so rc-repro **prints it,
doesn't auto-edit** `/etc/hosts`.

**Alternatives considered:**
- `host.docker.internal:8081` — resolves from the container on Docker Desktop, but
  the host browser doesn't resolve it reliably across OSes. ❌ inconsistent.
- Split hosts (authorize on `localhost:8081`, token on `keycloak:8080`) — RC's
  Custom OAuth uses one `url` for both, so this isn't cleanly supported. ❌

## 6. Provider registration

Like SAML, RC may only register a Custom OAuth login button on a **settings
change**, not from boot-injected `OVERWRITE_SETTING`s. If the button doesn't
appear at boot, a `post_ready` step toggles `Accounts_OAuth_Custom-Keycloak`
off→on to force registration (same pattern the `saml` preset uses for its cert).
Since the OIDC secret is known at boot (no fetch), this may be the *only*
post_ready action needed — to verify during build.

## 7. Implementation sketch

- New `rc_repro/oidc_preset.py`: `build(params)` generates the realm (reuse
  `saml_preset`'s user generation; swap the SAML client for an OIDC client with a
  fixed secret) and the `Accounts_OAuth_Custom-Keycloak-*` env.
- Register `"oidc"` in `presets._dynamic_builders()`.
- `post_ready`: `keycloak_master_ssl_off` (reuse) + optional OAuth-provider toggle.
- `notes`: the `/etc/hosts` line + Keycloak console URL + `user1/user1`.
- Params: `users` (like ldap/saml).

## 8. Open questions (your call — edit inline)

1. **The `/etc/hosts` requirement (§5):** accept the one-line `127.0.0.1 keycloak`
   setup (rc-repro prints it, `doctor` checks it)? Or do you know a Docker-Desktop
   setup on your team's machines where `host.docker.internal` resolves on the host
   too (which would avoid the hosts edit)?
2. **Provider name:** call it `keycloak` (URLs become `/_oauth/keycloak`) or a
   generic `oidc`?
3. **Login style:** `redirect` (full-page, most reliable) vs `popup`?
4. **Share one Keycloak realm** that has *both* a SAML and an OIDC client (so a
   single preset could do both), or keep `saml` and `oidc` as separate presets?
5. **Role/group mapping** from OIDC claims (`roles`/`groups`) — in v1, or later
   (it's the EE-ish advanced case, needs `--reg-token`)?
6. **Port:** Keycloak on **8080** for OIDC (needed so browser+backend align) vs the
   `8081` the `saml` preset uses — do we standardise both on 8080?

## 9. Out of scope (v1)

- Non-Keycloak IdPs (Okta/Azure/Google) — the settings are the same shape; a user
  can point `-url` at a real IdP via a custom preset.
- OIDC back-channel logout, PKCE-only public clients, encrypted userinfo.
- Role/group → RC role mapping (unless we pull it into §8.5).
