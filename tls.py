#!/usr/bin/env python3
"""
🔒 scout TLS — auto self-signed certificate for WebAuthn over LAN/IP.

Why
---
WebAuthn (passkeys) only runs in a "secure context": HTTPS, or http://localhost.
The moment you open the dashboard at http://192.168.1.x:8080 the browser refuses
to do the passkey ceremony. So for any real LAN/field use the dashboard must
speak HTTPS. We don't want to depend on Let's Encrypt (the rover may have no
public DNS) so we mint our OWN self-signed cert, valid for this box's hostname
+ every local IP we can find.

Trade-off: a self-signed cert is NOT trusted by browsers, so the user sees a
one-time "Your connection is not private" warning and clicks through (Advanced →
Proceed). That's fine — the secure-context requirement is satisfied, so passkeys
work. For a polished setup you can instead drop a real cert at DASH_TLS_CERT /
DASH_TLS_KEY (e.g. mkcert, or a cert for a private domain).

Usage
-----
  DASH_TLS=true python dashboard_server.py        # auto self-signed on :8443
  DASH_TLS=true DASH_PORT=443 ...                 # custom port
  DASH_TLS_CERT=/path/fullchain.pem DASH_TLS_KEY=/path/key.pem DASH_TLS=true ...

The generated cert/key are cached (DASH_TLS_DIR, default ./.scout_tls) and
regenerated only when missing or expired, so the cert is stable across restarts
(important: passkeys bind to the ORIGIN, and a stable origin keeps logins valid).
"""
from __future__ import annotations

import datetime
import ipaddress
import os
import socket
from pathlib import Path
from typing import List, Optional, Tuple


def _local_ips() -> List[str]:
    """Best-effort: every IPv4 this host answers on (for cert SANs)."""
    ips = set()
    try:
        # the trick: a UDP socket 'connected' to a public IP reveals our LAN IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    ips.add("127.0.0.1")
    return sorted(ips)


def _hostnames() -> List[str]:
    names = {"localhost"}
    try:
        names.add(socket.gethostname())
        names.add(socket.getfqdn())
    except Exception:
        pass
    # always include the mDNS name we advertise (scout.local) so the cert is
    # valid for the hostname WebAuthn actually uses.
    mdns = os.getenv("SCOUT_MDNS_NAME", "scout").strip().rstrip(".")
    if mdns:
        names.add(f"{mdns}.local")
    # allow operator to pin extra names (e.g. a private domain or mDNS .local)
    extra = os.getenv("DASH_TLS_HOSTS", "").strip()
    if extra:
        for n in extra.replace(",", " ").split():
            if n:
                names.add(n)
    return sorted(n for n in names if n)


def _generate_self_signed(cert_path: Path, key_path: Path) -> None:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    hostnames = _hostnames()
    ips = _local_ips()

    cn = hostnames[0] if hostnames else "scout.local"
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "scout rover"),
    ])

    san: List[x509.GeneralName] = []
    for h in hostnames:
        san.append(x509.DNSName(h))
    for ip in ips:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=825))  # ~max browsers accept
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        os.chmod(key_path, 0o600)
    except Exception:
        pass


def _cert_still_valid(cert_path: Path) -> bool:
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        from cryptography import x509 as _x
        na = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
        return na > datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)
    except Exception:
        return False


def _try_mkcert(tls_dir: Path) -> Optional[Tuple[str, str]]:
    """If mkcert is installed, mint a LOCALLY-TRUSTED cert (no browser warning).

    mkcert installs a local CA into the OS/browser trust stores; certs it issues
    are trusted on machines that ran `mkcert -install`. Great for a team laptop
    fleet. Controlled by DASH_TLS_MKCERT (default 'auto' = use if present).
    """
    import shutil
    import subprocess

    mode = os.getenv("DASH_TLS_MKCERT", "auto").strip().lower()
    if mode in ("0", "false", "no", "off"):
        return None
    mkcert = shutil.which("mkcert")
    if not mkcert:
        if mode in ("1", "true", "yes", "on"):
            print("⚠️  DASH_TLS_MKCERT requested but mkcert not found in PATH")
        return None

    cert_path = tls_dir / "scout-mkcert.pem"
    key_path = tls_dir / "scout-mkcert-key.pem"
    if cert_path.exists() and key_path.exists() and _cert_still_valid(cert_path):
        return str(cert_path), str(key_path)

    names = _hostnames() + _local_ips()
    try:
        # ensure local CA is installed (idempotent; needs to have been trusted once)
        subprocess.run([mkcert, "-install"], check=False, capture_output=True, timeout=30)
        cmd = [mkcert, "-cert-file", str(cert_path), "-key-file", str(key_path)] + names
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and cert_path.exists():
            print(f"🔒 mkcert: locally-trusted cert minted (no browser warning on trusting machines)")
            print(f"   SANs: {', '.join(names)}")
            return str(cert_path), str(key_path)
        print(f"⚠️  mkcert failed ({r.returncode}); falling back to self-signed: {r.stderr.strip()[:200]}")
    except Exception as e:
        print(f"⚠️  mkcert error; falling back to self-signed: {e}")
    return None


