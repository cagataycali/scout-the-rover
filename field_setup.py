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
import re
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


def wordmark_svg() -> str:
    """The STRANDS wordmark (docs/media/branding/strands-wordmark.svg) for server-rendered pages;
    empty string when the asset is missing so a page never fails on branding."""
    try:
        svg = (Path(__file__).resolve().parent / "docs" / "media" / "branding" / "strands-wordmark.svg").read_text()
        svg = re.sub(r"<!--.*?-->", "", svg, flags=re.S)
        return re.sub(r">\s+<", "><", svg).strip()
    except Exception:
        return ""


HTML_CARD = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>scout · field setup</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600;700&display=swap">
<style>
  @media print {{ @page {{ margin: 12mm; }} .noprint {{ display:none; }} }}
  :root {{ --bg:#000; --fg:#fff; --fg-light:#b6b6b6; --muted:#999696; --line:#28292a; --line-strong:#fff; --accent:#00cc60; --on-accent:#000; }}
  @media (prefers-color-scheme: light) {{ :root {{ --bg:#fff; --fg:#000; --fg-light:#3d3c3c; --muted:#767373; --line:rgba(0,0,0,.15); --line-strong:#000; --accent:#007a3d; --on-accent:#fff; }} }}
  body {{ font-family:"Space Grotesk",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:var(--bg); color:var(--fg); margin:0;
         display:flex; justify-content:center; padding:24px; -webkit-font-smoothing:antialiased; }}
  .card {{ width:440px; max-width:100%; background:var(--bg); border:1px solid var(--line-strong); border-radius:12px; padding:30px; text-align:center; }}
  .brand {{ display:flex; justify-content:center; align-items:baseline; gap:10px; margin-bottom:4px; }}
  .brand svg {{ height:16px; width:auto; fill:var(--accent); }}
  .brand b {{ font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace; font-size:15px; font-weight:600; }}
  .sub {{ font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--muted); font-size:11px; letter-spacing:.06em; text-transform:uppercase; margin:0 0 20px; }}
  .qr {{ background:#fff; border:1px solid var(--line); border-radius:8px; padding:14px; display:inline-block; }}
  .qr img {{ display:block; width:260px; height:260px; image-rendering:pixelated; }}
  .url {{ font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace; font-size:16px; font-weight:600; margin:18px 0 4px; color:var(--accent); word-break:break-all; }}
  .alt {{ font-size:12px; color:var(--muted); margin:0 0 16px; }} .alt b {{ font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--fg-light); font-weight:500; }}
  ol {{ text-align:left; font-size:13px; line-height:1.7; color:var(--fg-light); margin:14px 4px 0; padding-left:20px; }}
  ol b {{ color:var(--fg); }} small {{ color:var(--muted); }}
  .tok {{ margin-top:16px; padding:10px 12px; border:1px dashed var(--accent); border-radius:8px; font-size:13px; color:var(--fg-light); }}
  .tok b {{ font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--accent); }}
  .foot {{ margin-top:18px; font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace; font-size:10.5px; color:var(--muted); letter-spacing:.02em; }}
  .print {{ position:fixed; bottom:20px; right:20px; padding:10px 18px; border:1px solid var(--accent); border-radius:999px;
            background:var(--accent); color:var(--on-accent); font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace; font-weight:600; font-size:13px; cursor:pointer; }}
  @media print {{ :root {{ --bg:#fff; --fg:#000; --fg-light:#333; --muted:#777; --line:#ccc; --line-strong:#000; --accent:#007a3d; }} .card {{ border-color:#000; }} }}
</style></head><body>
<div class="card">
  <div class="brand">{wordmark}<b>/scout</b></div>
  <p class="sub">field setup · scan to drive</p>
  <div class="qr"><img src="data:image/png;base64,{qr_b64}" alt="QR"></div>
  <div class="url">{url}</div>
  <p class="alt">if scout.local doesn't resolve: <b>{ip_url}</b></p>
  <ol>
    <li>Connect your phone/laptop to the <b>same Wi-Fi</b> as scout.</li>
    <li>Scan the QR (or type the URL).</li>
    <li>Accept the security warning once (Advanced → Proceed){trusted_note}.<br>
        <small>to remove warnings on iOS/Android, open <b>/trust</b> first.</small></li>
    <li>Tap <b>Create / Unlock passkey</b> — use Face ID / Touch ID / fingerprint.</li>
    <li>You're in. Drive responsibly.</li>
  </ol>
  {token_block}
  <div class="foot">passwordless · your key never leaves your device · {host}</div>
</div>
<button class="noprint print" onclick="window.print()">Print</button>
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
        host=socket.gethostname(), trusted_note=trusted_note, wordmark=wordmark_svg(),
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
