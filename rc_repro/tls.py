"""HTTPS add-on: terminate TLS in front of Rocket.Chat with Traefik.

Layered onto ANY repro via `up --https` rather than being its own preset --
`--preset` is single-valued (cli.py), so a `tls` preset would be mutually
exclusive with oidc/saml/livechat/multi-instance, which are exactly the ones that
need HTTPS. Same shape as the monitoring add-on: services(), files(), notes().

Three ways to get the certificate, matching the three real situations:

  local  `--https`                        a CA made here with openssl, once, then a
                                          leaf per repro. Offline, no rate limits.
  acme   `--https --domain X --acme-email` Let's Encrypt via Traefik's own ACME.
  own    `--https --tls-cert/--tls-key`    a certificate you already have.

Only `local` generates anything; `acme` hands the job to Traefik, and `own` just
mounts the files. All three end up serving on a TLS entrypoint and setting the
repro's ROOT_URL to https, which is what RC actually cares about.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rc_repro import config
from rc_repro.errors import ReproError, ValidationError

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
# Credentials for a dns-01 provider. A file, never argv: `ps` shows command lines
# to every user on the box.
DNS_ENV_FILE = "dns.env"
LEGO_PROVIDER_DOCS = "https://go-acme.github.io/lego/dns/"

MODE_LOCAL, MODE_ACME, MODE_OWN = "local", "acme", "own"


@dataclass
class TlsSpec:
    """Resolved HTTPS settings for one repro. `mode` picks the certificate source."""
    mode: str = MODE_LOCAL
    host: str = ""               # external hostname (a domain, or <name>.rcrepro.localhost)
    port: int = 0                # published host port for the TLS entrypoint
    acme_email: str = ""
    acme_staging: bool = False
    acme_challenge: str = "tlsalpn"      # tlsalpn (inbound :443) | dns (no inbound)
    acme_dns_provider: str = ""
    cert_path: str = ""          # mode=own: source paths on the host
    key_path: str = ""
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
            "the workspace is reached at, because it becomes ROOT_URL. (A wildcard "
            "certificate is a separate thing and needs the dns challenge.)")
    if any(not _LABEL_RE.match(lbl) for lbl in raw.split(".")):
        raise ValidationError(
            f"--domain {value!r} is not a valid hostname (letters, digits and "
            "hyphens per label, separated by dots).")
    return raw, ("; ".join(fixes) if fixes else "")


# --- openssl -----------------------------------------------------------------

def _openssl(*args: str, stdin: str | None = None) -> None:
    """Run openssl, turning any failure into a user-facing error.

    openssl writes its real diagnosis to stderr and exits non-zero; surfacing
    that beats "command failed", which is all the exit code says.
    """
    try:
        proc = subprocess.run(("openssl",) + args, capture_output=True, text=True,
                              input=stdin, timeout=60)
    except FileNotFoundError as exc:
        raise ReproError(
            "openssl not found - it generates the local CA for --https. Install it, "
            "or supply a certificate with --tls-cert/--tls-key.") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReproError(f"openssl failed: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise ReproError("openssl " + args[0] + " failed: "
                         + (detail[-1] if detail else f"exit {proc.returncode}"))


def ensure_ca() -> tuple[Path, Path]:
    """Create the local CA if absent; return (key, cert). Idempotent.

    One CA per machine, shared by every repro and never regenerated: it is what
    `trust-ca` installs, so replacing it would silently invalidate that trust.
    """
    d = ca_dir()
    key, crt = d / CA_KEY, d / CA_CRT
    if key.exists() and crt.exists():
        return key, crt
    d.mkdir(parents=True, exist_ok=True)
    _openssl("genrsa", "-out", str(key), "4096")
    # 0600: this key can mint a certificate for any name the browser will trust.
    os.chmod(key, 0o600)
    _openssl("req", "-x509", "-new", "-nodes", "-key", str(key), "-sha256",
             "-days", str(_CA_DAYS), "-out", str(crt),
             "-subj", "/CN=rc-repro local CA/O=rc-repro",
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    return key, crt


def _leaf_ext(host: str, extra_sans: list[str]) -> str:
    """The x509 extension block for a leaf cert.

    SANs, not CN: browsers have ignored commonName since Chrome 58, so a cert
    with only a CN is rejected with ERR_CERT_COMMON_NAME_INVALID.
    """
    dns = [host]
    ips: list[str] = []
    for s in extra_sans:
        s = s.strip()
        if not s:
            continue
        (ips if _is_ip(s) else dns).append(s)
    # localhost/127.0.0.1 too, so the existing http:// habits and any in-container
    # health check keep working against the same cert.
    for d in ("localhost",):
        if d not in dns:
            dns.append(d)
    if "127.0.0.1" not in ips:
        ips.append("127.0.0.1")
    lines = ["basicConstraints=CA:FALSE",
             "keyUsage=digitalSignature,keyEncipherment",
             "extendedKeyUsage=serverAuth",
             "subjectAltName=@alt",
             "",
             "[alt]"]
    lines += [f"DNS.{i} = {d}" for i, d in enumerate(dns, 1)]
    lines += [f"IP.{i} = {p}" for i, p in enumerate(ips, 1)]
    return "\n".join(lines) + "\n"


def _is_ip(value: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def issue_leaf(host: str, extra_sans: list[str] | None = None) -> tuple[str, str]:
    """Mint a leaf cert for `host`, signed by the local CA. Returns (cert, key) PEM.

    Written into the repro workspace by the caller, so it is disposable: `up`
    regenerates it and `down --volumes` deleting it costs nothing. That is the
    opposite of the ACME case, where losing state costs rate-limit budget.
    """
    import tempfile
    ca_key, ca_crt = ensure_ca()
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        key, csr, crt, ext = t / "tls.key", t / "tls.csr", t / "tls.crt", t / "ext.cnf"
        ext.write_text(_leaf_ext(host, extra_sans or []), encoding="utf-8")
        _openssl("genrsa", "-out", str(key), "2048")
        _openssl("req", "-new", "-key", str(key), "-out", str(csr), "-subj", f"/CN={host}")
        _openssl("x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
                 "-CAcreateserial", "-out", str(crt), "-days", str(_LEAF_DAYS),
                 "-sha256", "-extfile", str(ext))
        return crt.read_text(encoding="utf-8"), key.read_text(encoding="utf-8")


def read_own_cert(cert_path: str, key_path: str) -> tuple[str, str]:
    """Read a user-supplied cert/key, checking what Traefik would only fail on later.

    Traefik's failure for a mismatched pair is a startup log line nobody sees --
    the repro just doesn't serve. Checking here turns it into an error at `up`.
    """
    cert, key = Path(cert_path).expanduser(), Path(key_path).expanduser()
    for label, p in (("--tls-cert", cert), ("--tls-key", key)):
        if not p.is_file():
            raise ValidationError(f"{label}: no such file: {p}")
    pem, keypem = cert.read_text(encoding="utf-8"), key.read_text(encoding="utf-8")
    if "BEGIN CERTIFICATE" not in pem:
        raise ValidationError(f"--tls-cert: {cert} is not a PEM certificate")
    if "PRIVATE KEY" not in keypem:
        raise ValidationError(f"--tls-key: {key} is not a PEM private key")
    return pem, keypem


# --- compose ------------------------------------------------------------------

def _dynamic_yml(backends: list[str], mode: str, host: str = "") -> str:
    """Traefik file-provider config: route everything to the RC backend(s) over TLS.

    File provider rather than the Docker one: it needs no access to the Docker
    socket, which the multi-instance preset already avoids for the same reason.
    """
    servers = "\n".join(f'          - url: "http://{b}:{config.RC_CONTAINER_PORT}"'
                        for b in backends)
    # mode=acme leaves cert selection to the resolver; local/own load a static pair.
    if mode == MODE_ACME:
        # `domains` is REQUIRED here. Traefik derives what to request from the
        # router's Host() matcher, and this rule is PathPrefix(`/`) on purpose (so
        # the workspace also answers on localhost and the container name). Without
        # an explicit domain, Traefik has nothing to ask for: it logs
        # "no domain found" and silently serves its default certificate — which
        # looks exactly like an ACME failure, with no ACME request ever made.
        tls_block = ("      tls:\n        certResolver: le\n        domains:\n"
                     f'          - main: "{host}"\n')
    else:
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
        '      rule: "PathPrefix(`/`)"\n'
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
        args.append("--certificatesresolvers.le.acme.dnschallenge=true")
        if spec.acme_dns_provider:
            args.append("--certificatesresolvers.le.acme.dnschallenge.provider="
                        + spec.acme_dns_provider)
    else:
        # TLS-ALPN-01: only needs the one port we already publish. The only inbound
        # challenge offered -- http-01 needs port 80 as WELL as 443 and buys nothing
        # tlsalpn does not already do, so it was removed rather than maintained.
        args.append("--certificatesresolvers.le.acme.tlschallenge=true")
    return args


def can_redirect_http(mode: str, port: int) -> bool:
    """Whether an http->https redirect is even applicable: only when we own 443.

    Local mode runs on an allocated port, where claiming a second one per repro
    just to redirect to the first costs more than it gives.
    """
    return mode in (MODE_ACME, MODE_OWN) and port == 443


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

_LINUX_ANCHORS = (
    # (directory, refresh command) — first one whose directory exists wins.
    ("/usr/local/share/ca-certificates", ["update-ca-certificates"]),      # Debian/Ubuntu
    ("/etc/pki/ca-trust/source/anchors", ["update-ca-trust", "extract"]),  # Fedora/RHEL
)
_ANCHOR_NAME = "rc-repro-local-ca.crt"


def _sudo(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a trust-store command, with sudo when not already root.

    Non-interactive (`sudo -n`): a CLI that silently blocks on a hidden password
    prompt looks like a hang, so a missing sudo becomes the manual-instructions path.
    """
    full = cmd if os.geteuid() == 0 else ["sudo", "-n"] + cmd
    return subprocess.run(full, capture_output=True, text=True, timeout=120)


