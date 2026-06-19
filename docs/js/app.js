/* 
scout dashboard — client logic.
WS chat streaming · camera polling · telemetry · joystick · voice · config.
*/
'use strict';

const $ = (id) => document.getElementById(id);
const LS = window.localStorage;

// connection config 
// WS URL is a parameter: ?ws=... query → localStorage → page origin.
function defaultBase() {
  const q = new URLSearchParams(location.search).get('ws');
  if (q) return q.replace(/\/$/, '');
  const saved = LS.getItem('scout_ws');
  if (saved) return saved.replace(/\/$/, '');
  return location.origin; // e.g. http://host:8080
}
function httpBase() {
  let b = defaultBase();
  if (b.startsWith('ws://')) b = 'http://' + b.slice(5);
  else if (b.startsWith('wss://')) b = 'https://' + b.slice(6);
  return b;
}
function wsBase() {
  let b = defaultBase();
  if (b.startsWith('http://')) return 'ws://' + b.slice(7);
  if (b.startsWith('https://')) return 'wss://' + b.slice(8);
  return b;
}

// toasts 
function toast(msg, kind = '') {
  const host = $('toastHost');
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  host.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, 2200);
  setTimeout(() => el.remove(), 2600);
}

// chat log 
const chatLog = $('chatLog');
function addMsg(text, cls) {
  const el = document.createElement('div');
  el.className = `msg ${cls}`;
  el.textContent = text;
  chatLog.appendChild(el);
  chatLog.scrollTop = chatLog.scrollHeight;
  return el;
}
function addToolRow(name) {
  const el = document.createElement('div');
  el.className = 'tool-row';
  el.innerHTML = `<span class="spin"></span><span>🛠️ ${name}</span>`;
  chatLog.appendChild(el);
  chatLog.scrollTop = chatLog.scrollHeight;
  return el;
}

// WebSocket chat 
let chatWs = null;
let curBotEl = null;       // streaming assistant bubble
let curBotText = '';
const toolEls = {};        // id → element

function setConn(state) {
  const s = $('connState');
  s.textContent = state;
  s.className = state === 'online' ? 'live' : (state === 'offline' ? 'off' : '');
}

function connectChat() {
  try { if (chatWs) chatWs.close(); } catch (_) {}
  const url = `${wsBase()}/ws/chat`;
  setConn('connecting…');
  chatWs = new WebSocket(url);

  chatWs.onopen = () => { setConn('online'); };
  chatWs.onclose = () => { setConn('offline'); setTimeout(connectChat, 2500); };
  chatWs.onerror = () => { setConn('offline'); };

  chatWs.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    switch (m.type) {
      case 'token': {
        if (!curBotEl) { curBotEl = addMsg('', 'bot'); curBotText = ''; }
        curBotText += m.data || '';
        curBotEl.textContent = curBotText;
        chatLog.scrollTop = chatLog.scrollHeight;
        break;
      }
      case 'tool': {
        if (m.status === 'start') {
          toolEls[m.id] = addToolRow(m.name || 'tool');
        } else {
          const el = toolEls[m.id];
          if (el) el.classList.add(m.status === 'error' ? 'error' : 'done');
        }
        break;
      }
      case 'reasoning': break; // (could surface as a faded line)
      case 'done': {
        if (curBotEl && !curBotText && m.text) curBotEl.textContent = m.text;
        curBotEl = null; curBotText = '';
        break;
      }
      case 'error': {
        addMsg('⚠️ ' + m.error, 'sys');
        curBotEl = null;
        break;
      }
      case 'control_ack': break;
    }
  };
}

function sendChat() {
  const inp = $('cmdInput');
  const text = inp.value.trim();
  if (!text) return;
  if (!chatWs || chatWs.readyState !== 1) { toast('not connected', 'err'); return; }
  addMsg(text, 'user');
  chatWs.send(JSON.stringify({ type: 'chat', text }));
  inp.value = '';
  curBotEl = null;
}

