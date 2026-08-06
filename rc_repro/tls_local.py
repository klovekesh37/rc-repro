"""rc-repro's own certificate authority, for `up --https` without a domain.

Split out of tls.py: this is the offline path -- an openssl CA whose leaf
certificates a browser only trusts after `rc-repro trust-ca`. The Let's Encrypt
path in tls.py shares nothing with it but the Traefik service that serves the
result, so keeping them in one module made both harder to follow.

Everything here is local. Nothing in this file talks to a certificate authority
over the network.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from rc_repro.errors import ReproError
from rc_repro.tls import CA_CRT, CA_KEY, ca_dir

_LINUX_ANCHORS = (
    # (directory, refresh command) — first one whose directory exists wins.
    ("/usr/local/share/ca-certificates", ["update-ca-certificates"]),      # Debian/Ubuntu
    ("/etc/pki/ca-trust/source/anchors", ["update-ca-trust", "extract"]),  # Fedora/RHEL
)
_ANCHOR_NAME = "rc-repro-local-ca.crt"


_CA_DAYS = 3650          # the root: made once, install once
_LEAF_DAYS = 825         # a leaf: regenerated on every `up`, so this is generous

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
             "-subj", "/CN=rc-repro local CA/O=rc-repro")
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


