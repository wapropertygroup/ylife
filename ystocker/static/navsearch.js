/*
 * navsearch.js — the top-banner ticker search.
 *
 * The banner magnifier used to be a plain link to /lookup, so searching a
 * symbol cost a full page load before you could even type. This turns it into
 * an in-place box: type, pick, land on /history/<TICKER>.
 *
 * Two mount points share this code (see base.html):
 *   - desktop: a [data-navsearch-toggle] button collapses/expands the panel
 *   - mobile:  no toggle, so the box is always open inside the hamburger menu
 * A mount with no toggle is treated as permanently open, which is the only
 * difference between the two.
 *
 * Suggestions come from /api/search, which only knows PEER_GROUPS members — so
 * the typed symbol always leads the list, named from a matching suggestion when
 * there is one and offered bare when there is not. Without that lead row
 * anything off the curated list (most of the market) would look unsearchable,
 * even though /history/<TICKER> and /api/ticker/<TICKER> take any symbol.
 */
(function () {
  'use strict';

  var RECENT_KEY = 'ystocker_recent_tickers';  // shared with lookup.html
  var DEBOUNCE_MS = 130;
  var MAX_LEN = 12;

  /*
   * i18n.js declares `const I18n = (function(){...})()`. A top-level `const`
   * in a classic script is a *script-scoped* binding, not a property of
   * window — so `window.I18n` is undefined here even though the bare
   * identifier resolves through the scope chain. Guarding on window.I18n
   * therefore failed open: every label fell back to English and ?lang=zh was
   * dropped from every URL, on a page that was otherwise fully translated.
   */
  function i18n() {
    try { return typeof I18n !== 'undefined' ? I18n : null; } catch (_) { return null; }
  }

  function t(key, fallback) {
    var m = i18n();
    try { return (m && m.t && m.t(key)) || fallback; } catch (_) { return fallback; }
  }

  /* Yahoo symbols are not just letters: BRK-B, ^GSPC, GC=F, 7203.T all need
     to survive. Anything outside that set is dropped rather than escaped, so
     a pasted "$AAPL" or a stray space still resolves. */
  function clean(raw) {
    return String(raw || '')
      .toUpperCase()
      .replace(/[^A-Z0-9.\-^=]/g, '')
      .slice(0, MAX_LEN);
  }

  function langSuffix() {
    var m = i18n();
    try {
      if (m && m.getLang && m.getLang() === 'zh') return '?lang=zh';
    } catch (_) {}
    return '';
  }

  function historyUrl(ticker) {
    return '/history/' + encodeURIComponent(ticker) + langSuffix();
  }

  function lookupUrl(ticker) {
    var q = ticker ? '?ticker=' + encodeURIComponent(ticker) : '';
    var lang = langSuffix();
    if (q && lang) return '/lookup' + q + '&lang=zh';
    return '/lookup' + (q || lang);
  }

  function getRecent() {
    try {
      var arr = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]');
      return Array.isArray(arr) ? arr : [];
    } catch (_) { return []; }
  }

  /* Mirrors lookup.html's _addRecent so the two surfaces share one history
     list — searching from the banner populates the Recent chips on /lookup. */
  function addRecent(ticker, name) {
    try {
      var arr = getRecent().filter(function (x) { return x && x.ticker !== ticker; });
      arr.unshift({ ticker: ticker, name: name || ticker });
      localStorage.setItem(RECENT_KEY, JSON.stringify(arr.slice(0, 6)));
    } catch (_) {}
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function init(root) {
    var toggle  = root.querySelector('[data-navsearch-toggle]');
    var panel   = root.querySelector('[data-navsearch-panel]');
    var input   = root.querySelector('[data-navsearch-input]');
    var results = root.querySelector('[data-navsearch-results]');
    var fullLink = root.querySelector('[data-navsearch-full]');
    if (!input || !results) return;

    // Per-mount state. The desktop and mobile mounts are both in the DOM at
    // once (only CSS hides one), so nothing here may be shared between them.
    var rows = [];      // [{ticker, name, group}] — what Enter/arrows act on
    var hits = [];      // last /api/search payload for this mount
    var active = -1;
    var timer = null;
    var controller = null;

    function isOpen() {
      return !panel || !panel.hasAttribute('hidden');
    }

    function open() {
      if (panel) panel.removeAttribute('hidden');
      if (toggle) toggle.setAttribute('aria-expanded', 'true');
      render();
      // The panel is hidden when the click lands, and focusing a
      // display:none input is a no-op, so this waits a frame.
      requestAnimationFrame(function () { input.focus(); input.select(); });
    }

    function close() {
      if (panel) panel.setAttribute('hidden', '');
      if (toggle) toggle.setAttribute('aria-expanded', 'false');
      active = -1;
    }

    function go(ticker, name) {
      ticker = clean(ticker);
      if (!ticker) return;
      addRecent(ticker, name);
      window.location.href = historyUrl(ticker);
    }

    function render() {
      var typed = clean(input.value);
      var html = '';

      if (fullLink) fullLink.setAttribute('href', lookupUrl(typed));

      if (!typed) {
        var recent = getRecent();
        if (recent.length) {
          html += header(t('nav.search_recent', 'Recent'));
          html += recent.map(function (r, i) {
            return row(r.ticker, r.name, '', i);
          }).join('');
          rows = recent.map(function (r) {
            return { ticker: r.ticker, name: r.name, group: '' };
          });
        } else {
          rows = [];
          html += '<p class="px-3 py-3 text-xs text-slate-500">' +
                  esc(t('nav.search_empty', 'Type a symbol, then press Enter.')) + '</p>';
        }
        results.innerHTML = html;
        return;
      }

      /*
       * The typed symbol always leads the list: /api/search only knows
       * PEER_GROUPS members, but /history/<TICKER> renders any symbol, so the
       * lead row is what makes the rest of the market reachable.
       *
       * When a suggestion *is* the typed symbol it is folded into that lead row
       * rather than listed under it — otherwise typing a full symbol showed
       * "WMT — Search this symbol" and hid the one row that carried the company
       * name, and picking it stored a nameless entry in Recent ("NVDA NVDA").
       */
      var exact = null, others = [];
      hits.forEach(function (h) {
        if (!h || !h.ticker) return;
        if (h.ticker === typed) exact = h; else others.push(h);
      });

      var lead = (exact && exact.name)
        ? { ticker: typed, name: exact.name, group: exact.group || '' }
        : { ticker: typed, name: t('nav.search_symbol', 'Search this symbol'),
            group: '', _hint: true };

      rows = [lead].concat(others);

      html += rows.map(function (r, i) {
        return row(r.ticker, r.name, r.group, i, r._hint);
      }).join('');
      results.innerHTML = html;
      paint();
    }

    function header(label) {
      return '<p class="px-3 pt-2 pb-1 text-[10px] font-semibold tracking-widest uppercase text-slate-500">' +
             esc(label) + '</p>';
    }

    function row(ticker, name, group, i, hint) {
      return '' +
        '<button type="button" data-i="' + i + '" data-ticker="' + esc(ticker) + '"' +
        ' class="navsearch-row w-full flex items-center gap-2 px-3 py-2 text-left' +
        ' hover:bg-brand/20 transition">' +
        '<span class="font-mono text-xs font-semibold text-brand shrink-0">' + esc(ticker) + '</span>' +
        '<span class="text-xs text-slate-400 truncate flex-1' + (hint ? ' italic' : '') + '">' +
        esc(name || '') + '</span>' +
        (group ? '<span class="text-[10px] text-slate-600 shrink-0">' + esc(group) + '</span>' : '') +
        '</button>';
    }

    function paint() {
      var els = results.querySelectorAll('.navsearch-row');
      for (var i = 0; i < els.length; i++) {
        els[i].classList.toggle('bg-brand/20', i === active);
      }
      if (active >= 0 && els[active]) {
        els[active].scrollIntoView({ block: 'nearest' });
      }
    }

    function fetchHits(q) {
      if (controller) controller.abort();
      controller = new AbortController();
      fetch('/api/search?q=' + encodeURIComponent(q), { signal: controller.signal })
        .then(function (r) { return r.ok ? r.json() : []; })
        .then(function (hits_) {
          // A slow response for an older prefix must not overwrite the
          // suggestions for what is in the box now.
          if (clean(input.value) !== q) return;
          hits = Array.isArray(hits_) ? hits_ : [];
          render();
        })
        .catch(function () { /* aborted or offline — typed row still works */ });
    }

    input.addEventListener('input', function () {
      var typed = clean(input.value);
      if (input.value !== typed) input.value = typed;
      active = -1;
      hits = [];
      render();
      clearTimeout(timer);
      if (typed) timer = setTimeout(function () { fetchHits(typed); }, DEBOUNCE_MS);
    });

    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (!rows.length) return;
        active += (e.key === 'ArrowDown' ? 1 : -1);
        if (active < 0) active = rows.length - 1;
        if (active >= rows.length) active = 0;
        paint();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        // With nothing highlighted, Enter takes the lead row — which on a
        // non-empty box is always the typed symbol, so Enter is never a no-op
        // there. On an *empty* box rows[0] is the newest Recent entry, which
        // the visitor did not ask for, so Enter does nothing instead.
        var pick = active >= 0 ? rows[active] : (clean(input.value) ? rows[0] : null);
        if (!pick) return;
        go(pick.ticker, pick._hint ? '' : pick.name);
      } else if (e.key === 'Escape') {
        if (panel) { close(); input.blur(); }
      }
    });

    results.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.navsearch-row') : null;
      if (!btn) return;
      var pick = rows[parseInt(btn.getAttribute('data-i'), 10)];
      go(btn.getAttribute('data-ticker'), pick && !pick._hint ? pick.name : '');
    });

    if (toggle) {
      toggle.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        if (isOpen()) close(); else open();
      });

      document.addEventListener('click', function (e) {
        if (isOpen() && !root.contains(e.target)) close();
      });
    } else {
      render();  // always-open (mobile) mount
    }

    document.addEventListener('i18n:langchange', function () {
      if (isOpen()) render();
    });
  }

  function boot() {
    document.querySelectorAll('[data-navsearch]').forEach(init);

    /* "/" focuses the search from anywhere, the convention on every finance
       site. Skipped while a field is focused so it stays typeable. */
    document.addEventListener('keydown', function (e) {
      if (e.key !== '/' || e.metaKey || e.ctrlKey || e.altKey) return;
      var el = document.activeElement;
      var tag = el && el.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' ||
          (el && el.isContentEditable)) return;
      var mount = document.querySelector('[data-navsearch] [data-navsearch-toggle]');
      if (!mount) return;
      e.preventDefault();
      mount.click();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