$('btnSend').addEventListener('click', sendChat);
$('cmdInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') sendChat(); });

// telemetry polling 
async function pollTelemetry() {
  try {
    const r = await fetch(`${httpBase()}/api/telemetry`);
    if (!r.ok) throw 0;
    const d = await r.json();
    const bat = d.battery ?? d.battery_level ?? '--';
    $('vBattery').textContent = (bat === '--') ? '--' : `${Math.round(bat)}%`;
    const pillB = $('pillBattery');
    pillB.className = 'pill' + (bat !== '--' && bat < 15 ? ' crit' : (bat !== '--' && bat < 30 ? ' warn' : ''));
    $('vSignal').textContent = (d.signal_level ?? d.signal ?? '--') + (d.signal_level != null ? '/4' : '');
    const gpsOk = (d.gps_signal ?? 0) > 0 && (d.latitude ?? 1000) !== 1000;
    $('vGps').textContent = gpsOk ? 'fix' : 'no fix';
    $('pillGps').className = 'pill' + (gpsOk ? '' : ' warn');
    $('hudSpeed').textContent = `${(d.speed ?? 0).toFixed ? (d.speed).toFixed(1) : (d.speed ?? 0)} m/s`;
    $('hudOrient').textContent = `${Math.round(d.orientation ?? 0)}°`;
  } catch {
    $('vBattery').textContent = '--';
  }
}

// camera polling 
let frontFirst = true; // which cam is the big one
async function pollCam(view, imgEl) {
  try {
    const r = await fetch(`${httpBase()}/api/frame/${view}`);
    if (!r.ok) return;
    const d = await r.json();
    const b64 = d[`${view}_frame`];
    if (b64) imgEl.src = `data:image/jpeg;base64,${b64}`;
  } catch {}
}
function pollCameras() {
  const bigView = frontFirst ? 'front' : 'rear';
  const pipView = frontFirst ? 'rear' : 'front';
  pollCam(bigView, $('camFront'));
  if (!$('camRear').classList.contains('hidden')) pollCam(pipView, $('camRear'));
}

$('btnSwap').addEventListener('click', () => {
  const pip = $('camRear');
  if (pip.classList.contains('hidden')) {
    pip.classList.remove('hidden');
    $('camTag').textContent = frontFirst ? 'FRONT' : 'REAR';
  } else {
    frontFirst = !frontFirst;
    $('camTag').textContent = frontFirst ? 'FRONT' : 'REAR';
  }
});

// lamp toggle
let lampOn = false;
$('btnLamp').addEventListener('click', async () => {
  lampOn = !lampOn;
  $('btnLamp').classList.toggle('on', lampOn);
  try {
    await fetch(`${httpBase()}/api/lamp`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ on: lampOn }),
    });
  } catch { toast('lamp failed', 'err'); }
});

// snapshot
$('btnSnap').addEventListener('click', () => {
  const a = document.createElement('a');
  a.href = $('camFront').src; a.download = `scout-${Date.now()}.jpg`; a.click();
});

// joystick (touch + mouse) 
const joyDock = $('joyDock'), joy = $('joystick'), knob = $('joyKnob');
let joyActive = false, joyTimer = null;
let curLinear = 0, curAngular = 0;

$('btnJoy').addEventListener('click', () => joyDock.classList.toggle('show'));
$('btnJoyHide').addEventListener('click', () => joyDock.classList.remove('show'));

