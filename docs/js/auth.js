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
(async function gate() {
  if (!('credentials' in navigator) || !navigator.credentials.create) {
    // browser without WebAuthn — surface a clear message but don't hard-block
    console.warn('WebAuthn not supported by this browser');
  }

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

  const mode = st.setup_required ? 'setup' : 'login';

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
