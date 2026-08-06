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

import re
import socket
import subprocess
from dataclasses import dataclass
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
    # Publish :80 and redirect it to https, as the official compose files do.
    # Decided by the caller, which is the only place that can probe port 80 --
    # this is a pure builder.
    http_redirect: bool = False

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

def _dynamic_yml(backends: list[str], mode: str, host: str = "") -> str:
    """Traefik file-provider config: route everything to the RC backend(s) over TLS.

    File provider rather than the Docker one: it needs no access to the Docker
    socket, which the multi-instance preset already avoids for the same reason.
    """
    servers = "\n".join(f'          - url: "http://{b}:{config.RC_CONTAINER_PORT}"'
                        for b in backends)
    # The rule differs by mode, and that is what decides whether ACME works.
    #
    # acme: Host(`domain`), exactly as the official compose.traefik.yml does it.
    #   Traefik derives what to REQUEST from this matcher, so with a Host() rule no
    #   `tls.domains` block is needed. The previous PathPrefix(`/`) rule named no
    #   host, which made Traefik log "no domain found" and silently serve its
    #   default certificate without ever making an ACME request -- indistinguishable
    #   from a failed issuance. Matching the docs removes that failure mode.
    # local: PathPrefix(`/`), because a local certificate is loaded from disk rather
    #   than requested, and the workspace should also answer on localhost.
    if mode == MODE_ACME:
        rule = f'Host(`{host}`)'
        tls_block = "      tls:\n        certResolver: le\n"
    else:
        rule = "PathPrefix(`/`)"
        tls_block = "      tls: {}\n"
    static_certs = "" if mode == MODE_ACME else (
        "tls:\n"
        "  certificates:\n"
        "    - certFile: /etc/traefik/certs/tls.crt\n"
        "      keyFile: /etc/traefik/certs/tls.key\n"
    )
    return (
        "# Generated by rc-repro. Do not edit by hand -- re-run `rc-repro up`.\n"
        "http:\n"
        "  routers:\n"
        "    rocketchat:\n"
        f'      rule: "{rule}"\n'
        "      entryPoints: [websecure]\n"
        "      service: rocketchat\n"
        + tls_block
        + "  services:\n"
        "    rocketchat:\n"
        "      loadBalancer:\n"
        "        healthCheck:\n"
        "          path: /api/info\n"
        '          interval: "10s"\n'
        '          timeout: "5s"\n'
        "        sticky:\n"          # DDP websockets must not be bounced mid-session
        "          cookie:\n"
        "            name: rc_lb\n"
        "            secure: true\n"
        "        servers:\n"
        f"{servers}\n"
        + static_certs
    )


def acme_storage_name(staging: bool) -> str:
    return ACME_FILE_STAGING if staging else ACME_FILE


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



def _acme_args(spec: TlsSpec) -> list[str]:
    """Traefik's ACME flags."""
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
    else:
        # TLS-ALPN-01, the same challenge the official compose.traefik.yml uses
        # (`certificatesresolvers.le.acme.tlschallenge`). It validates on 443, which
        # is already published, so no second port has to be opened for it.
        args.append("--certificatesresolvers.le.acme.tlschallenge=true")
    return args


def can_redirect_http(mode: str, port: int) -> bool:
    """Whether an http->https redirect is even applicable: only when we own 443.

    Local mode runs on an allocated port, where claiming a second one per repro
    just to redirect to the first costs more than it gives.
    """
    return mode == MODE_ACME and port == 443


def service(spec: TlsSpec) -> dict:
    """The Traefik service. Bare `host:container` ports -- compose._bind_ports
    prefixes the bind interface in one pass, as it does for every other service."""
    cmd = [
        "--providers.file.filename=/etc/traefik/dynamic.yml",
        "--providers.file.watch=true",
        "--entryPoints.websecure.address=:443",
    ]
    volumes = ["./tls/dynamic.yml:/etc/traefik/dynamic.yml:ro"]
    ports = [f"{spec.port}:443"]

    # Port 80, with a permanent redirect to https -- what the official
    # RocketChat/rocketchat-compose Traefik files do
    # (entrypoints.http.http.redirections.entryPoint.*). Without it, typing the bare
    # hostname reaches nothing at all, because a browser tries http first.
    # Only for a real domain: local mode is on an allocated port, where claiming an
    # extra one per repro to redirect to costs more than it gives.
    if spec.http_redirect:
        cmd += [
            "--entryPoints.web.address=:80",
            "--entryPoints.web.http.redirections.entryPoint.to=websecure",
            "--entryPoints.web.http.redirections.entryPoint.scheme=https",
            "--entryPoints.web.http.redirections.entryPoint.permanent=true",
        ]
        ports.append("80:80")

    if spec.mode == MODE_ACME:
        cmd += _acme_args(spec)
        # Mounted from ~/.rc-repro/acme, not the workspace: see ACME_DIR_NAME.
        volumes.append(f"{acme_dir()}:/etc/traefik/acme")
    else:
        volumes.append("./tls/certs:/etc/traefik/certs:ro")

    svc: dict = {
        "image": f"docker.io/traefik:{TRAEFIK_TAG}",
        "restart": "unless-stopped",
        "command": cmd,
        "volumes": volumes,
        "ports": ports,
    }
    if spec.mode == MODE_ACME and spec.acme_challenge == "dns":
        # Unconditional: mounting this only "if it exists" meant a missing file
        # produced a Traefik that ran with no credentials and failed opaquely.
        # dns_credentials() has already refused that case by the time we get here.
        svc["env_file"] = [str(dns_env_path())]
    return svc


def files(spec: TlsSpec, backends: list[str],
          cert_pem: str = "", key_pem: str = "") -> list[tuple[str, str]]:
    """Files written into the repro workspace and mounted by Traefik."""
    out = [("tls/dynamic.yml", _dynamic_yml(backends, spec.mode, spec.host))]
    if spec.mode != MODE_ACME:
        out += [("tls/certs/tls.crt", cert_pem), ("tls/certs/tls.key", key_pem)]
    return out


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
