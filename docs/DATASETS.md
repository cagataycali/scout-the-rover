# Datasets — how Scout records, why episodes are sealed, how to repair

Scout records every agent turn that moved the rover into a **LeRobot v3** dataset,
one per day per persona:

```
datasets/scout__earth-rover-mini-YYYYMMDD/<persona>/     # thinker | dashboard_server | telegram | voice
├── meta/info.json                 fps, total_episodes, total_frames, features   (authoritative counters)
├── meta/episodes/chunk-000/file-NNN.parquet   one row per episode: length, dataset_from/to_index,
│                                              videos/<cam>/{file_index,from_timestamp,to_timestamp}, stats/*
├── meta/stats.json, meta/tasks.parquet
├── data/chunk-000/file-NNN.parquet            per-frame observation.state / action / action_age / timestamp
├── videos/observation.images.{front,rear}/chunk-000/file-NNN.mp4   h264 640×480 (SCOUT_VCODEC)
├── audio/episode_NNNNNN.wav                   rover mic, one wav per episode
├── reasoning/events.sqlite + episode_NNNNNN.{jsonl,ecot.json}      ECoT: agent reasoning aligned to frames
├── detections/episode_NNNNNN.jsonl            YOLO sidecar (yolo_detector.py), aligned by recorder status
└── images/<cam>/episode-NNNNNN/frame-*.png    TEMP staging while an episode is open (encoded → deleted)
```

The recorder is `tools/_recorder_engine.py` (`RecorderEngine`); the replay UI is
`/replay` (`dashboard_replay.py` + `docs/js/replay.js`).

## Episode sealing (why there is one parquet/mp4 per episode)

lerobot ≥ 0.6 writes `meta/episodes/*.parquet` **and** `data/*.parquet` through
streaming `pyarrow.parquet.ParquetWriter`s. Rows are flushed after every
`save_episode()`, but the parquet **footer** (schema + row-group index) is only
written by `LeRobotDataset.finalize()`. Until then pyarrow refuses the file:

```
ArrowInvalid: Parquet magic bytes not found in footer
```

