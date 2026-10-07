/**
 * watchlist.js — the reader's two lists of tickers, kept in this browser: the
 * watchlist (★, the stocks they saved) and the stocks they recently viewed.
 *
 * The watchlist is one list across every page: /lookup's star and chip panel,
 * the star on every /companies card (and its Watchlist view), the star in
 * /history's header, and the header search's empty box. The key and the record
 * shape -- {ticker, name}, newest first -- are /lookup's, which had a watchlist
 * first, so a list a reader already built there carries over rather than
 * starting again.
 *
 * Recently viewed (window.RecentTickers) is the same store under /lookup's
 * other key. That key used to hold "recently searched", written by three
 * copies of the same ten lines (/lookup, the header search, /dca) in two
 * shapes: /dca wrote bare strings and dropped everybody else's {ticker, name}
 * records on its next write, and /lookup drew a bare string as "undefined".
 * Now /history adds every stock page that loads, the other pages only read,
 * and clean() takes either shape, so a list written by the old code reads.
 *
 * Three things about it are deliberate:
 *
 * - /lookup capped the list at 12, sized for its chip panel. With a star on
 *   every one of 8,000 company cards that cap would have dropped the reader's
 *   oldest picks without a word the moment they passed it, so the cap is MAX
 *   (200) and lives here, once. Recently viewed keeps RECENT_MAX (24): it is a
 *   trail, not a choice, and its oldest end is meant to fall off.
 * - Storage can refuse (Safari private mode, a full quota, cookies blocked).
 *   The list then lives in memory for the visit, so a star still toggles and
 *   the Watchlist view still answers rather than a click doing nothing.
 * - Every change, from this tab or another, arrives as one event on document
 *   (`watchlist:change`, `recent:change`), so a page has a single thing to
 *   listen to per list.
 *
 * Loaded blocking from base.html's <head>, because page scripts read
 * window.Watchlist during their parse and the header search reads both lists
 * on every page. The store is no-DOM and injectable (`create(storage, emit,
 * opts)`) so tests/check_watchlist.mjs can pin it without a browser.
 */
(function (root) {
  'use strict';

  var KEY = 'ystocker_watchlist';
  var MAX = 200;
  var RECENT_KEY = 'ystocker_recent_tickers';
  var RECENT_MAX = 24;

  function norm(t) {
    var T = typeof t === 'string' ? t.trim().toUpperCase() : '';
    return T && T.length <= 20 ? T : '';
  }

  function cleanName(n) {
    return typeof n === 'string' ? n.trim().slice(0, 120) : '';
  }

  // Whatever is in storage, made safe to use: an array of {ticker, name}, one
  // per ticker, newest first. A bare string is accepted as a ticker; anything
  // else is skipped rather than failing the whole list.
  function clean(raw) {
    if (!Array.isArray(raw)) return [];
    var seen = {}, out = [];
    for (var i = 0; i < raw.length; i++) {
      var w = raw[i];
      var T = norm(typeof w === 'string' ? w : (w && w.ticker));
      if (!T || seen[T]) continue;
      seen[T] = true;
      out.push({ ticker: T, name: cleanName(w && w.name) || T });
    }
    return out;
  }

  // opts.key and opts.max pick the list; without them it is the watchlist.
  function create(storage, emit, opts) {
    var key = (opts && opts.key) || KEY;
    var max = (opts && opts.max) || MAX;
    var mem = storage ? null : [];   // set once storage refuses; then it is the list

    function read() {
      if (mem) return mem.slice();
      var raw;
      try { raw = storage.getItem(key); } catch (e) { mem = []; return []; }
      // Capped on the way out as well as in, so the cap holds over whatever
      // an older page left in storage.
      try { return clean(JSON.parse(raw || '[]')).slice(0, max); } catch (e) { return []; }
    }

    function write(list) {
      list = clean(list).slice(0, max);
      if (!mem) {
        try { storage.setItem(key, JSON.stringify(list)); } catch (e) { mem = list.slice(); }
      } else {
        mem = list.slice();
      }
      if (emit) emit(list.slice());
      return list;
    }

    function has(t) {
      var T = norm(t);
      return !!T && read().some(function (w) { return w.ticker === T; });
    }

    // Newest first, as /lookup has always kept it; adding a ticker already
    // there moves it to the front and keeps its name unless given a better one.
    function add(t, name) {
      var T = norm(t);
      if (!T) return false;
      var list = read(), prev = null;
      list = list.filter(function (w) { if (w.ticker === T) prev = w; return w.ticker !== T; });
      list.unshift({ ticker: T, name: cleanName(name) || (prev && prev.name) || T });
      write(list);
      return true;
    }

    function remove(t) {
      var T = norm(t);
      var list = read();
      var next = list.filter(function (w) { return w.ticker !== T; });
      if (next.length !== list.length) write(next);
      return next.length !== list.length;
    }

    return {
      KEY: key, MAX: max,
      list: read,
      tickers: function () { return read().map(function (w) { return w.ticker; }); },
      has: has,
      add: add,
      remove: remove,
      // The new state: true when the ticker is now on the list.
      toggle: function (t, name) { return has(t) ? (remove(t), false) : add(t, name); },
      clear: function () { write([]); },
    };
  }

  var api = { create: create, clean: clean, KEY: KEY, MAX: MAX,
              RECENT_KEY: RECENT_KEY, RECENT_MAX: RECENT_MAX };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;

  if (root && root.document) {
    var storage = null;
    try { storage = root.localStorage || null; } catch (e) { storage = null; }
    var announcer = function (type) {
      return function (list) {
        root.document.dispatchEvent(new root.CustomEvent(type, { detail: { list: list } }));
      };
    };
    var onWatch = announcer('watchlist:change'), onRecent = announcer('recent:change');
    var wl = create(storage, onWatch);
    var recent = create(storage, onRecent, { key: RECENT_KEY, max: RECENT_MAX });
    // Another tab changed one (or cleared all storage, key null): the same event.
    root.addEventListener('storage', function (e) {
      if (e.key === KEY || e.key === null) onWatch(wl.list());
      if (e.key === RECENT_KEY || e.key === null) onRecent(recent.list());
    });
    root.Watchlist = wl;
    root.RecentTickers = recent;
  }
})(typeof window !== 'undefined' ? window : this);
