"""🌌 NVIDIA Cosmos tools for scout — understand & generate video/image.

Wraps the `strands-for-cosmos` package (https://github.com/strands-labs/strands-for-cosmos)
so the rover agent can:
  • UNDERSTAND what its cameras see — caption / reason over video & images
    (Cosmos 3 Reasoner via a local vLLM server, default :8000)
  • GENERATE video — text→video, image→video (Cosmos 3 Generator, in-process Diffusers)

These run on-device (GPU) inside the scout Docker image, whose base is
`vllm/vllm-omni:cosmos3`, so the heavy deps are already present.

OPTIONAL: if `strands_cosmos` isn't installed (e.g. on a laptop without GPU),
this module degrades gracefully — COSMOS_TOOLS is empty and nothing breaks.
Enable/disable explicitly with SCOUT_ENABLE_COSMOS=1|0.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Curated subset most useful to a mobile robot. The full package has 45 tools
# (training/quantization/edge/eval) which aren't needed for the live agent.
_WANTED = [
    # understand (Reasoner via vLLM server)
    "cosmos3_reason",       # free-form reasoning over <video>/<image>
    "cosmos3_caption",      # detailed scene caption
    "cosmos3_temporal",     # notable events with timestamps
    "cosmos3_ground",       # spatial grounding / localization
    "cosmos_vision_invoke", # Reason2 lightweight VLM (edge)
    # embodied / robot reasoning + kinematics (Reasoner server + Cosmos Framework)
    "cosmos3_embodied",          # scene -> next immediate action (CoT)
    "cosmos3_action_cot",        # task -> 2D end-effector trajectory (CoT JSON)
    "cosmos3_policy",            # image + instruction -> action chunk + rollout video
    "cosmos3_forward_dynamics",  # start image + actions -> predicted future video
    "cosmos3_inverse_dynamics",  # video + instruction -> predicted action chunk
    # generate (Generator, in-process Diffusers — no server)
    "cosmos3_text2video",
    "cosmos3_image2video",
    "cosmos3_text2image",
    # I/O helpers
    "video_extract_frames",
    "video_probe",
]

COSMOS_TOOLS: list = []
COSMOS_AVAILABLE = False

# Allow hard-disable without uninstalling (default: auto-detect).
_enabled = os.getenv("SCOUT_ENABLE_COSMOS", "auto").lower()

if _enabled not in ("0", "false", "no"):
    try:
        import strands_cosmos as _sc  # noqa
        for _name in _WANTED:
            _tool = getattr(_sc, _name, None)
            if _tool is not None:
                COSMOS_TOOLS.append(_tool)
            else:
                logger.debug("cosmos tool not found in package: %s", _name)
        COSMOS_AVAILABLE = bool(COSMOS_TOOLS)
        if COSMOS_AVAILABLE:
            logger.info("🌌 Cosmos tools loaded: %s",
                        ", ".join(getattr(t, "__name__", str(t)) for t in COSMOS_TOOLS))
    except ImportError as e:
        logger.info("strands_cosmos not installed — Cosmos tools disabled (%s)", e)
    except Exception as e:
        logger.warning("Failed loading Cosmos tools: %s", e)
else:
    logger.info("SCOUT_ENABLE_COSMOS disabled — Cosmos tools off")

__all__ = ["COSMOS_TOOLS", "COSMOS_AVAILABLE"]