function joyMove(clientX, clientY) {
  const rect = joy.getBoundingClientRect();
  const cx = rect.left + rect.width / 2, cy = rect.top + rect.height / 2;
  let dx = clientX - cx, dy = clientY - cy;
  const R = rect.width / 2 - 27;
  const dist = Math.hypot(dx, dy);
  if (dist > R) { dx = dx / dist * R; dy = dy / dist * R; }
  knob.style.transform = `translate(${dx}px, ${dy}px)`;
  curAngular = +(dx / R).toFixed(2);     // right = +angular
  curLinear = +(-dy / R).toFixed(2);     // up = +linear (forward)
}
function joyReset() {
  knob.style.transform = 'translate(0,0)';
  curLinear = 0; curAngular = 0;
}
function joyStreamStart() {
  if (joyTimer) return;
  joyTimer = setInterval(() => {
    if (curLinear === 0 && curAngular === 0) return;
    if (chatWs && chatWs.readyState === 1) {
      chatWs.send(JSON.stringify({ type: 'control', linear: curLinear, angular: curAngular, duration: 0.25 }));
    }
  }, 200);
}
function joyStreamStop() {
  clearInterval(joyTimer); joyTimer = null;
  if (chatWs && chatWs.readyState === 1) chatWs.send(JSON.stringify({ type: 'stop' }));
}

function pStart(e) { joyActive = true; joyStreamStart(); pMove(e); }
function pMove(e) {
  if (!joyActive) return;
  const t = e.touches ? e.touches[0] : e;
  joyMove(t.clientX, t.clientY);
  e.preventDefault();
}
function pEnd() { joyActive = false; joyReset(); joyStreamStop(); }

joy.addEventListener('touchstart', pStart, { passive: false });
joy.addEventListener('touchmove', pMove, { passive: false });
joy.addEventListener('touchend', pEnd);
joy.addEventListener('mousedown', pStart);
window.addEventListener('mousemove', pMove);
window.addEventListener('mouseup', () => { if (joyActive) pEnd(); });

$('btnEstop').addEventListener('click', () => {
  if (chatWs && chatWs.readyState === 1) chatWs.send(JSON.stringify({ type: 'stop' }));
  joyReset();
  toast('STOP', 'err');
});

// voice (browser mic ↔ /ws/voice) 
let voiceWs = null, audioCtx = null, micStream = null, micNode = null;
let playTime = 0, voiceRate = 24000;
let voiceOn = false;

async function startVoice() {
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
  } catch { toast('mic permission denied', 'err'); return; }

  audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
  voiceWs = new WebSocket(`${wsBase()}/ws/voice`);
  voiceWs.binaryType = 'arraybuffer';

  voiceWs.onopen = () => {
    $('btnMic').classList.add('rec');
    voiceOn = true;
    toast('listening…', 'ok');
    const src = audioCtx.createMediaStreamSource(micStream);
    const proc = audioCtx.createScriptProcessor(2048, 1, 1);
    micNode = proc;
    src.connect(proc); proc.connect(audioCtx.destination);
    proc.onaudioprocess = (e) => {
      if (!voiceWs || voiceWs.readyState !== 1) return;
      const f32 = e.inputBuffer.getChannelData(0);
      const i16 = new Int16Array(f32.length);
      for (let i = 0; i < f32.length; i++) {
        const s = Math.max(-1, Math.min(1, f32[i]));
        i16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      voiceWs.send(i16.buffer);
    };
  };

  voiceWs.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === 'voice_meta') { voiceRate = m.rate || 24000; }
    else if (m.type === 'audio') { playPcm(m.data); }
    else if (m.type === 'error') { toast(m.error, 'err'); }
  };
  voiceWs.onclose = () => stopVoice(true);
  voiceWs.onerror = () => { toast('voice error', 'err'); };
}

function playPcm(b64) {
  if (!audioCtx) return;
  const bin = atob(b64);
  const len = bin.length / 2;
  const buf = audioCtx.createBuffer(1, len, voiceRate);
  const ch = buf.getChannelData(0);
  for (let i = 0; i < len; i++) {
    const lo = bin.charCodeAt(i * 2), hi = bin.charCodeAt(i * 2 + 1);
    let v = (hi << 8) | lo; if (v >= 0x8000) v -= 0x10000;
    ch[i] = v / 0x8000;
  }
  const node = audioCtx.createBufferSource();
  node.buffer = buf; node.connect(audioCtx.destination);
  const now = audioCtx.currentTime;
  if (playTime < now) playTime = now;
  node.start(playTime); playTime += buf.duration;
}

