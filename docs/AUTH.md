# 🔐 scout auth — WebAuthn passkeys + HTTPS

The dashboard is sealed behind **WebAuthn passkeys** (passwordless). Only enrolled
admin devices can drive the rover.

## Why HTTPS is required
WebAuthn only runs in a **secure context**: `https://…` or `http://localhost`.
Plain `http://<ip>:<port>` makes the browser refuse the passkey ceremony. So for
any LAN / field use the dashboard must serve HTTPS.

`DASH_TLS=true` mints a **self-signed cert** on first boot (cached in
`.scout_tls/`, valid for this host's name + LAN IPs). You accept a one-time
"not private" browser warning (Advanced → Proceed) — the secure-context
requirement is then satisfied and passkeys work.

```bash
make dashboard-tls            # local: https://localhost:8443
# docker: DASH_TLS=true is the default in docker-compose.yml
```

## ⚠️ The rpId gotcha — use a HOSTNAME, not a raw IP
WebAuthn's relying-party id (`rpId`) **must be a registrable domain or
`localhost`**. A raw IP address (`192.168.1.50`) is **not** a valid rpId — the
browser will reject enrollment. `https://<ip>:8443` gives you a secure context
but passkeys still won't enroll.

**Fixes (pick one):**
1. **mDNS / `.local` name** — reach the rover at `https://scout.local:8443`.
2. **hosts entry** — add `192.168.1.50  scout.local` to the *client's* `/etc/hosts`,
   then open `https://scout.local:8443`.
3. **private domain** — point a DNS name at the rover and set
   `SCOUT_AUTH_RP_ID=rover.example.com` + `SCOUT_AUTH_ORIGIN=https://rover.example.com`.

The login screen detects a raw-IP / insecure origin and shows a clear warning
instead of failing cryptically.

## Real certs (optional, nicer UX — no warning)
Drop a trusted cert (mkcert, private CA, or Let's Encrypt for a public name):
```bash
DASH_TLS=true \
DASH_TLS_CERT=/path/fullchain.pem \
DASH_TLS_KEY=/path/privkey.pem \
make dashboard-tls
```

## Env reference
| var | default | meaning |
|---|---|---|
| `DASH_TLS` | `false` (local) / `true` (docker) | serve HTTPS |
| `DASH_TLS_DIR` | `./.scout_tls` | where the self-signed cert is cached |
| `DASH_TLS_HOSTS` | — | extra SAN hostnames (space/comma sep) |
| `DASH_TLS_CERT` / `DASH_TLS_KEY` | — | use a real cert instead of self-signed |
| `SCOUT_AUTH_ENABLED` | `true` | master auth switch |
| `SCOUT_AUTH_RP_ID` | derived from Host | force rpId (set to your domain) |
| `SCOUT_AUTH_ORIGIN` | derived | force expected origin |
| `SCOUT_AUTH_STORE` | `./.scout_auth.json` | passkey + JWT-secret store |
| `SCOUT_AUTH_TOKEN_TTL` | `86400` | session length (s) |
| `SCOUT_AUTH_BOOTSTRAP_TOKEN` | — | one-time secret to gate first enrollment |

## Multi-admin
Settings drawer → **🔑 Admin passkeys**: enroll a teammate's device, rename, or
revoke. The last passkey can't be removed (would lock everyone out).

---

# 🎫 Field setup kit (mDNS + mkcert + QR)

Three pieces make "scan & drive" work in the field:

## 1. mDNS — `scout.local` resolves automatically
The dashboard advertises itself as **`scout.local`** on the LAN (pure-Python
zeroconf, auto-started). macOS/iOS/Windows10+/most Linux resolve `.local` with
zero client config. No more per-device `/etc/hosts` edits.

```bash
make mdns        # standalone test
# auto-runs inside the dashboard; name via SCOUT_MDNS_NAME (default 'scout')
```

> **Docker:** mDNS needs to reach the LAN, so run the dashboard with
> `network_mode: host` (commented hint in `docker-compose.yml`). In bridge mode
> the IP-fallback URL on the QR card still works; `scout.local` just won't
> resolve from other devices.

## 2. mkcert — trusted cert, NO browser warning (optional)
Self-signed works but shows a one-time warning. For a polished fleet, install
mkcert's local CA on your team's machines — then scout's cert is *trusted*:

```bash
make mkcert-install     # installs mkcert + trusts a local CA
make dashboard-tls      # tls.py auto-detects mkcert → trusted cert, no warning
```

`tls.py` priority: `DASH_TLS_CERT/KEY` → **mkcert** (if installed) → self-signed.
Opt out with `DASH_TLS_MKCERT=off`.

## 3. Printable QR card — scan to enroll
Generates a QR + printable card pointing at `https://scout.local:PORT`:

```bash
make field-card                          # prints ASCII QR + writes PNG/HTML
BOOTSTRAP=field-secret make field-card   # embeds the one-time setup token
```
Outputs to `.scout_tls/`:
- `field_setup_qr.png` — the QR image
- `field_setup.html` — a printable card (open → 🖨️ Print, or screenshot)

Live, always-current card is also served at **`/field-card`** (public — it only
contains the access URL + whatever bootstrap token you chose to embed).

### Field flow
1. Power on scout → dashboard advertises `scout.local`, mints its cert.
2. Print/show the card (`make field-card` or open `/field-card`).
3. Teammate joins the same Wi-Fi → scans QR → accepts cert once (or zero
   warnings if their machine trusts the mkcert CA) → enrols passkey.
4. Driving. 🛞

### New env
| var | default | meaning |
|---|---|---|
| `SCOUT_MDNS` | `true` | advertise scout.local on the LAN |
| `SCOUT_MDNS_NAME` | `scout` | advertised name (→ `<name>.local`) |
| `DASH_TLS_MKCERT` | `auto` | use mkcert if installed (`off` to disable) |
