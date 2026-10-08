/**
 * Behaviour tests for static/sector_map.js -- what /sectors derives from the
 * /api/sectors payload: which rows a view shows and in what order, the
 * sentence at the top, where the plot's axes stop, and where labels go.
 *
 * The failures worth catching all draw a plausible page: a sentence naming a
 * one-company "industry" as where money went, an axis fitted to one outlier
 * that squeezes 125 bubbles into a corner, a missing figure sorted as zero
 * into the middle of the list, a drill-down listing another sector's members.
 *
 * Run: node tests/check_sector_map_js.mjs
 */
import { createRequire } from 'module';
import path from 'path';

const root = path.resolve(import.meta.dirname, '..');
const require = createRequire(import.meta.url);
const SM = require(path.join(root, 'ystocker/static/sector_map.js'));

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${detail ? '  ' + detail : ''}`);
  if (!cond) failures.push(label);
};
const near = (a, b, eps = 1e-9) => Math.abs(a - b) < eps;

const sub = (id, name, zh, group, n, weight, r1d, r1w, r1m, quad) => ({
  id, name, name_zh: zh, group, sector: group.slice(0, 2), n, weight,
  returns: { '1D': r1d, '1W': r1w, '1M': r1m }, rel: { '1W': r1w, '1M': r1m }, quad,
});
const PAYLOAD = {
  min_headline_members: 3,
  index: { returns: { '1D': 0.59, '1W': 1.9, '1M': 1.29 } },
  levels: {
    sector: [{ id: '45', name: 'Information Technology', weight: 40, n: 3, returns: { '1D': 1.0 }, quad: 'lead' },
             { id: '55', name: 'Utilities', weight: 3, n: 3, returns: { '1D': 3.0 }, quad: 'improve' }],
    group: [{ id: '4530', name: 'Semiconductors & Semiconductor Equipment', sector: '45', weight: 30, n: 2, returns: { '1D': 1.2 } },
            { id: '4520', name: 'Technology Hardware & Equipment', sector: '45', weight: 10, n: 1, returns: { '1D': 0.4 } },
            { id: '5510', name: 'Utilities', sector: '55', weight: 3, n: 3, returns: { '1D': 3.0 } }],
    sub: [
      sub('semiconductors', 'Semiconductors', '半导体', '4530', 2, 30, 1.2, 4, 8, 'lead'),
      sub('communications-equipment', 'Communications Equipment', '通信设备', '4520', 1, 10, 9.0, 1, -2, 'improve'),
      sub('electric-utilities', 'Electric Utilities', '电力公用事业', '5510', 3, 3, 3.0, 2, -3, 'improve'),
      sub('gold', 'Gold', '黄金', '1510', 1, 0.1, null, -1, -4, 'lag'),
      sub('steel', 'Steel', '钢铁', '1510', 4, 0.2, -2.0, -3, 5, 'weak'),
    ],
  },
  members: [
    { t: 'NVDA', s: 'semiconductors', w: 20 }, { t: 'AMD', s: 'semiconductors', w: 10 },
    { t: 'CSCO', s: 'communications-equipment', w: 10 },
    { t: 'NEE', s: 'electric-utilities', w: 1.5 }, { t: 'SO', s: 'electric-utilities', w: 1 }, { t: 'DUK', s: 'electric-utilities', w: 0.5 },
  ],
};

console.log('quadrant');
t('the rotation map rule', ['lead', 'weak', 'lag', 'improve'].join() ===
  [SM.quadrant(1, 1), SM.quadrant(1, -1), SM.quadrant(-1, -1), SM.quadrant(-1, 1)].join());
t('zero is not behind', SM.quadrant(0, 0) === 'lead');
t('a missing window is none', SM.quadrant(null, 1) === null && SM.quadrant(1, undefined) === null);

console.log('select');
const byDay = SM.select(PAYLOAD, 'sub', { win: '1D' });
t('sorted by the window, largest first', byDay.map(r => r.id).slice(0, 3).join() === 'communications-equipment,electric-utilities,semiconductors');
t('a missing figure sorts last', byDay[byDay.length - 1].id === 'gold');
const asc = SM.select(PAYLOAD, 'sub', { sort: { key: '1D', dir: 1 } });
t('...whichever way the sort runs', asc[asc.length - 1].id === 'gold' && asc[0].id === 'steel');
t('a quadrant filter', SM.select(PAYLOAD, 'sub', { quad: 'improve' }).map(r => r.id).sort().join() === 'communications-equipment,electric-utilities');
t('a drill-down keeps its own sector', SM.select(PAYLOAD, 'sub', { parent: '45' }).every(r => r.sector === '45'));
t('...or its own group', SM.select(PAYLOAD, 'sub', { parent: '4530' }).map(r => r.id).join() === 'semiconductors');
t('search finds an industry by either name',
  SM.select(PAYLOAD, 'sub', { query: '电力' }).map(r => r.id).join() === 'electric-utilities'
  && SM.select(PAYLOAD, 'sub', { query: 'electric' }).map(r => r.id).join() === 'electric-utilities');
t('search finds an industry by a member ticker',
  SM.select(PAYLOAD, 'sub', { query: 'nee', members: PAYLOAD.members, names: {} }).map(r => r.id).join() === 'electric-utilities');
t('...or a member name', SM.select(PAYLOAD, 'sub', { query: 'cisco', members: PAYLOAD.members, names: { CSCO: 'Cisco Systems' } })
  .map(r => r.id).join() === 'communications-equipment');
t('sort by name', SM.select(PAYLOAD, 'sub', { sort: { key: 'name', dir: 1 } })[0].id === 'communications-equipment');
t('no payload is no rows', SM.select(null, 'sub', {}).length === 0);

console.log('verdict');
const v = SM.verdict(PAYLOAD, 'sub', '1D', 3);
t('counts what rose and fell, not the missing', v.up === 3 && v.down === 1 && v.flat === 0 && v.total === 5);
t('a one-company industry is never named', !v.top.some(r => r.n < 3), v.top.map(r => r.id).join());
t('the strongest named, largest move first', v.top.map(r => r.id).join() === 'electric-utilities');
t('the weakest named', v.bottom.map(r => r.id).join() === 'steel');
t('quadrant counts', v.quads.improve === 2 && v.quads.lead === 1 && v.quads.weak === 1 && v.quads.lag === 1);
t('the index figure for the window', v.index === 0.59);
const vs = SM.verdict(PAYLOAD, 'sector', '1D', 3);
t('sectors may be named whatever their size', vs.top.map(r => r.id).join() === '55,45');
t('an unknown window falls back to 1D', SM.verdict(PAYLOAD, 'sub', 'ZZ', 3).win === '1D');

console.log('bound and clamp');
const many = Array.from({ length: 125 }, (_, i) => (i % 25) - 12).concat([40]);
const b = SM.bound(many);
t('one outlier does not set the frame', b.max < 20 && b.max > 12, JSON.stringify(b));
t('the frame always reaches past zero', SM.bound([3, 4, 5, 6, 7, 8, 9, 10, 11, 12]).min < 0);
t('a short list keeps its extremes', SM.bound([-30, 1, 2]).min < -30);
const c = SM.clamp(40, b);
t('a point beyond is drawn on the edge and marked', c.v === b.max && c.out === true);
t('a point inside is left alone', SM.clamp(1, b).v === 1 && SM.clamp(1, b).out === false);
t('nothing to fit is a unit frame', JSON.stringify(SM.bound([])) === JSON.stringify({ min: -1, max: 1 }));

console.log('radius and colour');
t('area, not radius, in proportion', near(SM.radius(4, 800, 'sub') / SM.radius(1, 800, 'sub'), 2));
t('a tiny industry is still visible', SM.radius(0.001, 800, 'sub') >= 2.5);
const up = [0, 200, 0], down = [200, 0, 0], flat = [9, 9, 9];
t('up is green, down red', SM.colour(1, 2, up, down, flat).startsWith('rgba(0,200,0') && SM.colour(-1, 2, up, down, flat).startsWith('rgba(200,0,0'));
t('deeper further from zero', parseFloat(SM.colour(2, 2, up, down, flat).split(',')[3]) > parseFloat(SM.colour(0.2, 2, up, down, flat).split(',')[3]));
t('a missing figure is grey', SM.colour(null, 2, up, down, flat).startsWith('rgba(9,9,9'));

console.log('placeLabels');
const area = { left: 0, top: 0, right: 300, bottom: 200 };
const placed = SM.placeLabels([
  { id: 'a', x: 100, y: 100, r: 10, text: 'Alpha' },
  { id: 'b', x: 104, y: 100, r: 10, text: 'Beta' },
  { id: 'c', x: 298, y: 100, r: 5, text: 'Gamma' },
], area, s => s.length * 6, 12);
const boxes = placed.map(p => p.box);
const overlap = (a, b) => a.l < b.r && b.l < a.r && a.t < b.b && b.t < a.b;
t('labels never overlap', boxes.every((x, i) => boxes.every((y, j) => i === j || !overlap(x, y))));
t('labels stay inside the plot', boxes.every(x => x.l >= 0 && x.r <= 300 && x.t >= 0 && x.b <= 200));
t('a label against the edge goes to the other side', (placed.find(p => p.id === 'c') || {}).box?.r <= 298 - 5);

console.log('membersOf and children');
const eu = SM.membersOf(PAYLOAD, 'sub', PAYLOAD.levels.sub[2]);
t('a sub-industry lists its own members, heaviest first', eu.map(m => m.t).join() === 'NEE,SO,DUK');
t('shares of the group add to 100', near(eu.reduce((a, m) => a + m.share, 0), 100, 1e-6));
t('a sector lists every member under it', SM.membersOf(PAYLOAD, 'sector', PAYLOAD.levels.sector[0]).map(m => m.t).join() === 'NVDA,AMD,CSCO');
t('an industry group lists its own', SM.membersOf(PAYLOAD, 'group', PAYLOAD.levels.group[0]).map(m => m.t).join() === 'NVDA,AMD');
t('a sector’s children are its groups', SM.children(PAYLOAD, 'sector', PAYLOAD.levels.sector[0]).map(g => g.id).join() === '4530,4520');
t('a group’s children are its sub-industries', SM.children(PAYLOAD, 'group', PAYLOAD.levels.group[0]).map(s => s.id).join() === 'semiconductors');
t('a sub-industry has none', SM.children(PAYLOAD, 'sub', PAYLOAD.levels.sub[0]).length === 0);

console.log('label');
const tr = k => ({ 'gics.45': '信息技术' })[k] || '';
t('a sub-industry in Chinese uses its own name', SM.label(PAYLOAD.levels.sub[0], 'sub', true, tr) === '半导体');
t('...and in English its GICS name', SM.label(PAYLOAD.levels.sub[0], 'sub', false, tr) === 'Semiconductors');
t('a sector uses the gics.<code> string', SM.label(PAYLOAD.levels.sector[0], 'sector', true, tr) === '信息技术');
t('...falling back to its name', SM.label(PAYLOAD.levels.sector[1], 'sector', true, tr) === 'Utilities');

if (failures.length) {
  console.log(`\n${failures.length} FAILED`);
  process.exit(1);
}
console.log('\nall ok');