def trust(ca_crt: Path, uninstall: bool = False) -> tuple[bool, str]:
    """Install/remove the CA in the OS trust store. Returns (done, description)."""
    import platform
    system = platform.system()
    if system == "Darwin":
        if uninstall:
            cmd = ["security", "remove-trusted-cert", "-d", str(ca_crt)]
        else:
            cmd = ["security", "add-trusted-cert", "-d", "-r", "trustRoot",
                   "-k", "/Library/Keychains/System.keychain", str(ca_crt)]
        try:
            r = _sudo(cmd)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"macOS System keychain ({exc})"
        return (r.returncode == 0), "the macOS System keychain"
    if system == "Linux":
        for anchor_dir, refresh in _LINUX_ANCHORS:
            d = Path(anchor_dir)
            if not d.is_dir():
                continue
            target = d / _ANCHOR_NAME
            try:
                if uninstall:
                    r = _sudo(["rm", "-f", str(target)])
                else:
                    r = _sudo(["cp", str(ca_crt), str(target)])
                if r.returncode != 0:
                    return False, f"{anchor_dir} (need sudo: {(r.stderr or '').strip()[:60]})"
                r = _sudo(refresh)
            except (OSError, subprocess.SubprocessError) as exc:
                return False, f"{anchor_dir} ({exc})"
            return (r.returncode == 0), f"the system trust store ({anchor_dir})"
        return False, "no known trust-store directory on this Linux"
    return False, system or "unknown platform"


