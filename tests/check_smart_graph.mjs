/**
 * Behaviour tests for static/smart_graph.js -- the network on /smart-money:
 * which stocks and people it draws from the /api/smart-money payload, how
 * they are joined, where the simulation puts them, and where labels go.
 *
 * Every failure worth catching here still draws a plausible picture: a
 * manager split into two dots because one stock listed him under another id,
 * a hold that drags a stock nobody moved into the graph, a disabled source
 * still lighting its arc, a picked stock missing because the filters hid it,
 * a layout that differs on every reload, labels printed over each other.
 *
 * Run: node tests/check_smart_graph.mjs
 */
import { createRequire } from 'module';
import fs from 'fs';
import path from 'path';

const root = path.resolve(import.meta.dirname, '..');
const require = createRequire(import.meta.url);
const SG = require(path.join(root, 'ystocker/static/smart_graph.js'));

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${detail ? '  ' + detail : ''}`);
  if (!cond) failures.push(label);
};

const fund = (id, name, ch, extra = {}) => ({ id, name, name_zh: name + '中', org: name + ' Capital', ch, w: 1, ...extra });
const ins = (id, name, dir, extra = {}) => ({ id, name, title: 'Director', dir, plan: false, offering: false, ...extra });
const mem = (id, name, dir, extra = {}) => ({ id, name, name_zh: null, org: 'CA11', dir, ...extra });
const src = (side, who, extra = {}) => ({ buy: 0, sell: 0, hold: 0, side, who, ...extra });

// Five stocks in the table's own order (agreement first): NVDA is the
// busiest, BAC has two sources selling, KO is only held by a manager, PLAN has
// nothing but a plan sale, and XYZ has one House member.
const DATA = {
  tickers: [
    { t: 'BAC', agree: { side: 'sell', sources: ['funds', 'insiders'], span_days: 60 }, split: false, src: {
      funds: src('sell', [fund('fund-a', 'Ann', 'reduced')], { sell: 1 }),
      insiders: src('sell', [ins('ins-zed-bac', 'Zed', 'sell')], { sell: 1 }) } },
    { t: 'NVDA', agree: null, split: true, src: {
      funds: src('buy', [fund('fund-a', 'Ann', 'increased'), fund('fund-b', 'Bob', 'new'), fund('fund-c', 'Cy', 'reduced')], { buy: 2, sell: 1 }),
      insiders: src('sell', [ins('ins-xi-nvda', 'Xi', 'sell'), ins('ins-yu-nvda', 'Yu', 'sell', { plan: true })], { sell: 1, plan: 1 }),
      house: src('mixed', [mem('house-pat', 'Pat', 'mixed', { name_zh: '帕特' }), mem('house-quinn', 'Quinn', 'buy')], { buy: 2, sell: 1 }) } },
    { t: 'KO', agree: null, split: false, src: {
      funds: src(null, [], { hold: 2 }),
      house: src('sell', [mem('house-quinn', 'Quinn', 'sell')], { sell: 1 }) } },
    { t: 'PLAN', agree: null, split: false, src: {
      insiders: src(null, [ins('ins-wu-plan', 'Wu', 'sell', { plan: true })], { plan: 3 }) } },
    { t: 'XYZ', agree: null, split: false, src: { house: src('buy', [mem('house-pat', 'Pat', 'buy')], { buy: 1 }) } },
  ],
  people: [
    { kind: 'fund', id: 'fund-a', actions: [{ t: 'NVDA', dir: 'buy' }, { t: 'BAC', dir: 'sell' }, { t: 'KO', dir: 'hold' }, { t: 'AAPL', dir: 'hold' }] },
    { kind: 'fund', id: 'fund-b', actions: [{ t: 'NVDA', dir: 'buy' }, { t: 'KO', dir: 'hold' }] },
    { kind: 'fund', id: 'fund-d', actions: [{ t: 'KO', dir: 'hold' }] },        // holds only
    { kind: 'house', id: 'house-pat', actions: [] },
    { kind: 'insider', id: 'ins-xi-nvda', ticker: 'NVDA', actions: [] },
  ],
};
const ROWS = DATA.tickers;
const node = (g, id) => g.nodes.find(n => n.id === id);
const linksOf = (g, id) => g.links.filter(l => g.nodes[l.a].id === id || g.nodes[l.b].id === id);

console.log('build: who is drawn, and how they join');
{
  const g = SG.build(DATA, ROWS, {});
  const ids = g.nodes.map(n => n.id);
  t('node ids are unique', new Set(ids).size === ids.length);
  t('every link runs from a stock to a person',
    g.links.every(l => g.nodes[l.a] && g.nodes[l.a].type === 'stock' && g.nodes[l.b] && g.nodes[l.b].type === 'person'));
  t('a manager on two stocks is one node with both links',
    g.nodes.filter(n => n.key === 'fund-a').length === 1
      && ['t:NVDA', 't:BAC'].every(s => linksOf(g, 'p:fund-a').some(l => g.nodes[l.a].id === s && l.dir !== 'hold')));
  t('a 13F change reads as the side it is', (() => {
    const by = Object.fromEntries(linksOf(g, 't:NVDA').filter(l => l.src === 'funds').map(l => [g.nodes[l.b].key, l.dir]));
    return by['fund-a'] === 'buy' && by['fund-b'] === 'buy' && by['fund-c'] === 'sell';
  })());
  const grp = node(g, 'p:ins@NVDA');
  t('a company’s insiders are one node beside it', grp && grp.group && grp.kind === 'insider' && grp.count === 2
    && !g.nodes.some(n => n.key === 'ins-xi-nvda'));
  t('the insiders’ link has their side', linksOf(g, 'p:ins@NVDA')[0].dir === 'sell' && !linksOf(g, 'p:ins@NVDA')[0].soft);
  t('insiders who set no side are dashed', linksOf(g, 'p:ins@PLAN')[0].soft === true);
  t('a member who bought and sold is a mixed link', linksOf(g, 'p:house-pat').some(l => g.nodes[l.a].key === 'NVDA' && l.dir === 'mixed'));
  t('a member on two stocks keeps one node', g.nodes.filter(n => n.key === 'house-pat').length === 1 && linksOf(g, 'p:house-pat').length === 2);
  t('a person’s Chinese name travels with the node', node(g, 'p:house-pat').name_zh === '帕特');

  const holds = g.links.filter(l => l.dir === 'hold');
  t('a hold joins a manager already drawn to a stock already drawn',
    holds.some(l => g.nodes[l.a].key === 'KO' && g.nodes[l.b].key === 'fund-a')
      && holds.some(l => g.nodes[l.a].key === 'KO' && g.nodes[l.b].key === 'fund-b'));
  t('a hold never brings in a stock (AAPL) or a manager (fund-d)', !node(g, 't:AAPL') && !node(g, 'p:fund-d'));
  t('holds are dashed and do not count as moves', holds.every(l => l.soft) && node(g, 't:KO').moves === 1);
  t('a hold is not drawn twice beside a move', !g.links.some((l, i) => g.links.some((m, j) => j > i && m.a === l.a && m.b === l.b)));

  const ko = node(g, 't:KO');
  t('a source that only holds draws a hold arc', ko.arcs.funds === 'hold' && ko.arcs.house === 'sell' && ko.arcs.insiders === null);
  t('a source with only a plan trade draws a no-side arc', node(g, 't:PLAN').arcs.insiders === 'none');
  t('two sources selling the same way is the stock’s agreement', node(g, 't:BAC').agree === 'sell' && node(g, 't:NVDA').agree === null);
  t('stocks and people are counted', g.stocks === 5 && g.people === g.nodes.length - 5 && g.total === 5);
  const r = id => node(g, id).r;
  t('a busier stock is a bigger disc', r('t:NVDA') > r('t:BAC') && r('t:BAC') >= r('t:XYZ'));
  t('discs and dots stay within their bounds', g.nodes.every(n => (n.type === 'stock' ? n.r >= 10 && n.r <= 26 : n.r >= 3.2 && n.r <= 8)));
}

console.log('build: the filters the table applies');
{
  const g = SG.build(DATA, ROWS, { src: ['funds'] });
  t('a source switched off draws no one', g.nodes.every(n => n.type === 'stock' || n.kind === 'fund'));
  t('and no arc', g.nodes.filter(n => n.type === 'stock').every(n => n.arcs.insiders === null && n.arcs.house === null));
  t('a stock nobody in the sources on moved is left out', !node(g, 't:XYZ') && !node(g, 't:PLAN'));
  t('an agreement needing a source switched off is not drawn', node(g, 't:BAC').agree === null);
  const s = SG.build(DATA, ROWS, { src: new Set(['funds', 'insiders']) });
  t('a Set of sources works like an array', node(s, 't:BAC').agree === 'sell' && !s.nodes.some(n => n.kind === 'house'));
}

console.log('build: the busiest stocks, and the pick');
{
  const g = SG.build(DATA, ROWS, { max: 2 });
  t('the stocks with the most people win the cut, not the table’s first rows',
    g.nodes.filter(n => n.type === 'stock').map(n => n.key).join() === 'NVDA,BAC');
  t('the cut is reported against how many had anyone', g.stocks === 2 && g.total === 5);
  const f = SG.build(DATA, ROWS, { max: 2, focus: { t: 'XYZ' } });
  t('a picked stock outside the cut is added', !!node(f, 't:XYZ') && f.stocks === 3);
  const hidden = SG.build(DATA, ROWS.filter(r => r.t !== 'XYZ'), { max: 2, focus: { t: 'XYZ' } });
  t('a picked stock the filters hide is still added', !!node(hidden, 't:XYZ'));
  const none = SG.build(DATA, ROWS, { max: 2, focus: { t: 'PLAN' }, src: ['funds'] });
  t('a pick with no one on it in the sources on is not drawn', !node(none, 't:PLAN'));
  const p = SG.build(DATA, ROWS, { max: 1, focus: { p: 'house-pat' } });
  t('a picked person brings every stock they moved', ['t:NVDA', 't:XYZ'].every(id => node(p, id)));
  t('focusRows names them', SG.focusRows(DATA, ROWS, { p: 'house-pat' }, ['house']).map(r => r.t).join() === 'NVDA,XYZ');
  t('an insider’s pick is their company', SG.focusRows(DATA, ROWS, { p: 'ins-zed-bac' }).map(r => r.t).join() === 'BAC');
  t('a pick is found among all stocks when the filters hide every one',
    SG.focusRows(DATA, ROWS.filter(r => r.t !== 'BAC'), { p: 'ins-zed-bac' }).map(r => r.t).join() === 'BAC');
  t('no pick, nothing added', SG.focusRows(DATA, ROWS, null).length === 0 && SG.focusRows(DATA, ROWS, {}).length === 0);
}

console.log('build: amounts');
{
  // Dollars on top of DATA: NVDA's managers add and trim, BAC sees one big trim.
  const d = JSON.parse(JSON.stringify(DATA));
  const nv = d.tickers.find(r => r.t === 'NVDA'), bac = d.tickers.find(r => r.t === 'BAC');
  nv.src.funds.who.forEach((w, i) => { w.v = [2e8, 5e7, -1e8][i]; });
  Object.assign(nv.src.funds, { bought: 2.5e8, sold: 1e8 });
  Object.assign(nv.src.insiders, { bought: 0, sold: 3e6 });
  Object.assign(nv.src.house, { bought: 30000, sold: 8000 });
  bac.src.funds.who[0].v = -9e9;
  Object.assign(bac.src.funds, { bought: 0, sold: 9e9 });
  Object.assign(bac.src.insiders, { bought: 0, sold: 1e6 });
  const amt = SG.amountOn(nv, ['funds', 'insiders', 'house']);
  t('a row’s dollars add up over the sources switched on', amt.b === 2.5e8 + 30000 && amt.s === 1e8 + 3e6 + 8000);
  t('a source switched off adds nothing', SG.amountOn(nv, ['house']).b === 30000);
  const people = SG.build(d, d.tickers, { max: 1 });
  const money = SG.build(d, d.tickers, { max: 1, rank: 'amount' });
  t('by people the busiest stock is drawn', people.nodes.filter(n => n.type === 'stock').map(n => n.key).join() === 'NVDA' && people.rank === 'people');
  t('by amount the one the most money moved through', money.nodes.filter(n => n.type === 'stock').map(n => n.key).join() === 'BAC' && money.rank === 'amount');
  const g = SG.build(d, d.tickers, { rank: 'amount' });
  const r = id => node(g, id).r;
  t('by amount a disc grows with its dollars', r('t:BAC') > r('t:NVDA') && r('t:NVDA') >= r('t:XYZ'));
  const big = linksOf(g, 't:BAC').find(l => l.src === 'funds');
  t('a link carries its signed dollars', big.v === -9e9 && big.gross === 9e9);
  t('the largest link is the widest, a hold has no width',
    big.weight === 1 && g.links.every(l => l.weight <= 1) && g.links.filter(l => l.dir === 'hold').every(l => l.weight === 0));
  const grp = linksOf(g, 'p:ins@NVDA')[0];
  t('a company’s insiders carry both ways', grp.bought === 0 && grp.sold === 3e6 && grp.v === -3e6);
  t('a person’s dollars add up over their links', node(g, 'p:fund-a').amount === 2e8 + 9e9);
}

console.log('build: a payload from before ids');
{
  const old = JSON.parse(JSON.stringify(DATA));
  old.tickers.forEach(r => Object.values(r.src).forEach(x => x.who.forEach(w => { delete w.id; })));
  const g = SG.build(old, old.tickers, {});
  t('a manager is still one node across stocks', g.nodes.filter(n => n.name === 'Ann').length === 1
    && linksOf(g, g.nodes.find(n => n.name === 'Ann').id).length >= 2);
  t('only the two holds are missing, which need people’s ids', g.links.length === SG.build(DATA, ROWS, {}).links.length - 2,
    `${g.links.length}`);
}

console.log('build: an empty payload');
{
  const g = SG.build({ tickers: [], people: [] }, [], {});
  t('draws nothing and says so', g.nodes.length === 0 && g.links.length === 0 && g.stocks === 0);
  t('lays out without failing', SG.layout(g, { width: 800, height: 500 }) === g);
  t('bounds of nothing are a unit box', JSON.stringify(SG.bounds([])) === JSON.stringify({ x0: -1, y0: -1, x1: 1, y1: 1 }));
}

// A graph the size the page draws: 44 stocks, managers across many of them,
// one insider group each, a few House members.
function synthetic(nStocks = 44, nFunds = 22, nHouse = 30, seed = 1) {
  let a = seed;
  const rnd = () => ((a = (a * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
  const tickers = [];
  for (let i = 0; i < nStocks; i++) {
    const t = 'S' + String(i).padStart(2, '0');
    const funds = [], house = [], insiders = [];
    for (let f = 0; f < nFunds; f++) if (rnd() < 0.12) funds.push(fund('fund-' + f, 'F' + f, rnd() < 0.5 ? 'increased' : 'reduced'));
    for (let h = 0; h < nHouse; h++) if (rnd() < 0.05) house.push(mem('house-' + h, 'H' + h, rnd() < 0.5 ? 'buy' : 'sell'));
    const k = Math.floor(rnd() * 5);
    for (let j = 0; j < k; j++) insiders.push(ins(`ins-i${j}-${t}`, 'I' + j, 'sell'));
    const s = {};
    if (funds.length) s.funds = src('buy', funds, { buy: funds.length });
    if (house.length) s.house = src('sell', house, { sell: house.length });
    if (insiders.length) s.insiders = src('sell', insiders, { sell: insiders.length });
    if (Object.keys(s).length) tickers.push({ t, agree: null, split: false, src: s });
  }
  return { tickers, people: [] };
}

console.log('layout');
{
  const data = synthetic();
  const g1 = SG.build(data, data.tickers, { max: 44 });
  const t0 = performance.now();
  SG.layout(g1, { width: 820, height: 560 });
  const ms = performance.now() - t0;
  const g2 = SG.layout(SG.build(data, data.tickers, { max: 44 }), { width: 820, height: 560 });
  t('the same graph lands the same way every time', g1.nodes.every((n, i) => n.x === g2.nodes[i].x && n.y === g2.nodes[i].y));
  t('every position is a number', g1.nodes.every(n => Number.isFinite(n.x) && Number.isFinite(n.y)));
  t(`a page-sized graph lays out quickly (${g1.nodes.length} nodes in ${ms.toFixed(0)} ms)`, ms < 600);
  const st = g1.nodes.filter(n => n.type === 'stock');
  let overlap = 0;
  for (let i = 0; i < st.length; i++) for (let j = i + 1; j < st.length; j++) {
    if (Math.hypot(st[i].x - st[j].x, st[i].y - st[j].y) < st[i].r + st[j].r) overlap++;
  }
  t('no two stock discs overlap', overlap === 0, `${overlap}`);
  let under = 0;
  g1.nodes.filter(n => n.type === 'person').forEach(p => st.forEach(s => { if (Math.hypot(p.x - s.x, p.y - s.y) < s.r) under++; }));
  t('no dot sits under a disc', under === 0, `${under}`);
  const far = g1.nodes.filter(n => n.group).filter(gp => {
    const l = g1.links.find(x => g1.nodes[x.b] === gp), s = g1.nodes[l.a];
    return Math.hypot(gp.x - s.x, gp.y - s.y) > 6 * (s.r + gp.r);
  });
  t('a company’s insiders stay beside it', far.length === 0, `${far.length} far`);
  const b = SG.bounds(g1.nodes), cam = SG.fit(b, 820, 560);
  t('the fitted camera shows every node', g1.nodes.every(n => {
    const x = (n.x - cam.x) * cam.k + 410, y = (n.y - cam.y) * cam.k + 280;
    return x >= 0 && x <= 820 && y >= 0 && y <= 560;
  }));
  t('and does not shrink a page-sized graph to a speck', cam.k > 0.6, cam.k.toFixed(2));

  // A rebuild with the old positions keeps the picture.
  const prev = new Map(g1.nodes.map(n => [n.id, { x: n.x, y: n.y }]));
  const g3 = SG.build(data, data.tickers.slice(0, 40), { max: 44 });
  SG.createSim(g3, { width: 820, height: 560, prev, alpha: 0.6 }).run();
  const moved = g3.nodes.filter(n => prev.has(n.id)).map(n => Math.hypot(n.x - prev.get(n.id).x, n.y - prev.get(n.id).y));
  moved.sort((x, y) => x - y);
  const median = moved[moved.length >> 1];
  t('a node that stays moves a little, not across the canvas', median < 60, `median ${median.toFixed(1)}`);
  const pinned = SG.build(data, data.tickers, { max: 44 });
  pinned.nodes[0].fx = 123; pinned.nodes[0].fy = -45;
  SG.layout(pinned, { width: 820, height: 560 });
  t('a dragged node stays where it is held', pinned.nodes[0].x === 123 && pinned.nodes[0].y === -45);
}

console.log('labels');
{
  const c = (id, x, y, w, pri) => ({ id, x, y, r: 4, w, h: 13, pri, text: id });
  const placed = SG.placeLabels([c('a', 100, 100, 80, 3), c('b', 104, 100, 80, 2), c('c', 300, 100, 60, 1)], [], 800, 400);
  const box = p => p.align === 'left' ? [p.lx, p.ly - 6.5, p.lx + p.w, p.ly + 6.5]
    : p.align === 'right' ? [p.lx - p.w, p.ly - 6.5, p.lx, p.ly + 6.5] : [p.lx - p.w / 2, p.ly - 6.5, p.lx + p.w / 2, p.ly + 6.5];
  const clash = (p, q) => { const [a0, a1, a2, a3] = box(p), [b0, b1, b2, b3] = box(q); return a0 < b2 && a2 > b0 && a1 < b3 && a3 > b1; };
  t('the first goes right of its dot', placed[0].id === 'a' && placed[0].align === 'left');
  t('a clashing second moves to another side', placed.some(p => p.id === 'b') && !clash(placed[0], placed.find(p => p.id === 'b')));
  t('no two placed labels overlap', placed.every((p, i) => placed.every((q, j) => i === j || !clash(p, q))));
  const edge = SG.placeLabels([c('e', 790, 200, 60, 1)], [], 800, 400);
  t('a label at the right edge goes left instead', edge.length === 1 && edge[0].align === 'right');
  const blocked = SG.placeLabels([c('d', 100, 100, 60, 1)], [{ id: 'S', x: 140, y: 100, r: 30 }, { id: 'T', x: 60, y: 100, r: 30 },
                                                             { id: 'U', x: 100, y: 125, r: 14 }, { id: 'V', x: 100, y: 75, r: 14 }], 800, 400);
  t('a label boxed in by discs is dropped, not printed over them', blocked.length === 0);
  const own = SG.placeLabels([c('S', 100, 100, 60, 1)], [{ id: 'S', x: 100, y: 100, r: 3 }], 800, 400);
  t('a node does not block its own label', own.length === 1);
}

console.log('small pieces');
{
  t('the stocks drawn shrink with the width', SG.maxStocks(390) < SG.maxStocks(700) && SG.maxStocks(700) < SG.maxStocks(1100));
  t('a laptop draws what it did before the bigger view', SG.maxStocks(1010, 560) === 44 && SG.maxStocks(806, 560) === 35);
  t('the bigger view draws more', SG.maxStocks(1024, 830) > SG.maxStocks(1010, 560), `${SG.maxStocks(1024, 830)}`);
  t('a phone gets a floor and a huge screen a ceiling',
    SG.maxStocks(358, 400) === SG.MIN_STOCKS && SG.maxStocks(3840, 2000) === SG.MAX_STOCKS);
  t('a share class keeps its letter, an exchange suffix goes', SG.tickerLabel('BRK-B') === 'BRK-B' && SG.tickerLabel('7203.T') === '7203');
  t('a colour at an alpha', SG.rgba('#34d399', 0.5) === 'rgba(52,211,153,0.5)' && SG.rgba('#fff', 0) === 'rgba(255,255,255,0)');
  t('Chinese text is told from Latin', SG.hasCJK('英伟达') && !SG.hasCJK('Meta') && !SG.hasCJK(''));
}

console.log('the page wires it');
{
  const tpl = fs.readFileSync(path.join(root, 'ystocker/templates/smart_money.html'), 'utf8');
  t('the page loads the module', /static', filename='smart_graph\.js'/.test(tpl));
  t('it mounts with every hook the module calls', ['onPick', 'tooltip', 'label', 'sub'].every(k => new RegExp(`SmartGraph\\.mount\\([^)]*\\b${k}:`).test(tpl)));
  t('it builds from the table’s own rows', /SmartGraph\.build\(data, rows,/.test(tpl) && /const g = G\.graph\(\), rows = stockRows\(\)/.test(tpl));
  t('an insider pick lights their company’s insiders', tpl.includes("'p:ins@' + f.ticker"));
  t('the sort decides what the graph ranks by', /const rank = state\.sort === 'amount' \? 'amount' : 'people'/.test(tpl)
    && /state\.side, rank,/.test(tpl) && /focus: f, rank \}/.test(tpl));
  t('how many stocks fit is measured on the stage, both ways', /maxStocks\(stage\.clientWidth \|\| 800, stage\.clientHeight \|\| 560\)/.test(tpl));
  t('every column of the table sorts', ['ticker', 'default', 'amount', 'latest'].every(k => tpl.includes(`th('${k}'`))
    && /SRC\.map\(s => th\(s,/.test(tpl));
  t('the bigger view is a button, and Esc leaves it', /data-mo-big/.test(tpl) && /e\.key === 'Escape' && state\.big/.test(tpl));
  t('a reader behind the wall is shown the offer, not the bigger view', /if \(on && walled\(\)\)/.test(tpl));
  t('lit amounts come from the page', /amount: linkAmount/.test(tpl));
  t('the empty note can hide (its display would beat [hidden])', /\.mo-g-empty\[hidden\]\s*\{\s*display:\s*none/.test(tpl));
}

if (failures.length) {
  console.log(`\n${failures.length} failed`);
  process.exit(1);
}
console.log('\nall passed');
