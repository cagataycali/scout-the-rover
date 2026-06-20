#!/usr/bin/env bash
# Scout container entrypoint — dispatch to a service by name.
# Usage (compose passes this as `command:` or via $SCOUT_SERVICE):
#   scout-entrypoint sdk        # earth-rovers-sdk camera/telemetry server (:8002)
#   scout-entrypoint dashboard  # web dashboard (:8080)
#   scout-entrypoint telegram   # telegram listener
#   scout-entrypoint thinker    # slow-thinker background loop
#   scout-entrypoint listener   # voice listener (rover mic → whisper → agent)
#   scout-entrypoint media      # MediaHub fan-out (mic/front/rear/data)
#   scout-entrypoint cosmos_buffer # rolling video clips for Cosmos
#   scout-entrypoint yolo       # YOLO detection → dataset sidecar
#   scout-entrypoint agent      # interactive rover REPL
#   scout-entrypoint all        # sdk + dashboard + telegram + thinker (supervised)
set -euo pipefail

SERVICE="${1:-${SCOUT_SERVICE:-dashboard}}"
cd /app

echo "🛞 scout-entrypoint: starting service '${SERVICE}'"

# Regenerate earth-rovers-sdk/.env from container environment so all config
# flows from compose (.env), not a stale baked-in file.
write_sdk_env() {
  cat > /app/earth-rovers-sdk/.env <<EOF
SDK_API_TOKEN=${SDK_API_TOKEN:-}
BOT_SLUG=${BOT_SLUG:-}
MISSION_SLUG=${MISSION_SLUG:-}
MAP_ZOOM_LEVEL=${MAP_ZOOM_LEVEL:-18}
IMAGE_QUALITY=${IMAGE_QUALITY:-0.8}
IMAGE_FORMAT=${IMAGE_FORMAT:-jpeg}
CHROME_EXECUTABLE_PATH=${CHROME_EXECUTABLE_PATH:-/usr/bin/google-chrome-stable}
TTS_PROVIDER=${TTS_PROVIDER:-edge}
TTS_API_KEY=${TTS_API_KEY:-}
TTS_VOICE=${TTS_VOICE:-en-US-GuyNeural}
SDK_PORT=${SDK_PORT:-8002}
EOF
  echo "📝 wrote /app/earth-rovers-sdk/.env (BOT_SLUG=${BOT_SLUG:-unset}, chrome=${CHROME_EXECUTABLE_PATH:-default})"
}

start_sdk() {
  write_sdk_env
  cd /app/earth-rovers-sdk
  exec python3 -m hypercorn main:app --bind "0.0.0.0:${SDK_PORT:-8002}"
}

start_dashboard() {
  # ensure the persisted auth/TLS dir exists (named volume mount point)
  mkdir -p "${SCOUT_AUTH_STORE%/*}" "${DASH_TLS_DIR:-/app/.scout_tls}" 2>/dev/null || true
  exec python3 /app/dashboard_server.py
}

start_telegram() {
  exec python3 /app/telegram_listener.py
}

start_thinker() {
  exec python3 /app/thinker_loop.py
}

start_listener() {
  exec python3 /app/listener_loop.py
}

start_media() {
  exec python3 /app/media_hub.py
}

start_cosmos_buffer() {
  exec python3 /app/cosmos_buffer.py
}

start_yolo() {
  exec python3 /app/yolo_detector.py
}

start_agent() {
  exec python3 /app/agent.py
}

start_voice() {
  exec python3 /app/voice_agent.py
}

# Cosmos 3 Reasoner — vLLM server so cosmos3_reason/caption/embodied work.
# Mirrors `just c3-serve-reason`. The base image (vllm/vllm-omni:cosmos3) ships vllm.
start_warmup() {
  exec python3 /app/docker/warmup.py
}

start_reasoner() {
  local model="${C3_MODEL:-nvidia/Cosmos3-Nano}"
  local port="${C3_REASON_PORT:-8000}"
  local max_len="${C3_MAX_LEN:-32768}"
  local gpu_mem="${C3_GPU_MEM:-0.92}"
  local eager_flag=""
  [ "${C3_ENFORCE_EAGER:-false}" = "true" ] && eager_flag="--enforce-eager"
  echo "🚀 Cosmos reasoner: vllm serve ${model} on :${port} (max_len=${max_len}, gpu_mem=${gpu_mem})"
  # The model's own config.json declares Cosmos3ForConditionalGeneration (a
  # vLLM-supported arch), so NO --hf-overrides is needed. (Older builds forced
  # Cosmos3ReasonerForConditionalGeneration which this vLLM doesn't register.)
  exec vllm serve "${model}"     --tensor-parallel-size "${C3_TP:-1}"     --mm-encoder-tp-mode data     --async-scheduling     --max-model-len "${max_len}"     --gpu-memory-utilization "${gpu_mem}"     ${eager_flag}     --allowed-local-media-path /     --media-io-kwargs '{"video": {"num_frames": -1}}'     --port "${port}"
}

