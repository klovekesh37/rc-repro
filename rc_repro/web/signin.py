"""The sign-in document.

A server-rendered page with no JavaScript, not a state inside the SPA. Three
reasons, in order:

* `app.js` is 1873 lines that assume `api()` succeeds and that a repro list is
  reachable. Opening it to an unauthenticated caller means opening the file that
  maps the whole API surface, to get a form with two fields.
* The test suite is Python with no JS tests at all, so a server-rendered form is
  exercisable by the tests that already exist.
* HTTP Basic's failure mode — the browser's own grey dialog, with no error text,
  no styling and no way back — is the thing being removed. Replacing it with a
  page that needs a script to render its own error would keep half of it.

The page is a real `<form method="post">`, which is why the CSP had to move from
`form-action 'none'` to `form-action 'self'` (see web/app.py).
"""

from __future__ import annotations

import html

from rc_repro import __version__
from rc_repro.services.users import MIN_PASSWORD

#: The banner shown for each `?e=` value. Absent or unknown -> no banner, which is
#: the first-visit case. Keyed rather than free text so a redirect can never put
#: attacker-chosen prose on the page.
STATES: dict[str, tuple[str, str]] = {
    "bad": ("bad", "That name or password is not right."),
    "rate": ("bad", "Too many attempts from this address. Try again in a moment."),
    "expired": ("warn", "Your session expired. Sign in again to carry on."),
    "signedout": ("ok", "Signed out."),
    "revoked": ("warn", "That session was signed out from somewhere else."),
    "required": ("warn", "Sign in to use rc-repro."),
    "nouser": ("bad", "There are no accounts on this server yet. "
                      "Create one on the host: rc-repro users add &lt;name&gt;"),
}


def theme_attr(value: str | None) -> str:
    """`data-theme="dark"` for the stored choice, or "" to let CSS decide.

    The page has no script, so it cannot read the localStorage key app.js keeps —
    hence the cookie, which carries nothing but the palette. Anything that is not
    one of the two literals yields no attribute at all: this string is
    interpolated into the document, and a cookie is attacker-settable.
    """
    v = (value or "").strip()
    return f' data-theme="{v}"' if v in ("dark", "light") else ""


def safe_next(value: str | None) -> str:
    """Where to go after signing in — reduced to a same-origin path.

    Anything not starting with a single `/` becomes `/`. `//host` and
    `https://host` are the open-redirect shapes; a sign-in page that forwards to
    an attacker's origin after a successful login is how a credential prompt gets
    reused against the user.
    """
    v = (value or "/").strip()
    if not v.startswith("/") or v.startswith("//") or "\\" in v:
        return "/"
    return v


def page(*, error: str = "", next_url: str = "/", server: str = "",
         secure: bool = True, retry_after: int = 0, theme: str = "") -> str:
    """The whole document. Pure — no request object, so it unit-tests directly."""
    kind, message = STATES.get(error, ("", ""))
    banner = (f'<p class="signin-banner {kind}">{message}</p>') if message else ""

    # Independent of the ?e= banner: it describes the transport, not the attempt.
    warning = "" if secure else (
        '<p class="signin-banner bad">This connection is not encrypted — your '
        'password will cross the network in the clear.</p>')

    # The `rate` state has no submit to re-enable and no script to count down, so
    # the page reloads itself once the window is over. Without this it is a dead
    # end the user escapes only by knowing to refresh.
    refresh = (f'<meta http-equiv="refresh" content="{int(retry_after) + 1}">'
               if error == "rate" and retry_after else "")

    where = html.escape(server or "this host")
    nxt = html.escape(safe_next(next_url), quote=True)
    return f"""<!doctype html>
<html lang="en"{theme_attr(theme)}>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · rc-repro</title>
<link rel="stylesheet" href="/app.css">
{refresh}
</head>
<body class="signin-body">
<main class="signin">
  <div class="brand"><span class="dollar">$</span> rc-repro</div>
  {warning}
  {banner}
  <form method="post" action="/signin" class="signin-form">
    <input type="hidden" name="next" value="{nxt}">
    <label for="u">Name</label>
    <input id="u" name="user" autocomplete="username" autocapitalize="none"
           autocorrect="off" spellcheck="false" autofocus required>
    <label for="p">Password</label>
    <input id="p" name="password" type="password"
           autocomplete="current-password" required>
    <button class="btn primary" type="submit">Sign in</button>
  </form>
  <p class="signin-foot">Server: {where} &middot; rc-repro {html.escape(__version__)}</p>
  <p class="signin-foot">
    Lost your password? An admin resets it, on the host:<br>
    <code>rc-repro users passwd &lt;name&gt;</code>
  </p>
</main>
</body>
</html>
"""


# --- first run ------------------------------------------------------------------

#: The only script rc-repro serves to an unauthenticated caller. It exists because
#: a URL fragment is readable ONLY by JavaScript -- which is exactly the property
#: that keeps the first-run key out of access logs, proxy logs and Referer headers.
#: app.js is not opened for this: that file maps the whole API surface, and this
#: needs two fields and one POST.
SETUP_JS = """\
"use strict";
var key = (location.hash || "").replace(/^#k=/, "");
// Out of the address bar on first paint, so it does not survive a screenshot or
// a shared link. Doing it before anything else means even an error leaves no key.
history.replaceState(null, "", "/setup");
var form = document.getElementById("f");
var err = document.getElementById("err");
if (!key) {
  err.textContent = "This page needs the setup link rc-repro printed. "
    + "Copy the whole URL, including the part after #.";
  err.hidden = false;
  form.hidden = true;
}
form.addEventListener("submit", function (e) {
  e.preventDefault();
  err.hidden = true;
  var btn = form.querySelector("button");
  btn.disabled = true; btn.textContent = "Creating…";
  fetch("/api/session/first-run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({
      key: key, user: form.user.value.trim(), password: form.password.value,
    }),
  }).then(function (r) {
    return r.json().then(function (d) { return { ok: r.ok, d: d }; });
  }).then(function (res) {
    if (res.ok) { location.assign("/"); return; }
    err.textContent = res.d.error || "That did not work.";
    err.hidden = false;
    btn.disabled = false; btn.textContent = "Create admin account";
  }).catch(function () {
    err.textContent = "Could not reach the server.";
    err.hidden = false;
    btn.disabled = false; btn.textContent = "Create admin account";
  });
});
"""


def setup_page(theme: str = "") -> str:
    """The first-run document: create the first admin, once."""
    return f"""<!doctype html>
<html lang="en"{theme_attr(theme)}>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Set up rc-repro</title>
<link rel="stylesheet" href="/app.css">
</head>
<body class="signin-body">
<main class="signin">
  <div class="brand"><span class="dollar">$</span> rc-repro</div>
  <p class="signin-banner ok">This server has no accounts yet. Create the first
    one — it will be an admin, because somebody has to be able to add everyone
    else.</p>
  <p class="signin-banner bad" id="err" hidden></p>
  <form class="signin-form" id="f">
    <label for="u">Name</label>
    <input id="u" name="user" autocomplete="username" autocapitalize="none"
           autocorrect="off" spellcheck="false" autofocus required>
    <label for="p">Password</label>
    <input id="p" name="password" type="password"
           autocomplete="new-password" minlength="{MIN_PASSWORD}" required>
    <button class="btn primary" type="submit">Create admin account</button>
  </form>
  <p class="signin-foot">At least {MIN_PASSWORD} characters. rc-repro stores only a
    scrypt hash — there is no password recovery, an admin resets it.</p>
</main>
<script src="/setup.js"></script>
</body>
</html>
"""
