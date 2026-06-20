#!/usr/bin/env python3
"""
📡 scout mDNS — advertise the rover as `scout.local` on the LAN.

WebAuthn passkeys need a *hostname* (rpId can't be a raw IP). mDNS / Bonjour /
Avahi lets every device on the same network resolve `scout.local` → the rover's
IP automatically, with zero per-client /etc/hosts edits. macOS & iOS resolve
.local out of the box; Windows 10+ and most Linux do too.

We advertise two things:
  1. An address record for the hostname (default `scout`) → `scout.local`.
  2. An HTTP(S) service record so the dashboard shows up in network browsers.

Pure-Python (zeroconf), so it works inside the container without configuring
Avahi. If the host already runs Avahi/mDNS we don't conflict — we just add our
own record. Best-effort: failures never block the dashboard.

Env:
  SCOUT_MDNS         "true"/"false" (default true)
  SCOUT_MDNS_NAME    advertised name without .local (default "scout")
  DASH_PORT          port to advertise (default 8080)
  DASH_TLS           if true, advertise https (_https._tcp) else http
"""
from __future__ import annotations

import atexit
import os
import socket
from typing import List, Optional


def _bool(k: str, d: bool) -> bool:
    return os.getenv(k, str(d)).strip().lower() in ("1", "true", "yes", "on")


def _primary_ip() -> Optional[str]:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _all_ipv4() -> List[str]:
    ips = set()
    p = _primary_ip()
    if p:
        ips.add(p)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


_zc = None  # keep refs alive for the process lifetime
_info = None


def start() -> Optional[str]:
    """Advertise scout.local. Returns the advertised hostname or None."""
    if not _bool("SCOUT_MDNS", True):
        return None

    global _zc, _info
    try:
        from zeroconf import Zeroconf, ServiceInfo
    except Exception as e:
        print(f"⚠️  mDNS skipped (zeroconf not available): {e}", flush=True)
        return None

    name = os.getenv("SCOUT_MDNS_NAME", "scout").strip().rstrip(".") or "scout"
    fqdn = f"{name}.local"
    port = int(os.getenv("DASH_PORT", "8080"))
    https = _bool("DASH_TLS", False)
    svc_type = "_https._tcp.local." if https else "_http._tcp.local."

    ips = _all_ipv4()
    if not ips:
        print("⚠️  mDNS: no LAN IP found; not advertising", flush=True)
        return None

    try:
        _zc = Zeroconf()
        addresses = [socket.inet_aton(ip) for ip in ips]
        _info = ServiceInfo(
            type_=svc_type,
            name=f"{name}.{svc_type}",
            addresses=addresses,
            port=port,
            properties={"path": "/", "scheme": "https" if https else "http"},
            server=f"{fqdn}.",  # the A-record host → makes scout.local resolve
        )
        _zc.register_service(_info, allow_name_change=True)
        atexit.register(stop)
        scheme = "https" if https else "http"
        print(f"📡 mDNS: advertising {fqdn} → {', '.join(ips)}  ({scheme}://{fqdn}:{port})", flush=True)
        return fqdn
    except Exception as e:
        print(f"⚠️  mDNS advertise failed: {e}", flush=True)
        try:
            if _zc:
                _zc.close()
        except Exception:
            pass
        _zc = None
        return None


def stop():
    global _zc, _info
    try:
        if _zc and _info:
            _zc.unregister_service(_info)
        if _zc:
            _zc.close()
    except Exception:
        pass
    _zc = None
    _info = None


if __name__ == "__main__":
    import time
    host = start()
    print("advertised:", host)
    if host:
        print("Leaving advertisement up for 30s (Ctrl-C to stop)…")
        try:
            time.sleep(30)
        except KeyboardInterrupt:
            pass
        stop()
