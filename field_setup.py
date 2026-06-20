#!/usr/bin/env python3
"""
🎫 scout field-setup — printable QR card for first-time access.

Generates a QR pointing at the dashboard's secure URL (https://scout.local:PORT
by default) so anyone in the field just scans it, accepts the cert once, and
enrolls their passkey. Produces:
  • a PNG QR              (.scout_tls/field_setup_qr.png)
  • a printable HTML card (.scout_tls/field_setup.html) — open & print/screenshot
  • an ASCII QR in the terminal (scannable directly from the screen)

Usage:
  python field_setup.py                         # uses scout.local + DASH_PORT
  python field_setup.py --url https://scout.local:8080
  python field_setup.py --bootstrap MYSECRET    # embed one-time setup token
  make field-setup
"""
from __future__ import annotations

import argparse
import os
import socket
from pathlib import Path


def default_url() -> str:
    name = os.getenv("SCOUT_MDNS_NAME", "scout").strip().rstrip(".") or "scout"
    port = os.getenv("DASH_PORT", "8080")
    https = os.getenv("DASH_TLS", "true").strip().lower() in ("1", "true", "yes", "on")
    scheme = "https" if https else "http"
    return f"{scheme}://{name}.local:{port}"


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def ascii_qr(data: str) -> str:
    import qrcode
    qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    m = qr.get_matrix()
    # half-block rendering: 2 rows per text line → compact + crisp
    lines = []
    for y in range(0, len(m), 2):
        row = ""
        for x in range(len(m[y])):
            top = m[y][x]
            bot = m[y + 1][x] if y + 1 < len(m) else False
            if top and bot:
                row += "█"
            elif top and not bot:
                row += "▀"
            elif not top and bot:
                row += "▄"
            else:
                row += " "
        lines.append(row)
    return "\n".join(lines)


def png_qr(data: str, path: Path) -> None:
    import qrcode
    img = qrcode.make(data, box_size=10, border=4)
    img.save(str(path))


HTML_CARD = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>scout · field setup</title>
<style>
  @media print {{ @page {{ margin: 12mm; }} .noprint {{ display:none; }} }}
  body {{ font-family: system-ui,-apple-system,sans-serif; background:#0a0a0f; color:#e8e8f0;
         display:flex; justify-content:center; padding:24px; }}
  .card {{ width:440px; background:linear-gradient(160deg,#13131c,#0b0b12);
          border:1px solid rgba(255,255,255,.12); border-radius:24px; padding:32px;
          box-shadow:0 24px 70px rgba(0,0,0,.5); text-align:center; }}
  h1 {{ font-size:26px; margin:0 0 2px; }} .sub {{ color:#9aa; font-size:13px; margin:0 0 20px; }}
  .qr {{ background:#fff; border-radius:18px; padding:16px; display:inline-block; }}
  .qr img {{ display:block; width:260px; height:260px; image-rendering:pixelated; }}
  .url {{ font-size:18px; font-weight:600; margin:18px 0 4px; color:#9be319; word-break:break-all; }}
  .alt {{ font-size:12px; color:#778; margin:0 0 16px; }}
  ol {{ text-align:left; font-size:13px; line-height:1.7; color:#ccd; margin:14px 4px 0; padding-left:20px; }}
  .tok {{ margin-top:16px; padding:10px 12px; background:rgba(155,227,25,.08);
         border:1px dashed rgba(155,227,25,.4); border-radius:12px; font-size:13px; }}
  .tok b {{ font-family:ui-monospace,monospace; color:#9be319; }}
  .foot {{ margin-top:18px; font-size:11px; color:#556; }}
  @media print {{ body {{ background:#fff; color:#111; }} .card {{ box-shadow:none; border:1px solid #ccc; background:#fff; }}
                  .url {{ color:#3a7d00; }} ol {{ color:#333; }} .foot {{ color:#999; }} }}
</style></head><body>
<div class="card">
  <h1>🛞 scout</h1>
  <p class="sub">field setup · scan to drive</p>
  <div class="qr"><img src="data:image/png;base64,{qr_b64}" alt="QR"></div>
  <div class="url">{url}</div>
  <p class="alt">if scout.local doesn't resolve: <b>{ip_url}</b></p>
  <ol>
    <li>Connect your phone/laptop to the <b>same Wi-Fi</b> as scout.</li>
    <li>Scan the QR (or type the URL).</li>
    <li>Accept the security warning once (Advanced → Proceed){trusted_note}.<br>
        <small>📱 to remove warnings on iOS/Android, open <b>/trust</b> first.</small></li>
    <li>Tap <b>Create / Unlock passkey</b> — use Face ID / Touch ID / fingerprint.</li>
    <li>You're in. Drive responsibly. 🛞</li>
  </ol>
  {token_block}
  <div class="foot">passwordless · your key never leaves your device · {host}</div>
</div>
<button class="noprint" onclick="window.print()"
  style="position:fixed;bottom:20px;right:20px;padding:12px 18px;border:none;border-radius:12px;
         background:#76b900;color:#07120a;font-weight:600;cursor:pointer">🖨️ Print</button>
</body></html>"""


def build(url: str, out_dir: Path, bootstrap: str = "", trusted: bool = False) -> dict:
    import base64

    out_dir.mkdir(parents=True, exist_ok=True)
    png_path = out_dir / "field_setup_qr.png"
    html_path = out_dir / "field_setup.html"

    png_qr(url, png_path)
    qr_b64 = base64.b64encode(png_path.read_bytes()).decode()

    ip = _local_ip()
    port = url.rsplit(":", 1)[-1] if ":" in url.split("//", 1)[-1] else "8080"
    scheme = url.split("://", 1)[0]
    ip_url = f"{scheme}://{ip}:{port}"

    token_block = ""
    if bootstrap:
        token_block = f'<div class="tok">First-time setup token:<br><b>{bootstrap}</b></div>'
    trusted_note = " — none if the device trusts the scout CA" if trusted else ""

    html = HTML_CARD.format(
        qr_b64=qr_b64, url=url, ip_url=ip_url, token_block=token_block,
        host=socket.gethostname(), trusted_note=trusted_note,
    )
    html_path.write_text(html)
    return {"png": str(png_path), "html": str(html_path), "url": url, "ip_url": ip_url}


def main():
    ap = argparse.ArgumentParser(description="Generate scout field-setup QR card")
    ap.add_argument("--url", default=None, help="dashboard URL (default https://scout.local:DASH_PORT)")
    ap.add_argument("--out", default=os.getenv("DASH_TLS_DIR", "./.scout_tls"), help="output dir")
    ap.add_argument("--bootstrap", default=os.getenv("SCOUT_AUTH_BOOTSTRAP_TOKEN", ""), help="embed one-time setup token")
    ap.add_argument("--trusted", action="store_true", help="note that devices trusting the CA see no warning (mkcert)")
    args = ap.parse_args()

    url = args.url or default_url()
    res = build(url, Path(args.out).resolve(), bootstrap=args.bootstrap, trusted=args.trusted)

    print("\n" + ascii_qr(url) + "\n")
    print(f"🎫 scout field-setup card ready")
    print(f"   URL:  {res['url']}")
    print(f"   alt:  {res['ip_url']}  (if scout.local doesn't resolve)")
    print(f"   PNG:  {res['png']}")
    print(f"   HTML: {res['html']}   (open in a browser → 🖨️ Print)")
    if args.bootstrap:
        print(f"   setup token embedded on the card")
    print("\n📲 Scan the QR above with a phone on the same Wi-Fi to enroll a passkey.\n")


if __name__ == "__main__":
    main()
