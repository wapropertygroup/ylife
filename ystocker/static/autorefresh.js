/**
 * ystocker auto-refresh
 * ~~~~~~~~~~~~~~~~~~~~~
 * Keep a long-lived dashboard tab from quietly showing yesterday's numbers.
 *
 * The server side of these pages already refreshes itself — housing re-downloads
 * Zillow/Redfin/FRED/realtor.com every 24h from a background thread — but the
 * *browser* never learned. A tab opened on Tuesday and left open still rendered
 * Tuesday's payload on Thursday, under a header confidently reading "Data as of
 * Tuesday". Nothing was broken and nothing said so, which is the failure mode
 * worth engineering against.
 *
 *   AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 1756... });
 *
 * Design notes:
 *
 * - **It polls a stamp, not the payload.** `/api/housing` is 361 KB raw and
 *   ~100 KB gzipped; polling it to answer "is there anything new" would cost
 *   ~17 MB a day per open tab for data that changes once a day. The `meta`
 *   endpoint answers the same question in ~60 bytes and touches no upstream.
 * - **It reloads rather than re-rendering in place.** Not laziness: housing.html
 *   builds ~19 Chart.js instances inside a single `async` IIFE closed over
 *   `data`, so there is no `render()` to call twice — and a *partial* in-place
 *   update on a numbers dashboard is worse than none, because a KPI tile showing
 *   today above a chart still showing yesterday is wrong without looking wrong.
 *   A reload re-renders everything from one payload, at one vintage. The page's
 *   own cold-start path already reloads when data lands, so this is the same
 *   mechanism on a longer timer.
 * - **Only ever forward, and only once per stamp.** Two workers can disagree
 *   about the stamp: under gunicorn `--preload` the refresh thread lives in the
 *   master, so a forked worker serves the snapshot it inherited until it is
 *   recycled. So "stamp differs -> reload" can ping-pong forever between a
 *   worker that has the new payload and one that does not. Two guards: the
 *   stamp must be strictly *greater* than the rendered one, and the stamp we
 *   reloaded for is recorded in sessionStorage so a reload that fails to produce
 *   fresher content is never retried. MIN_GAP_MS is a third belt.
 * - **It does not yank the page out from under a reader.** A reload discards
 *   scroll position, the metro selection, every chart's range button and any
 *   open AI explanation. So an interaction inside the last INTERACTION_MS turns
 *   the reload into an offer — a dismissible pill with a Refresh action — and
 *   the automatic path is reserved for a tab that is being returned to rather
 *   than read. That is the case this module exists for.
 * - **The tab-return check is the one that matters.** Hidden tabs get their
 *   timers throttled to roughly once a minute, so the interval alone is not
 *   dependable overnight. `visibilitychange` -> visible checks immediately,
 *   which is exactly when a stale render is about to be looked at.
 * - **Declines when offline**, for the reason pulltorefresh.js does: a reload
 *   hands the navigation to sw.js, whose `networkFirst` falls through to
 *   offline.html, so it would swap a readable stale page for an unreadable one.
 *   As there, only `onLine === false` is trusted.
 *
 * Tests: `node tests/check_autorefresh.mjs` (no browser).
 */