def manual_trust_instructions(ca_crt: Path, uninstall: bool = False) -> str:
    verb = "Remove" if uninstall else "Install"
    return (
        f"\n  {verb} this file by hand:\n"
        f"    {ca_crt}\n\n"
        "  macOS:          Keychain Access -> System -> drag it in -> set to Always Trust\n"
        "  Debian/Ubuntu:  sudo cp it into /usr/local/share/ca-certificates/ "
        "&& sudo update-ca-certificates\n"
        "  Fedora/RHEL:    sudo cp it into /etc/pki/ca-trust/source/anchors/ "
        "&& sudo update-ca-trust extract\n"
        "  Windows:        certutil -addstore -f Root <path>\n"
        "  Firefox:        Settings -> Privacy & Security -> Certificates -> "
        "View Certificates -> Authorities -> Import\n\n"
        "  Or skip it: the browser warning is safe to click through for a local repro.\n"
    )


# --- verification -------------------------------------------------------------
# Traefik obtains certificates in the BACKGROUND after it starts, and falls back to
# a self-signed "TRAEFIK DEFAULT CERT" when ACME fails. So a repro can be "ready"
# (RC answers on its internal http port) while HTTPS is serving a dummy cert, or
# nothing at all. Nothing but an actual TLS connection can tell the difference.

