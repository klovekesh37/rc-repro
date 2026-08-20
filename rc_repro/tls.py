"""HTTPS add-on: terminate TLS in front of Rocket.Chat with Traefik.

Layered onto ANY repro via `up --https` rather than being its own preset --
`--preset` is single-valued (cli.py), so a `tls` preset would be mutually
exclusive with oidc/saml/livechat/multi-instance, which are exactly the ones that
need HTTPS. Same shape as the monitoring add-on: services(), files(), notes().

Two ways to get the certificate, and only two inputs for the common one:

  acme   `--domain X --email Y`  Let's Encrypt via Traefik's own ACME resolver,
                                 the same DOMAIN + LETSENCRYPT_EMAIL pair the
                                 official compose.traefik.yml takes.
  local  `--https`               a CA made here with openssl (see tls_local.py).
                                 Offline, no domain, no rate limits.

The ACME CHALLENGE is not a user-facing choice. TLS-ALPN-01 is the default and
what the official compose uses; dns-01 is selected automatically when provider
credentials exist in ~/.rc-repro/acme/dns.env, because that is the only way to
issue when Let's Encrypt cannot connect inbound -- behind NAT, behind a tunnel, or
behind a proxy that terminates TLS in front of you. Neither appears as a flag.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from rc_repro import config
from rc_repro.errors import ValidationError

# Pinned to match the multi-instance preset, so a repro that uses both runs one
# Traefik version rather than two.
TRAEFIK_TAG = "v3.4"
SERVICE = "traefik"

# Reserved by RFC 6761: resolves to loopback in browsers and most resolvers with
# no /etc/hosts edit and no DNS server. A per-repro hostname also stops repros
# sharing cookies, which they do today because they all answer on `localhost`.
LOCAL_SUFFIX = "rcrepro.localhost"

CA_DIR_NAME = "ca"
CA_KEY = "ca.key"
CA_CRT = "ca.crt"
_CA_DAYS = 3650          # the root: made once, install once
_LEAF_DAYS = 825         # a leaf: regenerated on every `up`, so this is generous

# Where ACME state lives. Deliberately OUTSIDE the repro workspace: Let's Encrypt
# allows 5 certificates per identical hostname per 7 days, and losing this file is
# the documented way to burn that -- `down --volumes` would do it every time.
ACME_DIR_NAME = "acme"
# Separate storage per ACME environment. Traefik keys stored accounts and
# certificates by RESOLVER name, not by CA server, so pointing the same resolver
# at staging and then at production leaves the staging certificate in place and
# keeps serving it -- the well-known "delete acme.json when you switch" trap. Two
# files means switching genuinely re-issues, and a staging cert can never leak
# into a production run.
ACME_FILE = "acme.json"
ACME_FILE_STAGING = "acme-staging.json"
#: Credentials for a dns-01 provider. A file, never argv: `ps` shows command lines
#: to every user on the box.
DNS_ENV_FILE = "dns.env"
LEGO_PROVIDER_DOCS = "https://go-acme.github.io/lego/dns/"

#: Let's Encrypt: 50 certificates per registered domain per 7 days. Only NEW names
#: cost -- an existing certificate is reused -- so this is only reachable by a team
#: creating many differently-named workspaces in a week. Counted and warned about
#: rather than pre-empted (§6): refusing to create a workspace over rc-repro's own
#: tally, which cannot see certificates issued by anything else on the domain,
#: would be wrong far more often than right.
CERT_LIMIT_PER_WEEK = 50
#: Warn from here. Deliberately well below the limit: the useful moment to hear
#: "a wildcard would remove this" is while there is still room to act.
CERT_WARN_AT = 25
#: Names rc-repro has caused a certificate request for, and when. Traefik's
#: acme.json holds the certificates themselves but no issuance dates, and parsing
#: X.509 out of it would need a crypto dependency this project does not have.
ISSUED_FILE = "issued.json"

MODE_LOCAL, MODE_ACME = "local", "acme"

#: Subject Traefik puts on the self-signed certificate it serves when a
#: resolver has produced nothing yet -- how `tls-status` tells "ACME has not
#: issued" apart from "ACME issued something wrong".
_TRAEFIK_FALLBACK = "TRAEFIK DEFAULT CERT"


@dataclass
class TlsSpec:
    """Resolved HTTPS settings for one repro. `mode` picks the certificate source."""
    mode: str = MODE_LOCAL
    host: str = ""               # external hostname (a domain, or <name>.rcrepro.localhost)
    port: int = 0                # published host port for the TLS entrypoint
    acme_email: str = ""
    acme_staging: bool = False
    # tlsalpn (Let's Encrypt connects in on 443) | dns (a TXT record, no inbound).
    # Derived from whether credentials exist, never asked for.
    acme_challenge: str = "tlsalpn"
    acme_dns_provider: str = ""

    @property
    def root_url(self) -> str:
        # 443 is implicit in an https URL; anything else has to be spelled out or
        # RC advertises a URL that does not answer.
        return f"https://{self.host}" + ("" if self.port == 443 else f":{self.port}")


def ca_dir() -> Path:
    return config.home() / CA_DIR_NAME


def acme_dir() -> Path:
    return config.home() / ACME_DIR_NAME


def local_host_for(name: str) -> str:
    return f"{name}.{LOCAL_SUFFIX}"


# A hostname label: alphanumeric with internal hyphens. Deliberately not a full
# RFC 1123 implementation -- this only has to reject the shapes that break us.
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def _clean_host(value: str) -> tuple[str, list[str]]:
    """Shared cleanup for a user-supplied hostname: scheme, trailing slash, dot, case.

    ONE implementation, because `--domain` and `--allow-host` are mistyped in exactly
    the same ways -- a pasted URL, a trailing slash, a capitalised name -- and
    diverging on which of those is forgiven is how one flag ends up accepting what
    the other refuses. They differ only on a PORT, which each decides for itself.
    """
    raw = value.strip()
    fixes = ["trimmed whitespace"] if raw != value else []
    for scheme in ("https://", "http://"):
        if raw.lower().startswith(scheme):
            raw = raw[len(scheme):]
            fixes.append(f"dropped {scheme!r}")
            break
    if raw.endswith("/"):
        raw = raw.rstrip("/")
        fixes.append("dropped the trailing slash")
    if raw.endswith("."):
        # A trailing dot is a legal FQDN but does not match SNI or a URL host.
        raw = raw.rstrip(".")
        fixes.append("dropped the trailing dot")
    if raw != raw.lower():
        raw = raw.lower()
        fixes.append("lower-cased it")
    return raw, fixes


def normalize_allow_host(value: str) -> tuple[str, str]:
    """Turn a user-supplied --allow-host into a bare hostname. Returns (host, note).

    The same cleanup as --domain, with one deliberate difference: a port is DROPPED
    and reported rather than refused. The Host guard compares hostnames with the port
    already stripped off the incoming header (`_hostname` in web/app.py), so
    `--allow-host lab.example.com:9944` names exactly the right host -- and keeping
    the port, which is what used to happen, made it match nothing at all.

    Not hypothetical. Measured on a lab box, three of the five forms somebody typed
    in a row -- `https://h/`, `h/`, `h:9944` -- were each unmatchable, and `serve`
    started and reported itself healthy every time before 403ing every request. The
    port form is the sharpest of the three: you are serving on 9944, so writing the
    host with its port is the obvious thing to do.

    `*` is the documented "any Host" wildcard and passes through untouched.
    """
    if value.strip() == "*":
        return "*", ""
    raw, fixes = _clean_host(value)
    if raw.startswith("["):
        # `[::1]:9944` / `[::1]` -- the guard unwraps the brackets too, so match it.
        inner = raw[1:].split("]", 1)[0]
        if inner:
            raw = inner
            fixes.append("unwrapped the IPv6 brackets")
    elif raw.count(":") == 1:
        # host:port. More than one colon and no brackets is a bare IPv6 literal
        # (`::1`), which has no port to drop and must survive untouched.
        raw, port = raw.split(":", 1)
        fixes.append(f"dropped the port {port!r} — a Host header's port is not matched")
    if not raw:
        raise ValidationError(
            f"--allow-host {value!r} has no hostname in it. Pass the name you will "
            "type in the address bar — e.g. --allow-host lab.example.com, or '*' "
            "for any host.")
    if "/" in raw:
        raise ValidationError(
            f"--allow-host {value!r} contains a path. Pass the hostname only "
            f"({raw.split('/', 1)[0]}) — the guard matches the Host header, and a "
            "Host header has no path in it.")
    return raw, ("; ".join(fixes) if fixes else "")


def normalize_domain(value: str) -> tuple[str, str]:
    """Turn a user-supplied --domain into a bare hostname. Returns (host, note).

    The official Rocket.Chat compose docs spell out that DOMAIN must be set
    "without https:// or trailing slashes", which is a strong hint that people get
    it wrong -- and unguarded it produced ROOT_URL="https://https://host" plus a
    bogus ACME `domains` entry that Let's Encrypt rejects.

    Forms with one obvious meaning are corrected and reported. Forms that would
    change what gets served (a port, a path) are refused, because silently
    dropping them would not do what the user asked.
    """
    raw, fixes = _clean_host(value)

    if not raw:
        raise ValidationError("--domain is empty")
    if "/" in raw:
        raise ValidationError(
            f"--domain {value!r} contains a path. Pass the hostname only "
            f"({raw.split('/', 1)[0]}); serving a repro under a sub-path is not "
            "supported.")
    if ":" in raw:
        raise ValidationError(
            f"--domain {value!r} contains a port. Pass the hostname only "
            f"({raw.split(':', 1)[0]}) — a repro on a real domain is served on 443, "
            "so the URL carries no port.")
    if raw.startswith("*."):
        raise ValidationError(
            f"--domain {value!r} is a wildcard. --domain has to be the concrete host "
            "the workspace is reached at, because it becomes ROOT_URL.")
    if any(not _LABEL_RE.match(lbl) for lbl in raw.split(".")):
        raise ValidationError(
            f"--domain {value!r} is not a valid hostname (letters, digits and "
            "hyphens per label, separated by dots).")
    return raw, ("; ".join(fixes) if fixes else "")


# --- openssl -----------------------------------------------------------------

def acme_storage_name(staging: bool) -> str:
    return ACME_FILE_STAGING if staging else ACME_FILE


# --- certificate budget (§6) ---------------------------------------------------

def registrable_domain(host: str) -> str:
    """The domain Let's Encrypt counts against, approximately.

    The real rule is the eTLD+1 from the Public Suffix List, which this project
    does not carry -- so the last two labels are used instead. That is right for
    `t1.support.xyz.com` -> `xyz.com` and WRONG for multi-part suffixes like
    `co.uk`, where it groups a whole suffix as one domain and so over-counts.

    Acceptable only because this drives a warning and never a refusal: the worst
    case is mentioning a wildcard slightly too early.
    """
    labels = [p for p in (host or "").strip(".").split(".") if p]
    return ".".join(labels[-2:]) if len(labels) >= 2 else (labels[0] if labels else "")


def issued_path() -> Path:
    return acme_dir() / ISSUED_FILE


def _read_issued() -> dict:
    try:
        data = json.loads(issued_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_issuance(host: str, when: str = "") -> None:
    """Note that `host` needed a certificate of its own.

    Keyed by name and only ever written ONCE per name: re-registering an existing
    name reuses its certificate and costs nothing, so overwriting the date would
    make a long-lived workspace look like a fresh request every time it is
    recreated. Best-effort -- a tally must never stop the work.
    """
    if not host:
        return
    try:
        issued = _read_issued()
        if host in issued:
            return
        issued[host] = when or datetime.now(timezone.utc).date().isoformat()
        acme_dir().mkdir(parents=True, exist_ok=True)
        tmp = issued_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(issued, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, issued_path())
    except OSError:
        pass


def recent_issuances(domain: str, days: int = 7) -> int:
    """How many distinct names under `domain` were first seen in the last `days`."""
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    count = 0
    for host, day in _read_issued().items():
        if registrable_domain(host) != domain:
            continue
        try:
            if date.fromisoformat(str(day)) > cutoff:
                count += 1
        except ValueError:
            continue
    return count


def budget_warning(host: str) -> str:
    """The §6 message, or "" while there is nothing worth saying."""
    domain = registrable_domain(host)
    if not domain:
        return ""
    used = recent_issuances(domain)
    if used < CERT_WARN_AT:
        return ""
    return (f"{used} certificates issued for {domain} in the last 7 days "
            f"(limit {CERT_LIMIT_PER_WEEK}). A wildcard certificate would remove "
            f"this — it needs a DNS API token in {dns_env_path()}.")


def dns_env_path() -> Path:
    return acme_dir() / DNS_ENV_FILE


# Which lego provider a credentials variable belongs to. The variables ARE
# provider-specific, so the file already says which provider it is -- asking the
# user to repeat that on every `up` was ceremony. Longest prefix wins, so
# CLOUDFLARE_* is not mistaken for anything shorter.
# Not exhaustive: an unrecognised file just means --acme-dns-provider is required,
# which is the old behaviour rather than a failure.
_DNS_VAR_PROVIDERS = {
    "CF_": "cloudflare", "CLOUDFLARE_": "cloudflare",
    "AWS_": "route53",
    "DO_AUTH_TOKEN": "digitalocean",
    "GCE_": "gcloud",
    "AZURE_": "azuredns",
    "LINODE_": "linode",
    "HETZNER_": "hetzner",
    "VULTR_": "vultr",
    "OVH_": "ovh",
    "GODADDY_": "godaddy",
    "NAMECHEAP_": "namecheap",
    "DNSIMPLE_": "dnsimple",
    "NS1_": "ns1",
    "DIGITALOCEAN_": "digitalocean",
}


def dns_env_vars() -> list[str]:
    """Variable NAMES in the credentials file (never values), or [] if absent."""
    path = dns_env_path()
    if not path.is_file():
        return []
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            names.append(line.split("=", 1)[0].strip())
    return names


def infer_dns_provider() -> tuple[str, str]:
    """Work out the lego provider from the credentials file. Returns (provider, why).

    ("", reason) when the file is missing, unrecognised, or names more than one
    provider -- in which case --acme-dns-provider has to be given explicitly.
    """
    names = dns_env_vars()
    if not names:
        return "", f"{dns_env_path()} does not exist or has no KEY=VALUE lines"
    found: dict[str, str] = {}
    for name in names:
        up = name.upper()
        # Longest prefix first so CLOUDFLARE_ beats CF_ and DIGITALOCEAN_ beats DO_.
        for prefix in sorted(_DNS_VAR_PROVIDERS, key=len, reverse=True):
            if up.startswith(prefix):
                found[_DNS_VAR_PROVIDERS[prefix]] = name
                break
    if not found:
        return "", f"none of {', '.join(names)} matches a known provider"
    if len(found) > 1:
        return "", (f"{', '.join(names)} look like more than one provider "
                    f"({', '.join(sorted(found))})")
    provider, via = next(iter(found.items()))
    return provider, f"{via} in {dns_env_path().name}"


def dns_credentials(provider: str) -> tuple[bool, str]:
    """Check the dns-01 credentials file, returning (ok, message).

    Each lego provider reads its OWN environment variables (CF_DNS_API_TOKEN,
    AWS_ACCESS_KEY_ID, DO_AUTH_TOKEN, GCE_PROJECT, ...), so this cannot verify the
    right ones are present -- but it can catch the file being missing or empty,
    which otherwise surfaces only as an opaque Traefik failure minutes later.
    """
    path = dns_env_path()
    if not path.is_file():
        return False, (
            f"--acme-challenge dns needs provider credentials, and {path} does not "
            f"exist.\n  Create it with the variables {provider!r} expects (see "
            f"{LEGO_PROVIDER_DOCS}{provider}/), e.g. for cloudflare:\n"
            f"    mkdir -p {path.parent} && chmod 700 {path.parent}\n"
            f"    printf 'CF_DNS_API_TOKEN=%s\\n' \"$TOKEN\" > {path}\n"
            f"    chmod 600 {path}")
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            names.append(line.split("=", 1)[0].strip())
    if not names:
        return False, (f"{path} has no KEY=VALUE lines. Add the variables {provider!r} "
                       f"expects — see {LEGO_PROVIDER_DOCS}{provider}/")
    # Names only. Never echo the values.
    return True, f"{path.name}: {', '.join(names)}"



def acme_args(spec: TlsSpec) -> list[str]:
    """Traefik's ACME flags.

    Public because the front door (services/edge.py) issues certificates the
    same way a workspace does; two copies of this would drift, and the challenge
    choice is the part that decides whether issuance works at all.
    """
    args = [
        "--certificatesresolvers.le.acme.email=" + spec.acme_email,
        "--certificatesresolvers.le.acme.storage=/etc/traefik/acme/"
        + acme_storage_name(spec.acme_staging),
    ]
    if spec.acme_staging:
        args.append("--certificatesresolvers.le.acme.caserver="
                    "https://acme-staging-v02.api.letsencrypt.org/directory")
    if spec.acme_challenge == "dns":
        # dns-01: validated by a TXT record the provider API writes, so Let's
        # Encrypt never connects here. The only option behind NAT, a tunnel, or a
        # proxy that terminates TLS in front of the origin.
        args.append("--certificatesresolvers.le.acme.dnschallenge=true")
        if spec.acme_dns_provider:
            args.append("--certificatesresolvers.le.acme.dnschallenge.provider="
                        + spec.acme_dns_provider)
    elif spec.acme_challenge == "http":
        # http-01, and it exists because the other two can both be unavailable at once.
        # Reported from an EC2 box where TLS-ALPN validation failed every time with
        # `remote error: tls: unrecognized name` -- Let's Encrypt reached :443 and the
        # challenge was not being answered -- and where dns-01 was impossible because the
        # operator had the machine and not the DNS zone. With only host access and :80
        # open, this is the one remaining option, and rc-repro could not select it.
        args.append("--certificatesresolvers.le.acme.httpchallenge=true")
        args.append("--certificatesresolvers.le.acme.httpchallenge.entrypoint=web")
    else:
        # TLS-ALPN-01, the same challenge the official compose.traefik.yml uses
        # (`certificatesresolvers.le.acme.tlschallenge`). It validates on 443, which
        # is already published, so no second port has to be opened for it.
        args.append("--certificatesresolvers.le.acme.tlschallenge=true")
    return args


# --- trust store --------------------------------------------------------------
# Deliberately narrow: macOS and Debian/Ubuntu/Fedora are handled, anything else
# gets printed instructions. Guessing wrong here means either a silent no-op or
# writing into a system trust store the user did not expect.

def _cert_field(pem: str, *args: str) -> str:
    try:
        proc = subprocess.run(("openssl", "x509", "-noout", *args),
                              input=pem, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip()


def verify(host: str, port: int = 443, timeout: float = 10.0,
           cafile: str | None = None, sni: str | None = None) -> dict:
    """Connect over TLS and report what is ACTUALLY being served.

    Deliberately connects with verification off first: a staging or local-CA
    certificate is untrusted by design, and refusing to look at it would report
    "broken" for a setup that is working exactly as intended. Trust is then a
    separate, second question.
    """
    import ssl
    out: dict = {"host": host, "port": port, "serving": False, "trusted": False,
                 "trusted_via_ca": False, "issuer": "", "subject": "", "dates": "",
                 "fallback": False, "error": ""}
    # `sni` lets the caller connect to an ADDRESS while presenting a different
    # server name -- how you check what this host serves for a domain without
    # letting a proxy in front answer instead.
    servername = sni or host
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=servername) as sock:
                der = sock.getpeercert(binary_form=True)
    except OSError as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    if not der:
        out["error"] = "the server offered no certificate"
        return out
    out["serving"] = True
    pem = ssl.DER_cert_to_PEM_cert(der)
    out["issuer"] = _cert_field(pem, "-issuer").removeprefix("issuer=").strip()
    out["subject"] = _cert_field(pem, "-subject").removeprefix("subject=").strip()
    out["dates"] = " ".join(_cert_field(pem, "-enddate").splitlines())
    out["fallback"] = _TRAEFIK_FALLBACK.lower() in (out["issuer"] + out["subject"]).lower()

    def _accepts(ctx_cafile: str | None) -> bool:
        ctx = (ssl.create_default_context(cafile=ctx_cafile) if ctx_cafile
               else ssl.create_default_context())
        try:
            with socket.create_connection((host, port), timeout=timeout) as raw:
                with ctx.wrap_socket(raw, server_hostname=servername):
                    return True
        except (OSError, ssl.SSLError):
            return False

    # Two DIFFERENT questions, kept apart on purpose:
    #   trusted        - would a browser accept this, using only the SYSTEM store?
    #   trusted_via_ca - does it chain to the CA we were given?
    # Collapsing them made local mode always report "trusted", because passing
    # rc-repro's own CA guarantees success and hides whether `trust-ca` has run.
    out["trusted"] = _accepts(None)
    out["trusted_via_ca"] = _accepts(cafile) if cafile else out["trusted"]
    return out


# Public resolvers, asked when the system one comes up empty. Let's Encrypt uses
# its OWN resolvers, so the system resolver is not authoritative for this question
# -- a split-horizon or stale-negative-cache resolver (common in labs and
# corporate networks) made a perfectly good record look absent.

# A few of Cloudflare's published edge ranges. Used ONLY to explain a failure more
# precisely, never to block: the list drifts, and being wrong here must not stop a
# working setup. See https://www.cloudflare.com/ips-v4
_CLOUDFLARE_HINT_NETS = ("104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13",
                         "173.245.48.0/20", "162.158.0.0/15", "108.162.192.0/18",
                         "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22",
                         "198.41.128.0/17", "131.0.72.0/22", "141.101.64.0/18",
                         "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22")


def notes(spec: TlsSpec, name: str = "<name>") -> list[str]:
    if spec.mode == MODE_LOCAL:
        return [
            f"HTTPS (local certificate): {spec.root_url}",
            "Signed by rc-repro's own CA, so a browser warns until you run"
            " `rc-repro trust-ca` once (then no warnings, and curl needs no -k).",
            "A phone cannot use this: the cert is untrusted there and .localhost"
            " resolves to the phone itself. Use --domain for mobile.",
        ]
    if spec.acme_staging:
        # Staging roots are NOT in any trust store -- that is the whole point of
        # staging. Claiming otherwise sent people looking for a broken workspace
        # when the browser warning was the expected, correct outcome.
        return [
            f"HTTPS (Let's Encrypt STAGING): {spec.root_url}",
            "A staging certificate is deliberately NOT trusted - a browser warning"
            " here is the SUCCESS signal, because TLS terminated with a real"
            " Let's Encrypt-issued cert.",
            "Confirm what is served:  rc-repro tls-status --name " + name,
            "Then `rc-repro config unset acme.staging` and re-run with --force.",
        ]
    return [
        f"HTTPS (Let's Encrypt): {spec.root_url}",
        "Publicly trusted, so the Rocket.Chat mobile app accepts it with nothing"
        f" to install - just add {spec.root_url} in the app.",
        "Traefik requests the certificate in the background. Confirm it issued:"
        "  rc-repro tls-status --name " + name,
        "Certificate state lives in ~/.rc-repro/acme/ and survives `down`, so"
        " re-creating this repro reuses the cert instead of re-issuing it.",
    ]
