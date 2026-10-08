/*
 * static/sector_map.js -- the arithmetic behind /sectors, kept out of the
 * template so tests/check_sector_map_js.mjs can run it without a browser.
 *
 * The page's figures all come from /api/sectors (ystocker/sector_map.py);
 * nothing here recomputes a return. What lives here is what the page derives
 * from them: which rows a view shows and in what order, the sentence at the
 * top, where the plot's axes stop, where labels go, and the colour of a
 * bubble. Exposed as window.SectorMap, and as module.exports for Node.
 */
(function (root) {
  'use strict';

  const LEVELS = ['sector', 'group', 'sub'];
  const WINDOWS = ['1D', '1W', '1M'];
  // Clockwise from top right, the order a group rotates through them; the
  // keys and the rule are the /markets rotation map's (_gicsQuad).
  const QUADS = ['lead', 'weak', 'lag', 'improve'];

  function quadrant(x, y) {
    if (x == null || y == null) return null;
    return x >= 0 ? (y >= 0 ? 'lead' : 'weak') : (y >= 0 ? 'improve' : 'lag');
  }

  const num = v => (typeof v === 'number' && isFinite(v) ? v : null);

  // A row's display name in the page's language. Sectors and industry groups
  // use the gics.<code> strings /markets already ships; sub-industries carry
  // their Chinese name in the payload.
  function label(row, level, zh, t) {
    if (level === 'sub') return (zh && row.name_zh) || row.name;
    const k = t ? t('gics.' + row.id) : '';
    return k || row.name;
  }

  // Rows of one level that pass the filters, in the chosen order.
  //   quad   -- a QUADS key, or '' for all
  //   query  -- matched against both names, and against member tickers and
  //             names when `members` (rows of payload.members) and `names` are given
  //   sort   -- {key: 'name'|'n'|'w'|<window>, dir: 1|-1}
  function select(payload, level, opts) {
    const o = opts || {};
    const rows = ((payload && payload.levels && payload.levels[level]) || []).slice();
    const q = (o.query || '').trim().toLowerCase();
    let hits = null;
    if (q && level === 'sub' && Array.isArray(o.members)) {
      hits = new Set();
      const names = o.names || {};
      o.members.forEach(m => {
        if (m.t.toLowerCase().startsWith(q) || String(names[m.t] || '').toLowerCase().includes(q)) hits.add(m.s);
      });
    }
    const out = rows.filter(r => {
      if (o.quad && r.quad !== o.quad) return false;
      if (o.parent && r.sector !== o.parent && r.group !== o.parent) return false;
      if (!q) return true;
      const names = [r.name, r.name_zh, o.labelOf ? o.labelOf(r) : ''].map(s => String(s || '').toLowerCase());
      return names.some(s => s.includes(q)) || (hits !== null && hits.has(r.id));
    });
    const s = o.sort || { key: o.win || '1D', dir: -1 };
    const val = r => (s.key === 'name' ? null
      : s.key === 'n' ? r.n : s.key === 'w' ? r.weight : num((r.returns || {})[s.key]));
    out.sort((a, b) => {
      if (s.key === 'name') {
        const la = o.labelOf ? o.labelOf(a) : a.name, lb = o.labelOf ? o.labelOf(b) : b.name;
        return s.dir * String(la).localeCompare(String(lb));
      }
      const va = val(a), vb = val(b);
      // A missing figure sorts last whichever way the sort runs.
      if (va == null && vb == null) return b.weight - a.weight;
      if (va == null) return 1;
      if (vb == null) return -1;
      return s.dir * (va - vb) || b.weight - a.weight;
    });
    return out;
  }

  function counts(rows) {
    const c = { lead: 0, weak: 0, lag: 0, improve: 0, none: 0 };
    rows.forEach(r => { c[r.quad && c[r.quad] != null ? r.quad : 'none'] += 1; });
    return c;
  }

  // The facts the sentence at the top states, for one level and window:
  // how many groups rose and fell, the quadrant counts, and where the money
  // went and left. A sub-industry of fewer than min_headline_members is never
  // named: one company's spike is not money moving into an industry.
  function verdict(payload, level, win, k) {
    const rows = (payload && payload.levels && payload.levels[level]) || [];
    const minN = level === 'sub' ? (payload.min_headline_members || 3) : 1;
    const w = WINDOWS.indexOf(win) >= 0 ? win : '1D';
    let up = 0, down = 0, flat = 0;
    const named = [];
    rows.forEach(r => {
      const v = num((r.returns || {})[w]);
      if (v == null) return;
      if (v > 0) up += 1; else if (v < 0) down += 1; else flat += 1;
      if (r.n >= minN) named.push({ row: r, v });
    });
    const take = k || 3;
    const top = named.slice().sort((a, b) => b.v - a.v).slice(0, take).filter(x => x.v > 0);
    const bottom = named.slice().sort((a, b) => a.v - b.v).slice(0, take).filter(x => x.v < 0);
    return {
      level, win: w, total: rows.length, up, down, flat,
      quads: counts(rows),
      top: top.map(x => x.row), bottom: bottom.map(x => x.row),
      index: num(((payload && payload.index && payload.index.returns) || {})[w]),
    };
  }

  function percentile(sorted, p) {
    if (!sorted.length) return 0;
    const i = (sorted.length - 1) * p, lo = Math.floor(i), hi = Math.ceil(i);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (i - lo);
  }

  // Where an axis stops. Fitted to the points between the 3rd and 97th
  // percentile, padded, and always reaching past zero so the index stays in
  // view; a point beyond is drawn on the edge and marked, its tooltip giving
  // the true figure. Fitting to every point let one sub-industry 40 points out
  // squeeze the other 125 into a corner. Under ten points there is no tail to
  // trim, so the bounds are the extremes, as on /markets.
  function bound(vals) {
    const v = vals.filter(x => typeof x === 'number' && isFinite(x)).sort((a, b) => a - b);
    if (!v.length) return { min: -1, max: 1 };
    const trim = v.length >= 10;
    const lo = Math.min(0, trim ? percentile(v, 0.03) : v[0]);
    const hi = Math.max(0, trim ? percentile(v, 0.97) : v[v.length - 1]);
    const pad = Math.max(0.5, (hi - lo) * 0.1);
    return { min: lo - pad, max: hi + pad };
  }

  function clamp(v, b) {
    return v < b.min ? { v: b.min, out: true } : v > b.max ? { v: b.max, out: true } : { v, out: false };
  }

  // Area, not radius, in proportion to index weight, scaled to the canvas.
  function radius(weight, width, level) {
    const base = level === 'sector' ? 2.2 : level === 'group' ? 2.6 : 3.2;
    const k = Math.min(level === 'sub' ? 6.5 : 5, Math.max(base, width / 200));
    return Math.max(level === 'sub' ? 2.5 : 3, k * Math.sqrt(Math.max(0, weight || 0)));
  }

  // A bubble's fill: green up, red down, deeper the further from zero against
  // `scale` (the window's 90th-percentile move). rgb triples are passed in so
  // the page picks the theme's.
  function colour(v, scale, up, down, flat) {
    if (v == null) return `rgba(${flat.join(',')},0.35)`;
    const s = Math.max(scale || 1, 0.25);
    const a = 0.28 + 0.6 * Math.min(1, Math.abs(v) / s);
    const c = v > 0 ? up : v < 0 ? down : flat;
    return `rgba(${c.join(',')},${a.toFixed(2)})`;
  }

  function scaleOf(rows, win) {
    const v = rows.map(r => num((r.returns || {})[win])).filter(x => x != null).map(Math.abs).sort((a, b) => a - b);
    return v.length ? percentile(v, 0.9) : 1;
  }

  // Greedy, largest first, as on /markets: each label tries the right of its
  // bubble, the left, above, below and the diagonals, and takes the first spot
  // inside the plot that clears every placed label and every other bubble. One
  // that fits nowhere is dropped rather than nudged away from its bubble.
  function placeLabels(items, area, measure, h, reserved) {
    const taken = (reserved || []).slice(), out = [];
    const PAD = 2, GAP = 3;
    const overlaps = (a, b) => a.l < b.r + PAD && b.l < a.r + PAD && a.t < b.b + PAD && b.t < a.b + PAD;
    const onBubble = (bx, c) => {
      const nx = Math.max(bx.l, Math.min(c.x, bx.r)), ny = Math.max(bx.t, Math.min(c.y, bx.b));
      return (nx - c.x) ** 2 + (ny - c.y) ** 2 < c.r * c.r;
    };
    items.forEach(it => {
      if (!it.text) return;
      const w = measure(it.text), d = it.r * Math.SQRT1_2;
      const spot = [
        [it.x + it.r + GAP, it.y - h / 2],
        [it.x - it.r - GAP - w, it.y - h / 2],
        [it.x - w / 2, it.y - it.r - GAP - h],
        [it.x - w / 2, it.y + it.r + GAP],
        [it.x + d + GAP, it.y - d - GAP - h],
        [it.x + d + GAP, it.y + d + GAP],
        [it.x - d - GAP - w, it.y - d - GAP - h],
        [it.x - d - GAP - w, it.y + d + GAP],
      ].map(([l, t]) => ({ l, t, r: l + w, b: t + h }))
       .find(bx => bx.l >= area.left && bx.r <= area.right && bx.t >= area.top && bx.b <= area.bottom
         && !taken.some(o => overlaps(bx, o)) && !items.some(o => o !== it && onBubble(bx, o)));
      if (spot) { taken.push(spot); out.push({ id: it.id, text: it.text, box: spot }); }
    });
    return out;
  }

  // The members of one group, heaviest first, each with its share of the
  // group (the payload's weights are shares of the index).
  function membersOf(payload, level, row) {
    const all = (payload && payload.members) || [];
    const subs = new Map(((payload.levels || {}).sub || []).map(s => [s.id, s]));
    const inRow = m => {
      if (level === 'sub') return m.s === row.id;
      const s = subs.get(m.s);
      if (!s) return false;
      return level === 'group' ? s.group === row.id : s.sector === row.id;
    };
    const mine = all.filter(inRow);
    const total = mine.reduce((a, m) => a + (m.w || 0), 0);
    return mine.map(m => Object.assign({}, m, { share: total > 0 ? (m.w / total) * 100 : null }))
      .sort((a, b) => (b.w || 0) - (a.w || 0));
  }

  // The level below a sector or industry group, for its "Industries in it" list.
  function children(payload, level, row) {
    const lv = payload.levels || {};
    if (level === 'sector') return (lv.group || []).filter(g => g.sector === row.id);
    if (level === 'group') return (lv.sub || []).filter(s => s.group === row.id);
    return [];
  }

  const api = { LEVELS, WINDOWS, QUADS, quadrant, label, select, counts, verdict, bound, clamp,
                radius, colour, scaleOf, placeLabels, membersOf, children, percentile };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.SectorMap = api;
})(typeof window !== 'undefined' ? window : globalThis);
