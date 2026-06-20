/*
 * 🔐 scout auth — WebAuthn (passkey) passwordless gate.
 *
 * Runs BEFORE app.js logic activates. Decides one of:
 *   • setup    → no passkey enrolled yet → show "Create admin passkey"
 *   • login    → passkey exists, no session → show "Unlock with passkey"
 *   • ready    → valid session token → hand off to the dashboard
 *
 * The session token (JWT) is kept in localStorage and attached to every
 * WS connection (?token=) and HTTP call (Authorization: Bearer / cookie).
 * The private key never touches JS — the browser's WebAuthn API signs the
 * server challenge inside the secure enclave (Touch ID / Face ID / YubiKey).
 */
'use strict';

const ScoutAuth = (() => {
  const TOKEN_KEY = 'scout_token';
  const apiBase = () => {
    // mirror app.js base resolution (origin unless ?ws= / saved override)
    const q = new URLSearchParams(location.search).get('ws');
    let b = q || localStorage.getItem('scout_ws') || location.origin;
    b = b.replace(/\/$/, '');
    if (b.startsWith('ws://')) b = 'http://' + b.slice(5);
    else if (b.startsWith('wss://')) b = 'https://' + b.slice(6);
    return b;
  };

  const getToken = () => localStorage.getItem(TOKEN_KEY) || '';
  const setToken = (t) => t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY);

  async function api(path, body) {
    const r = await fetch(`${apiBase()}${path}`, {
      method: body ? 'POST' : 'GET',
      headers: {
        'Content-Type': 'application/json',
        ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
      },
      credentials: 'include',
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) {
      let msg = `${r.status}`;
      try { msg = (await r.json()).error || msg; } catch (_) {}
      throw new Error(msg);
    }
    return r.json();
  }

  // ---- base64url helpers (WebAuthn wire format) ----
  const b64uToBuf = (s) => {
    s = s.replace(/-/g, '+').replace(/_/g, '/');
    const pad = s.length % 4 ? '='.repeat(4 - (s.length % 4)) : '';
    const bin = atob(s + pad);
    const buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    return buf.buffer;
  };
  const bufToB64u = (buf) => {
    const bytes = new Uint8Array(buf);
    let bin = '';
    for (const b of bytes) bin += String.fromCharCode(b);
    return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  };

  // PublicKeyCredentialCreationOptions JSON → live structs
  function prepCreate(opts) {
    opts.challenge = b64uToBuf(opts.challenge);
    opts.user.id = b64uToBuf(opts.user.id);
    if (opts.excludeCredentials) {
      opts.excludeCredentials = opts.excludeCredentials.map((c) => ({ ...c, id: b64uToBuf(c.id) }));
    }
    return opts;
  }
  function prepGet(opts) {
    opts.challenge = b64uToBuf(opts.challenge);
    if (opts.allowCredentials) {
      opts.allowCredentials = opts.allowCredentials.map((c) => ({ ...c, id: b64uToBuf(c.id) }));
    }
    return opts;
  }

  function credToJSON(cred) {
    const r = cred.response;
    const out = {
      id: cred.id,
      rawId: bufToB64u(cred.rawId),
      type: cred.type,
      clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
      response: {},
    };
    if (r.attestationObject !== undefined) {
      out.response.attestationObject = bufToB64u(r.attestationObject);
      out.response.clientDataJSON = bufToB64u(r.clientDataJSON);
    } else {
      out.response.authenticatorData = bufToB64u(r.authenticatorData);
      out.response.clientDataJSON = bufToB64u(r.clientDataJSON);
      out.response.signature = bufToB64u(r.signature);
      out.response.userHandle = r.userHandle ? bufToB64u(r.userHandle) : null;
    }
    return out;
  }

  // ---- ceremonies ----
  async function enroll(label, bootstrap) {
    const { challenge_id, options } = await api('/auth/register/begin', { label, bootstrap });
    const cred = await navigator.credentials.create({ publicKey: prepCreate(options) });
    const res = await api('/auth/register/finish', { challenge_id, credential: credToJSON(cred) });
    setToken(res.token);
    return res;
  }

  async function login() {
    const { challenge_id, options } = await api('/auth/login/begin', {});
    const cred = await navigator.credentials.get({ publicKey: prepGet(options) });
    const res = await api('/auth/login/finish', { challenge_id, credential: credToJSON(cred) });
    setToken(res.token);
    return res;
  }

  async function enrollAdditional(label) {
    // requires an active session (server enforces). Same ceremony as enroll.
    const { challenge_id, options } = await api('/auth/register/begin', { label });
    const cred = await navigator.credentials.create({ publicKey: prepCreate(options) });
    return api('/auth/register/finish', { challenge_id, credential: credToJSON(cred) });
  }

  async function listCredentials() { return (await api('/auth/credentials')).credentials; }
  async function renameCredential(id, name) { return api('/auth/credentials/rename', { id, name }); }
  async function deleteCredential(id) { return api('/auth/credentials/delete', { id }); }

  async function status() { return api('/auth/status'); }

  function logout() { setToken(''); api('/auth/logout', {}).catch(() => {}); location.reload(); }

  return { api, status, enroll, enrollAdditional, login, logout, getToken, setToken, apiBase, listCredentials, renameCredential, deleteCredential };
})();

// expose token globally so app.js can attach it
window.ScoutAuth = ScoutAuth;
window.SCOUT_TOKEN = ScoutAuth.getToken();