def ensure_cert() -> Optional[Tuple[str, str]]:
    """Return (cert_path, key_path) for the dashboard, or None if TLS is off.

    Priority:
      1. DASH_TLS_CERT + DASH_TLS_KEY  → use the operator-supplied cert as-is.
      2. mkcert (locally-trusted, no warning) if installed & DASH_TLS_MKCERT!=off.
      3. else auto-generate a cached self-signed cert (regenerated if expired).
    """
    if os.getenv("DASH_TLS", "false").strip().lower() not in ("1", "true", "yes", "on"):
        return None

    cert_env = os.getenv("DASH_TLS_CERT", "").strip()
    key_env = os.getenv("DASH_TLS_KEY", "").strip()
    if cert_env and key_env:
        if Path(cert_env).exists() and Path(key_env).exists():
            return cert_env, key_env
        print(f"⚠️  DASH_TLS_CERT/KEY set but file(s) missing — falling back")

    tls_dir = Path(os.getenv("DASH_TLS_DIR", "./.scout_tls")).resolve()
    tls_dir.mkdir(parents=True, exist_ok=True)

    # 2. mkcert (trusted, no warning) — opt-out via DASH_TLS_MKCERT=off
    mk = _try_mkcert(tls_dir)
    if mk:
        return mk

    # 3. self-signed fallback
    cert_path = tls_dir / "scout-cert.pem"
    key_path = tls_dir / "scout-key.pem"
    if not (cert_path.exists() and key_path.exists() and _cert_still_valid(cert_path)):
        print("🔒 generating self-signed TLS cert for WebAuthn (hostnames + LAN IPs)…")
        _generate_self_signed(cert_path, key_path)
        print(f"   cert: {cert_path}")
        print(f"   SANs: {', '.join(_hostnames() + _local_ips())}")

    return str(cert_path), str(key_path)


def mkcert_ca_path() -> Optional[str]:
    """Return the path to the mkcert root CA PEM, if mkcert is in use.

    iOS/Android must trust THIS CA to avoid cert warnings. We surface it so the
    dashboard can serve it for on-device install. Returns None if mkcert isn't
    active (self-signed mode — nothing to trust; the user just clicks through).
    """
    import shutil
    import subprocess

    if os.getenv("DASH_TLS_MKCERT", "auto").strip().lower() in ("0", "false", "no", "off"):
        return None
    mkcert = shutil.which("mkcert")
    if not mkcert:
        return None
    # CAROOT env wins; else ask mkcert
    caroot = os.getenv("CAROOT", "").strip()
    if not caroot:
        try:
            r = subprocess.run([mkcert, "-CAROOT"], capture_output=True, text=True, timeout=10)
            caroot = r.stdout.strip()
        except Exception:
            return None
    ca = Path(caroot) / "rootCA.pem"
    return str(ca) if ca.exists() else None


def access_urls(port: int) -> List[str]:
    """Pretty https:// URLs the operator can open."""
    urls = [f"https://localhost:{port}"]
    for ip in _local_ips():
        if ip != "127.0.0.1":
            urls.append(f"https://{ip}:{port}")
    return urls


if __name__ == "__main__":
    # quick manual test: DASH_TLS=true python tls.py
    res = ensure_cert()
    print("cert/key:", res)
    if res:
        print("urls:", access_urls(int(os.getenv("DASH_PORT", "8443"))))
