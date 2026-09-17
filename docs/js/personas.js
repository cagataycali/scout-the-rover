/* personas — topbar pills + settings section for Scout's personas.
   Talks to /api/personas (auth-gated). Vanilla, no deps; relies on app.js
   globals httpBase(), withAuth(), toast(). Poll every 3 s (faster while a
   persona is starting/stopping). */
(() => {
  const ORDER = ['voice', 'thinker', 'thinker_drive', 'telegram', 'recording'];
  const PILL_ORDER = ['voice', 'thinker', 'telegram', 'recording'];
  const CONFIRM_STOP = { voice: 'Stop the rover-voice agent? The rover goes silent.',
                         thinker: 'Stop the thinker? Scout stops exploring on its own.',
                         telegram: 'Stop the Telegram listener?',
                         thinker_drive: 'Forbid the thinker from moving the rover?',
                         recording: 'Pause dataset recording? Episodes will not be saved.' };
  const $ = (id) => document.getElementById(id);
  let snap = null, timer = null, busy = new Set(), openLogs = new Set();

  const api = async (path, opts = {}) => {
    const r = await fetch(`${httpBase()}${path}`, { ...opts, headers: withAuth({ 'Content-Type': 'application/json', ...(opts.headers || {}) }) });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || body.error || `HTTP ${r.status}`);
    return body;
  };

  const stateCls = (p) => p.state === 'running' ? 'on'
    : (p.state === 'starting' || p.state === 'stopping') ? 'busy'
    : p.state === 'error' ? 'err'
    : p.state === 'unavailable' ? 'na' : 'off';
  const stateTxt = (p) => p.kind === 'flag' ? (p.on ? 'on' : 'off') : p.state;

  function renderPills() {
    const host = $('personaPills'); if (!host || !snap) return;
    host.innerHTML = '';
    for (const name of PILL_ORDER) {
      const p = snap.personas[name]; if (!p) continue;
      const el = document.createElement('button');
      el.className = `ppill ${stateCls(p)}`;
      el.title = `${p.label}: ${stateTxt(p)}${p.last_error ? ' — ' + p.last_error : ''}\n${p.desc}`;
      el.innerHTML = `<span class="pi">${p.icon}</span><span class="pv">${p.label}</span><span class="pdot"></span>`;
      el.disabled = busy.has(name) || p.state === 'unavailable';
      el.onclick = () => toggle(name);
      host.appendChild(el);
    }
  }

  function renderSection() {
    const host = $('personaList'); if (!host || !snap) return;
    const note = $('personaNote');
    if (note) note.textContent = snap.supervisor === 'ok'
      ? `voice provider: ${snap.voice_provider} · flags: ${snap.flags_source || 'env'}`
      : `⚠ supervisor unavailable — ${snap.supervisor_error || 'container toggles disabled'}`;
    host.innerHTML = '';
    for (const name of ORDER) {
      const p = snap.personas[name]; if (!p) continue;
      const row = document.createElement('div');
      row.className = `prow ${stateCls(p)}`;
      row.innerHTML = `
        <div class="prow-main">
          <span class="pi">${p.icon}</span>
          <div class="prow-txt"><b>${p.label}</b><small>${p.desc}</small></div>
          <span class="pstate">${stateTxt(p)}</span>
          <label class="switch"><input type="checkbox" ${p.on ? 'checked' : ''} ${busy.has(name) || p.state === 'unavailable' ? 'disabled' : ''}/><span></span></label>
        </div>
        ${p.last_error ? `<div class="perr">⚠ ${escapeHtml(String(p.last_error))}</div>` : ''}
        ${p.kind === 'container' ? `<details class="plogs" ${openLogs.has(name) ? 'open' : ''}><summary>logs (last 20 lines)</summary><pre id="plog-${name}">…</pre></details>` : ''}`;
      row.querySelector('input').onchange = () => toggle(name);
      const det = row.querySelector('details');
      if (det) det.ontoggle = () => { if (det.open) { openLogs.add(name); loadLogs(name); } else openLogs.delete(name); };
      host.appendChild(row);
      if (det && det.open) loadLogs(name);
    }
  }

  const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  async function loadLogs(name) {
    const pre = $(`plog-${name}`); if (!pre) return;
    try { const r = await api(`/api/personas/${name}/logs?n=20`); pre.textContent = (r.lines || []).join('\n') || '(no output)'; if (r.error) pre.textContent += `\n⚠ ${r.error}`; }
    catch (e) { pre.textContent = `⚠ ${e.message}`; }
  }

  async function toggle(name) {
    if (!snap || busy.has(name)) return;
    const p = snap.personas[name]; if (!p) return;
    const action = p.on ? 'stop' : 'start';
    if (action === 'stop' && !confirm(CONFIRM_STOP[name] || `Stop ${name}?`)) { render(); return; }
    busy.add(name); render();
    try {
      const r = await api(`/api/personas/${name}`, { method: 'POST', body: JSON.stringify({ action }) });
      toast(`${p.icon} ${p.label}: ${r.state}`, r.state === 'error' ? 'err' : 'ok');
    } catch (e) { toast(`${p.label}: ${e.message}`, 'err'); }
    busy.delete(name);
    schedule(800);
  }

  function render() { renderPills(); renderSection(); }

  async function refresh() {
    try { snap = await api('/api/personas'); render(); }
    catch (e) { if (snap) { snap.supervisor = 'unavailable'; snap.supervisor_error = e.message; render(); } }
    const settling = snap && Object.values(snap.personas).some((p) => p.state === 'starting' || p.state === 'stopping');
    schedule(settling ? 1200 : 3000);
  }
  function schedule(ms) { clearTimeout(timer); timer = setTimeout(refresh, ms); }

  let started = false;
  function start() { if (started) return; started = true; refresh(); }

  // boot after the auth gate releases (app.js exposes window.scoutBoot)
  const prevBoot = window.scoutBoot;
  window.scoutBoot = function () { if (prevBoot) prevBoot(); start(); };
  const gate = document.getElementById('authGate');
  if (!gate || gate.classList.contains('hidden')) setTimeout(start, 80);

  window.ScoutPersonas = { refresh, toggle, get snapshot() { return snap; } };
})();
