/**
 * trade-agents service worker
 * ~~~~~~~~~~~~~~~~~~~~~~~~~~~
 * Makes the site installable and survivable offline, without ever serving a
 * stale number.
 *
 * The whole design turns on one rule: the `/api/` paths are **never cached**.
 * Everything on
 * this site is a dated, numeric claim about a market, and the codebase already
 * goes to some length to distinguish cache age from market-hours staleness from
 * a dead upstream (see freshness.py). A service worker that answered /api/markets
 * from disk would defeat all of it silently — the page would render a quote from
 * an hour ago with no indication, which is worse than an error, because an error
 * is visible. So API requests are not intercepted at all; they fail as they would
 * without a worker, and each page's own error state handles it.
 *
 * What *is* cached:
 *
 * - The `/static/` tree — **cache first**. Safe only because every asset URL
 *   carries
 *   `?v=<cache_bust>`, which changes whenever any js/css changes. A new deploy
 *   therefore requests new URLs and misses the cache by construction, so there is
 *   no way to pin a stale script against fresh HTML.
 * - **Navigations — network first**, falling back to a cached copy and then to an
 *   offline page. Caching HTML is acceptable here precisely because the HTML holds
 *   no data: every dashboard ships skeletons and fills them from /api/, so a
 *   cached shell shows its own spinners and then its own error, rather than
 *   yesterday's prices.
 *
 * Kill switch: bump CACHE_VERSION to invalidate everything on next load. To
 * disable the worker entirely, remove the registration in base.html and ship
 * `self.registration.unregister()` here for one deploy — simply deleting the file
 * leaves already-installed workers running.
 */

const CACHE_VERSION = 'ta-v1';
const CACHE_NAME    = `trade-agents-${CACHE_VERSION}`;
const OFFLINE_URL   = '/static/offline.html';

// Only what an offline cold start needs. Everything else is cached on demand:
// precaching a list of hashed asset URLs would mean templating this file with
// the current cache_bust value, and a wrong entry there fails the whole install.
const PRECACHE = [OFFLINE_URL, '/static/img/pwa-icon-192.png'];

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await cache.addAll(PRECACHE);
    // Deliberately no skipWaiting(): the new worker takes over on the next
    // launch. Swapping asset-cache generations under a page that is mid-render
    // can pair a new worker with HTML that referenced the previous generation.
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key !== CACHE_NAME) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

/** Cache-first, for content-hashed static assets. */
async function cacheFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  const hit = await cache.match(request);
  if (hit) return hit;
  const response = await fetch(request);
  // Only store a real success. Caching a 404 or an opaque error would pin the
  // failure for as long as the cache generation lives.
  if (response && response.ok && response.type === 'basic') {
    cache.put(request, response.clone());
  }
  return response;
}

/** Network-first, for navigations. */
async function networkFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    if (response && response.ok) cache.put(request, response.clone());
    return response;
  } catch (err) {
    const hit = await cache.match(request);
    if (hit) return hit;
    const offline = await cache.match(OFFLINE_URL);
    if (offline) return offline;
    throw err;
  }
}

self.addEventListener('fetch', event => {
  const request = event.request;

  // Never interfere with anything that changes server state.
  if (request.method !== 'GET') return;

  let url;
  try {
    url = new URL(request.url);
  } catch (err) {
    return;
  }

  // Third-party (Tailwind/Chart.js CDNs, Google sign-in, Stripe) manage their own
  // caching and must not be stored here; several are opaque cross-origin
  // responses that cannot be inspected before caching.
  if (url.origin !== self.location.origin) return;

  // Market data, AI briefs, agent runs: always live. See the note at the top.
  if (url.pathname.startsWith('/api/')) return;

  // Never cache the worker itself, or the manifest.
  if (url.pathname === '/sw.js' || url.pathname.endsWith('/manifest.json')) return;

  if (url.pathname.startsWith('/static/')) {
    event.respondWith(cacheFirst(request));
    return;
  }

  if (request.mode === 'navigate') {
    event.respondWith(networkFirst(request));
  }
});
