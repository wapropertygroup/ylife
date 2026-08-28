/**
 * Behaviour tests for static/sw.js routing rules.
 *
 * The risk in a service worker on this site is not that it fails to cache — it is
 * that it caches the wrong thing. Every page here is a dated numeric claim about a
 * market, and a worker that answered /api/markets from disk would serve an hour-old
 * quote with no indication anywhere, defeating freshness.py entirely. That failure
 * is invisible in a browser: the page renders, the numbers look plausible, and only
 * the timestamp is wrong.
 *
 * So these assert which requests the worker declines to handle at all — a request
 * it never calls respondWith() for goes to the network exactly as if no worker were
 * installed. The positive cases (static is cache-first, navigation is network-first)
 * are checked too, but the bypasses are the point.
 *
 * Run: node tests/check_sw_routing.mjs
 */
import fs from 'fs';
import path from 'path';

const root = path.resolve(import.meta.dirname, '..');

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${detail ? '  ' + detail : ''}`);
  if (!cond) failures.push(label);
};

const ORIGIN = 'https://stock.li-family.us';

/** Load sw.js against a stub ServiceWorkerGlobalScope. */
function install() {
  const listeners = {};
  const store = new Map();          // cacheName -> Map(url -> response)
  const fetched = [];

  const mkResponse = (url, { ok = true, type = 'basic' } = {}) => ({
    url, ok, type, clone() { return mkResponse(url, { ok, type }); },
  });

  globalThis.self = globalThis;
  globalThis.location = { origin: ORIGIN };
  globalThis.addEventListener = (ev, fn) => { listeners[ev] = fn; };
  globalThis.clients = { claim: async () => {} };
  globalThis.registration = {};
  globalThis.fetch = async (req) => {
    const url = typeof req === 'string' ? req : req.url;
    fetched.push(url);
    if (url.includes('__fail__')) throw new Error('offline');
    return mkResponse(url);
  };
  globalThis.caches = {
    async open(name) {
      if (!store.has(name)) store.set(name, new Map());
      const c = store.get(name);
      return {
        async match(req) { return c.get(typeof req === 'string' ? req : req.url) || undefined; },
        async put(req, res) { c.set(typeof req === 'string' ? req : req.url, res); },
        async addAll(urls) { for (const u of urls) c.set(u, mkResponse(u)); },
      };
    },
    async keys() { return [...store.keys()]; },
    async delete(name) { return store.delete(name); },
    async match(req) {
      const key = typeof req === 'string' ? req : req.url;
      for (const c of store.values()) if (c.has(key)) return c.get(key);
      return undefined;
    },
  };

  // eslint-disable-next-line no-eval
  eval(fs.readFileSync(path.join(root, 'ystocker/static/sw.js'), 'utf8'));
  return { listeners, store, fetched, mkResponse };
}

/** Dispatch one fetch event; report whether the worker took over. */
async function dispatch(env, { url, method = 'GET', mode = 'no-cors' }) {
  let handled = false;
  let responded;
  const event = {
    request: { url, method, mode },
    respondWith(p) { handled = true; responded = p; },
    waitUntil(p) { return p; },
  };
  env.listeners.fetch(event);
  const response = handled ? await responded : undefined;
  return { handled, response };
}

// ── The invariant: market data is never intercepted ────────────────────────
console.log('=== /api/ is never handled by the worker ===');
let env = install();
for (const p of ['/api/markets', '/api/fed', '/api/commodities/seasonality',
                 '/api/agents/job/123', '/api/market-brief']) {
  const { handled } = await dispatch(env, { url: ORIGIN + p });
  t(`${p} goes straight to network`, handled === false);
}

// The cases above pass even with the /api/ guard deleted, because a page's XHR is
// neither a /static/ path nor a navigation and so falls through the routing table
// anyway. They document the property; they cannot detect its removal. A navigation
// to an API URL — someone opening /api/fed in a tab, or any future catch-all route
// — is what the guard actually stops, so that is what is asserted here. Verified by
// deleting the guard and watching only these fail.
console.log();
console.log('=== a navigation to an API URL is still not cached ===');
env = install();
for (const p of ['/api/fed', '/api/markets']) {
  const { handled } = await dispatch(env, { url: ORIGIN + p, mode: 'navigate' });
  t(`navigating to ${p} is not intercepted`, handled === false);
}

console.log();
console.log('=== other bypasses ===');
env = install();
let r = await dispatch(env, { url: ORIGIN + '/api/subscribe', method: 'POST' });
t('POST is never handled', r.handled === false);
r = await dispatch(env, { url: 'https://cdn.jsdelivr.net/npm/chart.js', mode: 'no-cors' });
t('cross-origin CDN is never handled', r.handled === false);
r = await dispatch(env, { url: ORIGIN + '/sw.js' });
t('the worker does not cache itself', r.handled === false);
r = await dispatch(env, { url: ORIGIN + '/static/manifest.json' });
t('the manifest is not cached', r.handled === false);

// ── Static assets: cache first ─────────────────────────────────────────────
console.log();
console.log('=== /static/ is cache-first ===');
env = install();
const asset = ORIGIN + '/static/i18n.js?v=123';
r = await dispatch(env, { url: asset });
t('handled', r.handled === true);
t('fetched on first miss', env.fetched.includes(asset));
const before = env.fetched.length;
await dispatch(env, { url: asset });
t('second request served from cache, no refetch',
  env.fetched.length === before, `fetches: ${env.fetched.length}`);

console.log();
console.log('=== a failed static response is not cached ===');
env = install();
const bad = ORIGIN + '/static/__fail__.js';
try { await dispatch(env, { url: bad }); } catch (_) { /* rejects, as the network would */ }
const cached = await globalThis.caches.match(bad);
t('failure not stored', cached === undefined);

// ── Navigations: network first, cached shell as fallback ───────────────────
console.log();
console.log('=== navigations are network-first ===');
env = install();
r = await dispatch(env, { url: ORIGIN + '/markets', mode: 'navigate' });
t('handled', r.handled === true);
t('went to the network', env.fetched.includes(ORIGIN + '/markets'));

console.log();
console.log('=== an offline navigation falls back, and never to API data ===');
env = install();
// Prime the cache the way a previous successful visit would.
const shellCache = await globalThis.caches.open('trade-agents-ta-v1');
await shellCache.put(ORIGIN + '/markets', env.mkResponse(ORIGIN + '/markets'));
globalThis.fetch = async () => { throw new Error('offline'); };
r = await dispatch(env, { url: ORIGIN + '/markets', mode: 'navigate' });
t('serves the cached shell when offline', r.response?.url === ORIGIN + '/markets');

console.log();
console.log('=== install precaches the offline page ===');
env = install();
await env.listeners.install({ waitUntil: p => p });
const off = await globalThis.caches.match('/static/offline.html');
t('offline page precached', off !== undefined);

console.log();
console.log('=== activate drops superseded cache generations ===');
env = install();
await globalThis.caches.open('trade-agents-OLD');
await env.listeners.activate({ waitUntil: p => p });
const keys = await globalThis.caches.keys();
t('old generation deleted', !keys.includes('trade-agents-OLD'), `kept: ${keys.join(', ')}`);

console.log();
if (failures.length) {
  console.log(`RESULT: FAIL — ${failures.length}: ${failures.join(', ')}`);
  process.exit(1);
}
console.log('RESULT: OK');
