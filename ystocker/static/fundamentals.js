/**
 * fundamentals.js — the arithmetic behind the Fundamentals tab on
 * /history/<ticker>: which periods a range shows, how a value is written,
 * what "vs a year ago" compares, and where a ratio chart is capped.
 *
 * The payload is /api/fundamentals/<t> (ystocker/xbrl.py): parallel arrays per
 * view -- quarterly, ttm, annual -- plus valuation arrays aligned with the
 * quarterly periods. Everything here is pure: no DOM, no fetch, no Chart.js.
 * history.html draws the result; tests/check_fundamentals_js.mjs pins it.
 *
 * Two rules are worth knowing before changing anything:
 *
 * - "A year ago" is the same quarter last year, found by date, never "four
 *   bars back". A missing quarter or a gap the server filled would otherwise
 *   compare Q3 with Q4 and call the seasonal swing growth.
 * - A ratio chart (P/E, P/S) is capped at a multiple of its own median, and a
 *   capped bar says so in its tooltip. One quarter of near-zero earnings is a
 *   P/E of 1,149 -- a real figure for that quarter, and one that would flatten
 *   ten years of 20-40x into a line along the floor.
 */
(function (root) {
  'use strict';

  var RANGE_YEARS = { '3y': 3, '5y': 5, '10y': 10, max: Infinity };
  var CURRENCY = { USD: '$', EUR: '€', GBP: '£', JPY: '¥', CNY: 'CN¥', TWD: 'NT$',
                   CAD: 'C$', AUD: 'A$', HKD: 'HK$', KRW: '₩', INR: '₹', CHF: 'CHF ' };
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var DAY = 86400000;

  function isNum(v) { return typeof v === 'number' && isFinite(v); }

  function parseDay(iso) {
    var p = String(iso || '').split('-');
    return Date.UTC(+p[0], (+p[1] || 1) - 1, +p[2] || 1);
  }

  function symbol(currency) {
    return CURRENCY[currency] || ((currency || '') + ' ');
  }

  /** 46743000000 -> "46.7B": three significant figures and a unit. */
  function compact(a) {
    var units = [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K']];
    for (var i = 0; i < units.length; i++) {
      if (a >= units[i][0]) {
        var x = a / units[i][0];
        return x.toFixed(x >= 100 ? 0 : x >= 10 ? 1 : 2) + units[i][1];
      }
    }
    return a.toFixed(0);
  }

  /** "$46.7B", "-$1.2B", "€812M". */
  function money(v, currency) {
    if (!isNum(v)) return '—';
    return (v < 0 ? '-' : '') + symbol(currency) + compact(Math.abs(v));
  }

  /**
   * One value in the unit its card uses. `kind`: money | eps | pct | x | shares.
   */
  function format(v, kind, currency) {
    if (!isNum(v)) return '—';
    switch (kind) {
      case 'money': return money(v, currency);
      case 'eps': {
        var d = Math.abs(v) < 0.1 ? 3 : 2;
        return (v < 0 ? '-' : '') + symbol(currency) + Math.abs(v).toFixed(d);
      }
      case 'pct': return (v * 100).toFixed(1) + '%';
      case 'x': return v.toFixed(v >= 100 ? 0 : 1) + '×';
      case 'shares': return compact(Math.abs(v));
      default: return String(v);
    }
  }

  /** An axis tick: as short as the unit allows. */
  function axis(v, kind, currency) {
    if (!isNum(v)) return '';
    if (kind === 'pct') return Math.round(v * 100) + '%';
    if (kind === 'x') return Math.round(v) + '×';
    if (kind === 'eps') return symbol(currency) + (Math.abs(v) < 1 ? v.toFixed(2) : v.toFixed(Math.abs(v) < 10 ? 1 : 0));
    var s = format(v, kind, currency);
    return s.replace(/\.0+([TBMK])$/, '$1').replace(/(\.\d)\d([TBMK])$/, '$1$2');
  }

  /** "Jul '25" for a quarter, "FY25" for a year. */
  function tick(end, label, view) {
    if (view === 'annual' && label) return label.replace(/^FY(\d{2})(\d{2})$/, 'FY$2');
    var t = new Date(parseDay(end));
    return MONTHS[t.getUTCMonth()] + " '" + String(t.getUTCFullYear()).slice(2);
  }

  /**
   * Indices of the periods inside `range`, counted back from the newest one
   * with a value, by date. Trailing empty periods are not "now".
   */
  function window_(ends, range, hasValue) {
    var years = RANGE_YEARS[range] || RANGE_YEARS['10y'];
    var last = -1;
    for (var i = ends.length - 1; i >= 0; i--) {
      if (!hasValue || hasValue(i)) { last = i; break; }
    }
    if (last < 0) return [];
    var cutoff = years === Infinity ? -Infinity : parseDay(ends[last]) - years * 365.25 * DAY + 15 * DAY;
    var out = [];
    for (var j = 0; j <= last; j++) {
      if (parseDay(ends[j]) > cutoff) out.push(j);
    }
    return out;
  }

  /**
   * The index of the period a year before `i`: same quarter last year, found
   * by date within a few weeks, or null. `view` 'annual' means the year before.
   */
  function yearAgo(ends, i, view) {
    if (i <= 0) return null;
    var target = parseDay(ends[i]) - (view === 'annual' ? 365.25 : 364) * DAY;
    var best = null, bestGap = 25 * DAY;
    for (var j = i - 1; j >= 0; j--) {
      var gap = Math.abs(parseDay(ends[j]) - target);
      if (gap <= bestGap) { best = j; bestGap = gap; }
      if (parseDay(ends[j]) < target - 40 * DAY) break;
    }
    return best;
  }

  /**
   * Change vs a year ago. Growth is relative and needs a positive base -- a
   * swing from a loss is not "+240%"; margins move in percentage points.
   */
  function change(values, ends, i, view, kind) {
    var j = yearAgo(ends, i, view);
    if (j === null) return null;
    var now = values[i], then = values[j];
    if (!isNum(now) || !isNum(then)) return null;
    if (kind === 'pct') return { pp: (now - then) * 100 };
    if (then <= 0) return null;
    return { rel: (now - then) / then };
  }

  function median(values) {
    var xs = values.filter(isNum).slice().sort(function (a, b) { return a - b; });
    if (!xs.length) return null;
    var m = Math.floor(xs.length / 2);
    return xs.length % 2 ? xs[m] : (xs[m - 1] + xs[m]) / 2;
  }

  /**
   * Where to cap a ratio chart: `factor` x the median of the shown values, if
   * anything exceeds it. Returns {cap, clipped[]} -- cap null when nothing is
   * capped.
   */
  function capRatios(values, factor) {
    var m = median(values.filter(function (v) { return isNum(v) && v > 0; }));
    var f = factor || 4;
    var cap = m === null ? null : m * f;
    var any = cap !== null && values.some(function (v) { return isNum(v) && v > cap; });
    return {
      cap: any ? cap : null,
      clipped: values.map(function (v) { return any && isNum(v) && v > cap; }),
    };
  }

  /** The newest index in `idx` whose value is a number, or null. */
  function latest(values, idx) {
    for (var k = idx.length - 1; k >= 0; k--) {
      if (isNum(values[idx[k]])) return idx[k];
    }
    return null;
  }

  var api = {
    format: format, axis: axis, tick: tick, money: money,
    window: window_, yearAgo: yearAgo, change: change,
    median: median, capRatios: capRatios, latest: latest,
    RANGE_YEARS: RANGE_YEARS,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.Fundamentals = api;
})(typeof window !== 'undefined' ? window : this);