// ---- gate UI ----
// WebAuthn is ONLY available in a secure context: https:// or http://localhost.
// On plain http://<ip|host> the browser makes navigator.credentials undefined
// (Firefox especially). Detect that early so we never call .create() and crash.
function webauthnReady() {
  return (
    window.isSecureContext === true &&
    typeof navigator !== 'undefined' &&
    navigator.credentials &&
    typeof navigator.credentials.create === 'function' &&
    typeof window.PublicKeyCredential !== 'undefined'
  );
}

function httpsUpgradeUrl() {
  // suggest the same host over https on the configured/likely TLS port
  try {
    const u = new URL(location.href);
    u.protocol = 'https:';
    return u.toString();
  } catch (_) { return 'https://' + location.host + location.pathname; }
}

(async function gate() {
  const overlay = document.getElementById('authGate');
  const card = document.getElementById('authCard');
  const msg = document.getElementById('authMsg');
  const btnPrimary = document.getElementById('authPrimary');
  const bootstrapWrap = document.getElementById('authBootstrapWrap');
  const bootstrapInp = document.getElementById('authBootstrap');
  const titleEl = document.getElementById('authTitle');
  const subEl = document.getElementById('authSub');

  function show() { overlay.classList.remove('hidden'); }
  function hide() {
    overlay.classList.add('hidden');
    document.body.classList.remove('locked');
    if (window.scoutBoot) window.scoutBoot();
  }
  function setMsg(t, err) { msg.textContent = t || ''; msg.className = 'auth-msg' + (err ? ' err' : ''); }

  let st;
  try {
    st = await ScoutAuth.status();
  } catch (e) {
    // server unreachable — let the page load; app.js will show offline
    hide();
    return;
  }

  // auth disabled server-side → no gate
  if (st.available === false || st.enabled === false) { hide(); return; }

  // already have a token? verify it works against a guarded endpoint.
  if (ScoutAuth.getToken()) {
    try {
      await ScoutAuth.api('/api/health'); // public, but proves base reachable
      // probe a guarded route to validate token:
      await fetch(`${ScoutAuth.apiBase()}/api/telemetry`, {
        headers: { Authorization: `Bearer ${ScoutAuth.getToken()}` },
      }).then((r) => { if (r.status === 401) throw new Error('expired'); });
      hide();
      return;
    } catch (_) {
      ScoutAuth.setToken('');
      window.SCOUT_TOKEN = '';
    }
  }

  document.body.classList.add('locked');
  show();

  // 🚫 Hard guard: no WebAuthn here (insecure context) → explain, don't crash.
  if (!webauthnReady()) {
    const insecure = window.isSecureContext !== true;
    titleEl.textContent = '🔒 HTTPS required';
    if (insecure) {
      const host = location.hostname;
      subEl.innerHTML =
        'Passkeys need a secure connection. You\'re on <b>' + location.protocol + '//' +
        location.host + '</b>.<br><br>Open this dashboard over <b>HTTPS</b> instead:' +
        '<br>• <code>https://' + host + ':' + (location.port || '8080') + '</code>' +
        (host !== 'localhost' ? '<br>• or use <code>https://scout.local:' + (location.port || '8080') + '</code>' : '') +
        '<br><br>On iPhone/Android you may also need to trust the rover CA first — open <b>/trust</b>.';
      subEl.style.color = '#ffb454';
      const link = httpsUpgradeUrl();
      btnPrimary.textContent = '↗ Reload over HTTPS';
      btnPrimary.disabled = false;
      btnPrimary.onclick = () => { location.href = link; };
      setMsg('navigator.credentials is unavailable on insecure origins.', true);
    } else {
      subEl.textContent = 'This browser does not support WebAuthn passkeys. Try a recent Chrome, Safari, Edge, or Firefox.';
      subEl.style.color = '#ffb454';
      btnPrimary.disabled = true;
      btnPrimary.textContent = 'unsupported';
    }
    return; // do NOT wire the passkey ceremony
  }

  const mode = st.setup_required ? 'setup' : 'login';

  // Surface a clear warning if WebAuthn can't run here (raw IP / insecure http).
  if (st.warning) {
    subEl.textContent = st.warning;
    subEl.style.color = '#ffb454';
    if (st.secure_context === false || st.rpid_usable === false) {
      // still render the button but it will fail with a helpful server message
      setMsg('⚠️ ' + st.warning, true);
    }
  }

  if (mode === 'setup') {
    titleEl.textContent = '🔐 Seal this rover';
    subEl.textContent =
      'No admin passkey exists yet. Create one now — this becomes your device identity. ' +
      'Anyone without it will be locked out for good.';
    btnPrimary.textContent = '✦ Create admin passkey';
    if (st.bootstrap_required) bootstrapWrap.classList.remove('hidden');
  } else {
    titleEl.textContent = '🔐 Unlock scout';
    subEl.textContent = 'Authenticate with your passkey (Touch ID / Face ID / security key).';
    btnPrimary.textContent = '✦ Unlock with passkey';
  }

  btnPrimary.addEventListener('click', async () => {
    btnPrimary.disabled = true;
    setMsg('Waiting for your authenticator…');
    try {
      if (mode === 'setup') {
        await ScoutAuth.enroll('admin passkey', bootstrapInp ? bootstrapInp.value.trim() : '');
        setMsg('✓ Passkey created. Rover sealed.');
      } else {
        await ScoutAuth.login();
        setMsg('✓ Unlocked.');
      }
      window.SCOUT_TOKEN = ScoutAuth.getToken();
      setTimeout(hide, 400);
    } catch (e) {
      setMsg('✗ ' + (e.message || 'failed'), true);
      btnPrimary.disabled = false;
    }
  });
})();