function stopVoice(silent) {
  voiceOn = false;
  $('btnMic').classList.remove('rec');
  try { if (micNode) micNode.disconnect(); } catch {}
  try { if (micStream) micStream.getTracks().forEach(t => t.stop()); } catch {}
  try { if (voiceWs && voiceWs.readyState === 1) { voiceWs.send(JSON.stringify({ type: 'stop' })); voiceWs.close(); } } catch {}
  try { if (audioCtx) audioCtx.close(); } catch {}
  audioCtx = null; micStream = null; micNode = null; playTime = 0;
  if (!silent) toast('voice off');
}

$('btnMic').addEventListener('click', () => { voiceOn ? stopVoice() : startVoice(); });

// settings drawer 
const drawer = $('drawer'), scrim = $('drawerScrim');
function openDrawer() { loadConfig(); drawer.classList.remove('hidden'); scrim.classList.remove('hidden'); }
function closeDrawer() { drawer.classList.add('hidden'); scrim.classList.add('hidden'); }
$('btnSettings').addEventListener('click', openDrawer);
$('btnCloseDrawer').addEventListener('click', closeDrawer);
$('btnCloseDrawer2').addEventListener('click', closeDrawer);
scrim.addEventListener('click', closeDrawer);

let _envState = {};
async function loadConfig() {
  $('cfgWsUrl').value = LS.getItem('scout_ws') || defaultBase();
  try {
    const r = await fetch(`${httpBase()}/api/config`);
    const c = await r.json();
    $('cfgPrompt').value = c.system_prompt || '';
    $('cfgModel').value = c.model_id || '';
    $('cfgVoiceProvider').value = c.voice_provider || 'openai';
    $('cfgVoiceName').value = c.voice_name || '';
    _envState = c.env || {};
    renderEnv();
  } catch { toast('config load failed', 'err'); }
}

function renderEnv() {
  const list = $('envList'); list.innerHTML = '';
  Object.keys(_envState).sort().forEach((k) => {
    if (k.startsWith('#')) return;
    const row = document.createElement('div');
    row.className = 'env-row';
    row.innerHTML = `<span class="ek" title="${k}">${k}</span>`;
    const inp = document.createElement('input');
    inp.value = _envState[k] || '';
    inp.dataset.key = k;
    inp.placeholder = 'value';
    row.appendChild(inp);
    list.appendChild(row);
  });
}

$('btnEnvAdd').addEventListener('click', () => {
  const k = $('envNewKey').value.trim(); const v = $('envNewVal').value;
  if (!k) return;
  _envState[k] = v;
  $('envNewKey').value = ''; $('envNewVal').value = '';
  renderEnv();
});

$('btnResetPrompt').addEventListener('click', async () => {
  try {
    await fetch(`${httpBase()}/api/config`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reset_prompt: true }),
    });
    await loadConfig();
    toast('prompt reset', 'ok');
  } catch { toast('reset failed', 'err'); }
});

$('btnSaveCfg').addEventListener('click', async () => {
  // ws url is client-side
  const ws = $('cfgWsUrl').value.trim();
  if (ws) { LS.setItem('scout_ws', ws); }

  const env = {};
  document.querySelectorAll('#envList input').forEach((inp) => {
    env[inp.dataset.key] = inp.value;
  });

  const body = {
    system_prompt: $('cfgPrompt').value,
    model_id: $('cfgModel').value.trim(),
    voice_provider: $('cfgVoiceProvider').value,
    voice_name: $('cfgVoiceName').value.trim(),
    env,
  };
  try {
    await fetch(`${httpBase()}/api/config`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    toast('saved & applied', 'ok');
    closeDrawer();
    // reconnect chat in case ws url changed
    connectChat();
  } catch { toast('save failed', 'err'); }
});

// boot 
connectChat();
pollTelemetry(); setInterval(pollTelemetry, 2000);
pollCameras();  setInterval(pollCameras, 700);
