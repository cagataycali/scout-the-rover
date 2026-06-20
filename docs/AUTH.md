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
