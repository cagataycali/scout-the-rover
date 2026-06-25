// 🎞️ scout replay — dual-mode timeline (video OR live image-sequence).
//
// FINALIZED datasets pack all episodes into ONE LeRobot mp4; each episode is a
// [from,to] window we clamp the <video> to. LIVE datasets (agent still
// recording, or stopped without finalizing) have NO mp4 yet — only per-frame
// PNGs + a reasoning DB. For those we drive an <img> scrubber on a synthetic
// time axis (eff_fps = nframes / wall_duration) so reasoning, telemetry, and
// frames all line up on episode-relative SECONDS.
const $ = (id) => document.getElementById(id);
const _tok = () => (window.SCOUT_TOKEN || localStorage.getItem("scout_token") || "");
const _withTok = (u) => _tok() ? (u + (u.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(_tok())) : u;
const _authHdr = () => _tok() ? { Authorization: `Bearer ${_tok()}` } : {};
const api = (p) => fetch(p, { headers: _authHdr() }).then(r => r.json());

let DS = null, FPS = 4, EPISODES = [], EP = null, EPDATA = null;
let VIEW = "front", PLAYRATE = 1, playing = false;

// image-sequence playback state
let MODE = "video";          // "video" | "images"
let IMG_FRAMES = [];         // sorted PNG frame numbers for current ep/view
let IMG_EFF_FPS = null;      // frames per second (synthetic)
let IMG_DUR = 0;             // episode duration (s)
let IMG_T = 0;               // current episode-relative time (s)
let _imgTimer = null;        // play loop handle
let _imgUrlTpl = null;       // frame URL template
let _imgCache = new Map();   // frame# → preloaded Image

const vid = $("vid");
const vimg = $("vimg");

async function init() {
  const datasets = await api("/api/replay/datasets");
  const sel = $("dsSel");
  sel.innerHTML = "";
  (datasets || []).forEach(d => {
    const o = document.createElement("option");
    o.value = d.id;
    const eps = d.episode_count != null ? d.episode_count : d.total_episodes;
    const tags = [];
    if (d.has_reasoning) tags.push("🧠");
    if (d.live) tags.push("🔴live");
    else if (d.has_video) tags.push("🎬");
    o.textContent = `${d.id}  (${eps} ep${tags.length ? " · " + tags.join(" ") : ""})`;
    sel.appendChild(o);
  });
  sel.onchange = () => loadDataset(sel.value);
  if (datasets && datasets.length) loadDataset(datasets[0].id);
}

async function loadDataset(id) {
  DS = id;
  const r = await api(`/api/replay/${id}/episodes`);
  FPS = r.fps || 4; EPISODES = r.episodes || [];
  $("dsInfo").textContent = `${FPS} fps · ${EPISODES.length} episode${EPISODES.length === 1 ? "" : "s"}`;
  renderEpList();
  if (EPISODES.length) selectEpisode(EPISODES[0].episode_index);
}

// ── mode helpers ─────────────────────────────────────────────────────────
function isImageMode() { return MODE === "images"; }

function setModeUI() {
  const m = $("vmode");
  if (isImageMode()) {
    vid.style.display = "none";
    vimg.style.display = "block";
    if (m) { m.textContent = `🔴 live · image sequence · ${IMG_EFF_FPS ? IMG_EFF_FPS.toFixed(2) + " fps" : ""}`; m.className = "vmode live"; }
  } else {
    vid.style.display = "block";
    vimg.style.display = "none";
    if (m) { m.textContent = ""; m.className = "vmode"; }
  }
}

function setVideoSrc() {
  vid.src = _withTok(`/api/replay/${DS}/video/${VIEW}`);
  vid.load();
}

function renderEpList() {
  const el = $("eplist"); el.innerHTML = "";
  EPISODES.forEach(e => {
    const div = document.createElement("div");
    div.className = "epitem" + (EP && e.episode_index === EP.episode_index ? " active" : "");
    const dur = (e.video_to - e.video_from).toFixed(1);
    const live = e.live ? ` <span class="eplive">🔴</span>` : "";
    const nf = e.length != null ? `${e.length}f` : "–";
    div.innerHTML = `<div><b>ep ${e.episode_index}</b> · ${nf} · ${dur}s${live}</div>
                     <div class="et">${(e.tasks || []).join(" / ") || "<i>untasked</i>"}</div>`;
    div.onclick = () => selectEpisode(e.episode_index);
    el.appendChild(div);
  });
}

async function selectEpisode(idx) {
  // stop any running image loop before switching
  stopImagePlay();
  EP = EPISODES.find(e => e.episode_index === idx);
  renderEpList();
  EPDATA = await api(`/api/replay/${DS}/episode/${idx}`);

  // decide mode: live image episodes have mode==="images"
  MODE = (EP && EP.mode === "images") ? "images" : "video";

  if (isImageMode()) {
    await loadImageSequence(idx);
  } else {
    setModeUI();
    setVideoSrc();
    $("tDur").textContent = (EP.video_to - EP.video_from).toFixed(2) + "s";
    seekToEpisodeTime(0);
  }
  renderEvents();
  drawGraph();
}

// ── image-sequence loading + playback ──────────────────────────────────────
async function loadImageSequence(idx) {
  // pick a view that actually has frames
  const views = (EP.image_views && EP.image_views.length) ? EP.image_views : ["front"];
  if (!views.includes(VIEW)) VIEW = views[0];
  let meta;
  try {
    meta = await api(`/api/replay/${DS}/frames/${VIEW}/${idx}`);
  } catch (e) { meta = null; }
  if (!meta || !meta.frames || !meta.frames.length) {
    IMG_FRAMES = []; IMG_EFF_FPS = null; IMG_DUR = 0;
    setModeUI();
    return;
  }
  IMG_FRAMES = meta.frames;
  IMG_DUR = meta.duration || EP.video_to || (IMG_FRAMES.length / (meta.eff_fps || 1));
  IMG_EFF_FPS = meta.eff_fps || (IMG_FRAMES.length / Math.max(IMG_DUR, 0.001));
  _imgUrlTpl = meta.url_template;
  _imgCache.clear();
  $("vlabel").textContent = VIEW;
  $("tDur").textContent = IMG_DUR.toFixed(2) + "s";
  setModeUI();
  seekToEpisodeTime(0);
  preloadAround(0);
}

function imgFrameUrl(frameNum) {
  return _withTok(_imgUrlTpl.replace("{frame}", frameNum));
}

// map episode-time (s) → nearest available PNG frame number
function timeToImageFrame(t) {
  if (!IMG_FRAMES.length) return null;
  // ideal index along the sequence by fraction of duration
  const frac = IMG_DUR > 0 ? Math.max(0, Math.min(1, t / IMG_DUR)) : 0;
  const i = Math.round(frac * (IMG_FRAMES.length - 1));
  return IMG_FRAMES[i];
}
function imageFrameToTime(frameNum) {
  const i = IMG_FRAMES.indexOf(frameNum);
  if (i < 0 || IMG_FRAMES.length < 2) return 0;
  return (i / (IMG_FRAMES.length - 1)) * IMG_DUR;
}

function showImageAtTime(t) {
  const fn = timeToImageFrame(t);
  if (fn == null) return;
  const url = imgFrameUrl(fn);
  // use cache if present to avoid flicker
  const cached = _imgCache.get(fn);
  if (cached && cached.complete) vimg.src = cached.src;
  else vimg.src = url;
  preloadAround(IMG_FRAMES.indexOf(fn));
}

function preloadAround(i) {
  for (let k = i; k < Math.min(i + 6, IMG_FRAMES.length); k++) {
    const fn = IMG_FRAMES[k];
    if (!_imgCache.has(fn)) {
      const im = new Image();
      im.src = imgFrameUrl(fn);
      _imgCache.set(fn, im);
    }
  }
}

function startImagePlay() {
  if (_imgTimer) return;
  playing = true; $("btnPlay").textContent = "❚❚ pause";
  const stepMs = 1000 / ((IMG_EFF_FPS || 4) * PLAYRATE);
  let last = performance.now();
  const tick = (now) => {
    if (!playing) return;
    const dt = (now - last) / 1000; last = now;
    IMG_T += dt * PLAYRATE;
    if (IMG_T >= IMG_DUR) { IMG_T = IMG_DUR; stopImagePlay(); updateImageUI(); return; }
    updateImageUI();
    _imgTimer = requestAnimationFrame(tick);
  };
  _imgTimer = requestAnimationFrame(tick);
}
function stopImagePlay() {
  playing = false;
  if (_imgTimer) { cancelAnimationFrame(_imgTimer); _imgTimer = null; }
  const b = $("btnPlay"); if (b) b.textContent = "▶︎ play";
}

function updateImageUI() {
  const t = IMG_T, dur = IMG_DUR || 1;
  $("tCur").textContent = t.toFixed(2) + "s";
  $("scrub").value = dur > 0 ? Math.round((t / dur) * 1000) : 0;
  const fn = timeToImageFrame(t);
  $("frameLbl").textContent = `frame ${fn != null ? fn : 0}`;
  showImageAtTime(t);
  highlightEventsByTime(t);
  renderTelemetryByTime(t);
  drawPlayhead(t / dur);
}

// ── unified seek (works in both modes) ──────────────────────────────────────
// episode-relative time (0..dur) → absolute video time
function epAbs(t) { return EP.video_from + t; }
function epDur() { return isImageMode() ? IMG_DUR : (EP.video_to - EP.video_from); }
function curEpTime() {
  if (isImageMode()) return IMG_T;
  return Math.max(0, Math.min(epDur(), vid.currentTime - EP.video_from));
}

function seekToEpisodeTime(t) {
  t = Math.max(0, Math.min(epDur(), t));
  if (isImageMode()) {
    IMG_T = t;
    updateImageUI();
  } else {
    vid.currentTime = epAbs(t);
    updateUI();
  }
}

function updateUI() {
  if (!EP || isImageMode()) return;
  const t = curEpTime(), dur = epDur();
  $("tCur").textContent = t.toFixed(2) + "s";
  $("scrub").value = dur > 0 ? Math.round((t / dur) * 1000) : 0;
  const frame = Math.round(t * FPS);
  $("frameLbl").textContent = `frame ${frame}`;
  highlightEventsByTime(t);
  renderTelemetry(frame);
  drawPlayhead(t / (dur || 1));
}

// ── live telemetry strip (full state at current frame/time) ──
const TELEM_VIEW = [
  { k: "battery.level",  label: "battery", fmt: v => `${Math.round(v)}<small>%</small>`, warn: v => v < 20 },
  { k: "voltage",        label: "voltage", fmt: v => `${v.toFixed(1)}<small>V</small>` },
  { k: "current",        label: "current", fmt: v => `${Math.round(v)}<small>mA</small>` },
  { k: "orientation.deg",label: "heading", fmt: v => `${Math.round(v)}<small>°</small>` },
  { k: "signal.level",   label: "signal",  fmt: v => `${Math.round(v)}<small>/4</small>` },
  { k: "gps.signal",     label: "gps",     fmt: v => v > 0 ? `fix <small>${Math.round(v)}</small>` : "no fix", warn: v => v <= 0 },
  { k: "linear.vel",     label: "lin.vel", fmt: v => v.toFixed(2) },
  { k: "angular.vel",    label: "ang.vel", fmt: v => v.toFixed(2) },
  { k: "vibration",      label: "vibration", fmt: v => v.toFixed(2) },
  { k: "imu.accel.x",    label: "accel x", fmt: v => v.toFixed(2) },
  { k: "imu.accel.y",    label: "accel y", fmt: v => v.toFixed(2) },
  { k: "imu.accel.z",    label: "accel z", fmt: v => v.toFixed(2) },
];

function _telemRowAt(i) {
  const el = $("telem"); if (!el || !EPDATA) return;
  const st = EPDATA.series && EPDATA.series.state;
  if (!st) { el.innerHTML = '<div class="tnote">no telemetry — live episode (state parquet not flushed yet)</div>'; return; }
  el.innerHTML = "";
  TELEM_VIEW.forEach(t => {
    const col = st[t.k]; if (!col) return;
    const v = col[i];
    if (v == null || Number.isNaN(v)) return;
    const warn = t.warn ? t.warn(v) : false;
    const div = document.createElement("div");
    div.className = "tcell" + (warn ? " warn" : "");
    div.innerHTML = `<div class="tk">${t.label}</div><div class="tv">${t.fmt(v)}</div>`;
    el.appendChild(div);
  });
}

function renderTelemetry(frame) {
  const el = $("telem"); if (!el || !EPDATA) return;
  const st = EPDATA.series && EPDATA.series.state;
  const fidx = EPDATA.series && EPDATA.series.frame_index;
  if (!st || !fidx || !fidx.length) { el.innerHTML = '<div class="tnote">no telemetry yet (live episode)</div>'; return; }
  let i = fidx.indexOf(frame);
  if (i < 0) {
    let best = 0, bd = Infinity;
    for (let j = 0; j < fidx.length; j++) { const d = Math.abs(fidx[j] - frame); if (d < bd) { bd = d; best = j; } }
    i = best;
  }
  _telemRowAt(i);
}

function renderTelemetryByTime(t) {
  const el = $("telem"); if (!el || !EPDATA) return;
  const fidx = EPDATA.series && EPDATA.series.frame_index;
  if (!fidx || !fidx.length) { el.innerHTML = '<div class="tnote">no telemetry — live episode (state parquet not flushed yet)</div>'; return; }
  // map time → nearest series sample by fraction
  const frac = IMG_DUR > 0 ? t / IMG_DUR : 0;
  const i = Math.round(frac * (fidx.length - 1));
  _telemRowAt(Math.max(0, Math.min(fidx.length - 1, i)));
}

// ── events ──
function renderEvents() {
  const el = $("events"); el.innerHTML = "";
  const evs = EPDATA.reasoning || [];
  if (!evs.length) { el.innerHTML = '<div class="tnote">no reasoning events</div>'; return; }
  evs.forEach((r, i) => {
    const div = document.createElement("div");
    div.className = `ev ev-${r.type}`;
    div.dataset.t = (r.video_t != null ? r.video_t : 0);
    div.dataset.i = i;
    const t = r.video_t != null ? r.video_t.toFixed(1) + "s" : "–";
    let body = r.text || "";
    if (r.type === "tool_use") body = `${r.tool_name}(${(r.tool_input||"").slice(0,60)})`;
    const who = r.agent_id && r.agent_id !== "main" ? `<span class="agid">${r.agent_id}</span>` : "";
    div.innerHTML = `<span class="et">${t}</span>` +
      `<span class="badge b-${r.type}">${(r.type||"").replace("_"," ")}</span>` + who +
      `<span>${escapeHtml(body).slice(0,160)}</span>`;
    div.onclick = () => seekToEpisodeTime(r.video_t != null ? r.video_t : 0);
    el.appendChild(div);
  });
}

// highlight reasoning events near a given episode-time (works both modes)
function highlightEventsByTime(t) {
  const TOL = isImageMode() ? Math.max(0.5, 1.0 / (IMG_EFF_FPS || 1)) : (1.5 / FPS);
  let activeEl = null;
  document.querySelectorAll(".ev").forEach(d => {
    const et = parseFloat(d.dataset.t);
    const on = Math.abs(et - t) <= TOL;
    d.classList.toggle("active", on);
    if (on && !activeEl) activeEl = d;
  });
  if (activeEl) activeEl.scrollIntoView({ block: "nearest" });
}

// ── graph: linear/angular/speed over frames + reasoning markers ──
function drawGraph() {
  const c = $("graph"), ctx = c.getContext("2d");
  const W = c.width = c.clientWidth * devicePixelRatio;
  const H = c.height = c.clientHeight * devicePixelRatio;
  ctx.clearRect(0, 0, W, H);
  const s = EPDATA.series, n = (s && s.frame_index ? s.frame_index.length : 0);
  const midY = H / 2;
  ctx.strokeStyle = "rgba(255,255,255,.15)"; ctx.beginPath();
  ctx.moveTo(0, midY); ctx.lineTo(W, midY); ctx.stroke();

  if (n) {
    const x = (i) => (i / (n - 1)) * W;
    const series = [
      { key: "linear",  color: "#6cf", get: (i) => (s.action[i]||[0])[0] },
      { key: "angular", color: "#fc6", get: (i) => (s.action[i]||[0,0])[1] },
      { key: "speed",   color: "#9c6", get: (i) => (s.speed[i]||0) / 5 },
    ];
    series.forEach(ser => {
      ctx.strokeStyle = ser.color; ctx.lineWidth = 1.5 * devicePixelRatio;
      ctx.beginPath();
      for (let i = 0; i < n; i++) {
        const v = Math.max(-1, Math.min(1, ser.get(i)));
        const y = midY - v * (H/2 - 4*devicePixelRatio);
        i ? ctx.lineTo(x(i), y) : ctx.moveTo(x(i), y);
      }
      ctx.stroke();
    });
  } else {
    // no per-frame series (live) — annotate
    ctx.fillStyle = "rgba(255,255,255,.35)";
    ctx.font = `${12 * devicePixelRatio}px system-ui`;
    ctx.fillText("live episode · action/state graph appears after finalize", 8 * devicePixelRatio, midY - 6 * devicePixelRatio);
  }

  // reasoning markers — placed by TIME fraction (works in both modes)
  const dur = epDur() || 1;
  (EPDATA.reasoning || []).forEach(r => {
    if (r.video_t == null) return;
    const px = (r.video_t / dur) * W;
    ctx.fillStyle = colorFor(r.type);
    ctx.fillRect(px - 1, 0, 2, 8 * devicePixelRatio);
  });

  c.onclick = (e) => {
    const rect = c.getBoundingClientRect();
    const frac = (e.clientX - rect.left) / rect.width;
    seekToEpisodeTime(frac * epDur());
  };
  drawPlayhead(0);
}

let _playheadFrac = 0;
function drawPlayhead(frac) { _playheadFrac = frac; }
function colorFor(t){return {reasoning:"#7cf",tool_use:"#fc6",tool_result:"#9c6",user_input:"#c9f",assistant_end:"#aaa"}[t]||"#888";}
function escapeHtml(s){return (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}

// ── playback control (video mode: clamp to episode window) ──
vid.addEventListener("timeupdate", () => {
  if (!EP || isImageMode()) return;
  if (vid.currentTime >= EP.video_to) { vid.pause(); playing = false; $("btnPlay").textContent = "▶︎ play"; vid.currentTime = EP.video_to; }
  updateUI();
});
vid.addEventListener("loadedmetadata", () => { if (EP && !isImageMode()) updateUI(); });

$("scrub").oninput = (e) => seekToEpisodeTime((e.target.value / 1000) * epDur());

$("btnPlay").onclick = () => {
  if (isImageMode()) {
    if (playing) stopImagePlay();
    else { if (IMG_T >= IMG_DUR) IMG_T = 0; startImagePlay(); }
    return;
  }
  if (playing) { vid.pause(); playing = false; $("btnPlay").textContent = "▶︎ play"; }
  else {
    if (vid.currentTime < EP.video_from || vid.currentTime >= EP.video_to) vid.currentTime = EP.video_from;
    vid.playbackRate = PLAYRATE; vid.play(); playing = true; $("btnPlay").textContent = "❚❚ pause";
  }
};

$("btnView").onclick = async () => {
  const t = curEpTime();
  VIEW = VIEW === "front" ? "rear" : "front";
  $("vlabel").textContent = VIEW; $("btnView").textContent = VIEW === "front" ? "⇆ rear" : "⇆ front";
  if (isImageMode()) {
    await loadImageSequence(EP.episode_index);
    seekToEpisodeTime(t);
  } else {
    setVideoSrc();
    vid.addEventListener("loadedmetadata", () => seekToEpisodeTime(t), { once: true });
  }
};

$("btnSpeed").onclick = () => {
  PLAYRATE = PLAYRATE === 1 ? 2 : PLAYRATE === 2 ? 0.5 : 1;
  $("btnSpeed").textContent = PLAYRATE + "×";
  if (!isImageMode()) vid.playbackRate = PLAYRATE;
};

// ── 🔎 memory search (CLIP/text/object/audio) → seek scrubber ──
async function memSearch() {
  const q = $("memQ").value.trim();
  if (!q || !DS) return;
  const el = $("memHits");
  el.innerHTML = '<div style="opacity:.5;padding:8px;font-size:12px">searching…</div>';
  try {
    const r = await api(`/api/replay/${DS}/memory/search?q=${encodeURIComponent(q)}&k=12`);
    el.innerHTML = "";
    if (!r.hits || !r.hits.length) {
      el.innerHTML = '<div style="opacity:.5;padding:8px;font-size:12px">no hits</div>';
      return;
    }
    r.hits.forEach(h => {
      const div = document.createElement("div");
      const mod = h.modality;
      div.className = `ev ev-${mod === "image" ? "tool_use" : mod === "object" ? "tool_result" : "reasoning"}`;
      const icon = mod === "image" ? "🖼️" : mod === "object" ? "📦" : mod === "audio" ? "🎙️" : "📝";
      const body = mod === "image" ? `frame ${h.frame_index}` :
                   (h.objects ? h.objects : (h.text || "").slice(0, 80));
      div.innerHTML = `<span class="et">${icon} ep${h.episode}·${h.score.toFixed(2)}</span>` +
                      `<span>${escapeHtml(body)}</span>`;
      div.onclick = () => {
        const goSeek = () => { if (h.t_in_episode != null && h.t_in_episode >= 0) seekToEpisodeTime(h.t_in_episode); };
        if (!EP || EP.episode_index !== h.episode) selectEpisode(h.episode).then(goSeek);
        else goSeek();
      };
      el.appendChild(div);
    });
  } catch (e) {
    el.innerHTML = `<div style="opacity:.5;padding:8px;font-size:12px">error: ${e}</div>`;
  }
}
$("memGo").onclick = memSearch;
$("memQ").addEventListener("keydown", (e) => { if (e.key === "Enter") memSearch(); });

window.scoutBoot = function() { if (window._rbooted) return; window._rbooted = true; init(); };
if (!document.getElementById('authGate')) window.scoutBoot();
