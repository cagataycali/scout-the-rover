// 🎞️ scout replay — video scrubber synced to dataset frames + reasoning events.
// The packed LeRobot mp4 holds ALL episodes; each episode is a [from,to] window.
// We clamp the <video> to that window and map video-time → frame → reasoning.
const $ = (id) => document.getElementById(id);
const api = (p) => fetch(p).then(r => r.json());

let DS = null, FPS = 4, EPISODES = [], EP = null, EPDATA = null;
let VIEW = "front", PLAYRATE = 1, playing = false;

const vid = $("vid");

async function init() {
  const datasets = await api("/api/replay/datasets");
  const sel = $("dsSel");
  sel.innerHTML = "";
  datasets.forEach(d => {
    const o = document.createElement("option");
    o.value = d.id;
    o.textContent = `${d.id}  (${d.total_episodes} eps${d.has_reasoning ? " · 🧠" : ""})`;
    sel.appendChild(o);
  });
  sel.onchange = () => loadDataset(sel.value);
  if (datasets.length) loadDataset(datasets[0].id);
}

async function loadDataset(id) {
  DS = id;
  const r = await api(`/api/replay/${id}/episodes`);
  FPS = r.fps || 4; EPISODES = r.episodes;
  $("dsInfo").textContent = `${FPS} fps · ${EPISODES.length} episodes`;
  // point video at this dataset's packed mp4
  setVideoSrc();
  renderEpList();
  if (EPISODES.length) selectEpisode(EPISODES[0].episode_index);
}

function setVideoSrc() {
  vid.src = `/api/replay/${DS}/video/${VIEW}`;
  vid.load();
}

function renderEpList() {
  const el = $("eplist"); el.innerHTML = "";
  EPISODES.forEach(e => {
    const div = document.createElement("div");
    div.className = "epitem" + (EP && e.episode_index === EP.episode_index ? " active" : "");
    const dur = (e.video_to - e.video_from).toFixed(1);
    div.innerHTML = `<div><b>ep ${e.episode_index}</b> · ${e.length}f · ${dur}s</div>
                     <div class="et">${(e.tasks || []).join(" / ")}</div>`;
    div.onclick = () => selectEpisode(e.episode_index);
    el.appendChild(div);
  });
}

async function selectEpisode(idx) {
  EP = EPISODES.find(e => e.episode_index === idx);
  renderEpList();
  EPDATA = await api(`/api/replay/${DS}/episode/${idx}`);
  $("tDur").textContent = (EP.video_to - EP.video_from).toFixed(2) + "s";
  // seek video to episode start
  seekToEpisodeTime(0);
  renderEvents();
  drawGraph();
}

// episode-relative time (0..dur) → absolute video time
function epAbs(t) { return EP.video_from + t; }
function epDur() { return EP.video_to - EP.video_from; }
function curEpTime() { return Math.max(0, Math.min(epDur(), vid.currentTime - EP.video_from)); }

function seekToEpisodeTime(t) {
  vid.currentTime = epAbs(Math.max(0, Math.min(epDur(), t)));
  updateUI();
}

function updateUI() {
  if (!EP) return;
  const t = curEpTime(), dur = epDur();
  $("tCur").textContent = t.toFixed(2) + "s";
  $("scrub").value = dur > 0 ? Math.round((t / dur) * 1000) : 0;
  const frame = Math.round(t * FPS);
  $("frameLbl").textContent = `frame ${frame}`;
  highlightEvents(frame);
  drawPlayhead(t / (dur || 1));
}

// ── events ──
function renderEvents() {
  const el = $("events"); el.innerHTML = "";
  (EPDATA.reasoning || []).forEach((r, i) => {
    const div = document.createElement("div");
    div.className = `ev ev-${r.type}`;
    div.dataset.frame = r.frame_index ?? 0;
    div.dataset.i = i;
    const t = r.video_t != null ? r.video_t.toFixed(1) + "s" : "–";
    let body = r.text || "";
    if (r.type === "tool_use") body = `${r.tool_name}(${(r.tool_input||"").slice(0,60)})`;
    div.innerHTML = `<span class="et">${t}</span>` +
      `<span class="badge b-${r.type}">${r.type.replace("_"," ")}</span>` +
      `<span>${escapeHtml(body).slice(0,160)}</span>`;
    div.onclick = () => seekToEpisodeTime((r.frame_index ?? 0) / FPS);
    el.appendChild(div);
  });
}

function highlightEvents(frame) {
  document.querySelectorAll(".ev").forEach(d => {
    const f = parseInt(d.dataset.frame, 10);
    d.classList.toggle("active", Math.abs(f - frame) <= 1);
  });
  // auto-scroll active into view
  const act = document.querySelector(".ev.active");
  if (act) act.scrollIntoView({ block: "nearest" });
}

