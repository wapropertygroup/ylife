/**
 * ystocker deferred loading
 * ~~~~~~~~~~~~~~~~~~~~~~~~~
 * Runs a panel's data fetch when that panel is about to come into view, instead
 * of firing every fetch on page load.
 *
 * Why this exists: /markets fired ~12 API calls the moment it loaded, several of
 * them heavy (`/api/economic-events` is ~160 KB, `/api/markets` ~148 KB) and
 * several hitting yfinance, which serialises on a single cookie lock. The box
 * runs two sync gunicorn workers per app, so a dozen simultaneous requests is
 * already more than it can serve in parallel — the burst was enough to turn a
 * slow upstream into a page that never finished loading.
 *
 * Deferring does not make any single request faster. What it does is stop a
 * reader who only looks at the top of the page from paying for the whole page,
 * and spread the rest over the scroll rather than into one thundering herd.
 *
 * Design notes:
 *
 * - **Fires once.** The observer disconnects on the first intersection, so a
 *   loader is never run twice by scrolling back and forth.
 * - **Waits for layout before observing.** Registration happens during initial
 *   script parse, when the page is still skeletons and fixed-height chart
 *   wrappers are `display:none` — so it is far shorter than it will be, and
 *   panels that end up thousands of pixels down are briefly near the fold. An
 *   observer created then fires immediately and defers nothing. Measured on
 *   /markets: `#skewChart` settles at 1289px and `#yieldSpreadChartWrap` at
 *   5984px, yet both fetched on load. So registrations are queued and flushed
 *   after DOMContentLoaded plus two animation frames plus a short settle, and
 *   `DeferLoad.settle()` lets a page flush early once its own main render is
 *   done.
 * - **Loads eagerly when it cannot observe.** A target that is `display:none`
 *   has no box, so IntersectionObserver would never fire for it and its panel
 *   would stay empty forever. Those load immediately — the pre-existing
 *   behaviour — because a panel inside a collapsed section or an inactive tab
 *   must still work. Same when IntersectionObserver is missing entirely.
 * - **Starts before the panel is visible.** The default 300px root margin means
 *   a fetch begins while the card is still just off-screen, so normal scrolling
 *   does not wait on the network.
 *
 * Usage:
 *
 *     DeferLoad.when('#skewChart', () => loadSkew());
 *     DeferLoad.when(el, async () => { ... }, { rootMargin: '600px' });
 */
(function (global) {
  'use strict';

  const DEFAULT_ROOT_MARGIN = '300px 0px';
  const SETTLE_MS = 120;

  function resolve(target) {
    if (!target) return null;
    if (typeof target === 'string') return document.querySelector(target);
    return target.nodeType === 1 ? target : null;
  }

  /** True when the element can never intersect: no box at all. */
  function unobservable(el) {
    // offsetParent is null for display:none (and for position:fixed, hence the
    // rect check as well — a fixed panel does have a box and can intersect).
    const r = el.getBoundingClientRect();
    return el.offsetParent === null && r.width === 0 && r.height === 0;
  }

  function run(fn, label) {
    let out;
    try {
      out = fn();
    } catch (err) {
      console.error('[DeferLoad] ' + (label || 'loader') + ' threw:', err);
      return;
    }
    // A rejected promise from an async loader would otherwise be an unhandled
    // rejection with no hint as to which panel it came from.
    if (out && typeof out.catch === 'function') {
      out.catch(err => console.error('[DeferLoad] ' + (label || 'loader') + ' rejected:', err));
    }
  }

  /**
   * Run `fn` once, when `target` is near the viewport.
   *
   * @param {string|Element} target   selector or element to watch
   * @param {Function} fn             loader; may return a promise
   * @param {Object} [opts]
   * @param {string} [opts.rootMargin] how early to fire, default '300px 0px'
   * @param {string} [opts.label]      name used in console errors
   * @returns {boolean} true if deferred, false if run immediately
   */
  function when(target, fn, opts) {
    const o = opts || {};
    const label = o.label || (typeof target === 'string' ? target : 'element');

    // Resolution is deferred along with observation: at parse time the element
    // may not exist yet either.
    if (!settled) {
      pending.push({ target: target, fn: fn, opts: o, label: label });
      return true;
    }
    return observeNow(target, fn, o, label);
  }

  function observeNow(target, fn, o, label) {
    const el = resolve(target);

    // Nothing to watch: the panel is not on this page. Do not run the loader —
    // it would only fail against missing DOM.
    if (!el) return false;

    if (typeof global.IntersectionObserver !== 'function' || unobservable(el)) {
      run(fn, label);
      return false;
    }

    let fired = false;
    const io = new global.IntersectionObserver(function (entries) {
      for (const entry of entries) {
        if (!entry.isIntersecting || fired) continue;
        fired = true;
        io.disconnect();
        run(fn, label);
      }
    }, { rootMargin: o.rootMargin || DEFAULT_ROOT_MARGIN });

    io.observe(el);
    return true;
  }

  // ── Settling ──────────────────────────────────────────────────────────────
  let settled = false;
  const pending = [];

  /**
   * Flush queued registrations against the current layout. Idempotent.
   *
   * Called automatically after the document is ready; a page can call it sooner
   * once its own above-the-fold render is done, so panels are measured against
   * their real positions rather than a half-built page.
   */
  function settle() {
    if (settled) return;
    settled = true;
    while (pending.length) {
      const p = pending.shift();
      observeNow(p.target, p.fn, p.opts, p.label);
    }
  }

  function scheduleSettle() {
    // Two frames: the first lets style/layout apply, the second runs after it
    // has been painted. The timeout then gives synchronous post-parse render
    // work a moment to finish growing the page.
    const raf = global.requestAnimationFrame || function (f) { return setTimeout(f, 16); };
    raf(function () { raf(function () { setTimeout(settle, SETTLE_MS); }); });
  }

  if (typeof document === 'undefined') {
    settled = true;   // non-browser (tests import the module directly)
  } else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', scheduleSettle, { once: true });
  } else {
    scheduleSettle();
  }

  global.DeferLoad = { when: when, settle: settle };
})(window);
