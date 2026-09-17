/* Backend client. Same-origin in production (Caddy serves the PWA and proxies
   the API), overridable for local dev. */

export const API_BASE = import.meta.env.VITE_API_BASE || '';
const KEY = import.meta.env.VITE_API_KEY || '';

function headers(extra = {}) {
  return { 'Content-Type': 'application/json', 'X-VectraQuant-Key': KEY, ...extra };
}

async function req(path, opts = {}) {
  const res = await fetch(`${API_BASE}${path}`, { ...opts, headers: headers(opts.headers) });
  const text = await res.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = { detail: text };
  }
  if (!res.ok) throw new Error(body?.detail || `HTTP ${res.status}`);
  return body;
}

/* ---------------------------------------------------------------- reads */

export const getState = () => req('/state');
export const getConfig = () => req('/config');
export const getDayEnd = (date = '') => req(`/report/dayend${date ? `?date=${date}` : ''}`);
export const getPremarketReport = () => req('/report/premarket');
export const getJournal = () => req('/report/journal');

/* ---------------------------------------------------------------- writes */

/* A fresh idempotency key per tap. Two taps produce two keys, but the server
   re-checks can_enter and the broker de-dupes on reference, so exactly one
   order reaches the exchange. */
export const approve = (id) =>
  req(`/suggestions/${id}/approve`, {
    method: 'POST',
    headers: { 'Idempotency-Key': crypto.randomUUID() },
  });

export const reject = (id, reason = '') =>
  req(`/suggestions/${id}/reject`, { method: 'POST', body: JSON.stringify({ reason }) });

export const squareOff = () => req('/squareoff', { method: 'POST' });

export const setPreset = (preset) =>
  req('/config/preset', { method: 'POST', body: JSON.stringify({ preset }) });

export const getStrategies = () => req('/strategies');
export const getThunderboltStatus = () => req('/orderflow/thunderbolt/status');

/* Job-based backtest: submit returns a job_id immediately (202) — the sim runs
   on a background worker thread, never on the request. Progress streams over
   the /live socket as {type:'backtest', ...} frames; getBacktestRun is the
   REST fallback (also how a page refresh recovers a job already in flight). */
export const submitBacktestRun = (params) =>
  req('/backtest/runs', { method: 'POST', body: JSON.stringify(params) });

export const getBacktestRun = (jobId) => req(`/backtest/runs/${jobId}`);

export const cancelBacktestRun = (jobId) =>
  req(`/backtest/runs/${jobId}/cancel`, { method: 'POST' });

export const listBacktestRuns = (limit = 20) => req(`/backtest/runs?limit=${limit}`);

export const setActiveStrategy = (payload) =>
  req('/strategies/active', { method: 'POST', body: JSON.stringify(payload) });

export const updateDhanToken = (payload = {}) =>
  req('/broker/refresh-token', { method: 'POST', body: JSON.stringify(payload) });

export const getDataStatus = () => req('/data/status');

export const fetchCandles = (params) =>
  req('/data/fetch-candles', { method: 'POST', body: JSON.stringify(params) });

export const refreshChain = () =>
  req('/data/refresh-chain', { method: 'POST' });

export const setKillSwitch = (action, opts = {}) =>
  req('/killswitch', { method: 'POST', body: JSON.stringify({ action, ...opts }) });

export const putConfig = (params) =>
  req('/config', { method: 'PUT', body: JSON.stringify({ confirm: 'CONFIRM', params }) });

export const resetConfig = () =>
  req('/config/reset', { method: 'POST', body: JSON.stringify({ confirm: 'RESET' }) });

/* ---------------------------------------------------------------- push */

export const pushKey = () => req('/push/key');

export const pushSubscribe = (sub) =>
  req('/push/subscribe', { method: 'POST', body: JSON.stringify(sub) });

/* What the push state IS, not what this session's button did.

   The iOS subscription outlives the page, so a relaunch showing "Enable push" again
   looked like push had switched itself off. Cosmetic on its own — but it hid a real
   failure: if the backend was down when push was first enabled, iOS kept the
   subscription while the server never stored it, and no notification would ever
   arrive. So an existing subscription is re-asserted with the server on every load.
   /push/subscribe upserts by endpoint, so repeating it is free. */
export async function pushStatus() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return '';
  if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return '';
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (!sub) return '';
    await pushSubscribe(sub.toJSON());
    return 'Push enabled ✓';
  } catch {
    return 'Enabled on iOS · server unreachable, will re-register';
  }
}

const b64ToU8 = (b64) => {
  const pad = '='.repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
};

export async function enablePush() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    throw new Error('Push unsupported. On iOS the app must be installed to the home screen.');
  }
  const perm = await Notification.requestPermission();
  if (perm !== 'granted') throw new Error('Notification permission denied');
  const reg = await navigator.serviceWorker.ready;
  const { public_key: key } = await pushKey();
  if (!key) throw new Error('Server has no VAPID public key configured');
  const sub =
    (await reg.pushManager.getSubscription()) ||
    (await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: b64ToU8(key),
    }));
  await pushSubscribe(sub.toJSON());
  return true;
}

/* ---------------------------------------------------------------- live feed */

export function liveSocket(onMessage, onStatus) {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const base = API_BASE || `${proto}://${window.location.host}`;
  const url = `${base.replace(/^http/, 'ws')}/live?key=${encodeURIComponent(KEY)}`;
  let ws = null;
  let closed = false;
  let retry = 0;
  let ping = null;

  const connect = () => {
    if (closed) return;
    ws = new WebSocket(url);
    ws.onopen = () => {
      retry = 0;
      onStatus?.('connected');
      ping = setInterval(() => ws?.readyState === 1 && ws.send('ping'), 20000);
    };
    ws.onmessage = (e) => {
      try {
        onMessage(JSON.parse(e.data));
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onclose = () => {
      clearInterval(ping);
      onStatus?.('reconnecting');
      if (!closed) setTimeout(connect, Math.min(1000 * 2 ** retry++, 15000));
    };
    ws.onerror = () => ws?.close();
  };
  connect();

  return () => {
    closed = true;
    clearInterval(ping);
    ws?.close();
  };
}
