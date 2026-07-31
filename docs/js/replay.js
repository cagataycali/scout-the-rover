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
const aud = $("aud");
let AUDIO_ON = false;     // user toggle (persisted)
let HAS_AUDIO = false;    // current episode has a WAV sidecar

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
  else showEmptyState("no datasets found", "Drive scout (or record an episode) and they'll appear here.");
}

function showEmptyState(title, sub) {
  const ep = $("eplist"); if (ep) ep.innerHTML = `<div class="rempty"><b>${escapeHtml(title)}</b><span>${escapeHtml(sub||"")}</span></div>`;
  const evt = $("eptitle"); if (evt) evt.innerHTML = "";
  const ev = $("events"); if (ev) ev.innerHTML = "";
  const t = $("tDur"); if (t) t.textContent = "0.00s";
}

async function loadDataset(id) {
  DS = id;
  const r = await api(`/api/replay/${id}/episodes`);
  FPS = r.fps || 4; EPISODES = r.episodes || [];
  $("dsInfo").textContent = `${FPS} fps · ${EPISODES.length} episode${EPISODES.length === 1 ? "" : "s"}`;
  renderEpList();
  if (EPISODES.length) selectEpisode(EPISODES[0].episode_index);
  else {
    EP = null; EPDATA = null;
    showEmptyState("no episodes in this dataset", "This dataset has no recorded frames yet.");
    if ($("vmode")) $("vmode").textContent = "";
    vimg.removeAttribute("src"); vimg.style.display = "none"; vid.style.display = "block";
  }
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
    // thumbnail: first PNG frame (image mode) — cheap, cached by browser
    let thumb = "";
    if (e.mode === "images" && e.image_views && e.image_views.length && e.from_index != null) {
      const v = e.image_views.includes("front") ? "front" : e.image_views[0];
      const url = _withTok(`/api/replay/${DS}/frame/${v}/${e.episode_index}/${e.from_index}`);
      thumb = `<img class="epthumb" loading="lazy" src="${url}" alt="" />`;
    }
    div.innerHTML = `${thumb}<div class="epbody">` +
                    `<div><b>ep ${e.episode_index}</b> · ${nf} · ${dur}s${live}</div>` +
                    `<div class="et">${(e.tasks || []).join(" / ") || "<i>untasked</i>"}</div></div>`;
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
  EV_FILTER.clear(); AGENT_FILTER.clear();
  renderEpTitle();
  loadAudio(idx);

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
  renderEvFilter();
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
  syncAudioTo(IMG_T); audioPlay();
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
  audioPause();
}

