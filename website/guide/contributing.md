---
title: Contributing
description: "Tests, the docs pipeline, docstring conventions, and what never goes in a commit."
---

# Contributing

## Ground rules

- **No secrets, ever.** `.env`, `.tiny-mcp.env`, `earth-rovers-sdk/`, `*.creds.json`, `.scout_auth.json`, `.supervisor/`, `notes.md`
  are gitignored — keep them that way. Before pushing: `git diff origin/main | grep -nEi 'sk-|token=|Bearer [A-Za-z0-9]|psk|PASSWORD|SECRET|BEGIN .*PRIVATE'`.
- **Safety code stays boring.** Anything that touches `/control` goes through `tools/_rover_common.sdk_post` (clamp, auto-stop,
  thinker gate). Don't add a second path to the wheels.
- **Docstrings are documentation.** The [tools reference](../reference/tools/index.md) is generated from the `@tool` docstrings the
  model itself reads — Google style (`Args:`, `Returns:`, `Examples:`). Fix the docstring, not the page.

## Tests

```bash
make test                                   # pytest tests/
pytest tests/test_personas.py -q            # supervisor client + flags
pytest tests/test_episode_index.py -q       # recorder sealing + repair
pytest tests/test_tiny_mcp.py -q            # fleet bridge guards
pytest tests/test_docs.py -q                # docs drift: every env var / tool / route documented, no secrets in website/
```

In the image (pytest is not installed there): `docker run --rm -v $PWD:/src -w /src --entrypoint sh scout:slim -c 'pip install -q pytest && pytest -q'`.

## The docs site

```bash
pip install -r requirements-docs.txt
mkdocs serve                                # http://127.0.0.1:8000 — regenerates reference pages on every build
mkdocs build --strict                       # what CI runs; broken links fail the build
```

- Source lives in **`website/`** (`docs/` is the cockpit's frontend). `mkdocs.yml` sets `docs_dir: website`.
- `website/hooks/gen.py` runs `scripts/tooldoc.py`, `scripts/envdoc.py`, `scripts/routedoc.py` before each build and splices
  their output between `<!-- gen:… -->` markers, so `reference/tools/*`, `reference/env.md` and `reference/api.md` can't rot.
- Give a new env var a one-line purpose in `scripts/envdoc_notes.py`; a new route a note in `scripts/routedoc_notes.py`.
- `.github/workflows/docs.yml` builds strictly and deploys to GitHub Pages on every push to `main` that touches the site or the
  code it is generated from.

## Style

Python 3.12, type hints where they help, emoji log prefixes are a house style (`🛞 🎬 🧠 🌉 📷`). Keep tools small: one HTTP hop
to the SDK, clear failure text — the model reads your error messages.