# What Traefik serves when it has no real certificate — the signature of a failure.
_TRAEFIK_FALLBACK = "TRAEFIK DEFAULT CERT"


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
_PUBLIC_RESOLVERS = ("1.1.1.1", "8.8.8.8")

# A few of Cloudflare's published edge ranges. Used ONLY to explain a failure more
# precisely, never to block: the list drifts, and being wrong here must not stop a
# working setup. See https://www.cloudflare.com/ips-v4
_CLOUDFLARE_HINT_NETS = ("104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13",
                         "173.245.48.0/20", "162.158.0.0/15", "108.162.192.0/18",
                         "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22",
                         "198.41.128.0/17", "131.0.72.0/22", "141.101.64.0/18",
                         "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22")


def _dig(host: str, resolver: str) -> list[str]:
    try:
        proc = subprocess.run(["dig", "+short", "+time=3", "+tries=1",
                               f"@{resolver}", host, "A"],
                              capture_output=True, text=True, timeout=12)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        # +short prints CNAME targets too; keep only addresses.
        if line and not line.endswith("."):
            out.append(line)
    return out


def resolves_to(host: str, public: bool = True) -> list[str]:
    """Addresses `host` resolves to, [] if nothing answers.

    Falls back to public resolvers because the caller is really asking "what will
    Let's Encrypt see?", and the local resolver may not agree with the internet.
    """
    try:
        infos = socket.getaddrinfo(host, None)
        addrs = sorted({i[4][0] for i in infos})
    except OSError:
        addrs = []
    if addrs or not public:
        return addrs
    for resolver in _PUBLIC_RESOLVERS:
        found = _dig(host, resolver)
        if found:
            return sorted(set(found))
    return []


def _is_proxy_fronted(addrs: list[str]) -> bool:
    import ipaddress
    nets = [ipaddress.ip_network(n) for n in _CLOUDFLARE_HINT_NETS]
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.version == 4 and any(ip in n for n in nets):
            return True
    return False


def dns_preflight(host: str, challenge: str = "tlsalpn") -> tuple[bool, str]:
    """Rule out ACME setups that are certain to fail, before one costs quota.

    Cannot prove inbound reachability — NAT and firewalls are invisible from here
    — so a pass is necessary, not sufficient. What it can catch: a name nothing
    resolves, a name pointing only somewhere unroutable, and a name fronted by a
    TLS-terminating proxy while using a challenge that requires reaching the origin.
    """
    import ipaddress
    addrs = resolves_to(host)

    if challenge == "dns":
        # dns-01 validates by reading a TXT record at _acme-challenge.<host>, which
        # the provider API creates. Let's Encrypt never connects to the origin, so
        # the hostname does not need an A/AAAA record AT ALL -- requiring one here
        # refused a setup that would have issued fine.
        where = ", ".join(addrs) if addrs else "no A/AAAA record (fine for dns-01)"
        return True, f"{host} -> {where} (dns-01 needs no inbound access)"

    if not addrs:
        return False, (
            f"{host} does not resolve, on this machine or via {' / '.join(_PUBLIC_RESOLVERS)}. "
            f"Add a record for it, then check with `dig +short {host}` — or use "
            "--acme-challenge dns, which needs no record for the host itself.")

    private = []
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_private or ip.is_link_local:
            private.append(a)
    if private and len(private) == len(addrs):
        return False, (f"{host} resolves only to {', '.join(private)}, which Let's "
                       "Encrypt cannot reach. It must resolve to a public address, "
                       "or use --acme-challenge dns.")

    if _is_proxy_fronted(addrs):
        return False, (
            f"{host} resolves to {', '.join(addrs)}, which looks like Cloudflare's "
            f"proxy (an orange-clouded record). Cloudflare terminates TLS there, so "
            f"the {challenge} challenge can never reach this host and Let's Encrypt "
            f"would validate against Cloudflare's certificate instead.\n"
            "  Either grey-cloud the record (DNS only), or keep the proxy and use:\n"
            "    --acme-challenge dns --acme-dns-provider cloudflare\n"
            "  (dns-01 needs no inbound access at all — put a zone-scoped token in "
            "~/.rc-repro/acme/dns.env as CF_DNS_API_TOKEN=...)")
    return True, f"{host} -> {', '.join(addrs)}"


