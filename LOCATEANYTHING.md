# LocateAnything-3B backend (open-vocab grounding)

`yolo_detector.py` supports two backends via `DET_BACKEND`:
- `yolo`   — Ultralytics YOLOv8 (fast, fixed COCO classes). Default. Runs in the
             main scout image.
- `locate` — NVIDIA **LocateAnything-3B** (open-vocabulary, language-prompted
             grounding via `LOCATE_QUERY`). Output: `<ref>label</ref><box>..</box>`.

## ⚠️ Dependency isolation
LocateAnything's released modeling code requires **`transformers==4.57.1`**
(it calls APIs removed in transformers 5.x). The main scout image ships
transformers 5.x for Cosmos/lerobot/strands. So the `locate` backend runs in an
**isolated venv**:

```bash
python3 -m venv .venv-locate
.venv-locate/bin/pip install torch torchvision transformers==4.57.1 \
    decord==0.6.0 lmdb==1.7.5 pillow numpy accelerate peft safetensors \
    huggingface_hub requests opencv-python-headless

# run the detector with the locate venv:
DET_BACKEND=locate MEDIA_HUB_URL=http://localhost:8090 \
  LOCATE_QUERY="person</c>chair</c>sofa</c>door</c>plant" \
  .venv-locate/bin/python yolo_detector.py
```

Query format: categories separated by `</c>` (e.g. `person</c>door`) — or a
plain `". "`-separated string, which is auto-normalized.

Verified on a live rover frame: sofa, door, box grounded correctly in ~5s/frame
on an L4-class GPU (model load ~6s).
