/**
 * watchlist.js — the reader's watchlist: tickers kept in this browser.
 *
 * One list, three pages: /lookup's star and chip panel, the star on every
 * /companies card (and its Watchlist view), and the star in /history's header.
 * The key and the record shape -- {ticker, name}, newest first -- are /lookup's,
 * which had a watchlist first, so a list a reader already built there carries
 * over rather than starting again.
 *
 * Three things about it are deliberate:
 *
 * - /lookup capped the list at 12, sized for its chip panel. With a star on
 *   every one of 8,000 company cards that cap would have dropped the reader's
 *   oldest picks without a word the moment they passed it, so the cap is MAX
 *   (200) and lives here, once.
 * - Storage can refuse (Safari private mode, a full quota, cookies blocked).
 *   The list then lives in memory for the visit, so a star still toggles and
 *   the Watchlist view still answers rather than a click doing nothing.
 * - Every change, from this tab or another, arrives as one `watchlist:change`
 *   event on document, so a page has a single thing to listen to.
 *
 * The store is no-DOM and injectable (`create(storage, emit)`) so
 * tests/check_watchlist.mjs can pin it without a browser; the page instance is
 * window.Watchlist.
 */
(function (root) {
  'use strict';

  var KEY = 'ystocker_watchlist';
  var MAX = 200;

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

  function create(storage, emit) {
    var mem = storage ? null : [];   // set once storage refuses; then it is the list

    function read() {
      if (mem) return mem.slice();
      var raw;
      try { raw = storage.getItem(KEY); } catch (e) { mem = []; return []; }
      try { return clean(JSON.parse(raw || '[]')); } catch (e) { return []; }
    }

    function write(list) {
      list = clean(list).slice(0, MAX);
      if (!mem) {
        try { storage.setItem(KEY, JSON.stringify(list)); } catch (e) { mem = list.slice(); }
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
      KEY: KEY, MAX: MAX,
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

  var api = { create: create, clean: clean, KEY: KEY, MAX: MAX };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;

  if (root && root.document) {
    var storage = null;
    try { storage = root.localStorage || null; } catch (e) { storage = null; }
    var fire = function (list) {
      root.document.dispatchEvent(new root.CustomEvent('watchlist:change', { detail: { list: list } }));
    };
    var wl = create(storage, fire);
    // Another tab changed it (or cleared all storage, key null): the same event.
    root.addEventListener('storage', function (e) {
      if (e.key === KEY || e.key === null) fire(wl.list());
    });
    root.Watchlist = wl;
  }
})(typeof window !== 'undefined' ? window : this);
