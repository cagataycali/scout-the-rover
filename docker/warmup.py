#!/usr/bin/env python3
"""🔥 Cosmos warmup — make the container 'ready with all the goodies'.

Pre-downloads + pre-loads heavy Cosmos assets so the FIRST real agent call is
fast instead of paying a multi-minute model download/load penalty:

  • Generator (Diffusers, in-process): instantiate Cosmos3GeneratorModel and
    trigger the pipeline load (downloads weights, builds the pipe on GPU).
  • Optionally a tiny smoke generation to JIT/compile kernels (WARMUP_GENERATE=1).
  • Optionally pre-fetch the reasoner model snapshot (WARMUP_REASONER_DOWNLOAD=1)
    so a later `reasoner` server boots without a cold HF pull.

All steps are best-effort: a failure logs a warning and exits 0 so it never
blocks container startup. Controlled by env:

  SCOUT_WARMUP                = 1|0   (master switch, default 1)
  WARMUP_GENERATOR            = 1|0   (load generator pipeline, default 1)
  WARMUP_GENERATE             = 1|0   (run a tiny throwaway gen, default 0)
  WARMUP_REASONER_DOWNLOAD    = 1|0   (snapshot_download reasoner, default 0)
  C3_GEN_MODEL                = HF id (default nvidia/Cosmos3-Nano)
  C3_MODEL                    = HF id reasoner (default nvidia/Cosmos3-Nano)
"""
from __future__ import annotations

import logging
import os
import sys
import time

logging.basicConfig(level=logging.INFO, format="🔥 warmup %(levelname)s: %(message)s")
log = logging.getLogger("warmup")


def _on(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).lower() not in ("0", "false", "no")


def warmup_generator() -> None:
    if not _on("WARMUP_GENERATOR", "1"):
        log.info("generator warmup disabled"); return
    model_id = os.getenv("C3_GEN_MODEL", "nvidia/Cosmos3-Nano")
    t0 = time.time()
    log.info("loading Cosmos3 generator pipeline (model_id=%s) …", model_id)
    from strands_cosmos.cosmos3_generator_model import Cosmos3GeneratorModel
    gen = Cosmos3GeneratorModel(model_id=model_id)
    # Trigger the lazy pipeline load (downloads + builds on GPU). The package's
    # generate() calls _load_pipeline(); use it, with fallbacks for API drift.
    loader = None
    for name in ("_load_pipeline", "_ensure_pipe", "_load_pipe", "_get_pipe"):
        fn = getattr(gen, name, None)
        if callable(fn):
            loader = fn
            break
    if loader is not None:
        loader()
    elif hasattr(gen, "pipe"):
        _ = gen.pipe  # property access loads it
    log.info("✅ generator pipeline ready in %.1fs", time.time() - t0)

    if _on("WARMUP_GENERATE", "0"):
        t1 = time.time()
        log.info("running tiny smoke generation to compile kernels …")
        try:
            gen.generate(mode="text2video", prompt="warmup",
                         out_path="/tmp/_warmup.mp4", num_frames=9,
                         num_inference_steps=2, resolution="480")
            log.info("✅ smoke gen done in %.1fs", time.time() - t1)
        except Exception as e:
            log.warning("smoke gen skipped: %s", e)


def warmup_reasoner_download() -> None:
    if not _on("WARMUP_REASONER_DOWNLOAD", "0"):
        return
    model_id = os.getenv("C3_MODEL", "nvidia/Cosmos3-Nano")
    log.info("pre-downloading reasoner snapshot (%s) …", model_id)
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(model_id, token=os.getenv("HF_TOKEN") or None)
        log.info("✅ reasoner snapshot cached")
    except Exception as e:
        log.warning("reasoner snapshot download skipped: %s", e)


def main() -> int:
    if not _on("SCOUT_WARMUP", "1"):
        log.info("SCOUT_WARMUP disabled — skipping"); return 0
    try:
        import strands_cosmos  # noqa
    except ImportError:
        log.info("strands_cosmos not installed — nothing to warm up"); return 0

    try:
        warmup_generator()
    except Exception as e:
        log.warning("generator warmup failed (non-fatal): %s", e)
    try:
        warmup_reasoner_download()
    except Exception as e:
        log.warning("reasoner download warmup failed (non-fatal): %s", e)

    log.info("🔥 warmup complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
