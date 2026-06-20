"""📂 Dataset index → system-prompt block.

Scans the local LeRobot-style `datasets/` tree and surfaces ABSOLUTE paths to
the recorded media (mp4 videos per camera, images) and reasoning traces
(.jsonl / .ecot.json) so the agent can feed them straight into Cosmos tools:

    cosmos3_caption(video="/abs/path/observation.images.front/.../file-000.mp4")
    cosmos3_reason(prompt="What happens here? <video>/abs/.../file-000.mp4</video>")
    cosmos3_inverse_dynamics(input_jsonl="/abs/.../episode_000000.jsonl")

The block is intentionally compact (paths + counts, capped) so it doesn't blow
up the prompt. Tune with env:
    SCOUT_DATASETS_DIR        (default: ./datasets)
    SCOUT_DATASET_MAX_VIDEOS  (default: 12)
    SCOUT_DATASET_MAX_TRACES  (default: 12)
    SCOUT_INJECT_DATASETS     (1|0, default 1)
"""
from __future__ import annotations

import os
from pathlib import Path

_VIDEO_EXT = {".mp4", ".mov", ".webm"}
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
_TRACE_EXT = {".jsonl", ".json"}


def _root() -> Path:
    return Path(os.getenv("SCOUT_DATASETS_DIR", "datasets")).resolve()


def dataset_index_block() -> str:
    """Build a compact prompt block listing dataset media + trace paths."""
    if os.getenv("SCOUT_INJECT_DATASETS", "1").lower() in ("0", "false", "no"):
        return ""

    root = _root()
    if not root.is_dir():
        return ""

    max_v = int(os.getenv("SCOUT_DATASET_MAX_VIDEOS", "12"))
    max_t = int(os.getenv("SCOUT_DATASET_MAX_TRACES", "12"))

    videos, images, traces = [], [], []
    try:
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            if ext in _VIDEO_EXT:
                videos.append(p)
            elif ext in _IMAGE_EXT:
                images.append(p)
            elif ext in _TRACE_EXT and ("reasoning" in p.parts or p.name.endswith(".jsonl")
                                        or p.name.endswith(".ecot.json")):
                traces.append(p)
    except Exception:
        return ""

    if not (videos or images or traces):
        return ""

    videos.sort(); images.sort(); traces.sort()

    def _group_by_camera(paths):
        """Group video paths by their camera key (…/<video_key>/chunk-…)."""
        groups: dict[str, list[Path]] = {}
        for p in paths:
            key = "video"
            parts = p.parts
            for i, seg in enumerate(parts):
                if seg == "videos" and i + 1 < len(parts):
                    key = parts[i + 1]
                    break
            groups.setdefault(key, []).append(p)
        return groups

    lines = ["## 📂 LOCAL DATASETS (feed these ABSOLUTE paths into Cosmos tools):"]
    lines.append(f"Root: {root}")

    if videos:
        groups = _group_by_camera(videos)
        lines.append(f"\n🎞️  Videos ({len(videos)} total, by camera key):")
        shown = 0
        for key, paths in groups.items():
            lines.append(f"  • {key}:")
            for p in paths:
                if shown >= max_v:
                    break
                lines.append(f"      {p}")
                shown += 1
            if shown >= max_v:
                lines.append(f"      … (+{len(videos) - shown} more)")
                break

    if images:
        lines.append(f"\n🖼️  Images ({len(images)}): {images[0].parent}/  (e.g. {images[0].name})")

    if traces:
        lines.append(f"\n🧠 Reasoning traces ({len(traces)} — jsonl/ecot.json):")
        for p in traces[:max_t]:
            lines.append(f"      {p}")
        if len(traces) > max_t:
            lines.append(f"      … (+{len(traces) - max_t} more)")

    lines.append(
        "\nHOW TO USE: pass a video path to cosmos3_caption(video=…) / "
        "cosmos3_reason(prompt='… <video>PATH</video>') / cosmos3_temporal(video=…). "
        "Pass a .jsonl trace to cosmos3_inverse_dynamics(input_jsonl=…). These are "
        "REAL recordings of your own runs — use them to reflect, caption past "
        "episodes, or learn dynamics."
    )
    return "\n".join(lines) + "\n"


__all__ = ["dataset_index_block"]
