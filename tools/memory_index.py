"""🧠🔎 Multimodal time-keyed memory index (LanceDB).

The recall layer that sits ON TOP of the existing dataset spine — it does NOT
duplicate pixels. Every row carries the time bridge
(wall_ts + dataset/episode/frame_index) so a hit points straight back into the
real LeRobot mp4 → the replay scrubber can jump to that frame.

Modalities (all in ONE table, unified on time):
    • image  — CLIP embedding of a keyframe
    • text   — reasoning event / turn text embedding (CLIP text tower → shared space)
    • object — YOLO detection (stored as text label + bbox meta, CLIP-text embedded)
    • audio  — Whisper transcript segment (text-embedded)

Encoders are lazy + shared (transformers CLIP, already installed). Image and
text land in the SAME CLIP space → you can query images BY TEXT and vice-versa.

Public surface:
    idx = MemoryIndex(dataset_dir)          # one table per dataset
    idx.add_image(frame_index, pil_image, episode, wall_ts, meta=...)
    idx.add_text(text, episode, frame_index, wall_ts, source=..., meta=...)
    idx.search(query, k=10, modality=None, episode=None,
               time_range=(lo,hi), objects=[...])   # query = str OR PIL.Image
    idx.stats()

Design:
    * CLIP ViT-B/32 (512-d) shared image+text space — small, fast on MPS/CPU.
    * Cosine metric. On-disk Lance table → offline, portable, memory-mapped.
    * frame_index is the JOIN KEY back into the dataset (replay seek).
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import numpy as np

_CLIP_MODEL_ID = os.getenv("SCOUT_CLIP_MODEL", "openai/clip-vit-base-patch32")
_EMBED_DIM = 512

# ── lazy shared CLIP (image + text in one space) ─────────────────────────────
_clip_lock = threading.Lock()
_clip = {"model": None, "processor": None, "device": None}


def _get_clip():
    if _clip["model"] is not None:
        return _clip
    with _clip_lock:
        if _clip["model"] is not None:
            return _clip
        import torch
        from transformers import CLIPModel, CLIPProcessor
        dev = "mps" if torch.backends.mps.is_available() else (
              "cuda" if torch.cuda.is_available() else "cpu")
        model = CLIPModel.from_pretrained(_CLIP_MODEL_ID).to(dev).eval()
        proc = CLIPProcessor.from_pretrained(_CLIP_MODEL_ID)
        _clip.update(model=model, processor=proc, device=dev)
    return _clip


def _to_vec(feat) -> np.ndarray:
    """Extract a 1-D embedding from CLIP features across transformers versions.

    transformers 4.x: get_*_features returns a Tensor [1, D].
    transformers 5.x: returns a BaseModelOutputWithPooling → use pooler_output.
    """
    import torch
    if hasattr(feat, "pooler_output"):       # 5.x output object
        t = feat.pooler_output
    elif isinstance(feat, torch.Tensor):     # 4.x tensor
        t = feat
    elif hasattr(feat, "last_hidden_state"):
        t = feat.last_hidden_state[:, 0]
    else:
        t = feat
    v = t[0].float().cpu().numpy()
    return (v / (np.linalg.norm(v) + 1e-8)).astype(np.float32)


def embed_image(pil_image) -> np.ndarray:
    import torch
    c = _get_clip()
    inp = c["processor"](images=pil_image, return_tensors="pt").to(c["device"])
    with torch.no_grad():
        f = c["model"].get_image_features(**inp)
    return _to_vec(f)


def embed_text(text: str) -> np.ndarray:
    import torch
    c = _get_clip()
    inp = c["processor"](text=[text[:300]], return_tensors="pt",
                         padding=True, truncation=True).to(c["device"])
    with torch.no_grad():
        f = c["model"].get_text_features(**inp)
    return _to_vec(f)


# ── the index ────────────────────────────────────────────────────────────────
class MemoryIndex:
    """One LanceDB table per dataset, living at <dataset>/memory/lance/."""

    def __init__(self, dataset_dir: Union[str, Path]):
        self.dataset_dir = Path(dataset_dir).resolve()
        self.repo_id = self.dataset_dir.name
        self._db_dir = self.dataset_dir / "memory" / "lance"
        self._db_dir.mkdir(parents=True, exist_ok=True)
        self._db = None
        self._tbl = None
        self._lock = threading.Lock()

    # — lazy connection —
    def _table(self):
        if self._tbl is not None:
            return self._tbl
        import lancedb
        import pyarrow as pa
        self._db = lancedb.connect(str(self._db_dir))
        name = "memory"
        if name in self._db.table_names():
            self._tbl = self._db.open_table(name)
        else:
            schema = pa.schema([
                pa.field("vector", pa.list_(pa.float32(), _EMBED_DIM)),
                pa.field("id", pa.string()),
                pa.field("modality", pa.string()),   # image|text|object|audio
                pa.field("source", pa.string()),     # reasoning|turn|frame|whisper|yolo
                pa.field("dataset", pa.string()),
                pa.field("episode", pa.int64()),
                pa.field("frame_index", pa.int64()),
                pa.field("wall_ts", pa.float64()),
                pa.field("t_in_episode", pa.float64()),
                pa.field("text", pa.string()),
                pa.field("objects", pa.string()),    # comma-joined labels for filtering
                pa.field("meta", pa.string()),       # JSON blob
            ])
            self._tbl = self._db.create_table(name, schema=schema)
        return self._tbl

    def _row(self, vector, *, modality, source, episode, frame_index,
             wall_ts, t_in_episode=None, text="", objects="", meta=None):
        import json
        rid = f"{self.repo_id}:{episode}:{frame_index}:{modality}:{source}:{int((wall_ts or 0)*1000)}"
        return {
            "vector": list(map(float, vector)),
            "id": rid,
            "modality": modality,
            "source": source,
            "dataset": self.repo_id,
            "episode": int(episode) if episode is not None else -1,
            "frame_index": int(frame_index) if frame_index is not None else -1,
            "wall_ts": float(wall_ts or 0.0),
            "t_in_episode": float(t_in_episode) if t_in_episode is not None else -1.0,
            "text": (text or "")[:2000],
            "objects": objects or "",
            "meta": json.dumps(meta or {}, ensure_ascii=False),
        }

    # — writers —
    def add_image(self, frame_index, pil_image, *, episode, wall_ts,
                  t_in_episode=None, objects="", meta=None) -> None:
        v = embed_image(pil_image)
        with self._lock:
            self._table().add([self._row(
                v, modality="image", source="frame", episode=episode,
                frame_index=frame_index, wall_ts=wall_ts,
                t_in_episode=t_in_episode, objects=objects, meta=meta)])

    def add_text(self, text, *, episode, frame_index, wall_ts,
                 source="reasoning", t_in_episode=None, meta=None) -> None:
        if not (text or "").strip():
            return
        v = embed_text(text)
        with self._lock:
            self._table().add([self._row(
                v, modality="text", source=source, episode=episode,
                frame_index=frame_index, wall_ts=wall_ts,
                t_in_episode=t_in_episode, text=text, meta=meta)])

    def add_objects(self, labels: List[str], *, episode, frame_index, wall_ts,
                    t_in_episode=None, meta=None) -> None:
        """Store YOLO detections as a searchable text row (label list)."""
        if not labels:
            return
        phrase = "a photo of " + ", ".join(sorted(set(labels)))
        v = embed_text(phrase)
        with self._lock:
            self._table().add([self._row(
                v, modality="object", source="yolo", episode=episode,
                frame_index=frame_index, wall_ts=wall_ts,
                t_in_episode=t_in_episode, text=phrase,
                objects=",".join(labels), meta=meta)])

    def delete_episode(self, episode_index: int) -> None:
        """Remove all rows for one episode (for idempotent re-enrichment)."""
        with self._lock:
            self._table().delete(f"episode = {int(episode_index)}")

    def add_batch(self, rows: List[Dict[str, Any]]) -> None:
        """rows: list of pre-built dicts from _row (used by enricher for speed)."""
        if not rows:
            return
        with self._lock:
            self._table().add(rows)

    # — search —
    def search(self, query: Union[str, Any], *, k: int = 10,
               modality: Optional[str] = None, episode: Optional[int] = None,
               time_range: Optional[tuple] = None,
               objects: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Semantic search. query = text str OR PIL.Image (both → CLIP space).

        Filters compose with vector search (LanceDB WHERE).
        Returns hits with frame_index → feed to /replay to seek.
        """
        if isinstance(query, str):
            qv = embed_text(query)
        else:
            qv = embed_image(query)
        tbl = self._table()

        def _base_conds(extra_mod=None):
            conds = []
            m = extra_mod or modality
            if m:
                conds.append(f"modality = '{m}'")
            if episode is not None:
                conds.append(f"episode = {int(episode)}")
            if time_range:
                lo, hi = time_range
                conds.append(f"wall_ts >= {float(lo)} AND wall_ts <= {float(hi)}")
            return conds

        def _run(extra_mod, limit):
            q = tbl.search(qv).metric("cosine").limit(limit)
            conds = _base_conds(extra_mod)
            if conds:
                q = q.where(" AND ".join(conds), prefilter=True)
            return q.to_list()

        # ── MODALITY-GAP FIX ──────────────────────────────────────────────
        # CLIP text↔text cosine sits systematically higher than text↔image,
        # so a single pool buries images under text. We instead query EACH
        # modality separately for its own top-k, then merge — guaranteeing
        # visual hits surface alongside textual ones. Scores stay raw (per
        # modality), so we tag rank-within-modality for fair interleaving.
        if modality:
            # explicit single-modality query → straightforward
            rows = _run(None, k * 3)
            pools = {modality: rows}
        else:
            present = self._modalities()
            pools = {m: _run(m, k) for m in present}

        # object post-filter (substring match on comma-joined labels)
        def _obj_ok(r):
            if not objects:
                return True
            want = set(o.lower() for o in objects)
            return bool(want & set((r.get("objects") or "").lower().split(",")))

        merged = []
        for m, rows in pools.items():
            rank = 0
            for r in rows:
                if not _obj_ok(r):
                    continue
                merged.append({
                    "id": r["id"], "modality": r["modality"], "source": r["source"],
                    "dataset": r["dataset"], "episode": r["episode"],
                    "frame_index": r["frame_index"], "wall_ts": r["wall_ts"],
                    "t_in_episode": r.get("t_in_episode"),
                    "text": r.get("text", ""), "objects": r.get("objects", ""),
                    "score": 1.0 - float(r.get("_distance", 0.0)),
                    "_mod_rank": rank,
                })
                rank += 1
        # Interleave by within-modality rank first (round-robin fairness),
        # then by score — so #1-image and #1-text both appear near the top.
        merged.sort(key=lambda x: (x["_mod_rank"], -x["score"]))
        for r in merged:
            r.pop("_mod_rank", None)
        return merged[:k]

    def _modalities(self):
        """Distinct modalities present in the table (cached cheap)."""
        try:
            t = self._table().to_arrow().select(["modality"])
            return sorted(set(t.column("modality").to_pylist()))
        except Exception:
            return ["image", "text", "object", "audio"]

    def stats(self) -> Dict[str, Any]:
        try:
            tbl = self._table()
            n = tbl.count_rows()
            by_mod = {}
            if n:
                # group via pyarrow (no pandas/pylance dependency)
                t = tbl.to_arrow().select(["modality"])
                import pyarrow.compute as pc
                tab = t.group_by("modality").aggregate([("modality", "count")])
                by_mod = dict(zip(tab.column("modality").to_pylist(),
                                  tab.column("modality_count").to_pylist()))
            return {"dataset": self.repo_id, "rows": n, "by_modality": by_mod,
                    "db": str(self._db_dir)}
        except Exception as e:
            return {"dataset": self.repo_id, "error": str(e)}


# ── module-level cache (one index per dataset) ───────────────────────────────
_indexes: Dict[str, MemoryIndex] = {}
_idx_lock = threading.Lock()


def get_index(dataset_dir: Union[str, Path]) -> MemoryIndex:
    key = str(Path(dataset_dir).resolve())
    with _idx_lock:
        if key not in _indexes:
            _indexes[key] = MemoryIndex(dataset_dir)
        return _indexes[key]


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 3:
        print("usage: python -m tools.memory_index <dataset_dir> <query> [k]")
        sys.exit(1)
    idx = get_index(sys.argv[1])
    print("stats:", json.dumps(idx.stats(), indent=2))
    if len(sys.argv) >= 3:
        k = int(sys.argv[3]) if len(sys.argv) > 3 else 8
        for h in idx.search(sys.argv[2], k=k):
            print(f"  [{h['score']:.3f}] ep{h['episode']} f{h['frame_index']} "
                  f"{h['modality']:6} {h['source']:9} {h['text'][:60]!r} obj={h['objects']}")