// ── graph: linear/angular/speed over frames + reasoning markers ──
function drawGraph() {
  const c = $("graph"), ctx = c.getContext("2d");
  const W = c.width = c.clientWidth * devicePixelRatio;
  const H = c.height = c.clientHeight * devicePixelRatio;
  ctx.clearRect(0, 0, W, H);
  const s = EPDATA.series, n = (s.frame_index || []).length;
  if (!n) return;
  const x = (i) => (i / (n - 1)) * W;
  const midY = H / 2;
  // zero line
  ctx.strokeStyle = "rgba(255,255,255,.15)"; ctx.beginPath();
  ctx.moveTo(0, midY); ctx.lineTo(W, midY); ctx.stroke();
  const series = [
    { key: "linear",  color: "#6cf", get: (i) => (s.action[i]||[0])[0] },
    { key: "angular", color: "#fc6", get: (i) => (s.action[i]||[0,0])[1] },
    { key: "speed",   color: "#9c6", get: (i) => (s.speed[i]||0) / 5 }, // scale speed
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
  // reasoning markers
  (EPDATA.reasoning || []).forEach(r => {
    if (r.frame_index == null) return;
    const i = s.frame_index.indexOf(r.frame_index);
    const px = i >= 0 ? x(i) : (r.frame_index / (n-1)) * W;
    ctx.fillStyle = colorFor(r.type);
    ctx.fillRect(px - 1, 0, 2, 8 * devicePixelRatio);
    // span shading for motion
    if (r.frame_span) {
      const a = s.frame_index.indexOf(r.frame_span[0]);
      const b = s.frame_index.indexOf(r.frame_span[1]);
      if (a >= 0 && b >= 0) {
        ctx.fillStyle = "rgba(255,200,100,.10)";
        ctx.fillRect(x(a), 0, x(b)-x(a), H);
      }
    }
  });
  // click to seek
  c.onclick = (e) => {
    const rect = c.getBoundingClientRect();
    const frac = (e.clientX - rect.left) / rect.width;
    seekToEpisodeTime(frac * epDur());
  };
  drawPlayhead(0);
}

let _playheadFrac = 0;
function drawPlayhead(frac) {
  _playheadFrac = frac;
  drawGraphOverlay();
}
function drawGraphOverlay() {
  // redraw is heavy; instead draw a separate playhead by re-rendering graph base
  // (simple approach: full redraw is fine at 4fps scale)
}
function colorFor(t){return {reasoning:"#7cf",tool_use:"#fc6",tool_result:"#9c6",user_input:"#c9f",assistant_end:"#aaa"}[t]||"#888";}
function escapeHtml(s){return (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}

// ── playback control: clamp to episode window ──
vid.addEventListener("timeupdate", () => {
  if (!EP) return;
  if (vid.currentTime >= EP.video_to) { vid.pause(); playing = false; $("btnPlay").textContent = "▶︎ play"; vid.currentTime = EP.video_to; }
  updateUI();
});
vid.addEventListener("loadedmetadata", () => { if (EP) updateUI(); });

$("scrub").oninput = (e) => seekToEpisodeTime((e.target.value / 1000) * epDur());
$("btnPlay").onclick = () => {
  if (playing) { vid.pause(); playing = false; $("btnPlay").textContent = "▶︎ play"; }
  else {
    if (vid.currentTime < EP.video_from || vid.currentTime >= EP.video_to) vid.currentTime = EP.video_from;
    vid.playbackRate = PLAYRATE; vid.play(); playing = true; $("btnPlay").textContent = "❚❚ pause";
  }
};
$("btnView").onclick = () => {
  const t = curEpTime();
  VIEW = VIEW === "front" ? "rear" : "front";
  $("vlabel").textContent = VIEW; $("btnView").textContent = VIEW === "front" ? "⇆ rear" : "⇆ front";
  setVideoSrc();
  vid.addEventListener("loadedmetadata", () => seekToEpisodeTime(t), { once: true });
};
$("btnSpeed").onclick = () => {
  PLAYRATE = PLAYRATE === 1 ? 2 : PLAYRATE === 2 ? 0.5 : 1;
  $("btnSpeed").textContent = PLAYRATE + "×"; vid.playbackRate = PLAYRATE;
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
        // jump to the right episode, then seek to the hit's frame
        if (!EP || EP.episode_index !== h.episode) {
          selectEpisode(h.episode).then(() => {
            if (h.t_in_episode != null && h.t_in_episode >= 0) seekToEpisodeTime(h.t_in_episode);
          });
        } else if (h.t_in_episode != null && h.t_in_episode >= 0) {
          seekToEpisodeTime(h.t_in_episode);
        }
      };
      el.appendChild(div);
    });
  } catch (e) {
    el.innerHTML = `<div style="opacity:.5;padding:8px;font-size:12px">error: ${e}</div>`;
  }
}
$("memGo").onclick = memSearch;
$("memQ").addEventListener("keydown", (e) => { if (e.key === "Enter") memSearch(); });

init();