def reachability_gaps(spec: TlsSpec, bind_host: str) -> list[str]:
    """Why the workspace will NOT be reachable at spec.root_url, or [] if it should be.

    Getting a certificate and being reachable at the name are separate things, and
    dns-01 makes the gap easy to miss: it issues with no DNS record and no public
    route, after which the summary advertises an https URL nothing outside this
    machine can open.
    """
    gaps = []
    if not resolves_to(spec.host):
        gaps.append(f"{spec.host} has no DNS record")
    if bind_host not in ("", "0.0.0.0", "::"):
        gaps.append(f"the workspace is bound to {bind_host}")
    elif not host_has_public_address():
        gaps.append("this host has no public address")
    return gaps


def host_has_public_address() -> bool:
    """Whether any local interface has a routable address.

    Behind NAT (a lab container, a laptop) an inbound challenge cannot reach us
    even with correct DNS. Only a hint: a port-forward can make it work anyway.
    """
    import ipaddress
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None)
    except OSError:
        return False
    for i in infos:
        try:
            ip = ipaddress.ip_address(i[4][0])
        except ValueError:
            continue
        if not (ip.is_loopback or ip.is_private or ip.is_link_local):
            return True
    return False


def notes(spec: TlsSpec, name: str = "<name>") -> list[str]:
    if spec.mode == MODE_LOCAL:
        return [
            f"HTTPS (local CA): {spec.root_url}",
            "The certificate is signed by rc-repro's own CA, so a browser warns until"
            " you run `rc-repro trust-ca` once (then no warnings, and curl needs no -k).",
            "A phone cannot use this: the cert is untrusted there and .localhost"
            " resolves to the phone itself. Use --domain for mobile.",
        ]
    if spec.mode == MODE_ACME:
        if spec.acme_staging:
            # Staging roots are NOT in any trust store -- that is the whole point of
            # staging. Claiming otherwise sent people looking for a broken workspace
            # when the browser warning was the expected, correct outcome.
            return [
                f"HTTPS (Let's Encrypt STAGING): {spec.root_url}",
                "A staging certificate is deliberately NOT trusted by browsers or the"
                " mobile app - a warning here is the SUCCESS signal: TLS terminated"
                " with a real Let's Encrypt-issued cert, so DNS, port 443 and the"
                " challenge all work.",
                "Confirm what is being served:  rc-repro tls-status --name " + name,
                "Then switch to a trusted certificate by re-running WITHOUT"
                " --acme-staging and with --force.",
            ]
        return [
            f"HTTPS (Let's Encrypt): {spec.root_url}",
            "Publicly trusted, so the Rocket.Chat mobile app accepts it with nothing"
            f" to install - just add {spec.root_url} in the app.",
            "Confirm it really issued:  rc-repro tls-status --name " + name,
            "Certificate state is kept in ~/.rc-repro/acme/ and survives `down`, so"
            " re-creating this repro reuses the cert instead of re-issuing it.",
        ]
    return [
        f"HTTPS (supplied certificate): {spec.root_url}",
        "Trusted wherever your certificate's issuer is trusted.",
        "Confirm what is being served:  rc-repro tls-status --name " + name,
    ]