function updateImageUI() {
  const t = IMG_T, dur = IMG_DUR || 1;
  $("tCur").textContent = t.toFixed(2) + "s";
  $("scrub").value = dur > 0 ? Math.round((t / dur) * 1000) : 0;
  const fn = timeToImageFrame(t);
  $("frameLbl").textContent = `frame ${fn != null ? fn : 0}`;
  showImageAtTime(t);
  if (!playing) syncAudioTo(t);
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
  if (!playing) syncAudioTo(t);
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

// ── episode title ──
function renderEpTitle() {
  const el = $("eptitle"); if (!el || !EP) return;
  const task = (EP.tasks || []).join(" / ") || "untasked episode";
  const live = EP.live ? '<span class="badge b-live">🔴 live</span>' : "";
  const nf = EP.length != null ? `${EP.length} frames` : "";
  const dur = (EP.video_to - EP.video_from).toFixed(1);
  el.innerHTML = `<div class="ept-task">${escapeHtml(task)}</div>` +
    `<div class="ept-meta">ep ${EP.episode_index} · ${nf} · ${dur}s ${live}</div>`;
}

// ── reasoning type filter ──
let EV_FILTER = new Set();   // empty = show all
let AGENT_FILTER = new Set();// empty = show all agents
const EV_TYPES = ["user_input","reasoning","tool_use","tool_result","assistant_end"];
function renderEvFilter() {
  const el = $("evfilter"); if (!el) return;
  // tally types + agents present in this episode
  const present = new Set((EPDATA.reasoning||[]).map(r=>r.type));
  const agents = Array.from(new Set((EPDATA.reasoning||[]).map(r=>r.agent_id).filter(Boolean)));
  el.innerHTML = "";
  EV_TYPES.filter(t=>present.has(t)).forEach(t => {
    const b = document.createElement("button");
    b.className = "evchip b-" + t + (EV_FILTER.size===0||EV_FILTER.has(t) ? " on" : "");
    b.textContent = t.replace("_"," ");
    b.onclick = () => {
      if (EV_FILTER.has(t)) EV_FILTER.delete(t);
      else {
        if (EV_FILTER.size===0) EV_TYPES.forEach(x=>{if(present.has(x))EV_FILTER.add(x);});
        EV_FILTER.delete(t);
      }
      if (EV_FILTER.size===0 || EV_FILTER.size===[...present].length) EV_FILTER.clear();
      renderEvFilter(); renderEvents(); updateActiveByTime();
    };
    el.appendChild(b);
  });
  if (agents.length > 1) {
    const lg = document.createElement("span");
    lg.className = "evagents";
    lg.appendChild(document.createTextNode("agents: "));
    agents.forEach(a => {
      const chip = document.createElement("button");
      const on = AGENT_FILTER.size === 0 || AGENT_FILTER.has(a);
      chip.className = "agchip" + (on ? " on" : "");
      chip.textContent = a;
      chip.onclick = () => {
        if (AGENT_FILTER.has(a)) AGENT_FILTER.delete(a);
        else {
          if (AGENT_FILTER.size === 0) agents.forEach(x => AGENT_FILTER.add(x));
          AGENT_FILTER.delete(a);
        }
        if (AGENT_FILTER.size === 0 || AGENT_FILTER.size === agents.length) AGENT_FILTER.clear();
        renderEvFilter(); renderEvents(); updateActiveByTime();
      };
      lg.appendChild(chip);
    });
    el.appendChild(lg);
  }
}
function evVisible(r){
  const typeOk = EV_FILTER.size===0 || EV_FILTER.has(r.type);
  const ag = r.agent_id || "main";
  const agentOk = AGENT_FILTER.size===0 || AGENT_FILTER.has(ag);
  return typeOk && agentOk;
}
function updateActiveByTime(){ highlightEventsByTime(curEpTime()); }

// pretty-print tool_input JSON compactly
function fmtToolInput(raw) {
  if (!raw) return "";
  try {
    const o = JSON.parse(raw);
    return Object.entries(o).map(([k,v]) =>
      `${k}=${typeof v==="object"?JSON.stringify(v):v}`).join(" ").slice(0,80);
  } catch { return String(raw).slice(0,80); }
}

// ── events ──
function renderEvents() {
  const el = $("events"); el.innerHTML = "";
  const evs = EPDATA.reasoning || [];
  if (!evs.length) { el.innerHTML = '<div class="tnote">no reasoning events</div>'; return; }
  evs.forEach((r, i) => {
    if (!evVisible(r)) return;
    const div = document.createElement("div");
    div.className = `ev ev-${r.type}`;
    div.dataset.t = (r.video_t != null ? r.video_t : 0);
    div.dataset.i = i;
    const t = r.video_t != null ? r.video_t.toFixed(1) + "s" : "–";
    let body = r.text || "";
    if (r.type === "tool_use") body = `${r.tool_name}(${fmtToolInput(r.tool_input)})`;
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
  // cache the rendered base so the playhead can be composited cheaply
  try { _graphBase = ctx.getImageData(0, 0, W, H); } catch (_) { _graphBase = null; }
  _graphCtx = ctx; _graphW = W; _graphH = H;
  drawPlayhead(0);
}

let _playheadFrac = 0;
let _graphBase = null, _graphCtx = null, _graphW = 0, _graphH = 0;
function drawPlayhead(frac) {
  _playheadFrac = Math.max(0, Math.min(1, frac || 0));
  if (!_graphCtx) return;
  // restore base graph then draw the playhead line on top
  if (_graphBase) _graphCtx.putImageData(_graphBase, 0, 0);
  const x = _playheadFrac * _graphW;
  _graphCtx.strokeStyle = "rgba(255,255,255,.85)";
  _graphCtx.lineWidth = 1.5 * devicePixelRatio;
  _graphCtx.beginPath();
  _graphCtx.moveTo(x, 0);
  _graphCtx.lineTo(x, _graphH);
  _graphCtx.stroke();
  // little knob at top
  _graphCtx.fillStyle = "#fff";
  _graphCtx.beginPath();
  _graphCtx.arc(x, 3 * devicePixelRatio, 3 * devicePixelRatio, 0, Math.PI * 2);
  _graphCtx.fill();
}
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
  if (playing) { vid.pause(); playing = false; $("btnPlay").textContent = "▶︎ play"; audioPause(); }
  else {
    if (vid.currentTime < EP.video_from || vid.currentTime >= EP.video_to) vid.currentTime = EP.video_from;
    vid.playbackRate = PLAYRATE; vid.play(); playing = true; $("btnPlay").textContent = "❚❚ pause";
    syncAudioTo(curEpTime()); audioPlay();
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
  if (HAS_AUDIO && aud) aud.playbackRate = PLAYRATE;
};

if ($("btnAudio")) $("btnAudio").onclick = () => {
  AUDIO_ON = !AUDIO_ON;
  localStorage.setItem("scout_replay_audio", AUDIO_ON ? "1" : "0");
  if (aud) aud.muted = !AUDIO_ON;
  updateAudioBtn();
  if (AUDIO_ON && playing) { syncAudioTo(curEpTime()); audioPlay(); }
  else if (!AUDIO_ON) audioPause();
};
AUDIO_ON = localStorage.getItem("scout_replay_audio") === "1";

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

// ── 🔊 audio sidecar (full-episode WAV @ episode-relative seconds) ──
function loadAudio(idx) {
  HAS_AUDIO = !!(EP && EP.has_audio);
  const btn = $("btnAudio");
  if (!HAS_AUDIO) {
    if (aud) { aud.pause(); aud.removeAttribute("src"); }
    if (btn) { btn.style.display = "none"; }
    return;
  }
  if (btn) btn.style.display = "";
  aud.src = _withTok(`/api/replay/${DS}/audio/${idx}`);
  aud.load();
  aud.muted = !AUDIO_ON;
  updateAudioBtn();
  // keep audio time pinned to the scrubber position
  aud.currentTime = 0;
}
function updateAudioBtn() {
  const btn = $("btnAudio"); if (!btn) return;
  btn.textContent = AUDIO_ON ? "🔊 audio" : "🔇 audio";
  btn.classList.toggle("on", AUDIO_ON);
}
function syncAudioTo(t) {
  if (!HAS_AUDIO || !aud) return;
  // only correct drift > 0.25s to avoid choppiness
  if (Math.abs((aud.currentTime || 0) - t) > 0.25) {
    try { aud.currentTime = Math.max(0, t); } catch (_) {}
  }
}
function audioPlay() { if (HAS_AUDIO && AUDIO_ON && aud) aud.play().catch(()=>{}); }
function audioPause() { if (HAS_AUDIO && aud) aud.pause(); }

// ── ⌨️ keyboard shortcuts: space=play/pause, ←/→ step, j/k jump 5s, home/end ──
function _stepTime(dt) { seekToEpisodeTime(curEpTime() + dt); }
document.addEventListener("keydown", (e) => {
  if (!EP) return;
  if (e.target && /input|textarea|select/i.test(e.target.tagName)) return;
  const step = isImageMode() ? (1 / (IMG_EFF_FPS || 1)) : (1 / FPS);
  switch (e.key) {
    case " ": e.preventDefault(); $("btnPlay").click(); break;
    case "ArrowRight": e.preventDefault(); _stepTime(step); break;
    case "ArrowLeft":  e.preventDefault(); _stepTime(-step); break;
    case "k": _stepTime(5); break;
    case "j": _stepTime(-5); break;
    case "Home": e.preventDefault(); seekToEpisodeTime(0); break;
    case "End":  e.preventDefault(); seekToEpisodeTime(epDur()); break;
    case "v": $("btnView").click(); break;
  }
});

if ($("btnRefresh")) $("btnRefresh").onclick = async () => {
  if (!DS) return;
  const keep = EP ? EP.episode_index : null;
  const btn = $("btnRefresh"); btn.classList.add("spin");
  try {
    const r = await api(`/api/replay/${DS}/episodes`);
    FPS = r.fps || 4; EPISODES = r.episodes || [];
    $("dsInfo").textContent = `${FPS} fps · ${EPISODES.length} episode${EPISODES.length === 1 ? "" : "s"}`;
    renderEpList();
    const sel = (keep != null && EPISODES.some(e => e.episode_index === keep)) ? keep
              : (EPISODES.length ? EPISODES[0].episode_index : null);
    if (sel != null) selectEpisode(sel);
  } finally { setTimeout(() => btn.classList.remove("spin"), 400); }
};

window.scoutBoot = function() { if (window._rbooted) return; window._rbooted = true; init(); };
if (!document.getElementById('authGate')) window.scoutBoot();
