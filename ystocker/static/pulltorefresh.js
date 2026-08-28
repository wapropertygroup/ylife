/**
 * ystocker pull-to-refresh
 * ~~~~~~~~~~~~~~~~~~~~~~~~
 * Pull down at the top of a page on a touch device to reload it.
 *
 * What the gesture does is `location.reload()` — deliberately *not* what the
 * header's `↻ Refresh` button does. That button navigates to a per-endpoint
 * refresh route which purges the server cache and re-fetches from Yahoo, FRED
 * and SEC EDGAR; it is cooldown-gated to 10 minutes precisely because it costs
 * real upstream calls. A gesture that an overscroll can trigger by accident must
 * not be wired to that. A reload re-renders against whatever the server already
 * has warm, which is what a phone reader wants and what the browser's own
 * pull-to-refresh does.
 *
 * Design notes:
 *
 * - **Replaces the native gesture, does not stack with it.** Chrome on Android
 *   already has pull-to-refresh, so without suppression a pull would fire twice.
 *   The `overscroll-behavior-y: contain` that suppresses it is injected *by this
 *   module*, so if the script fails to load the native gesture is left intact
 *   rather than removed with nothing in its place.
 * - **Self-contained CSS.** Tailwind is compiled here (`css/tailwind.css`), so a
 *   class this file invented would not exist in the bundle. All styling is in
 *   the injected stylesheet below.
 * - **The indicator floats; the content never moves.** Translating the page down
 *   fights iOS Safari's rubber-band, which is still free to bounce under
 *   `contain`. A pill sliding out from under the sticky header reads correctly
 *   whether or not the viewport is also bouncing.
 * - **Damped travel.** The indicator moves at roughly half the finger and stops
 *   at MAX_TRAVEL, so crossing the threshold takes a deliberate ~110px pull. A
 *   flick that merely grazes the top edge does not reload the page.
 * - **The non-passive listener is attached per gesture, not for the page's life.**
 *   `touchmove` has to be non-passive to hold back the rubber-band, and a
 *   non-passive document listener costs the browser its fast scroll path — which
 *   these dashboards can least afford. So it is bound on a touchstart that could
 *   still become a pull (single finger, already at scrollTop 0) and unbound on
 *   touchend. A touch anywhere down a scrolled page never installs it at all.
 * - **Bails on anything that is not plainly a pull.** Multi-touch, a
 *   horizontally-dominant swipe, a pull that starts inside a scrolled sub-list,
 *   an open research-desk sheet, or a focused text field all abandon the
 *   gesture. The failure this guards against is not a refresh that does not
 *   happen — it is a reload nobody asked for, which throws away scroll position
 *   and anything typed.
 *
 * Tests: `node tests/check_pulltorefresh.mjs` (no browser).
 */
