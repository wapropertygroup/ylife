/*
 * static/smart_graph.js -- the network on /smart-money (星系图), after the one
 * on openbit.trade/people (asked for 2026-10-09).
 *
 * Stocks and the people who moved them, drawn as one graph. A stock is a disc
 * with a ring of three arcs, one per source -- 13F managers at the top, then
 * insiders, then House members, clockwise -- each split green and red by the
 * dollars that source bought and sold, so "two or more agree" reads as two
 * arcs of one colour, and a stock where they do (by count, the table's rule)
 * also glows. A person is a dot in the colour of what they
 * are (manager, insider, member). A link is coloured by what that person did
 * to that stock, dashed where it does not set the side (a hold, a 10b5-1 plan
 * sale, a purchase in an offering), and a dot travels along it the way the
 * money went: into the stock on a buy, out of it on a sale.
 *
 * Nothing here is a new figure. The links are each stock's ``who`` lists from
 * /api/smart-money (ystocker/smart_money.py), joined to the people by the id
 * every entry carries, plus each named manager's unchanged positions as holds.
 *
 * Three parts, the first two pure so tests/check_smart_graph.mjs can run them
 * in Node: build() turns the payload into nodes and links, layout() places
 * them (a seeded force simulation, so a reload draws the same picture), and
 * mount() draws them on a canvas and handles the pointer. Exposed as
 * window.SmartGraph, and as module.exports for Node.
 */