Consequences we hit on 2026-09-17 (owner report: *"every episode stored in the
scout is the same video"*):

* during a session — and forever after a `SIGKILL`/`docker rm` — the episode
  index was unreadable, so the replay had no per-episode
  `[from_timestamp, to_timestamp]` window and fell back to `video_from=0` for
  every episode → the same clip everywhere;
* the recorder could not `resume()` the dataset (`load_episodes` dies), so
  **every** later `start_episode` of that persona failed silently.

Fix: the recorder now **seals** after every successful `save_episode()` —
`finalize()` the dataset (footers land, handle dropped) and lazily `resume()` on
the next `start_episode()`. Between episodes the dataset on disk is always a
valid, loadable LeRobot dataset. lerobot's `resume()` opens a fresh
`file-NNN` for data, episodes **and** video, so a sealed dataset has one
parquet + one mp4 per episode instead of packed files. v3 readers glob
`chunk-*/file-*` so nothing else changes; `/api/replay/<ds>/video/<view>?file=chunk-000/file-NNN`
serves the right mp4 and the episode rows carry `video_files`.

Knob: `SCOUT_SEAL_EPISODES=0` restores the packed single-writer layout (footer
only at process exit — do not use with docker `stop` timeouts).

Also on `start_episode`: orphan `images/<cam>/episode-N/` staging dirs with
`N ≥ total_episodes` (left by a kill mid-episode) are removed, otherwise lerobot
would sweep the stale PNGs into the next episode's mp4.

## Index health in the replay API

`GET /api/replay/<ds>/episodes` → `index_state`:

| state | meaning | windows |
|---|---|---|
| `finalized` | every episodes parquet readable | exact |
| `partial` | some readable; the rest reconstructed | mixed, rows tagged `index:"unfinalized"`, `approx:true` |
| `unfinalized` | no readable index | from `data/*.parquet` frame rows grouped by episode (distinct, approx) or, if those are footerless too, from reasoning anchors (`window_known:false`) |
| `none` | no index files (live image-sequence dataset) | — |

`unreadable_files`, `fallback_source` and a `warning` (with the repair command)
accompany it; `GET /api/replay/datasets` carries a cheap footer-only
`index_state` per dataset. The UI shows **⚠︎ index unfinalized** badges on the
dataset picker, the episode list, the title and the dataset info line.

## Repairing a footerless dataset

```
# inside the image (needs pyarrow + ffprobe); the repo is bind-mounted at /src
docker run --rm --entrypoint python -v $HOME/scout-the-rover:/src -w /src scout:slim \
    -m tools.repair_episode_index --scan datasets            # list broken datasets
docker run --rm --entrypoint python -v $HOME/scout-the-rover:/src -w /src scout:slim \
    -m tools.repair_episode_index datasets/<ds>/<persona> --dry-run
docker run --rm --entrypoint python -v $HOME/scout-the-rover:/src -w /src scout:slim \
    -m tools.repair_episode_index datasets/<ds>/<persona> --yes \
    [--lengths 82,149,113,131] [--schema-from <readable sibling episodes parquet>]
```

What it does (idempotent, never deletes):

1. moves unreadable `meta/episodes` + `data` parquet and orphan staging dirs to
   `<root>/_repair_backup/<UTC ts>/`;
2. rebuilds the lost episode rows from `meta/info.json` counters + the mp4s:
   * one-mp4-per-episode (sealed layout) → frame counts via ffprobe, **exact**;
   * packed mp4 → keyframe-parity boundaries (encoder GOP `g=2`: an odd-length
     episode flips the I-frame phase — exact for those) and wall-clock
     durations from `reasoning/events.sqlite` anchors for the rest
     (**approximate**, `method` says so). Pass `--lengths` when you know the
     counts (thinker logs print `auto-record: ep N saved (X frames)`);
3. copies the column schema from a readable sibling so the nested dataset stays
   uniform; `stats/*` come from `meta/stats.json` with `count = length`;
4. verifies (`pq.read_table`, indices `0..N-1`, frames sum to `total_frames`)
   and appends to `meta/repair_log.json`.

A footerless **data** parquet cannot be recovered (its schema lives in the
footer): those episodes keep video, audio, reasoning and detections but lose
per-frame action/state (`data_lost_episodes` in the report; the replay shows an
empty telemetry strip for them).

Before repairing, make sure no running persona still holds the writer: the
recorder `status()` reports `dataset_open`; if it does and no episode is open,
stop that persona gracefully (SIGTERM → finalize) instead of repairing.

Tests: `tests/test_episode_index.py` (host: `python3 -m pytest tests/test_episode_index.py`;
image: `docker run --rm --entrypoint sh -v $HOME/scout-the-rover:/src -w /src scout:slim -c "pip install -q pytest; python -m pytest tests/test_episode_index.py -q -o addopts="`).

## Knobs

- `SCOUT_VCODEC=h264|hevc|libsvtav1|auto` — default h264 so `<video>` plays everywhere.
- `SCOUT_SEAL_EPISODES=1|0` — seal after every episode (default 1).
- `ROVER_DATASET_ROOT`, `ROVER_REPO_ID`, `ROVER_AGENT_ID` — where/which dataset.
- Recording only saves an episode when the turn had ≥ 1 action
  (`auto-record: skipped (0 actions in turn)` is normal); the ⏺ rec persona pill
  / `SCOUT_AUTO_RECORD` gate it.

## History

- 2026-09-17 00:34Z — `lerobot[dataset]` extra + `rgb_encoder` (lerobot 0.6 dropped `vcodec=`): recording works again.
- 2026-09-17 01:40Z — episode index fix: sealing, repair tool, index health in the API/UI.
  `20260917/thinker` repaired with exact lengths `82,149,113,131` (backup `_repair_backup/20260917T014016Z`).