(function (global) {
  'use strict';

  var doc = global.document;
  if (!doc) return;                        // non-browser (tests import directly)

  // Touch only. A mouse has Cmd-R and the header's Refresh button, and hooking
  // wheel overscroll would make trackpad flicks reload the page.
  var nav = global.navigator;
  if (!('ontouchstart' in global) && !(nav && nav.maxTouchPoints > 0)) return;

  var THRESHOLD  = 56;   // travel px that arms a refresh
  var MAX_TRAVEL = 84;   // travel px the indicator stops at
  var RESISTANCE = 0.55; // finger px -> travel px
  var AXIS_SLOP  = 10;   // finger px before the gesture's axis is decided
  var PARKED_Y   = -34;  // indicator resting position, just off the top edge
  var COMMIT_MS  = 220;  // let the committed state paint before reload() blocks

  var STYLE = [
    /* Suppresses the browser's own pull-to-refresh, so this module replaces it
       rather than adding a second one. */
    'html{overscroll-behavior-y:contain}',
    '.ystk-ptr{position:fixed;top:0;left:50%;z-index:60;',
    'display:flex;align-items:center;gap:.45rem;padding:.35rem .8rem;',
    'border-radius:9999px;pointer-events:none;white-space:nowrap;',
    'font:500 12px/1.25 Inter,system-ui,sans-serif;color:#a5b4fc;',
    'background:rgba(15,23,42,.96);border:1px solid #334155;',
    'box-shadow:0 8px 24px rgba(0,0,0,.5);',
    'transform:translate(-50%,-34px);opacity:0;',
    'will-change:transform,opacity}',
    '.ystk-ptr-icon{display:block;font-size:14px;line-height:1}',
    '.ystk-ptr-snap{transition:transform .25s ease-out,opacity .25s ease-out}',
    '.ystk-ptr-busy .ystk-ptr-icon{animation:ystk-ptr-spin .8s linear infinite}',
    '@keyframes ystk-ptr-spin{to{transform:rotate(360deg)}}',
    '@media (prefers-reduced-motion:reduce){',
    '.ystk-ptr-snap{transition:none}',
    '.ystk-ptr-busy .ystk-ptr-icon{animation:none}}'
  ].join('');

  // English is the fallback rather than the source of truth: I18n.t() may not be
  // loaded, and a missing key returns the key itself in some builds.
  var LABELS = {
    pull:    ['ptr.pull',    'Pull to refresh'],
    release: ['ptr.release', 'Release to refresh'],
    busy:    ['ptr.busy',    'Refreshing…']
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

  // ── Indicator ─────────────────────────────────────────────────────────────
  var el, iconEl, textEl;

  function build() {
    var style = doc.createElement('style');
    style.textContent = STYLE;
    doc.head.appendChild(style);

    iconEl = doc.createElement('span');
    iconEl.className = 'ystk-ptr-icon';
    iconEl.textContent = '↻';          // matches the header's ↻ Refresh
    textEl = doc.createElement('span');

    el = doc.createElement('div');
    el.className = 'ystk-ptr';
    // Decorative: the reload it announces is itself announced by the page load.
    el.setAttribute('aria-hidden', 'true');
    el.appendChild(iconEl);
    el.appendChild(textEl);
    doc.body.appendChild(el);
  }

  /** Slide the pill to `travel`, clearing the sticky header as it comes down. */
  function place(travel) {
    var clear = headerOffset * Math.min(1, travel / MAX_TRAVEL);
    el.style.transform = 'translate(-50%,' + (PARKED_Y + travel + clear) + 'px)';
  }

  // ── Gesture ───────────────────────────────────────────────────────────────
  var startY = 0, startX = 0, travel = 0, headerOffset = 0;
  var tracking = false, engaged = false, armed = false, busy = false;

  function scrollTop() {
    if (typeof global.scrollY === 'number') return global.scrollY;
    var de = doc.documentElement || {};
    return de.scrollTop || (doc.body && doc.body.scrollTop) || 0;
  }

  /** True when this touch belongs to something other than a page refresh. */
  function blocked(target) {
    // A focused field means the keyboard is up, so a downward drag is far more
    // likely a mis-swipe than a refresh — and reloading would discard the text.
    var active = doc.activeElement;
    if (active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName || '')) return true;

    // The research desk is a full-screen sheet on a phone; a pull inside it is
    // the sheet's gesture, not the page's underneath.
    var sheet = doc.getElementById('agentsFloatingPanel');
    if (sheet && !sheet.hidden) return true;

    // A pull that starts in a sub-list which is itself scrolled belongs to that
    // list — the nav search results, a scrollable table body.
    for (var n = target; n && n.nodeType === 1 && n !== doc.body; n = n.parentNode) {
      if (n.scrollTop > 0 && n.scrollHeight > n.clientHeight + 1) return true;
    }
    return false;
  }

  function onStart(e) {
    // Unconditional, so a gesture that ended without a touchend cannot leave the
    // scroll-blocking listener installed.
    detachMove();

    var touches = e.touches || [];
    if (busy || touches.length !== 1 || scrollTop() > 0 || blocked(e.target)) {
      tracking = false;
      return;
    }
    tracking = true;
    engaged = false;
    armed = false;
    travel = 0;
    startY = touches[0].clientY;
    startX = touches[0].clientX;

    // Measured per gesture rather than cached: the header is sticky at top:0
    // while scrollTop is 0, so its height is exactly what the pill must clear,
    // and it changes with viewport width.
    var header = doc.querySelector('header.sticky');
    headerOffset = header ? Math.round(header.getBoundingClientRect().height) : 0;

    attachMove();
  }

  function onMove(e) {
    if (!tracking || busy) return;

    var touches = e.touches || [];
    if (touches.length !== 1) { abandon(); return; }   // pinch-zoom

    var dy = touches[0].clientY - startY;
    var dx = touches[0].clientX - startX;

    if (!engaged) {
      // Upward: an ordinary scroll. Stop watching once it is unambiguous, so a
      // later change of direction in the same touch cannot start a pull.
      if (dy <= 0) {
        if (-dy > AXIS_SLOP) tracking = false;
        return;
      }
      if (dy < AXIS_SLOP) return;                       // axis undecided
      if (Math.abs(dx) > dy) { tracking = false; return; }  // horizontal swipe
      if (scrollTop() > 0) { tracking = false; return; }
      engaged = true;
      el.classList.remove('ystk-ptr-snap');             // follow the finger
    }

    travel = Math.max(0, Math.min(MAX_TRAVEL, (dy - AXIS_SLOP) * RESISTANCE));
    armed = travel >= THRESHOLD;

    place(travel);
    el.style.opacity = String(Math.min(1, travel / 24));
    iconEl.style.transform = 'rotate(' + Math.round(travel * 4) + 'deg)';
    textEl.textContent = label(armed ? 'release' : 'pull');

    // Suppresses the rubber-band so the pill is the only thing moving. Not
    // cancelable once the browser has committed to a scroll, in which case
    // there is nothing to suppress anyway.
    if (e.cancelable) e.preventDefault();
  }

  function onEnd() {
    detachMove();
    if (!tracking || busy) { tracking = false; return; }
    tracking = false;
    if (!engaged) return;
    engaged = false;
    if (armed) refresh();
    else retract();
  }

  /** Give up mid-gesture: put the pill back, do not refresh. */
  function abandon() {
    detachMove();
    tracking = false;
    if (engaged) { engaged = false; retract(); }
  }

  function retract() {
    armed = false;
    travel = 0;
    el.classList.add('ystk-ptr-snap');
    place(0);
    el.style.opacity = '0';
  }

  function refresh() {
    busy = true;
    armed = false;
    el.classList.add('ystk-ptr-snap', 'ystk-ptr-busy');
    iconEl.style.transform = '';        // hand the icon to the spin keyframes
    place(MAX_TRAVEL);
    el.style.opacity = '1';
    textEl.textContent = label('busy');
    // reload() tears the page down synchronously, so without a beat the
    // committed state never paints and the gesture looks like it did nothing.
    global.setTimeout(function () { global.location.reload(); }, COMMIT_MS);
  }

  // Non-passive so onMove can preventDefault(). Bound only for the length of a
  // candidate gesture — see the note on the fast scroll path above. Repeated
  // attach() calls are harmless: the same fn + options pair is not double-added.
  var MOVE_OPTS = { passive: false };
  function attachMove() { doc.addEventListener('touchmove', onMove, MOVE_OPTS); }
  function detachMove() { doc.removeEventListener('touchmove', onMove, MOVE_OPTS); }

  function init() {
    build();
    doc.addEventListener('touchstart', onStart, { passive: true });
    doc.addEventListener('touchend', onEnd, { passive: true });
    doc.addEventListener('touchcancel', abandon, { passive: true });

    // Restored from the back/forward cache with `busy` still set, the pill would
    // be stuck mid-spin and the gesture dead for the rest of the page's life.
    global.addEventListener('pageshow', function (e) {
      if (!e.persisted) return;
      busy = false;
      el.classList.remove('ystk-ptr-busy');
      retract();
    });
  }

  if (doc.readyState === 'loading') {
    doc.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }

  global.PullToRefresh = { threshold: THRESHOLD, maxTravel: MAX_TRAVEL };
})(window);
