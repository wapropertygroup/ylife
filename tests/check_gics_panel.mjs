// Checks the GICS panel's renderer in templates/markets.html.
//
// The panel is 36 rows in three modes with a sort and a collapse, built by
// string concatenation — the shape in which this page's bugs have historically
// shipped, because a wrong row renders just as confidently as a right one. What
// is pinned here is what a reader would never catch by looking: that a sort
// cannot lift an industry group out from under its own sector, that "could not
// be measured" sorts last rather than lowest, that a one-group sector does not
// pretend to expand, and that the loader always ends on something visible.
//
// The block is extracted from the template rather than copied, as in
// check_dca_row_cells.mjs: a copy agrees on the day it is written and drifts.
//
// Run: node tests/check_gics_panel.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const tpl = readFileSync(join(here, '..', 'ystocker', 'templates', 'markets.html'), 'utf8');
const START = '// Its own escape';
const END = "}, { label: 'gics' });";
const a = tpl.indexOf(START), b = tpl.indexOf(END);
if (a < 0 || b < 0) throw new Error('GICS block markers not found in markets.html');
const block = tpl.slice(a, b + END.length);

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${cond || !detail ? '' : '  — ' + detail}`);
  if (!cond) failures.push(label);
};

// ── A page, just big enough ─────────────────────────────────────────────────
function makePage() {
  const els = {};
  // The map's card starts hidden, as it does in the template.
  const el = id => {
    if (els[id]) return els[id];
    const cls = new Set(id === 'gicsMapSection' ? ['hidden'] : []);
    return (els[id] = { id, innerHTML: '', _handlers: {}, attrs: {}, cls,
      classList: { add: c => cls.add(c), remove: c => cls.delete(c), contains: c => cls.has(c) },
      setAttribute(k, v) { this.attrs[k] = String(v); },
      parentNode: { clientWidth: 880 },
      addEventListener(ev, fn) { this._handlers[ev] = fn; } });
  };
  const modeBtns = ['ret', 'rel', 'wchg'].map(m => ({
    dataset: { gicsMode: m }, _cls: new Set(m === 'ret' ? ['active'] : []),
    classList: { toggle(c, on) { on ? this._o._cls.add(c) : this._o._cls.delete(c); } },
    addEventListener(ev, fn) { this._click = fn; },
  }));
  modeBtns.forEach(bn => { bn.classList._o = bn; });
  const document = {
    getElementById: id => el(id),
    querySelectorAll: sel => sel === '[data-gics-mode]' ? modeBtns : [],
    addEventListener() {},
  };
  return { els, el, modeBtns, document };
}

// I18n resolves to the key itself, bracketed, so an assertion can see which
// key a cell used — a null stub would send everything down the fallback path
// and a wrong key would pass unnoticed.
// The two interpolating labels resolve to real templates, because a bracketed
// key has no {date} in it and the date would vanish unnoticed; test_gics.py
// asserts both languages keep the placeholder. The map's templates likewise,
// so a count or a window that failed to land in its sentence shows up here.
const TEMPLATES = { 'markets.gics_asof': 'As of the {date} close.',
                    'markets.gics_note_method': 'Weights from SPY holdings ({date}).',
                    'markets.gics_map_axis': '{p} vs index',
                    'markets.gics_map_count': '{q}: {n} ({w})',
                    'markets.gics_map_counts': 'By quadrant: {list}',
                    'markets.gics_map_missing': '{n} not plotted.',
                    'markets.gics_map_tip_weight': 'Weight {w} · {n} names',
                    'markets.gics_map_pair_tip': 'Up {y}, across {x}',
                    'markets.gics_map_tip_then': 'Week before: {at}',
                    'markets.gics_map_trail_note': 'Lines from a week back.',
                    'markets.gics_map_aria': 'Map of {n} groups.' };
const I18n = { t: k => TEMPLATES[k] ?? `[${k}]` };

function load({ fetch = async () => ({ status: 200, ok: true, json: async () => ({}) }), chartThrows = false } = {}) {
  const page = makePage();
  let loader = null;
  const DeferLoad = { when: (_sel, fn) => { loader = fn; } };
  let clock = 0;
  const fakeDate = { now: () => clock, parse: s => Date.parse(s) };
  const fakeTimeout = (fn, ms) => { clock += ms; fn(); };
  // Chart.js, as far as the map touches it outside a real canvas: the config
  // it was built with, and every update and destroy after that.
  const charts = [];
  class Chart {
    static defaults = { font: { family: 'test-sans' } };
    constructor(canvas, config) {
      if (chartThrows) throw new Error('no canvas here');
      Object.assign(this, { canvas, config, data: config.data, options: config.options,
                            updates: [], destroyed: false, _active: [] });
      charts.push(this);
    }
    update(mode) { this.updates.push(mode ?? 'default'); }
    destroy() { this.destroyed = true; }
    getActiveElements() { return this._active; }
  }
  const warnings = [];
  const console_ = { warn: (...a) => warnings.push(a.map(String).join(' ')), info() {} };
  // CT, GRID_COL and TICK_COL are the page's globals, set up by base.html and
  // the top of the page script; the dark theme is the one written in the file.
  const CT = { c: x => x, dark: true };
  const api = new Function('document', 'DeferLoad', 'I18n', 'fetch', 'setTimeout', 'Date', 'console',
    'Chart', 'CT', 'GRID_COL', 'TICK_COL',
    `${block}\nreturn { _GICS, renderGics, _gicsTone, _gicsFmt, _gicsSorted, _GICS_MAP, renderGicsMap,
      _gicsQuad, _gicsMapRadius, _gicsMapBound, _gicsPlaceLabels, _gicsMapPlugin, _gicsMapTipLines, _gicsMapTipTitle,
      _GICS_MAP_QCOL, _GICS_MAP_COL, _gicsDrawTrails };`)(
    page.document, DeferLoad, I18n, fetch, fakeTimeout, fakeDate, console_, Chart, CT, '#1e293b', '#64748b');
  return { ...api, page, charts, warnings, loader: () => loader() };
}

// ── Fixture: three sectors, one of them single-group ────────────────────────
const P = ['1D', '1W', '1M', '3M', '6M', 'YTD', '1Y'];
const vals = (...xs) => Object.fromEntries(P.map((p, i) => [p, xs[i] ?? null]));
const row = (code, name, weight, n, ret, rel = ret, wchg = ret) =>
  ({ code, name, weight, n, returns: vals(...ret), rel: vals(...rel), weight_chg: vals(...wchg) });
const DATA = {
  asof: '2026-09-25', weights_asof: '2026-09-24', stale: false, periods: P,
  base_dates: { '1D': '2026-09-24', '1W': '2026-09-18', '1M': '2026-08-25', '3M': '2026-06-25',
                '6M': '2026-03-25', YTD: '2025-12-31', '1Y': '2025-09-25' },
  index: { returns: vals(0.5, 1.2, 0.9, 5.3, 17.8, 13.5, 17.3) },
  coverage: { priced: 503 },
  sectors: [
    { ...row('45', 'Information Technology', 34.1, 74, [0.9, 2.0, 3.0, 4.0, 30, 25, 40]),
      groups: [
        { ...row('4530', 'Semis', 14.2, 20, [0.5, 4.0, 8.7, 0.6, 42.8, 44.4, 56.6]), top: [{ t: 'NVDA', w: 8.2 }, { t: '<b>X', w: 0.1 }] },
        { ...row('4510', 'Software', 10.5, 30, [1.4, -1.0, -2.0, 5.0, 10, 5, 12]), top: [] },
        { ...row('4520', 'Hardware', 9.4, 24, [null, 0.1, 0.2, 0.3, 1, 2, 3]), top: [] },
      ] },
    { ...row('40', 'Financials', 13.2, 76, [1.1, -1.7, -4.7, -2.0, 13, 4, 10]),
      groups: [
        { ...row('4020', 'Financial Services', 8.0, 40, [1.0, -1.0, -3.0, -1.0, 12, 5, 11]), top: [] },
        { ...row('4010', 'Banks', 3.1, 13, [1.3, -1.7, -4.6, -2.0, 13, 4, 10]), top: [] },
      ] },
    { ...row('55', 'Utilities', 2.3, 31, [0.4, -3.2, -8.4, -13.8, -12.7, -7.5, -7.5]),
      groups: [{ ...row('5510', 'Utilities', 2.3, 31, [0.4, -3.2, -8.4, -13.8, -12.7, -7.5, -7.5]), top: [] }] },
  ],
};

// The rotation map's trail, shaped as gics.trail() sent it before it was cut
// to one week — which is what the box's cache still holds until its next daily
// build, so the map must draw only the newest week of it: six weekly closes,
// newest first, the head equal to the table's own figure and every earlier
// week one point lower. Hardware is missing its 1M figure a week back.
DATA.trail = {
  dates: ['2026-09-25', '2026-09-18', '2026-09-11', '2026-09-04', '2026-08-28', '2026-08-21'],
  step_days: 7,
  rel: Object.fromEntries(DATA.sectors.flatMap(s => s.groups).map(g => [g.code,
    Object.fromEntries(['1W', '1M', '3M', '6M', '1Y'].map(p => [p,
      [0, 1, 2, 3, 4, 5].map(k => (g.rel[p] == null ? null : +(g.rel[p] - k).toFixed(2)))]))])),
};
DATA.trail.rel['4520']['1M'][1] = null;
// Utilities stood far lower a week back, further out than the frame's padding
// reaches, so a frame not fitted to the lines would cut its line off.
DATA.trail.rel['5510']['1M'][1] = -14.4;
// …and as gics.trail() sends it now: the latest close and the one before.
const TWO_WEEKS = { ...DATA, trail: { ...DATA.trail, dates: DATA.trail.dates.slice(0, 2),
  rel: Object.fromEntries(Object.entries(DATA.trail.rel).map(([code, per]) => [code,
    Object.fromEntries(Object.entries(per).map(([p, v]) => [p, v.slice(0, 2)]))])) } };

// Row order as rendered: "S" for a sector, "G" for a group, with its code.
const order = html => [...html.matchAll(/<tr class="gics-(index|sector|group)[^"]*"[^>]*>(?:<td>(?:<span class="gics-caret">[^<]*<\/span>)?\[gics\.(\d+)\])?/g)]
  .map(m => m[1] === 'index' ? 'I' : (m[1] === 'sector' ? 'S' : 'G') + m[2]);

console.log('\nlayout');
{
  const g = load(); g._GICS.data = DATA; g.renderGics();
  const html = g.page.els.gicsTable.innerHTML;
  const o = order(html);
  t('index row first, then sectors by weight', o.join(' ') === 'I S45 G4530 G4510 G4520 S40 G4020 G4010 S55', o.join(' '));
  t('a one-group sector does not expand', !/data-toggle="55"/.test(html) && !o.includes('G5510'));
  t('a multi-group sector does', /data-toggle="45"/.test(html));
  t('names come from i18n by code', html.includes('[gics.4530]') && html.includes('[markets.gics_col_weight]'));
  t('top names are escaped', html.includes('&lt;b&gt;X') && !html.includes('<b>X'));
  t('index row shows its own return in the return view', /gics-index.*\+0\.5%/.test(html));
  t('an unmeasured cell is a dash, not 0', html.includes('>—<'));
  const note = g.page.els.gicsNote.innerHTML;
  t('the note carries the as-of date', note.includes('As of the 2026-09-25 close.'), note.slice(0, 90));
  t('…and the weights date', note.includes('(2026-09-24)'));
}

console.log('\nsorting');
{
  const g = load(); g._GICS.data = DATA;
  // Sort by 1W descending: IT's 2.0 > Fin's -1.7 > Util's -3.2 — but within IT
  // the groups must still sit under IT, in their own 1W order.
  g._GICS.sort = '1W'; g._GICS.dir = -1; g.renderGics();
  const o = order(g.page.els.gicsTable.innerHTML);
  t('sectors sort among themselves', o.filter(x => x[0] === 'S').join() === 'S45,S40,S55', o.join(' '));
  t('groups stay directly under their sector', o.join(' ') === 'I S45 G4530 G4520 G4510 S40 G4020 G4010 S55', o.join(' '));

  // 1D ascending: Hardware's 1D is null and must still come last, not first.
  g._GICS.sort = '1D'; g._GICS.dir = 1; g.renderGics();
  const asc = order(g.page.els.gicsTable.innerHTML);
  t('null sorts last ascending', asc.slice(1, 5).join(' ') === 'S55 S45 G4530 G4510' && asc[asc.indexOf('G4510') + 1] === 'G4520', asc.join(' '));
  g._GICS.dir = -1; g.renderGics();
  const desc = order(g.page.els.gicsTable.innerHTML);
  t('…and last descending', desc[desc.indexOf('S45') + 3] === 'G4520', desc.join(' '));
}

console.log('\ncollapse and click handling');
{
  const g = load(); g._GICS.data = DATA; g.renderGics();
  const click = g.page.els.gicsTable._handlers.click;
  const fakeEv = (sel, dataset) => ({ target: { closest: s => (s === sel ? { dataset } : null) } });
  click(fakeEv('tr[data-toggle]', { toggle: '45' }));
  const o = order(g.page.els.gicsTable.innerHTML);
  t('collapsing IT hides only its groups', o.join(' ') === 'I S45 S40 G4020 G4010 S55', o.join(' '));
  t('the collapsed row is marked', /gics-sector gics-collapsed" data-toggle="45"/.test(g.page.els.gicsTable.innerHTML));
  click(fakeEv('th[data-sort]', { sort: '1M' }));
  t('a new sort column starts descending', g._GICS.sort === '1M' && g._GICS.dir === -1);
  click(fakeEv('th[data-sort]', { sort: '1M' }));
  t('clicking it again reverses', g._GICS.dir === 1);
}

console.log('\nmodes');
{
  const g = load(); g._GICS.data = DATA;
  g.page.modeBtns[2]._click();
  const html = g.page.els.gicsTable.innerHTML;
  t('weight change is in percentage points', /\+\d+\.\d{2}pp/.test(html));
  t('the index row is a dash outside the return view', /gics-index[\s\S]*?gics-muted">—</.test(html));
  t('the note switches to the weight-change caveat', g.page.els.gicsNote.innerHTML.includes('[markets.gics_note_wchg]'));
  t('the toggle marks the active mode', g.page.modeBtns[2]._cls.has('active') && !g.page.modeBtns[0]._cls.has('active'));
  g._GICS.mode = 'ret';
  t('percent in the return view', g._gicsFmt(1.234) === '+1.2%' && g._gicsFmt(-1.25) === '-1.3%' && g._gicsFmt(null) === '—');
  t('a value that rounds to zero carries no sign', g._gicsFmt(-0.04) === '0.0%' && g._gicsFmt(0.04) === '0.0%',
    `${g._gicsFmt(-0.04)} / ${g._gicsFmt(0.04)}`);
}

console.log('\ncolour scales with the horizon');
{
  const g = load(); g._GICS.data = DATA; g._GICS.mode = 'ret';
  const strongUp = 'dark:bg-emerald-900/60';
  t('+2% in a day is strong', g._gicsTone(2.0, '1D').includes(strongUp));
  t('+2% in a year is barely coloured', !g._gicsTone(2.0, '1Y').includes('emerald'));
  t('+30% in a year is strong', g._gicsTone(30, '1Y').includes(strongUp));
  t('symmetric on the way down', g._gicsTone(-2.0, '1D').includes('dark:bg-rose-900/60'));
}

console.log('\nthe loader always ends on something visible');
{
  const g = load({ fetch: async () => ({ status: 202, ok: true, json: async () => ({ warming: true }) }) });
  await g.loader();
  t('202 for ever ends on "still computing"', g.page.els.gicsTable.innerHTML.includes('[markets.gics_gave_up]'),
    g.page.els.gicsTable.innerHTML.slice(0, 80));
}
{
  const g = load({ fetch: async () => ({ status: 503, ok: false, json: async () => ({ status: 'unavailable' }) }) });
  await g.loader();
  t('503 says unavailable', g.page.els.gicsTable.innerHTML.includes('[markets.gics_unavailable]'));
}
{
  let n = 0;
  const g = load({ fetch: async () => (++n < 3
    ? { status: 202, ok: true, json: async () => ({ warming: true }) }
    : { status: 200, ok: true, json: async () => DATA }) });
  await g.loader();
  t('warming then ready renders the table', g.page.els.gicsTable.innerHTML.startsWith('<table') && n === 3, `after ${n} polls`);
}
{
  const g = load({ fetch: async () => { throw new TypeError('network'); } });
  await g.loader();
  t('a thrown fetch says unavailable', g.page.els.gicsTable.innerHTML.includes('[markets.gics_unavailable]'));
}

console.log('\nrotation map');
const GROUPS = DATA.sectors.flatMap(s => s.groups);
const pairClick = (g, pair) => g.page.els.gicsMapPairs._handlers.click(
  { target: { closest: s => (s === '[data-gics-pair]' ? { dataset: { gicsPair: pair } } : null) } });
{
  const g = load(); g._GICS.data = DATA; g.renderGics(); g.renderGicsMap();
  t('the card is revealed once there is data', !g.page.els.gicsMapSection.cls.has('hidden'));
  t('one chart, and a bubble chart', g.charts.length === 1 && g.charts[0].config.type === 'bubble');
  const c = g.charts[0], pts = c.data.datasets[0].data;
  t('every group is plotted', pts.length === GROUPS.length, `${pts.length} of ${GROUPS.length}`);
  // The chart's whole claim to be checkable: a coordinate is a table cell.
  t("coordinates are the table's own vs-S&P cells, 3M across and 1M up", GROUPS.every(gr => {
    const p = pts.find(q => q.code === gr.code);
    return p && p.x === gr.rel['3M'] && p.y === gr.rel['1M'];
  }));
  t('largest first, so a small bubble paints over a large one', pts.every((p, i) => !i || pts[i - 1].w >= p.w));
  const k = pts.filter(p => p.r > 3).map(p => p.r * p.r / p.w);
  t('area, not radius, follows weight', k.length >= 2 && Math.max(...k) / Math.min(...k) < 1.0001,
    k.map(v => v.toFixed(2)).join(' '));
  t('and nothing is below the 3px floor', pts.every(p => p.r >= 3));
  const { x, y } = c.options.scales;
  t('both axes reach past zero, so the index lines stay in view', x.min < 0 && x.max > 0 && y.min < 0 && y.max > 0);
  t('…tick on round numbers only, not on the fitted bounds', x.ticks.includeBounds === false && y.ticks.includeBounds === false);
  t('…and clear every point', pts.every(p => p.x > x.min && p.x < x.max && p.y > y.min && p.y < y.max));
  t('axis titles name their window', x.title.text === '[markets.gics_p_3M] vs index' && y.title.text === '[markets.gics_p_1M] vs index',
    `${x.title.text} / ${y.title.text}`);
  const q = Object.fromEntries(pts.map(p => [p.code, p.q]));
  t('each group knows its quadrant', q['4530'] === 'lead' && q['4510'] === 'weak' && q['4010'] === 'lag', JSON.stringify(q));
  const note = g.page.els.gicsMapNote.innerHTML;
  // Semis 14.2 + Hardware 9.4 lead; Software 10.5 weakens; the other three lag.
  t('the note counts each quadrant and its share of the index', note.includes('By quadrant: [markets.gics_map_q_lead]: 2 (24%) · '
    + '[markets.gics_map_q_weak]: 1 (11%) · [markets.gics_map_q_lag]: 3 (13%) · [markets.gics_map_q_improve]: 0 (0%)'), note.slice(0, 200));
  t('…says nothing is missing by saying nothing', !note.includes('not plotted'));
  t('…and carries the as-of date', note.includes('As of the 2026-09-25 close.'));
  t('the canvas has a text alternative', g.page.els.gicsMapCanvas.attrs['aria-label'] === 'Map of 6 groups.');
  const btns = [...g.page.els.gicsMapPairs.innerHTML.matchAll(/data-gics-pair="([^"]+)"/g)].map(m => m[1]);
  t('four window pairs are offered', btns.join() === '1W|1M,1M|3M,1M|6M,3M|1Y', btns.join());
  t('…with the default marked', /class="gics-btn active" data-gics-pair="1M\|3M"/.test(g.page.els.gicsMapPairs.innerHTML));

  pairClick(g, '1M|6M');
  const moved = c.data.datasets[0].data;
  t('a pair switch moves the same chart rather than building another', g.charts.length === 1 && c.updates.length === 1);
  t('…onto the new windows', GROUPS.every(gr => {
    const p = moved.find(r => r.code === gr.code);
    return p.x === gr.rel['6M'] && p.y === gr.rel['1M'];
  }));
  t('…with the axis refitted and retitled', moved.every(p => p.x > c.options.scales.x.min && p.x < c.options.scales.x.max)
    && c.options.scales.x.title.text === '[markets.gics_p_6M] vs index');
  t('…and the button following', /class="gics-btn active" data-gics-pair="1M\|6M"/.test(g.page.els.gicsMapPairs.innerHTML));
  pairClick(g, '1M|6M');
  t('clicking the active pair again does nothing', c.updates.length === 1);
}
{
  // The table's view must not leak into the map's figures: weight change is
  // in percentage points, and the map is always relative return.
  const g = load(); g._GICS.data = DATA; g._GICS.mode = 'wchg'; g.renderGicsMap();
  const p = g.charts[0].data.datasets[0].data.find(r => r.code === '4530');
  const lines = g._gicsMapTipLines({ raw: p });
  t('tooltip leads with the quadrant and both figures, in percent whatever the table shows',
    lines[0] === '[markets.gics_map_q_lead] · [markets.gics_p_1M] +8.7% · [markets.gics_p_3M] +0.6%', lines[0]);
  // A week back, not five: 8.7 - 1 up and 0.6 - 1 across, which is the
  // improving quadrant — so the line says where it came from, too.
  t('…then where it was a week earlier',
    lines[1] === 'Week before: [markets.gics_map_q_improve] · [markets.gics_p_1M] +7.7% · [markets.gics_p_3M] -0.4%', lines[1]);
  t('…then the weight and the count', lines[2] === 'Weight 14.2% · 20 names', lines[2]);
  t('…then the sector and its largest names', lines[3].startsWith('[gics.45] · NVDA'), lines[3]);
  t('its title is the full name', g._gicsMapTipTitle([{ raw: p }]) === '[gics.4530]');
  const bare = { ...p, prev: null };
  t('no week before, no line for it', g._gicsMapTipLines({ raw: bare }).length === 3);
}
{
  const g = load(); g._GICS.data = DATA; g.renderGicsMap();
  const c = g.charts[0], ds = c.data.datasets[0];
  const raw = code => ds.data.find(p => p.code === code);
  const colour = code => ds.backgroundColor({ raw: raw(code) });
  const Q = g._GICS_MAP_QCOL;
  t('four quadrants, four fills', new Set(Object.values(Q).map(q => q.fill)).size === 4);
  t('each bubble wears its quadrant', colour('4530') === Q.lead.fill && colour('4510') === Q.weak.fill
    && colour('4010') === Q.lag.fill, `${colour('4530')} ${colour('4510')} ${colour('4010')}`);
  t('…hovered, the solid of the same hue', ds.hoverBackgroundColor({ raw: raw('4510') }) === Q.weak.solid);
  c._active = [{ datasetIndex: 0, index: ds.data.findIndex(p => p.code === '4510') }];
  g._gicsMapPlugin.afterEvent(c);
  t('hovering a bubble brings its sector forward', g._GICS_MAP.focus === '45' && c.updates.length === 1);
  t('…its groups keep their quadrant colours and the rest fade', colour('4530') === Q.lead.fill
    && colour('4510') === Q.weak.fill && colour('4010') === g._GICS_MAP_COL.faded);
  g._gicsMapPlugin.afterEvent(c);
  t('the same hover again does not redraw', c.updates.length === 1);
  c._active = [];
  g._gicsMapPlugin.afterEvent(c);
  t('leaving the canvas clears it', g._GICS_MAP.focus === null && c.updates.length === 2 && colour('4010') === Q.lag.fill);
  t('the chart does not animate', c.options.animation === false);
}
{
  const data = structuredClone(DATA);
  data.sectors[0].groups[2].rel['3M'] = null;   // Hardware: no 3M figure
  const g = load(); g._GICS.data = data; g.renderGicsMap();
  const pts = g.charts[0].data.datasets[0].data;
  t('a group missing one window is left off, not put at zero', pts.length === 5 && !pts.some(p => p.code === '4520'));
  t('…and the note says how many', g.page.els.gicsMapNote.innerHTML.includes('<p>1 not plotted.</p>'));
  pairClick(g, '1M|6M');
  t('a pair that changes which groups plot updates the same chart with the new set',
    g.charts.length === 1 && g.charts[0].data.datasets[0].data.length === 6);
}
{
  const g = load(); g._GICS.data = { ...DATA, periods: ['1D', '1W', '1M', '3M', '6M', 'YTD'] }; g.renderGicsMap();
  const btns = [...g.page.els.gicsMapPairs.innerHTML.matchAll(/data-gics-pair="([^"]+)"/g)].map(m => m[1]);
  t('a pair is offered only when both its windows exist', btns.join() === '1W|1M,1M|3M,1M|6M', btns.join());
  g._GICS_MAP.pair = '3M|1Y'; g.renderGicsMap();
  t('…and a stale choice falls back to one that does', g._GICS_MAP.pair === '1W|1M');
}
{
  const g = load(); g._GICS.data = { ...DATA, periods: ['1D'] }; g.renderGicsMap();
  t('no usable pair keeps the card hidden and builds nothing', g.page.els.gicsMapSection.cls.has('hidden') && !g.charts.length);
}
{
  const g = load({ chartThrows: true, fetch: async () => ({ status: 200, ok: true, json: async () => DATA }) });
  await g.loader();
  t('a chart that throws leaves the table drawn', g.page.els.gicsTable.innerHTML.startsWith('<table'));
  t('…hides its own card again', g.page.els.gicsMapSection.cls.has('hidden'));
  t('…and says why in the console', g.warnings.some(w => w.includes('GICS map failed')));
}
{
  const g = load();
  t('on an axis counts as ahead of the index', g._gicsQuad(0, 0) === 'lead');
  t('the four quadrants, clockwise from top right', [g._gicsQuad(1, 1), g._gicsQuad(1, -1), g._gicsQuad(-1, -1), g._gicsQuad(-1, 1)].join()
    === 'lead,weak,lag,improve');
  const b = g._gicsMapBound([30.5, -18.7]), above = g._gicsMapBound([2, 5]);
  t('bounds are fitted to the points, with room either side', b.min < -18.7 && b.min > -25 && b.max > 30.5 && b.max < 36,
    JSON.stringify(b));
  t('…and reach zero even when every point is on one side of it', above.min < 0 && above.max > 5, JSON.stringify(above));
  t('bubble scale follows the canvas, within limits',
    g._gicsMapRadius(16, 330) < g._gicsMapRadius(16, 880) && g._gicsMapRadius(16, 5000) === g._gicsMapRadius(16, 1100));
}

console.log('\nrotation trails');
{
  const g = load(); g._GICS.data = DATA; g.renderGicsMap();
  const c = g.charts[0], pts = c.data.datasets[0].data;
  const semi = pts.find(p => p.code === '4530'), hw = pts.find(p => p.code === '4520');
  t('each group carries where it stood a week back for the pair, not five weeks back',
    semi.prev && semi.prev.x === -0.4 && semi.prev.y === 7.7, JSON.stringify(semi.prev));
  t('a week missing either figure is no line, not a line to zero', hw.prev === null);
  const { x, y } = c.options.scales;
  t('the axes fit the lines too, so none runs off the plot',
    pts.every(p => !p.prev || (p.prev.x > x.min && p.prev.x < x.max && p.prev.y > y.min && p.prev.y < y.max)));
  // Utilities sat 18.8 behind across five weeks back: in the cache, but not a
  // point the map draws, so it must not widen the frame either.
  t('…but not to weeks the map does not draw', x.min > -18.8, JSON.stringify({ x: x.min }));
  t('the note explains the lines', g.page.els.gicsMapNote.innerHTML.includes('<p>Lines from a week back.</p>'));

  // Drawn on a canvas that only records: one stroke per group, in the group's
  // quadrant colour, from a dot a week back into the bubble.
  const draw = (data = c.data) => {
    const rec = { strokes: [], dots: [], path: [], save() {}, restore() {}, beginPath() { this.path = []; },
      moveTo(px, py) { this.path.push([px, py]); }, lineTo(px, py) { this.path.push([px, py]); },
      arc(px, py) { this.path.push([px, py]); }, fill() { this.dots.push(this.path[0]); },
      stroke() { this.strokes.push({ col: this.strokeStyle, a: this.globalAlpha, lw: this.lineWidth, path: this.path }); } };
    g._gicsDrawTrails({ ctx: rec, data,
      scales: { x: { getPixelForValue: v => v * 10 }, y: { getPixelForValue: v => -v * 10 } } });
    return rec;
  };
  const all = draw();
  // Six groups, one line each, but Hardware has no figure a week back.
  t('one line per group, and none without a week before', all.strokes.length === 5, `${all.strokes.length} strokes`);
  t('…in the quadrant colour', all.strokes.some(s => s.col === g._GICS_MAP_QCOL.lead.fill)
    && all.strokes.some(s => s.col === g._GICS_MAP_QCOL.lag.fill));
  const semiIdx = pts.filter(p => p.prev).indexOf(semi);
  const from = [-0.4 * 10, -7.7 * 10], to = [0.6 * 10, -8.7 * 10];
  t('…from where it stood a week back into its bubble',
    JSON.stringify(all.strokes[semiIdx].path) === JSON.stringify([from, to]), JSON.stringify(all.strokes[semiIdx].path));
  t('…with a dot where it started, and no other', all.dots.length === 5
    && JSON.stringify(all.dots[semiIdx]) === JSON.stringify(from), JSON.stringify(all.dots));
  g._GICS_MAP.focus = '45';
  t('hovering a sector keeps only its lines', draw().strokes.length === 2, `${draw().strokes.length} strokes`);
  g._GICS_MAP.focus = null;
  // The same line drawn for an 18% group and a 0.1% one.
  const one = w => draw({ datasets: [{ data: [{ ...semi, w }] }] }).strokes[0];
  const big = one(18), small = one(0.1);
  t('a line is as heavy as its group', big.a === 1 && small.a < 0.2 && big.lw > small.lw, JSON.stringify({ big, small }));
  g._GICS_MAP.focus = semi.sector;
  const lifted = one(0.1);
  t('…unless its sector is hovered, which draws it in full', lifted.a === 1 && lifted.lw === 2, JSON.stringify(lifted));
  g._GICS_MAP.focus = null;
}
{
  // The shape gics.trail() sends now, which must draw what the cache does.
  const g = load(); g._GICS.data = TWO_WEEKS; g.renderGicsMap();
  const pts = g.charts[0].data.datasets[0].data, semi = pts.find(p => p.code === '4530');
  t('a two-close trail draws the same line', semi.prev.x === -0.4 && semi.prev.y === 7.7
    && pts.find(p => p.code === '4520').prev === null && pts.filter(p => p.prev).length === 5);
}
{
  // History that ran out: the bubble alone. No line, and no note about lines.
  const short = { ...DATA, trail: { ...DATA.trail, dates: DATA.trail.dates.slice(0, 1),
    rel: Object.fromEntries(Object.entries(DATA.trail.rel).map(([code, per]) => [code,
      Object.fromEntries(Object.entries(per).map(([p, v]) => [p, v.slice(0, 1)]))])) } };
  const g = load(); g._GICS.data = short; g.renderGicsMap();
  t('a trail of one close draws no line and explains none',
    g.charts[0].data.datasets[0].data.every(p => p.prev === null)
    && !g.page.els.gicsMapNote.innerHTML.includes('Lines from'));
}
{
  const { trail, ...bare } = DATA;
  const g = load(); g._GICS.data = bare; g.renderGicsMap();
  const pts = g.charts[0].data.datasets[0].data;
  t('a payload from before the trails still draws the bubbles', pts.length === 6 && pts.every(p => p.prev === null));
  t('…and says nothing about lines it does not have', !g.page.els.gicsMapNote.innerHTML.includes('Lines from'));
}

console.log('\nbubble labels');
{
  const g = load();
  const area = { left: 0, top: 0, right: 400, bottom: 300 };
  const measure = s => s.length * 6, H = 13;
  const place = (items, reserved) => g._gicsPlaceLabels(items, area, measure, H, reserved);
  const one = place([{ x: 100, y: 100, r: 10, text: 'Alpha' }]);
  t('the first choice is to the right of the bubble', one.length === 1 && one[0].box.l === 113 && one[0].box.t === 93.5,
    JSON.stringify(one[0] && one[0].box));
  const edge = place([{ x: 390, y: 100, r: 6, text: 'Alpha' }]);
  t('against the right edge it goes left', edge.length === 1 && edge[0].box.r === 381, JSON.stringify(edge[0] && edge[0].box));
  // Four neighbours sitting on all four of its spots.
  const boxed = place([{ x: 200, y: 150, r: 5, text: 'Hemmed' },
                       { x: 230, y: 150, r: 22, text: '' }, { x: 170, y: 150, r: 22, text: '' },
                       { x: 200, y: 125, r: 10, text: '' }, { x: 200, y: 175, r: 10, text: '' }]);
  t('a label that fits nowhere is dropped, not stacked', boxed.length === 0, JSON.stringify(boxed));
  // Small neighbours on all four sides, none on the diagonals.
  const diag = place([{ x: 200, y: 150, r: 5, text: 'Hemmed' },
                      { x: 215, y: 150, r: 6, text: '' }, { x: 185, y: 150, r: 6, text: '' },
                      { x: 200, y: 134, r: 6, text: '' }, { x: 200, y: 166, r: 6, text: '' }]);
  t('blocked on four sides, it takes a diagonal', diag.length === 1 && diag[0].box.l > 205 && diag[0].box.b < 145,
    JSON.stringify(diag[0] && diag[0].box));
  const rival = place([{ x: 100, y: 100, r: 10, text: 'First' }, { x: 100, y: 118, r: 4, text: 'Second' }]);
  t('the larger bubble, coming first, keeps its spot', rival[0] && rival[0].text === 'First' && rival[0].box.l === 113);
  const reserved = [{ l: 100, t: 80, r: 200, b: 120 }];
  const avoid = place([{ x: 90, y: 100, r: 5, text: 'Alpha' }], reserved);
  t('the quadrant names are avoided', avoid.length === 1 && !(avoid[0].box.l < 202 && avoid[0].box.r > 98), JSON.stringify(avoid[0] && avoid[0].box));

  // A crowd: whatever survives must not touch another label or another bubble,
  // and must sit inside the plot. Deterministic, so a failure reproduces.
  let s = 7;
  const rnd = () => ((s = (s * 16807) % 2147483647) / 2147483647);
  const crowd = Array.from({ length: 40 }, (_, i) => ({ x: 20 + rnd() * 360, y: 20 + rnd() * 260, r: 3 + rnd() * 14, text: 'Group ' + i }));
  const got = place(crowd);
  const hit = (a, b) => a.l < b.r && b.l < a.r && a.t < b.b && b.t < a.b;
  const onCircle = (bx, c) => {
    const nx = Math.max(bx.l, Math.min(c.x, bx.r)), ny = Math.max(bx.t, Math.min(c.y, bx.b));
    return (nx - c.x) ** 2 + (ny - c.y) ** 2 < c.r * c.r;
  };
  t('in a crowd, some labels still land', got.length >= 8, `${got.length} of 40`);
  t('…none overlaps another', got.every((a, i) => got.every((b, j) => i === j || !hit(a.box, b.box))));
  t('…or sits on another bubble', got.every(a => crowd.every(c => c.text === a.text || !onCircle(a.box, c))));
  t('…or leaves the plot', got.every(({ box }) => box.l >= 0 && box.r <= 400 && box.t >= 0 && box.b <= 300));
}

console.log('\nshort names');
{
  // Composed in JS as 'gics_short.' + code, which neither I18n.apply() nor
  // test_gics.py's scan of `markets.gics_*` keys can see.
  const src = readFileSync(join(here, '..', 'ystocker', 'static', 'i18n.js'), 'utf8');
  const codes = [...src.matchAll(/'gics\.(\d{4})'\s*:/g)].map(m => m[1]);
  const missing = codes.filter(c => !new RegExp(`'gics_short\\.${c}'\\s*:\\s*\\{\\s*en:\\s*'[^']+',\\s*zh:\\s*'[^']+'\\s*\\}`).test(src));
  t('every industry group has a short label in both languages', codes.length === 25 && !missing.length,
    `${codes.length} groups; missing ${missing.join(' ')}`);
}

console.log('\nthe block runs at all');
{
  // Everything above tests the block in isolation, which says nothing about
  // where it sits in the page. Pasted inside another function — after a
  // `return`, say — it still *parses*: declarations in a block are legal, just
  // unreachable, so the panel would sit on "Loading…" for ever with every name
  // in it block-scoped. This happened once, from a zero-context partial stage.
  // `export` is legal only at a module's top level, so a probe dropped at the
  // block's first line and handed to `node --check` (parse, never run) fails
  // exactly when the block is nested.
  const { writeFileSync, mkdtempSync } = await import('node:fs');
  const { spawnSync } = await import('node:child_process');
  const { tmpdir } = await import('node:os');
  const script = [...tpl.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
    .map(m => m[1]).find(s => s.includes(END));
  const js = script.replace(/\{\{[\s\S]*?\}\}/g, '0').replace(/\{%[\s\S]*?%\}/g, '');
  const probe = (text) => {
    const f = join(mkdtempSync(join(tmpdir(), 'gics-')), 'page.mjs');
    writeFileSync(f, text);
    return spawnSync(process.execPath, ['--check', f], { encoding: 'utf8' }).status === 0;
  };
  t('the page script parses as a module', probe(js));
  t('the GICS block is at the top level of the page script',
    probe(js.replace(START, `export const __gicsProbe = 1;\n${START}`)));
  // And the probe itself can fail: nest the same block one level down.
  t('…and the probe does fail when it is not', !probe(`{ export const x = 1; }\n${js}`));
}

console.log(failures.length
  ? `\n${failures.length} FAILED: ${failures.join(', ')}\n`
  : '\nAll checks passed.\n');
process.exit(failures.length ? 1 : 0);