(function (root) {
  'use strict';

  const SRC = ['funds', 'insiders', 'house'];
  const KIND = { funds: 'fund', insiders: 'insider', house: 'house' };
  const CH_DIR = { new: 'buy', increased: 'buy', reduced: 'sell', unchanged: 'hold' };
  // How many stocks the graph draws: about one per 12,800 px² of canvas -- 44
  // on a laptop's 1010x560, 35 at 806x560, 66 in the bigger view at 1024x830
  // -- and never so few that a phone's picture is empty, nor so many it stops
  // being one (at 390px a 44-stock graph was a hairball of labels).
  const STOCK_AREA = 12800, MIN_STOCKS = 18, MAX_STOCKS = 90;
  // A focused person's stocks are added to the graph, up to this many.
  const MAX_FOCUS_EXTRA = 30;

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function hash(s) {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }

  // mulberry32: a small seeded generator, so the layout is the same each load.
  function rng(seed) {
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function maxStocks(width, height) {
    const area = Math.max(1, width || 0) * Math.max(1, height || 560);
    return clamp(Math.round(area / STOCK_AREA), MIN_STOCKS, MAX_STOCKS);
  }

  // ── building ──────────────────────────────────────────────────────────────

  function enabled(src) {
    if (!src) return SRC.slice();
    const has = s => (typeof src.has === 'function' ? src.has(s) : src.indexOf(s) >= 0);
    return SRC.filter(has);
  }

  const peopleOn = (row, src) => src.reduce((n, s) => n + ((row.src[s] && row.src[s].who) || []).length, 0);

  /**
   * Dollars a row's sources switched on moved: ``b`` into the stock, ``s``
   * out of it. Every trade counts, past the cut on ``who`` too; a 13F's is
   * the shares added or trimmed at the quarter-end price, a House trade's the
   * midpoint of its range (ystocker/smart_money.py).
   */
  function amountOn(row, src) {
    const out = { b: 0, s: 0 };
    src.forEach(k => {
      const x = row.src[k];
      if (x) { out.b += x.bought || 0; out.s += x.sold || 0; }
    });
    return out;
  }
  const grossOn = (row, src) => { const a = amountOn(row, src); return a.b + a.s; };

  // A row's agreement, but only if every source in it is switched on: the
  // table's "Same way" filter applies the same rule.
  function agreeOn(row, src) {
    const a = row.agree;
    return a && a.sources.every(s => src.indexOf(s) >= 0) ? a : null;
  }

  // The arc one source draws on a stock: its side, or what it did without
  // taking one (named managers holding it unchanged; insiders selling only
  // under a plan), or null where the source has nothing on it.
  function arcOf(x) {
    if (!x) return null;
    if (x.side === 'buy' || x.side === 'sell' || x.side === 'mixed') return x.side;
    if (x.hold) return 'hold';
    return 'none';
  }

  function personId(s, w, ticker) {
    // Payloads built before ids were added name a person by their name; an
    // insider's name alone can repeat across two companies.
    if (w.id) return w.id;
    return s + ':' + (w.name || '?') + (s === 'insiders' ? ':' + ticker : '');
  }

  /**
   * The rows a pick needs drawn: a picked stock, or every stock a picked
   * person moved (up to MAX_FOCUS_EXTRA), from the rows the table shows, or
   * from all of them if the filters hide every one.
   */
  function focusRows(data, rows, focus, srcOpt) {
    if (!focus) return [];
    const src = Array.isArray(srcOpt) ? srcOpt : enabled(srcOpt);
    const all = (data && data.tickers) || [];
    if (focus.t) {
      const r = (rows || []).find(x => x.t === focus.t) || all.find(x => x.t === focus.t);
      return r && peopleOn(r, src) > 0 ? [r] : [];
    }
    if (!focus.p) return [];
    const moved = r => src.some(s => r.src[s] && (r.src[s].who || []).some(w => personId(s, w, r.t) === focus.p));
    let out = (rows || []).filter(moved);
    if (!out.length) out = all.filter(moved);
    return out.slice(0, MAX_FOCUS_EXTRA);
  }

  /**
   * Nodes and links for the stocks in ``rows``, which are the rows the table
   * shows, already filtered. The graph keeps the ``max`` with the most people
   * on them, so the busiest stocks are the hubs the picture is made of, and
   * always the focus: a stock picked from the table, or every stock a picked
   * person moved.
   *
   *   opts.src    -- the sources switched on (array or Set)
   *   opts.max    -- how many stocks at most (focus additions aside)
   *   opts.focus  -- {t: ticker} or {p: person id} or null
   */
  function build(data, rows, opts) {
    const o = opts || {};
    const src = enabled(o.src);
    const max = o.max || 44;
    // By amount, the stocks the most money moved through; otherwise the ones
    // with the most people on them, which are the hubs.
    const byAmount = o.rank === 'amount';
    const all = (data && data.tickers) || [];
    const ranked = (rows || []).map((r, i) => ({ r, i, n: peopleOn(r, src), v: grossOn(r, src) }))
      .filter(x => x.n > 0)
      .sort((a, b) => (byAmount ? b.v - a.v : 0) || b.n - a.n
        || ((agreeOn(b.r, src) ? b.r.agree.sources.length : 0) - (agreeOn(a.r, src) ? a.r.agree.sources.length : 0))
        || a.i - b.i);
    const chosen = ranked.slice(0, max).map(x => x.r);
    const have = new Set(chosen.map(r => r.t));
    focusRows(data, rows, o.focus, src).forEach(r => { if (!have.has(r.t)) { chosen.push(r); have.add(r.t); } });

    const nodes = [], links = [], index = new Map();
    chosen.forEach(r => {
      const arcs = {}, split = {};
      SRC.forEach(s => {
        const on = src.indexOf(s) >= 0, x = r.src[s];
        arcs[s] = on ? arcOf(x) : null;
        // Each third is split green and red by the dollars bought and sold
        // (asked 2026-10-09: "drawn on amount instead of persons"); a third
        // with no dollars keeps its side's colour.
        const b = on && x ? x.bought || 0 : 0, sold = on && x ? x.sold || 0 : 0;
        split[s] = b + sold > 0 ? b / (b + sold) : null;
      });
      const agree = agreeOn(r, src);
      index.set('t:' + r.t, nodes.length);
      const amt = amountOn(r, src);
      nodes.push({ id: 't:' + r.t, type: 'stock', key: r.t, row: r, arcs, split,
                   agree: agree ? agree.side : null, split: !!r.split, moves: peopleOn(r, src), deg: 0,
                   bought: amt.b, sold: amt.s, amount: amt.b + amt.s });
    });
    const person = (pid, make) => {
      let pi = index.get('p:' + pid);
      if (pi == null) {
        pi = nodes.length;
        index.set('p:' + pid, pi);
        nodes.push(Object.assign({ id: 'p:' + pid, type: 'person', key: pid, moves: 0, deg: 0, amount: 0 }, make()));
      }
      return pi;
    };
    chosen.forEach(r => {
      const si = index.get('t:' + r.t);
      src.forEach(s => {
        const x = r.src[s];
        const who = (x && x.who) || [];
        if (!who.length) return;
        if (s === 'insiders') {
          // A company's insiders are one node beside it. Each of them is a
          // leaf on one stock, and drawn one by one they were most of the
          // picture -- 545 of 1,258 links on the day this was built -- while
          // saying nothing the stock's own arc does not.
          const side = x.side === 'buy' || x.side === 'sell' || x.side === 'mixed' ? x.side : null;
          const pi = person('ins@' + r.t, () => ({ kind: 'insider', group: true, name: r.t, name_zh: null,
                                                     sub: '', ticker: r.t, who, count: who.length }));
          const dirs = new Set(who.map(w => w.dir));
          const dir = side || (dirs.size === 1 ? who[0].dir : 'mixed');
          const b = x.bought || 0, sold = x.sold || 0;
          links.push({ a: si, b: pi, src: s, dir, soft: !side, ch: null,
                       v: b || sold ? b - sold : null, gross: b + sold, bought: b, sold });
          nodes[pi].moves = who.length;
          nodes[pi].amount = b + sold;
          return;
        }
        who.forEach(w => {
          const pi = person(personId(s, w, r.t), () => ({ kind: KIND[s], name: w.name || '?', name_zh: w.name_zh || null,
                                                          sub: w.org || '', ticker: null }));
          const dir = s === 'funds' ? (CH_DIR[w.ch] || 'hold') : (w.dir || 'none');
          const v = typeof w.v === 'number' ? w.v : null;
          links.push({ a: si, b: pi, src: s, dir, soft: !!(w.plan || w.offering) || dir === 'hold', ch: w.ch || null,
                       v, gross: v == null ? 0 : Math.abs(v), pct: w.pct != null ? w.pct : null,
                       lo: w.lo || null, hi: w.hi || null });
          nodes[pi].moves++;
          nodes[pi].amount += v == null ? 0 : Math.abs(v);
        });
      });
    });
    // A named manager's unchanged positions, between nodes already drawn: a
    // hold says who else is in the stock, but does not pull a stock or a
    // manager into the picture on its own.
    if (src.indexOf('funds') >= 0) {
      const joined = new Set(links.map(l => l.a + ':' + l.b));
      ((data && data.people) || []).forEach(p => {
        if (p.kind !== 'fund') return;
        const pi = index.get('p:' + p.id);
        if (pi == null) return;
        (p.actions || []).forEach(a => {
          if (a.dir !== 'hold' || !a.t) return;
          const si = index.get('t:' + a.t);
          if (si == null || joined.has(si + ':' + pi)) return;
          joined.add(si + ':' + pi);
          links.push({ a: si, b: pi, src: 'funds', dir: 'hold', soft: true, ch: 'unchanged', v: null, gross: 0 });
        });
      });
    }
    links.forEach(l => { nodes[l.a].deg++; nodes[l.b].deg++; });
    // A disc's size is what the graph is ranked by: people, or dollars (on a
    // square-root scale, so area follows the amount).
    const top = Math.max(1, ...nodes.filter(n => n.type === 'stock').map(n => n.amount || 0));
    nodes.forEach(n => {
      n.r = n.type !== 'stock' ? clamp(2.6 + 1.2 * Math.sqrt(n.moves), 3.2, 7) + (n.kind === 'fund' ? 0.9 : 0)
        : byAmount ? 10 + 16 * Math.sqrt((n.amount || 0) / top)
        : clamp(6 + 3 * Math.sqrt(n.moves), 10, 26);
    });
    // A link's width is its dollars against the largest drawn.
    const most = Math.max(1, ...links.map(l => l.gross || 0));
    links.forEach(l => { l.weight = l.gross ? Math.sqrt(l.gross / most) : 0; });
    return {
      nodes, links,
      stocks: chosen.length,
      people: nodes.length - chosen.length,
      // How many rows had anyone on them, so the page can say the graph is a cut.
      total: ranked.length,
      rank: byAmount ? 'amount' : 'people',
    };
  }

  // ── layout ────────────────────────────────────────────────────────────────

  // The simulation's constants, overridable through opts.tune for tuning.
  const TUNE = {
    leaf: 14, bridge: 30, holdPull: 0.3,
    stockCharge: 300, stockChargeK: 30, personCharge: 46,
    stockPad: 7, personPad: 3, gravity: 0.1, range: 300,
  };

  /**
   * A force simulation in d3's manner (velocity Verlet, a cooling alpha):
   * links pull a person towards each stock they moved, every pair repels,
   * overlapping discs push apart, and a weak gravity keeps the whole near the
   * middle, stronger vertically so a wide canvas is filled side to side.
   * Seeded, so the same graph lands the same way every load.
   *
   *   opts.width, opts.height -- the canvas the result will be fitted to
   *   opts.prev  -- Map of node id -> {x, y} from the graph before, so a node
   *                 that stays keeps its place and the picture changes rather
   *                 than being redrawn from scratch
   */
  function createSim(g, opts) {
    const o = opts || {};
    const nodes = g.nodes, links = g.links, N = nodes.length;
    const W = o.width || 900, H = o.height || 560;
    const aspect = clamp(W / H, 0.6, 2.6);
    const random = rng(o.seed || 7);
    const prev = o.prev || null;

    const vx = new Float64Array(N), vy = new Float64Array(N);
    const count = new Float64Array(N);
    links.forEach(l => { count[l.a] += 1; count[l.b] += 1; });

    // Starting places: stocks on a sunflower spiral, busiest in the middle,
    // stretched to the canvas's shape; a person at the middle of the stocks
    // they moved, a little out along an angle fixed by their id.
    const stocks = nodes.map((n, i) => i).filter(i => nodes[i].type === 'stock')
      .sort((a, b) => nodes[b].moves - nodes[a].moves || (nodes[a].id < nodes[b].id ? -1 : 1));
    const step = Math.sqrt((W * H * 0.5) / Math.max(1, stocks.length) / Math.PI);
    const placed = n => typeof n.x === 'number' && isFinite(n.x) && typeof n.y === 'number' && isFinite(n.y);
    stocks.forEach((i, k) => {
      const n = nodes[i], p = prev && prev.get(n.id);
      if (p) { n.x = p.x; n.y = p.y; return; }
      if (placed(n)) return;
      const ang = k * 2.399963229728653, rad = step * Math.sqrt(k + 0.5) * 1.1;
      n.x = Math.cos(ang) * rad * Math.sqrt(aspect);
      n.y = Math.sin(ang) * rad / Math.sqrt(aspect);
    });
    const adj = nodes.map(() => []);
    links.forEach(l => { adj[l.b].push(l.a); adj[l.a].push(l.b); });
    nodes.forEach((n, i) => {
      if (n.type !== 'person') return;
      const p = prev && prev.get(n.id);
      if (p) { n.x = p.x; n.y = p.y; return; }
      if (placed(n)) return;
      let x = 0, y = 0, k = 0, r = 0;
      adj[i].forEach(j => { if (nodes[j].type === 'stock') { x += nodes[j].x; y += nodes[j].y; k++; r = Math.max(r, nodes[j].r); } });
      const ang = (hash(n.id) % 6283) / 1000;
      const out = k === 1 ? r + 22 : 6;
      n.x = (k ? x / k : 0) + Math.cos(ang) * out;
      n.y = (k ? y / k : 0) + Math.sin(ang) * out;
    });

    // A person on one stock orbits it close; one between several stocks needs
    // the room to sit between them.
    const T = Object.assign({}, TUNE, o.tune || {});
    const dist = links.map(l => nodes[l.a].r + nodes[l.b].r + (count[l.b] <= 1 ? T.leaf : T.bridge));
    const strength = links.map(l => (l.dir === 'hold' ? T.holdPull : 1) / Math.min(count[l.a], count[l.b]));
    const bias = links.map(l => count[l.a] / (count[l.a] + count[l.b]));
    const charge = nodes.map(n => (n.type === 'stock' ? T.stockCharge + T.stockChargeK * Math.sqrt(n.moves) : T.personCharge));
    const pad = nodes.map(n => (n.type === 'stock' ? n.r + T.stockPad : n.r + T.personPad));
    const gx = T.gravity, gy = T.gravity * aspect;
    const maxD2 = Math.pow(T.range, 2);

    const sim = { alpha: o.alpha != null ? o.alpha : 1, alphaMin: 0.003 };
    const iterations = o.iterations || 300;
    sim.decay = 1 - Math.pow(sim.alphaMin, 1 / iterations);

    function tick() {
      const alpha = sim.alpha;
      for (let i = 0; i < links.length; i++) {
        const l = links[i], A = nodes[l.a], B = nodes[l.b];
        let dx = B.x + vx[l.b] - A.x - vx[l.a], dy = B.y + vy[l.b] - A.y - vy[l.a];
        let d = Math.sqrt(dx * dx + dy * dy);
        if (d < 1e-6) { dx = (random() - 0.5) * 1e-3; dy = (random() - 0.5) * 1e-3; d = Math.sqrt(dx * dx + dy * dy); }
        const f = (d - dist[i]) / d * alpha * strength[i];
        dx *= f; dy *= f;
        vx[l.b] -= dx * bias[i]; vy[l.b] -= dy * bias[i];
        vx[l.a] += dx * (1 - bias[i]); vy[l.a] += dy * (1 - bias[i]);
      }
      for (let i = 0; i < N; i++) {
        const A = nodes[i];
        for (let j = i + 1; j < N; j++) {
          const B = nodes[j];
          let dx = B.x - A.x, dy = B.y - A.y, l = dx * dx + dy * dy;
          if (l < 1e-6) { dx = (random() - 0.5) * 1e-2; dy = (random() - 0.5) * 1e-2; l = dx * dx + dy * dy; }
          if (l < maxD2) {
            const w = alpha / Math.max(l, 36);
            vx[i] -= dx * charge[j] * w; vy[i] -= dy * charge[j] * w;
            vx[j] += dx * charge[i] * w; vy[j] += dy * charge[i] * w;
          }
          const r = pad[i] + pad[j];
          if (l < r * r) {
            const d = Math.sqrt(l), push = (r - d) / d * 0.5;
            const ri = pad[j] * pad[j] / (pad[i] * pad[i] + pad[j] * pad[j]);
            vx[i] -= dx * push * ri; vy[i] -= dy * push * ri;
            vx[j] += dx * push * (1 - ri); vy[j] += dy * push * (1 - ri);
          }
        }
      }
      for (let i = 0; i < N; i++) {
        const n = nodes[i];
        vx[i] -= n.x * gx * alpha; vy[i] -= n.y * gy * alpha;
        if (n.fx != null) { n.x = n.fx; n.y = n.fy; vx[i] = 0; vy[i] = 0; continue; }
        vx[i] *= 0.6; vy[i] *= 0.6;
        n.x += vx[i]; n.y += vy[i];
      }
      sim.alpha += (0 - sim.alpha) * sim.decay;
    }
    sim.tick = function (n) { for (let k = 0; k < (n || 1); k++) tick(); return sim; };
    sim.run = function () { while (sim.alpha > sim.alphaMin) tick(); return sim; };
    sim.reheat = function (a) { sim.alpha = Math.max(sim.alpha, a); return sim; };
    return sim;
  }

  function layout(g, opts) {
    if (!g.nodes.length) return g;
    createSim(g, opts).run();
    return g;
  }

  function bounds(nodes) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    nodes.forEach(n => {
      x0 = Math.min(x0, n.x - n.r); y0 = Math.min(y0, n.y - n.r);
      x1 = Math.max(x1, n.x + n.r); y1 = Math.max(y1, n.y + n.r);
    });
    return nodes.length ? { x0, y0, x1, y1 } : { x0: -1, y0: -1, x1: 1, y1: 1 };
  }

  // The camera that shows every node with ``pad`` pixels to spare; labels hang
  // off the right of a dot, so the right side gets more.
  function fit(b, W, H, pad) {
    const p = pad == null ? 24 : pad;
    const bw = Math.max(1, b.x1 - b.x0), bh = Math.max(1, b.y1 - b.y0);
    const k = clamp(Math.min((W - 2 * p - 40) / bw, (H - 2 * p) / bh), 0.25, 2.2);
    return { x: (b.x0 + b.x1) / 2 + 20 / k, y: (b.y0 + b.y1) / 2, k };
  }

  // ── labels ────────────────────────────────────────────────────────────────

  /**
   * Greedy placement in screen space: candidates in priority order, each tried
   * right of its dot, then left, below and above, and dropped if every spot
   * overlaps a label already placed, a stock disc, or the canvas edge.
   * ``cands`` items are {id, x, y, r, w, h}; returns the placed ones with
   * {lx, ly, align} added.
   */
  function placeLabels(cands, blockers, W, H) {
    const boxes = [];
    const hit = (b, own) => {
      if (b.x0 < 2 || b.y0 < 2 || b.x1 > W - 2 || b.y1 > H - 2) return true;
      for (const o of boxes) if (b.x0 < o.x1 && b.x1 > o.x0 && b.y0 < o.y1 && b.y1 > o.y0) return true;
      for (const o of blockers) {
        if (o.id === own) continue;
        const cx = clamp(o.x, b.x0, b.x1), cy = clamp(o.y, b.y0, b.y1);
        if ((cx - o.x) * (cx - o.x) + (cy - o.y) * (cy - o.y) < o.r * o.r) return true;
      }
      return false;
    };
    const out = [];
    cands.forEach(c => {
      const g = 3 + c.r;
      const spots = [
        { lx: c.x + g, ly: c.y, align: 'left', b: { x0: c.x + g, y0: c.y - c.h / 2, x1: c.x + g + c.w, y1: c.y + c.h / 2 } },
        { lx: c.x - g, ly: c.y, align: 'right', b: { x0: c.x - g - c.w, y0: c.y - c.h / 2, x1: c.x - g, y1: c.y + c.h / 2 } },
        { lx: c.x, ly: c.y + g + c.h / 2, align: 'center', b: { x0: c.x - c.w / 2, y0: c.y + g, x1: c.x + c.w / 2, y1: c.y + g + c.h } },
        { lx: c.x, ly: c.y - g - c.h / 2, align: 'center', b: { x0: c.x - c.w / 2, y0: c.y - g - c.h, x1: c.x + c.w / 2, y1: c.y - g } },
      ];
      for (const s of spots) {
        if (hit(s.b, c.id)) continue;
        boxes.push(s.b);
        out.push(Object.assign({}, c, { lx: s.lx, ly: s.ly, align: s.align }));
        return;
      }
    });
    return out;
  }

  // ── drawing ───────────────────────────────────────────────────────────────

  const PAL = {
    dark: {
      buy: '#34d399', sell: '#fb7185', mixed: '#fbbf24', hold: '#64748b', none: '#64748b',
      fund: '#a78bfa', insider: '#e2e8f0', house: '#38bdf8',
      disc: '#0a1020', track: 'rgba(148,163,184,0.16)', ticker: '#f1f5f9', sub: '#94a3b8',
      label: '#cbd5e1', labelStrong: '#f8fafc', halo: 'rgba(5,8,18,0.85)', star: 'rgba(203,213,225,',
      linkA: 0.34, linkDim: 0.05, nodeDim: 0.22, ring: '#f1f5f9',
    },
    light: {
      buy: '#059669', sell: '#e11d48', mixed: '#d97706', hold: '#94a3b8', none: '#94a3b8',
      fund: '#7c3aed', insider: '#64748b', house: '#0284c7',
      disc: '#ffffff', track: 'rgba(100,116,139,0.16)', ticker: '#0f172a', sub: '#64748b',
      label: '#334155', labelStrong: '#0f172a', halo: 'rgba(255,255,255,0.9)', star: null,
      linkA: 0.42, linkDim: 0.06, nodeDim: 0.25, ring: '#0f172a',
    },
  };
  // The three arcs, clockwise from the top: 13F, insiders, House.
  const ARC_START = { funds: -Math.PI / 2, insiders: Math.PI / 6, house: Math.PI * 5 / 6 };
  const ARC_GAP = 0.16;
  const MONO = '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace';
  const SANS = 'system-ui, -apple-system, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif';

  const hasCJK = s => /[㐀-鿿]/.test(s || '');

  // '#34d399' at alpha a. A gradient that fades to transparent black instead
  // of transparent colour leaves a grey fringe on a white card.
  function rgba(hex, a) {
    const h = hex.replace('#', '');
    const v = parseInt(h.length === 3 ? h.replace(/(.)/g, '$1$1') : h, 16);
    return `rgba(${(v >> 16) & 255},${(v >> 8) & 255},${v & 255},${a})`;
  }

  // What a stock's disc says in the middle: the ticker, less any exchange
  // suffix (7203.T), which the tooltip carries.
  const tickerLabel = key => key.replace(/\..*$/, '');

  /**
   * Draws a graph into ``host`` and wires the pointer. ``opts``:
   *   onPick(node|null)    -- a click on a node, or on nothing
   *   tooltip(node, g)     -- the tooltip's HTML (already escaped)
   *   label(node)          -- a person's label text
   *   sub(node)            -- the line under a stock's ticker, or ''
   *   amount(link)         -- a link's dollars as text ('' for none), shown
   *                           beside what is lit
   * Returns {set(g, {focus}), focus(id), zoom(f), fit(), redraw(), destroy()}.
   */
  function mount(host, opts) {
    const o = opts || {};
    const doc = host.ownerDocument, win = doc.defaultView;
    const canvas = doc.createElement('canvas');
    canvas.className = 'sg-canvas';
    host.appendChild(canvas);
    const tip = doc.createElement('div');
    tip.className = 'sg-tip';
    tip.hidden = true;
    host.appendChild(tip);
    const ctx = canvas.getContext('2d');
    const reduced = !!(win.matchMedia && win.matchMedia('(prefers-reduced-motion: reduce)').matches);
    const now = () => (win.performance ? win.performance.now() : Date.now());

    let W = 0, H = 0, dpr = 1;
    let g = null, nbr = [], pairs = new Map();
    let cam = { x: 0, y: 0, k: 1 }, camAnim = null, userCam = false;
    let focusId = null, hoverId = null;
    let labels = [], labelsKey = '';
    let tween = null, sim = null;
    let stars = [];
    let raf = 0, visible = true, lastFrame = 0;
    const widths = new Map();

    const pal = () => (doc.documentElement.classList.contains('dark') ? PAL.dark : PAL.light);
    const sx = x => (x - cam.x) * cam.k + W / 2;
    const sy = y => (y - cam.y) * cam.k + H / 2;
    const wx = x => (x - W / 2) / cam.k + cam.x;
    const wy = y => (y - H / 2) / cam.k + cam.y;
    // Discs grow more slowly than the distances between them as the camera
    // zooms, so a zoomed-out graph keeps readable tickers.
    const scale = () => Math.sqrt(cam.k);
    const rOf = n => (n.type === 'stock' ? n.r * scale() : Math.max(2.2, n.r * scale()));

    function measure(text, font) {
      const key = font + '|' + text;
      let w = widths.get(key);
      if (w == null) { ctx.font = font; w = ctx.measureText(text).width; widths.set(key, w); }
      return w;
    }

    // The host's size; true if it changed.
    function measureHost() {
      const r = host.getBoundingClientRect();
      const w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
      const d = Math.min(2, win.devicePixelRatio || 1);
      if (w === W && h === H && d === dpr) return false;
      W = w; H = h; dpr = d;
      canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
      const random = rng(hash('stars' + W + 'x' + H));
      stars = Array.from({ length: Math.round(W * H / 5200) },
        () => ({ x: random() * W, y: random() * H, r: random() * 0.9 + 0.3, a: random() * 0.45 + 0.1 }));
      labelsKey = '';
      return true;
    }
    function resize() {
      if (!measureHost()) return;
      if (g && !userCam) cam = fit(bounds(g.nodes), W, H);
      kick(true);
    }

    function index() {
      nbr = g.nodes.map(() => new Set());
      pairs = new Map();
      g.links.forEach(l => {
        nbr[l.a].add(l.b); nbr[l.b].add(l.a);
        // A hold beside a move is never drawn, so one link per pair.
        if (!pairs.has(l.a + ':' + l.b) || l.dir !== 'hold') pairs.set(l.a + ':' + l.b, l);
        const h = hash(g.nodes[l.a].id + '>' + g.nodes[l.b].id);
        l.bend = ((h & 1) ? 1 : -1) * (0.07 + (h % 7) / 100);
        l.phase = (h % 1000) / 1000;
        l.speed = 0.08 + ((h >>> 3) % 7) / 100;
      });
    }

    const byId = id => (g && id != null ? g.nodes.findIndex(n => n.id === id) : -1);
    const active = () => (hoverId != null ? hoverId : focusId);

    function set(next, setOpts) {
      const so = setOpts || {};
      const old = g && g.nodes.length ? new Map(g.nodes.map(n => [n.id, { x: n.x, y: n.y }])) : null;
      measureHost();
      g = next;
      sim = null;
      createSim(g, { width: W > 1 ? W : 900, height: H > 1 ? H : 560, prev: old, alpha: old ? 0.6 : 1, tune: o.tune }).run();
      index();
      focusId = byId(so.focus) >= 0 ? so.focus : null;
      hoverId = null;
      tip.hidden = true;
      labelsKey = '';
      userCam = false;
      const target = fit(bounds(g.nodes), W, H);
      if (old && !reduced) {
        // A node that stays slides from where it was; a new one fades in
        // where it lands.
        const to = new Map(g.nodes.map(n => [n.id, { x: n.x, y: n.y }]));
        tween = { t0: now(), dur: 560, from: old, to };
        g.nodes.forEach(n => { const p = old.get(n.id); if (p) { n.x = p.x; n.y = p.y; } n.fade = p ? 1 : 0; });
        camAnim = { t0: now(), dur: 560, from: Object.assign({}, cam), to: target };
      } else {
        tween = null;
        camAnim = null;
        cam = target;
      }
      kick(true);
    }

    function focus(id) {
      focusId = byId(id) >= 0 ? id : null;
      labelsKey = '';
      if (focusId != null && !tween) {
        // Bring it into view if a pan or a zoom has put it off the canvas.
        const n = g.nodes[byId(focusId)];
        const x = sx(n.x), y = sy(n.y);
        if (x < 30 || x > W - 30 || y < 30 || y > H - 30) animateCam({ x: n.x, y: n.y, k: cam.k });
      }
      kick(true);
      return focusId != null;
    }

    function animateCam(to) {
      if (reduced) { cam = to; kick(true); return; }
      camAnim = { t0: now(), dur: 420, from: Object.assign({}, cam), to };
      kick(true);
    }

    function zoom(f, px, py) {
      if (!g) return;
      const x = px == null ? W / 2 : px, y = py == null ? H / 2 : py;
      const k = clamp(cam.k * f, 0.2, 6);
      const ax = wx(x), ay = wy(y);
      cam = { k, x: ax - (x - W / 2) / k, y: ay - (y - H / 2) / k };
      userCam = true; camAnim = null; labelsKey = '';
      kick(true);
    }

    function refit() {
      if (!g) return;
      userCam = false;
      animateCam(fit(bounds(g.nodes), W, H));
    }

    // ── the frame ──
    const ease = t => 1 - Math.pow(1 - t, 3);

    function advance(t) {
      let moving = false;
      if (tween) {
        const k = clamp((t - tween.t0) / tween.dur, 0, 1), e = ease(k);
        g.nodes.forEach(n => {
          const a = tween.from.get(n.id), b = tween.to.get(n.id);
          if (a) { n.x = a.x + (b.x - a.x) * e; n.y = a.y + (b.y - a.y) * e; } else { n.x = b.x; n.y = b.y; n.fade = e; }
        });
        if (k >= 1) { tween = null; g.nodes.forEach(n => { n.fade = 1; }); }
        moving = true; labelsKey = '';
      }
      if (camAnim) {
        const k = clamp((t - camAnim.t0) / camAnim.dur, 0, 1), e = ease(k);
        const a = camAnim.from, b = camAnim.to;
        cam = { x: a.x + (b.x - a.x) * e, y: a.y + (b.y - a.y) * e, k: a.k + (b.k - a.k) * e };
        if (k >= 1) camAnim = null;
        moving = true; labelsKey = '';
      }
      if (sim && !tween && sim.alpha > sim.alphaMin) { sim.tick(1); moving = true; labelsKey = ''; }
      return moving;
    }

    function curve(l) {
      // From the person to the stock, bowed a little to one side.
      const A = g.nodes[l.a], B = g.nodes[l.b];
      const x0 = sx(B.x), y0 = sy(B.y), x1 = sx(A.x), y1 = sy(A.y);
      const mx = (x0 + x1) / 2, my = (y0 + y1) / 2, dx = x1 - x0, dy = y1 - y0;
      return { x0, y0, x1, y1, cx: mx - dy * l.bend, cy: my + dx * l.bend };
    }
    const at = (c, t) => {
      const u = 1 - t;
      return [u * u * c.x0 + 2 * u * t * c.cx + t * t * c.x1, u * u * c.y0 + 2 * u * t * c.cy + t * t * c.y1];
    };
    const fadeOf = n => (n.fade == null ? 1 : n.fade);

    function draw(t) {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      if (!g || W < 2) return;
      const P = pal();
      const ai = byId(active());
      const lit = ai >= 0 ? nbr[ai] : null;
      const isLit = i => ai < 0 || i === ai || lit.has(i);
      if (P.star) stars.forEach(s => { ctx.fillStyle = P.star + s.a + ')'; ctx.fillRect(s.x, s.y, s.r, s.r); });

      // Links, then the dots travelling along them.
      ctx.lineCap = 'round';
      g.links.forEach(l => {
        const on = ai < 0 || l.a === ai || l.b === ai;
        const c = curve(l);
        ctx.globalAlpha = (on ? (ai >= 0 ? 0.92 : P.linkA) : P.linkDim) * Math.min(fadeOf(g.nodes[l.a]), fadeOf(g.nodes[l.b]));
        ctx.strokeStyle = P[l.dir] || P.none;
        // Wider for more dollars (l.weight, square-root of its share of the
        // largest drawn), so the big adds and trims stand out.
        ctx.lineWidth = (on && ai >= 0 ? 1.5 : 0.9) + l.weight * (on && ai >= 0 ? 3.2 : 2.4);
        ctx.setLineDash(l.soft ? [3, 4] : []);
        ctx.beginPath(); ctx.moveTo(c.x0, c.y0); ctx.quadraticCurveTo(c.cx, c.cy, c.x1, c.y1); ctx.stroke();
      });
      ctx.setLineDash([]);
      if (!reduced) {
        const secs = t / 1000;
        g.links.forEach(l => {
          if (l.dir === 'hold' || l.dir === 'none' || l.soft) return;
          if (ai >= 0 && l.a !== ai && l.b !== ai) return;
          const c = curve(l);
          ctx.fillStyle = P[l.dir];
          // Into the stock on a buy, out of it on a sale; both on a mix.
          const ways = l.dir === 'mixed' ? [1, -1] : [l.dir === 'buy' ? 1 : -1];
          ways.forEach((way, k) => {
            const u = (secs * l.speed + l.phase + k * 0.5) % 1;
            const [x, y] = at(c, way > 0 ? u : 1 - u);
            ctx.globalAlpha = (ai >= 0 ? 1 : 0.75) * Math.sqrt(Math.sin(Math.PI * u)) * Math.min(fadeOf(g.nodes[l.a]), fadeOf(g.nodes[l.b]));
            ctx.beginPath(); ctx.arc(x, y, ai >= 0 ? 2.1 : 1.6, 0, Math.PI * 2); ctx.fill();
          });
        });
      }

      // People under stocks, so a hub's disc is never hidden by its crowd.
      g.nodes.forEach((n, i) => {
        if (n.type !== 'person') return;
        const x = sx(n.x), y = sy(n.y), r = rOf(n);
        ctx.globalAlpha = (isLit(i) ? 1 : P.nodeDim) * fadeOf(n);
        ctx.fillStyle = P[n.kind];
        ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
        if (i === ai) { ctx.strokeStyle = P.ring; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(x, y, r + 3, 0, Math.PI * 2); ctx.stroke(); }
      });
      g.nodes.forEach((n, i) => {
        if (n.type !== 'stock') return;
        const x = sx(n.x), y = sy(n.y), r = rOf(n);
        const alpha = (isLit(i) ? 1 : P.nodeDim) * fadeOf(n);
        if (n.agree || i === ai) {
          // Two or more sources on one side glow that side's colour.
          const col = i === ai && !n.agree ? P.ring : P[n.agree];
          const grd = ctx.createRadialGradient(x, y, r * 0.85, x, y, r * 2.2);
          grd.addColorStop(0, rgba(col, (i === ai ? 0.42 : 0.3) * alpha));
          grd.addColorStop(1, rgba(col, 0));
          ctx.globalAlpha = 1;
          ctx.fillStyle = grd; ctx.beginPath(); ctx.arc(x, y, r * 2.2, 0, Math.PI * 2); ctx.fill();
        }
        ctx.globalAlpha = alpha;
        ctx.fillStyle = P.disc;
        ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
        const lw = Math.max(2.6, r * 0.22);
        SRC.forEach(s => {
          const a0 = ARC_START[s] + ARC_GAP / 2, a1 = ARC_START[s] + Math.PI * 2 / 3 - ARC_GAP / 2;
          const side = n.arcs[s], f = n.split ? n.split[s] : null;
          ctx.lineWidth = side ? lw : lw * 0.55;
          if (side && f != null) {
            // Bought, clockwise from the third's start, then sold.
            const mid = a0 + (a1 - a0) * f;
            if (f > 0) { ctx.strokeStyle = P.buy; ctx.beginPath(); ctx.arc(x, y, r - lw / 2, a0, mid); ctx.stroke(); }
            if (f < 1) { ctx.strokeStyle = P.sell; ctx.beginPath(); ctx.arc(x, y, r - lw / 2, mid, a1); ctx.stroke(); }
            return;
          }
          ctx.strokeStyle = side ? (P[side] || P.none) : P.track;
          ctx.beginPath(); ctx.arc(x, y, r - lw / 2, a0, a1); ctx.stroke();
        });
        const label = tickerLabel(n.key);
        const font = px => `700 ${px}px ${MONO}`;
        let fs = clamp(r * 0.6, 8, 13);
        while (fs > 6.5 && measure(label, font(fs)) > r * 1.5) fs -= 0.5;
        const sub = r >= 17 && o.sub ? o.sub(n) : '';
        ctx.fillStyle = P.ticker;
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.font = font(fs);
        ctx.fillText(label, x, sub ? y - fs * 0.34 : y + 0.5);
        if (sub) {
          ctx.font = `500 ${clamp(r * 0.36, 8, 10)}px ${SANS}`;
          ctx.fillStyle = P.sub;
          ctx.fillText(sub, x, y + fs * 0.66);
        }
        if (i === ai) { ctx.strokeStyle = P.ring; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(x, y, r + 4, 0, Math.PI * 2); ctx.stroke(); }
      });
      ctx.globalAlpha = 1;
      drawLabels(P, ai, lit);
    }

    // Labels are placed again only when what they depend on moves.
    function drawLabels(P, ai, lit) {
      const key = [ai, cam.x.toFixed(1), cam.y.toFixed(1), cam.k.toFixed(3), W, H, doc.documentElement.lang].join('|');
      const font = `500 11px ${SANS}`;
      if (key !== labelsKey) {
        labelsKey = key;
        const cands = [];
        g.nodes.forEach((n, i) => {
          if (n.type !== 'person') return;
          const near = ai >= 0 && (i === ai || lit.has(i));
          // With nothing picked the managers and the busier members are
          // named; the rest, mostly one-company insiders, wait for a pick.
          const pri = near ? (i === ai ? 5000 : 1000 + n.moves)
            : n.kind === 'fund' ? 200 + n.moves
            : n.kind === 'house' && n.moves >= 2 ? 100 + n.moves : -1;
          if (pri < 0) return;
          let text = o.label ? o.label(n) : n.name;
          if (!text) return;
          // A lit stock's people say how much each moved.
          const l = near && i !== ai && g.nodes[ai].type === 'stock' ? pairs.get(ai + ':' + i) : null;
          const amt = l && o.amount ? o.amount(l) : '';
          if (amt) text += '  ' + amt;
          cands.push({ id: n.id, text, pri, near, x: sx(n.x), y: sy(n.y), r: rOf(n), w: measure(text, font), h: 13 });
        });
        // A lit person's stocks say how much went into or out of each.
        if (ai >= 0 && g.nodes[ai].type === 'person' && o.amount) {
          lit.forEach(j => {
            const l = pairs.get(j + ':' + ai), n = g.nodes[j];
            const text = l && n.type === 'stock' ? o.amount(l) : '';
            if (!text) return;
            cands.push({ id: n.id, text, pri: 4000 + (l.gross || 0) / 1e12, near: true, color: P[l.dir] || null,
                         x: sx(n.x), y: sy(n.y), r: rOf(n) + 2, w: measure(text, font), h: 13 });
          });
        }
        cands.sort((a, b) => b.pri - a.pri);
        // With something lit, a dimmed disc does not keep a lit name off the
        // canvas: the label's halo reads over it.
        const blockers = g.nodes.filter((n, i) => n.type === 'stock' && (ai < 0 || i === ai || lit.has(i)))
          .map(n => ({ id: n.id, x: sx(n.x), y: sy(n.y), r: rOf(n) + 2 }));
        labels = placeLabels(cands, blockers, W, H);
      }
      ctx.font = font;
      ctx.textBaseline = 'middle';
      ctx.lineJoin = 'round';
      labels.forEach(lb => {
        ctx.globalAlpha = ai >= 0 && !lb.near ? 0.2 : 1;
        ctx.textAlign = lb.align;
        ctx.lineWidth = 3; ctx.strokeStyle = P.halo;
        ctx.strokeText(lb.text, lb.lx, lb.ly);
        ctx.fillStyle = lb.color || (lb.near ? P.labelStrong : P.label);
        ctx.fillText(lb.text, lb.lx, lb.ly);
      });
      ctx.globalAlpha = 1;
    }

    function frame(t) {
      raf = 0;
      if (!visible || doc.hidden) return;
      const moving = advance(t);
      // The travelling dots alone are drawn about 30 times a second; a
      // transition gets every frame.
      if (moving || t - lastFrame > 32) { draw(t); lastFrame = t; }
      if (moving || !reduced) raf = win.requestAnimationFrame(frame);
    }
    // ``now``: draw this frame even if a loop is already running.
    function kick(force) {
      if (force) lastFrame = 0;
      if (!visible || doc.hidden) { draw(now()); return; }
      if (!raf) raf = win.requestAnimationFrame(frame);
    }

    // ── the pointer ──
    function pickAt(px, py) {
      if (!g) return -1;
      // People first: they are small and sit over the links.
      for (let pass = 0; pass < 2; pass++) {
        let best = -1, bd = Infinity;
        g.nodes.forEach((n, i) => {
          if ((pass === 0) !== (n.type === 'person')) return;
          const r = rOf(n) + (n.type === 'person' ? 5 : 2);
          const dx = sx(n.x) - px, dy = sy(n.y) - py, d = dx * dx + dy * dy;
          if (d <= r * r && d < bd) { bd = d; best = i; }
        });
        if (best >= 0) return best;
      }
      return -1;
    }
    function local(e) {
      const r = canvas.getBoundingClientRect();
      return [e.clientX - r.left, e.clientY - r.top];
    }
    function showTip(i, px, py) {
      if (i < 0 || !o.tooltip) { tip.hidden = true; return; }
      tip.innerHTML = o.tooltip(g.nodes[i], g);
      tip.hidden = false;
      const tw = tip.offsetWidth, th = tip.offsetHeight;
      let x = px + 16, y = py + 16;
      if (x + tw > W - 6) x = Math.max(6, px - tw - 16);
      if (y + th > H - 6) y = Math.max(6, py - th - 16);
      tip.style.left = x + 'px'; tip.style.top = y + 'px';
    }

    let drag = null;
    canvas.addEventListener('pointerdown', e => {
      if (!g || (e.button != null && e.button !== 0)) return;
      const [px, py] = local(e);
      drag = { i: pickAt(px, py), x0: px, y0: py, moved: false, id: e.pointerId, cam: Object.assign({}, cam) };
    });
    canvas.addEventListener('pointermove', e => {
      if (!g) return;
      const [px, py] = local(e);
      if (drag && drag.id === e.pointerId) {
        if (!drag.moved && Math.hypot(px - drag.x0, py - drag.y0) > 4) {
          drag.moved = true;
          try { canvas.setPointerCapture(e.pointerId); } catch (_) { /* the pointer is gone */ }
          tip.hidden = true;
          canvas.classList.add('is-dragging');
        }
        if (drag.moved) {
          if (drag.i >= 0) {
            const n = g.nodes[drag.i];
            n.fx = wx(px); n.fy = wy(py);
            if (!sim) sim = createSim(g, { width: W, height: H, alpha: 0.3, iterations: 140, tune: o.tune });
            sim.reheat(0.3);
          } else {
            cam = { k: cam.k, x: drag.cam.x - (px - drag.x0) / cam.k, y: drag.cam.y - (py - drag.y0) / cam.k };
            userCam = true; camAnim = null;
          }
          labelsKey = '';
          kick(true);
        }
        return;
      }
      const i = pickAt(px, py);
      const id = i >= 0 ? g.nodes[i].id : null;
      canvas.classList.toggle('is-over', i >= 0);
      if (id !== hoverId) { hoverId = id; labelsKey = ''; kick(true); }
      showTip(i, px, py);
    });
    function endDrag(e, cancelled) {
      if (!drag || drag.id !== e.pointerId) return;
      const d = drag;
      drag = null;
      canvas.classList.remove('is-dragging');
      if (d.i >= 0 && d.moved) {
        const n = g.nodes[d.i];
        delete n.fx; delete n.fy;
        if (sim) sim.reheat(0.12);
      }
      if (!d.moved && !cancelled && o.onPick) o.onPick(d.i >= 0 ? g.nodes[d.i] : null);
    }
    canvas.addEventListener('pointerup', e => endDrag(e, false));
    canvas.addEventListener('pointercancel', e => endDrag(e, true));
    canvas.addEventListener('pointerleave', () => {
      if (drag) return;
      canvas.classList.remove('is-over');
      if (hoverId != null) { hoverId = null; labelsKey = ''; kick(true); }
      tip.hidden = true;
    });
    // A plain wheel scrolls the page, as it would anywhere else; with Ctrl or
    // Cmd held, or a trackpad pinch (which arrives as one), it zooms.
    canvas.addEventListener('wheel', e => {
      if (!(e.ctrlKey || e.metaKey) || !g) return;
      e.preventDefault();
      const [px, py] = local(e);
      zoom(Math.exp(-e.deltaY * 0.0022), px, py);
    }, { passive: false });
    canvas.addEventListener('dblclick', e => { const [px, py] = local(e); if (pickAt(px, py) < 0) refit(); });

    // Stop drawing while nobody can see it.
    let io = null, ro = null;
    if (win.IntersectionObserver) {
      io = new win.IntersectionObserver(es => {
        visible = es.some(x => x.isIntersecting);
        if (visible) kick(true);
      });
      io.observe(host);
    }
    if (win.ResizeObserver) { ro = new win.ResizeObserver(() => resize()); ro.observe(host); }
    doc.addEventListener('visibilitychange', () => { if (!doc.hidden) kick(true); });

    const api = {
      set, focus, fit: refit,
      zoom: f => zoom(f),
      redraw() { labelsKey = ''; kick(true); },
      graph: () => g,
      focused: () => focusId,
      // Where a node is drawn, in the host's pixels: for the console, and for
      // browser checks that need to point at one.
      screenOf(id) { const i = byId(id); return i < 0 ? null : { x: sx(g.nodes[i].x), y: sy(g.nodes[i].y), r: rOf(g.nodes[i]) }; },
      destroy() {
        if (io) io.disconnect();
        if (ro) ro.disconnect();
        if (raf) win.cancelAnimationFrame(raf);
        host.removeChild(canvas); host.removeChild(tip);
        delete host.smartGraph;
      },
    };
    host.smartGraph = api;
    return api;
  }

  const api = { build, focusRows, amountOn, layout, createSim, bounds, fit, placeLabels, maxStocks, mount, hasCJK, tickerLabel, rgba, SRC, MAX_STOCKS, MIN_STOCKS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.SmartGraph = api;
})(typeof window !== 'undefined' ? window : globalThis);