(function (global) {
  'use strict';

  var doc = global.document;
  if (!doc) return;                        // non-browser (tests import directly)

  var POLL_MS        = 30 * 60 * 1000;  // idle cadence; data changes once a day
  var WARM_POLL_MS   = 60 * 1000;       // server says a rebuild is in flight
  var MAX_POLL_MS    = 2 * 60 * 60 * 1000;  // backoff ceiling after fetch errors
  var MIN_GAP_MS     = 5 * 60 * 1000;   // floor between two reloads, belt three
  var INTERACTION_MS = 60 * 1000;       // "someone is reading this right now"
  var SETTLE_MS      = 400;             // let the pill paint before reload() blocks

  var STYLE = [
    '.ystk-ar{position:fixed;left:50%;bottom:18px;z-index:60;',
    'display:none;align-items:center;gap:.6rem;padding:.5rem .75rem .5rem 1rem;',
    'border-radius:9999px;white-space:nowrap;',
    'font:500 12.5px/1.3 Inter,system-ui,sans-serif;',
    /* Hand-written CSS, so Tailwind's dark: variant reaches none of it — these
       are the shared tokens from base.html, with literals as the fallback for
       any page that somehow lacks them. */
    'color:var(--t-ink,#cbd5e1);',
    'background:var(--t-surface,#0f172a);',
    'border:1px solid var(--t-accent-line,#312e81);',
    'box-shadow:0 10px 30px rgba(0,0,0,.28);',
    'transform:translate(-50%,8px);opacity:0;',
    'transition:transform .22s ease-out,opacity .22s ease-out}',
    '.ystk-ar.ystk-ar-on{display:flex;transform:translate(-50%,0);opacity:1}',
    '.ystk-ar-dot{width:6px;height:6px;border-radius:50%;flex:none;',
    'background:var(--t-accent,#6366f1)}',
    '.ystk-ar-go{border:0;background:transparent;cursor:pointer;padding:2px 4px;',
    'font:600 12.5px/1.3 Inter,system-ui,sans-serif;',
    'color:var(--t-accent-ink,#a5b4fc)}',
    '.ystk-ar-go:hover{text-decoration:underline}',
    '.ystk-ar-x{border:0;background:transparent;cursor:pointer;padding:2px 6px;',
    'font:400 15px/1 Inter,system-ui,sans-serif;',
    'color:var(--t-ink-dim,#64748b)}',
    '.ystk-ar-x:hover{color:var(--t-ink,#cbd5e1)}',
    '@media (prefers-reduced-motion:reduce){.ystk-ar{transition:none}}'
  ].join('');

  // English is the fallback rather than the source of truth: I18n.t() may not
  // have loaded, and a missing key returns the key itself in some builds.
  var LABELS = {
    fresh:   ['autorefresh.new_data', 'Newer data is available'],
    action:  ['autorefresh.refresh',  'Refresh'],
    dismiss: ['autorefresh.dismiss',  'Dismiss']
  };

  function label(kind) {
    var spec = LABELS[kind];
    try {
      if (global.I18n && typeof global.I18n.t === 'function') {
        var s = global.I18n.t(spec[0]);
        if (s && s !== spec[0]) return s;
      }
    } catch (_) { /* fall through to English */ }
    return spec[1];
  }

  /** True only when the browser is *certain* there is no connection. */
  function offline() {
    var nav = global.navigator;
    return !!nav && nav.onLine === false;
  }

  // sessionStorage rather than a variable: the guard has to survive the very
  // reload it authorises, or it cannot tell a reload that worked from one that
  // landed on a worker still serving the old snapshot.
  function claimed(key) {
    try {
      var v = global.sessionStorage && global.sessionStorage.getItem(key);
      return v == null ? null : Number(v);
    } catch (_) { return null; }          // Safari private mode throws
  }

  function claim(key, stamp) {
    try {
      if (global.sessionStorage) global.sessionStorage.setItem(key, String(stamp));
    } catch (_) { /* best effort — MIN_GAP_MS still bounds the damage */ }
  }

  var watched = {};

  function watch(opts) {
    opts = opts || {};
    var url = opts.meta;
    var rendered = Number(opts.stamp) || 0;
    if (!url) return;
    // A second watch() on the same endpoint would double the poll rate and race
    // two pills. Cheap to hit by accident: a template that both includes a
    // partial and calls watch() itself.
    if (watched[url]) return;
    watched[url] = true;

    var pollMs   = Number(opts.intervalMs) || POLL_MS;
    var claimKey = 'ystk-ar:' + url;
    var lastActivity = now();
    var lastReload = 0;
    var pendingStamp = 0;
    var timer = null;
    var errors = 0;
    var pill, textEl;

    function now() { return typeof Date.now === 'function' ? Date.now() : 0; }

    // ── the offer ─────────────────────────────────────────────────────────
    function build() {
      var style = doc.createElement('style');
      style.textContent = STYLE;
      doc.head.appendChild(style);

      var dot = doc.createElement('span');
      dot.className = 'ystk-ar-dot';

      textEl = doc.createElement('span');
      textEl.textContent = label('fresh');

      var go = doc.createElement('button');
      go.className = 'ystk-ar-go';
      go.type = 'button';
      go.textContent = '↻ ' + label('action');
      go.onclick = function () { reload(pendingStamp); };

      var x = doc.createElement('button');
      x.className = 'ystk-ar-x';
      x.type = 'button';
      x.textContent = '×';
      x.setAttribute('aria-label', label('dismiss'));
      // Dismissing claims the stamp: the reader has been told and said no, so
      // the same news must not reappear every interval for the rest of the day.
      x.onclick = function () {
        if (pendingStamp) claim(claimKey, pendingStamp);
        hide();
      };

      pill = doc.createElement('div');
      pill.className = 'ystk-ar';
      pill.setAttribute('role', 'status');
      pill.appendChild(dot);
      pill.appendChild(textEl);
      pill.appendChild(go);
      pill.appendChild(x);
      doc.body.appendChild(pill);
    }

    function offer(stamp) {
      pendingStamp = stamp;
      if (!pill) build();
      // Re-read on every show: the reader may have switched language since the
      // pill was built, and I18n.apply() cannot reach a string set from JS.
      textEl.textContent = label('fresh');
      pill.classList.add('ystk-ar-on');
    }

    function hide() {
      if (pill) pill.classList.remove('ystk-ar-on');
    }

    // ── the decision ──────────────────────────────────────────────────────
    function reload(stamp) {
      if (offline()) { hide(); return; }
      claim(claimKey, stamp);
      lastReload = now();
      hide();
      // reload() tears the page down synchronously, so give the pill's
      // disappearance a frame — otherwise a click on Refresh looks inert.
      global.setTimeout(function () { global.location.reload(); }, SETTLE_MS);
    }

    /** Newer stamp seen. Reload, or offer, or stay quiet. */
    function act(stamp) {
      if (stamp <= rendered) return;              // not newer: nothing to do
      if (claimed(claimKey) >= stamp) return;     // already acted on this one
      if (now() - lastReload < MIN_GAP_MS) return;
      if (offline()) return;

      // Reload only when nobody is mid-sentence. Everything a reload costs —
      // scroll position, the metro selection, chart ranges, an open explanation
      // — belongs to someone who has touched the page recently; a tab being
      // returned to has none of it yet.
      if ((now() - lastActivity) < INTERACTION_MS) offer(stamp);
      else reload(stamp);
    }

    function schedule(ms) {
      if (timer) global.clearTimeout(timer);
      timer = global.setTimeout(tick, ms);
    }

    function tick() {
      // Hidden tabs are throttled anyway, and the check that matters for a
      // backgrounded tab is the visibilitychange one below. Skipping the fetch
      // keeps a tab open for a week from quietly making 300 requests.
      if (doc.hidden) { schedule(pollMs); return; }
      poll(function (next) { schedule(next); });
    }

    function poll(done) {
      if (offline()) { done(pollMs); return; }
      var fetchFn = global.fetch;
      if (typeof fetchFn !== 'function') { done(pollMs); return; }

      fetchFn(url, { headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          errors = 0;
          var stamp = Number(d && d.fetched_at) || 0;
          act(stamp);
          // A rebuild in flight means the stamp is about to move; waiting the
          // full idle interval would show stale numbers for another half hour
          // after the new payload had already landed.
          done(d && d.warming ? WARM_POLL_MS : pollMs);
        })
        .catch(function () {
          // A stamp we cannot read is indistinguishable from "nothing new", so
          // back off rather than surfacing anything. The page is fine; it is the
          // poll that failed.
          errors += 1;
          done(Math.min(MAX_POLL_MS, pollMs * Math.pow(2, Math.min(errors, 3))));
        });
    }

    // ── wiring ────────────────────────────────────────────────────────────
    ['pointerdown', 'keydown', 'wheel', 'touchstart'].forEach(function (ev) {
      doc.addEventListener(ev, function () { lastActivity = now(); }, { passive: true });
    });

    doc.addEventListener('visibilitychange', function () {
      if (doc.hidden) return;
      // Coming back to the tab is the moment a stale render is about to be
      // read, and also the moment a reload is least disruptive — nothing has
      // been scrolled or selected yet in this visit. Treated as no activity so
      // `act()` takes the automatic path.
      lastActivity = 0;
      poll(function (next) { schedule(next); });
    });

    // The pill's text is set from JS, so I18n.apply() — which walks data-i18n
    // attributes — cannot reach it. Without this, switching to Chinese with the
    // offer on screen leaves an English pill sitting on a Chinese page.
    doc.addEventListener('i18n:langchange', function () {
      if (!pill || !pill.classList.contains('ystk-ar-on')) return;
      pill.children[1].textContent = label('fresh');
      pill.children[2].textContent = '↻ ' + label('action');
      pill.children[3].setAttribute('aria-label', label('dismiss'));
    });

    global.addEventListener('pageshow', function (e) {
      // Restored from the back/forward cache the page can be arbitrarily old,
      // and its timer did not run while it was in there.
      if (e && e.persisted) { lastActivity = 0; poll(function (n) { schedule(n); }); }
    });

    schedule(pollMs);
  }

  global.AutoRefresh = {
    watch: watch,
    pollMs: POLL_MS,
    minGapMs: MIN_GAP_MS,
    interactionMs: INTERACTION_MS
  };
})(window);
