"""The docs site is generated from the code — these tests keep the two from drifting.

Run: pytest tests/test_docs.py   (stdlib generators; no rover, no strands needed)
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import envdoc, routedoc, tooldoc  # noqa: E402

SITE = ROOT / "website"


def _all_docs_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in SITE.rglob("*.md"))


def test_every_env_var_has_a_purpose_note():
    from envdoc_notes import NOTES
    names = set(envdoc.collect())
    assert names, "envdoc found nothing — scanner broken?"
    missing = sorted(n for n in names if n not in NOTES)
    assert not missing, f"add a one-line purpose to scripts/envdoc_notes.py for: {missing}"


def test_tools_reference_is_fresh():
    pages = tooldoc.build()
    out = SITE / "reference" / "tools"
    stale = [n for n, t in pages.items() if not (out / n).exists() or (out / n).read_text(encoding="utf-8") != t]
    assert not stale, f"run `python scripts/tooldoc.py` — stale: {stale}"


def test_every_tool_module_has_a_page():
    inv = tooldoc.inventory()
    undescribed = [m for m, v in inv.items() if "not yet described" in v["blurb"]]
    assert not undescribed, f"add these modules to scripts/tooldoc.MODULES: {undescribed}"
    total = sum(len(v["tools"]) for v in inv.values())
    assert total >= 21, total  # 20 rover tools + use_device — never fewer


def test_every_dashboard_route_has_a_note():
    from routedoc_notes import NOTES  # noqa: F401
    rs = routedoc.routes()
    assert len(rs) >= 35, len(rs)
    missing = sorted(f"{r['method']} {r['path']}" for r in rs if not r["doc"])
    assert not missing, f"add to scripts/routedoc_notes.py: {missing}"


def test_generated_blocks_present():
    for page, name in (("reference/env.md", "env"), ("reference/api.md", "api")):
        text = (SITE / page).read_text(encoding="utf-8")
        assert f"<!-- gen:{name} -->" in text and f"<!-- /gen:{name} -->" in text, page


def test_no_secrets_in_docs():
    text = _all_docs_text() + "\n" + (ROOT / "README.md").read_text(encoding="utf-8")
    for pat in (r"sk-[A-Za-z0-9_-]{20,}", r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}", r"AKIA[0-9A-Z]{16}",
                r"wifi-sec\.psk\s+\S", r"\b\d{9,10}:[A-Za-z0-9_-]{35}\b"):
        assert not re.search(pat, text), f"secret-looking string in docs: {pat}"


def test_dashboard_frontend_untouched_by_site():
    """docs/ is the cockpit's static frontend served by dashboard_server.py — the site must live in website/."""
    import yaml  # mkdocs dependency
    cfg = yaml.load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert cfg.get("docs_dir") == "website"
    for f in ("docs/index.html", "docs/replay.html", "docs/js/app.js", "docs/css/style.css"):
        assert (ROOT / f).exists(), f