# 'all' = run SDK + dashboard + telegram + thinker in one container, supervised.
# Each child auto-restarts; container exits only if everything dies.
start_all() {
  write_sdk_env
  declare -A PIDS
  run() { # name cmd...
    local name="$1"; shift
    ( while true; do
        echo "▶️  [$name] starting"; "$@" || true
        echo "⚠️  [$name] exited; restart in 5s"; sleep 5
      done ) &
    PIDS[$name]=$!
  }
  ( cd /app/earth-rovers-sdk && exec python3 -m hypercorn main:app --bind "0.0.0.0:${SDK_PORT:-8002}" ) &
  PIDS[sdk]=$!
  # 🔥 One-shot Cosmos warmup so the first agent call isn't cold (best-effort).
  if [ "${SCOUT_WARMUP:-1}" = "1" ]; then
    echo "🔥 warming up Cosmos assets (set SCOUT_WARMUP=0 to skip)…"
    python3 /app/docker/warmup.py || echo "⚠️  warmup non-fatal failure; continuing"
  fi
  sleep 4   # let SDK bind before dashboard proxies it
  [ "${SCOUT_ENABLE_MEDIA:-1}"     = "1" ] && run media     python3 /app/media_hub.py
  [ "${SCOUT_ENABLE_COSMOS_BUFFER:-0}" = "1" ] && run cosmos_buffer python3 /app/cosmos_buffer.py
  [ "${SCOUT_ENABLE_YOLO:-0}"      = "1" ] && run yolo      python3 /app/yolo_detector.py
  [ "${SCOUT_ENABLE_DASHBOARD:-1}" = "1" ] && run dashboard python3 /app/dashboard_server.py
  [ "${SCOUT_ENABLE_TELEGRAM:-1}"  = "1" ] && run telegram  python3 /app/telegram_listener.py
  [ "${SCOUT_ENABLE_THINKER:-1}"   = "1" ] && run thinker   python3 /app/thinker_loop.py
  [ "${SCOUT_ENABLE_LISTENER:-0}"  = "1" ] && run listener  python3 /app/listener_loop.py
  [ "${SCOUT_ENABLE_VOICE:-0}"     = "1" ] && run voice     python3 /app/voice_agent.py
  # Cosmos reasoner co-located so cosmos3_reason/caption/embodied (which call
  # localhost:8000) resolve in-container. Off by default — it loads a model on GPU.
  if [ "${SCOUT_ENABLE_REASONER:-0}" = "1" ]; then
    run reasoner bash -lc 'vllm serve "${C3_MODEL:-nvidia/Cosmos3-Nano}"       --tensor-parallel-size "${C3_TP:-1}" --mm-encoder-tp-mode data --async-scheduling       --max-model-len "${C3_MAX_LEN:-32768}" --gpu-memory-utilization "${C3_GPU_MEM:-0.92}"       --allowed-local-media-path / --media-io-kwargs "{\"video\": {\"num_frames\": -1}}"       --port "${C3_REASON_PORT:-8000}"'
  fi
  echo "✅ scout 'all' up: sdk + dashboard(${SCOUT_ENABLE_DASHBOARD:-1}) + telegram(${SCOUT_ENABLE_TELEGRAM:-1}) + media(${SCOUT_ENABLE_MEDIA:-1}) + thinker(${SCOUT_ENABLE_THINKER:-1}) + listener(${SCOUT_ENABLE_LISTENER:-0}) + voice(${SCOUT_ENABLE_VOICE:-0}) + reasoner(${SCOUT_ENABLE_REASONER:-0})"
  trap 'kill ${PIDS[*]} 2>/dev/null || true' TERM INT
  wait -n
}

case "$SERVICE" in
  sdk)        start_sdk ;;
  dashboard)  start_dashboard ;;
  telegram)   start_telegram ;;
  thinker)    start_thinker ;;
  listener)   start_listener ;;
  media)      start_media ;;
  cosmos_buffer) start_cosmos_buffer ;;
  yolo)       start_yolo ;;
  agent)      start_agent ;;
  voice)      start_voice ;;
  reasoner)   start_reasoner ;;
  warmup)     start_warmup ;;
  all)        start_all ;;
  bash|sh)    exec /bin/bash ;;
  *) echo "unknown service '$SERVICE'"; echo "valid: sdk|dashboard|telegram|thinker|listener|media|cosmos_buffer|yolo|agent|voice|reasoner|warmup|all|bash"; exit 1 ;;
esac
