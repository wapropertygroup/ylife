/**
 * agent_calendar.js — the arithmetic behind /agents' decision calendar.
 *
 * The question it answers is "what did the desk say about this ticker on each
 * day it was asked". The history list answers a different one -- what ran
 * recently, newest first, every ticker mixed -- and makes a reader assemble a
 * single ticker's story in their head. So the calendar lays runs out by the
 * trade date they analysed (`date`, not when they ran: a Sunday run about
 * Friday's close belongs on Friday), and a ticker's timeline marks every
 * change of rating.
 *
 * Ratings arrive as `level`, from agents.rating_level on the server: 2 Buy,
 * 1 Overweight, 0 Hold, -1 Underweight, -2 Sell, null for no rating (queued,
 * running, failed, or a report that named none). A null is never read as a
 * Hold, and a change is measured only between two runs that both rated it.
 *
 * Pure: no DOM, no fetch, no clock. agents.html draws the result;
 * tests/check_agent_calendar.mjs pins it.
 */
(function (root) {
  'use strict';

  var KEYS = { '2': 'buy', '1': 'ow', '0': 'hold', '-1': 'uw', '-2': 'sell' };

  /** CSS key for a level: buy / ow / hold / uw / sell, or none. */
  function levelKey(level) {
    return (level === null || level === undefined) ? 'none' : (KEYS[String(level)] || 'none');
  }

  function pad(n) { return (n < 10 ? '0' : '') + n; }

  /** 'YYYY-MM-DD' for a UTC date's parts. */
  function iso(y, m, d) {
    var dt = new Date(Date.UTC(y, m - 1, d));
    return dt.getUTCFullYear() + '-' + pad(dt.getUTCMonth() + 1) + '-' + pad(dt.getUTCDate());
  }

  /**
   * The weeks covering one month, Monday first: each week is seven
   * {date, day, out} where `out` marks a day from the month before or after.
   * Always whole weeks, so the grid is a rectangle.
   */
  function monthGrid(year, month) {
    var first = new Date(Date.UTC(year, month - 1, 1));
    var lead = (first.getUTCDay() + 6) % 7;              // Monday = 0
    var days = new Date(Date.UTC(year, month, 0)).getUTCDate();
    var cells = Math.ceil((lead + days) / 7) * 7;
    var weeks = [], week = [];
    for (var i = 0; i < cells; i++) {
      var dt = new Date(Date.UTC(year, month - 1, 1 - lead + i));
      week.push({ date: iso(dt.getUTCFullYear(), dt.getUTCMonth() + 1, dt.getUTCDate()),
                  day: dt.getUTCDate(), out: dt.getUTCMonth() !== month - 1 });
      if (week.length === 7) { weeks.push(week); week = []; }
    }
    return weeks;
  }

  /** 'YYYY-MM' moved by `delta` months. */
  function shiftMonth(ym, delta) {
    var y = +ym.slice(0, 4), m = +ym.slice(5, 7) + delta;
    var dt = new Date(Date.UTC(y, m - 1, 1));
    return dt.getUTCFullYear() + '-' + pad(dt.getUTCMonth() + 1);
  }

  function byCreated(a, b) {
    return String(a.created_at || '').localeCompare(String(b.created_at || ''));
  }

  /** Runs of one ticker, or of every ticker when `ticker` is empty. */
  function forTicker(runs, ticker) {
    return (runs || []).filter(function (r) { return !ticker || r.ticker === ticker; });
  }

  /** {date: [runs, oldest first]} for the runs shown. */
  function byDate(runs, ticker) {
    var out = {};
    forTicker(runs, ticker).forEach(function (r) {
      (out[r.date] = out[r.date] || []).push(r);
    });
    Object.keys(out).forEach(function (d) { out[d].sort(byCreated); });
    return out;
  }

  /** The month of the newest run shown, or null when there is none. */
  function latestMonth(runs, ticker) {
    var newest = null;
    forTicker(runs, ticker).forEach(function (r) {
      if (!newest || r.date > newest) newest = r.date;
    });
    return newest ? newest.slice(0, 7) : null;
  }

  /** The months that hold a run, oldest first: where prev and next can go. */
  function months(runs, ticker) {
    var seen = {};
    forTicker(runs, ticker).forEach(function (r) { seen[r.date.slice(0, 7)] = true; });
    return Object.keys(seen).sort();
  }

  /**
   * One ticker's runs in date order, each with `change` against the last run
   * before it that carried a rating: 'up', 'down', 'same', 'first' for the
   * first rated run, or null for a run with no rating of its own.
   */
  function timeline(runs, ticker) {
    var list = forTicker(runs, ticker).slice().sort(function (a, b) {
      return a.date === b.date ? byCreated(a, b) : (a.date < b.date ? -1 : 1);
    });
    var last = null;
    return list.map(function (r) {
      var change = null;
      if (r.level !== null && r.level !== undefined) {
        change = last === null ? 'first' : r.level > last ? 'up' : r.level < last ? 'down' : 'same';
        last = r.level;
      }
      return Object.assign({}, r, { change: change });
    });
  }

  /**
   * Counts by rating key for the runs shown, the rated total, and the newest
   * rated run (`latest`) -- the "where does the desk stand now" line.
   */
  function summary(runs, ticker) {
    var counts = { buy: 0, ow: 0, hold: 0, uw: 0, sell: 0, none: 0 };
    var latest = null;
    forTicker(runs, ticker).forEach(function (r) {
      counts[levelKey(r.level)] += 1;
      if (r.level !== null && r.level !== undefined) {
        if (!latest || r.date > latest.date || (r.date === latest.date && byCreated(latest, r) < 0)) latest = r;
      }
    });
    var rated = counts.buy + counts.ow + counts.hold + counts.uw + counts.sell;
    return { counts: counts, rated: rated, total: rated + counts.none, latest: latest };
  }

  var api = { levelKey: levelKey, monthGrid: monthGrid, shiftMonth: shiftMonth,
              byDate: byDate, latestMonth: latestMonth, months: months,
              timeline: timeline, summary: summary };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.AgentCalendar = api;
})(typeof window !== 'undefined' ? window : this);
