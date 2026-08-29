/**
 * Behaviour tests for the indicator windows in templates/history.html.
 *
 * `api_history` does not serve one bar size — routes.py maps 1mo/3mo to 1d bars,
 * 6mo/1y/2y to 1wk and 5y/10y to 1mo — but every window in _buildConfig was a
 * fixed *daily* constant applied to whatever arrived. On the default 1Y view
 * that is 54 weekly bars, and the failure was silent in the worst way: HV60
 * asked for 60 bars out of 54, drew a dataset with zero points, and was still
 * advertised by a legend chip. MA50 drew five points hugging the right edge.
 * Nothing threw, nothing logged, and the card looked like a rendering bug.
 *
 * So these tests assert on *drawn point counts and legend chips*, not on whether
 * a config was produced. A config is always produced; that was never the bug.
 *
 * The DOM here is deliberately thin — enough for _applyChips to find a chip and
 * hide its wrapper — because the thing under test is arithmetic over a series,
 * and a real browser would not make a 60-bar window fit into 54 bars either.
 *
 * Run: node tests/check_history_indicators.mjs
 */
import fs from 'fs';
import path from 'path';
import vm from 'vm';

const root = path.resolve(import.meta.dirname, '..');
const SRC = path.join(root, 'ystocker/templates/history.html');

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${detail ? '  ' + detail : ''}`);
  if (!cond) failures.push(label);
};

// ── Load the template's inline JS into a sandbox ────────────────────────────
// Jinja expressions are neutralised rather than rendered: none of the chart
// builders read one, and standing up Flask to get at a pure function would make
// this a slow integration test for no extra coverage.
const html = fs.readFileSync(SRC, 'utf8');
const blocks = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (blocks.length === 0) { console.error('no inline <script> found in history.html'); process.exit(1); }
const js = blocks.join('\n;\n')
  .replace(/\{\{[\s\S]*?\}\}/g, 'JINJA')
  .replace(/\{%[\s\S]*?%\}/g, '');

// Chip elements, keyed by the ids history.html writes into.
const els = {};
const CHIP_IDS = ['legendMaS', 'legendMaL', 'legendBb', 'legendVolMa',
                  'legendHvS', 'legendHvL', 'rsiWin', 'stochWin'];
function makeEl(id) {
  const wrapper = { style: {}, tagName: 'SPAN' };
  return { id, style: {}, textContent: '', innerHTML: '', tagName: 'SPAN',
           className: '', dataset: {}, classList: { add(){}, remove(){}, toggle(){} },
           addEventListener(){}, appendChild(){}, insertBefore(){}, removeAttribute(){},
           setAttribute(){}, getAttribute: () => null, scrollIntoView(){},
           querySelector: () => null, querySelectorAll: () => [],
           closest: sel => (sel === 'span.flex' ? wrapper : null), _wrapper: wrapper };
}
CHIP_IDS.forEach(id => { els[id] = makeEl(id); });
// The page's bootstrap wires listeners onto ids that have nothing to do with the
// builders (the expand modal, the tab strip). Handing back a live stub lets the
// whole block run to completion, which matters because the window table is a
// top-level `const`: an exception before it aborts leaves it permanently in TDZ
// and every later _indWindows() call throws instead of returning windows.
const spares = {};
function elFor(id) {
  if (els[id]) return els[id];
  return (spares[id] ||= makeEl(id));
}

const sandbox = {
  console,
  I18n: {
    // Only the bar-unit words matter here; everything else falls back.
    t: k => ({ 'history.bar_unit_d': 'd', 'history.bar_unit_w': 'w',
               'history.bar_unit_m': 'm' })[k] ?? null,
    label: (k, fb) => ({ label: fb, i18nKey: k }),
    axis: (k, fb) => ({ text: fb, i18nKey: k }),
    apply: () => {}, getLang: () => 'en', t_: null,
  },
  CT: { c: v => v },
  Chart: function () { return { destroy() {}, update() {} }; },
  document: {
    getElementById: elFor,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
    createElement: () => makeEl('created'),
    dispatchEvent: () => {},
    body: makeEl('body'),
    head: makeEl('head'),
    documentElement: makeEl('html'),
  },
  window: { addEventListener: () => {}, matchMedia: () => ({ matches: false, addEventListener() {} }) },
  localStorage: { getItem: () => null, setItem: () => {} },
  fetch: () => Promise.reject(new Error('no network in this test')),
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0,
  location: { href: 'http://localhost/history/SMCI', search: '' },
  navigator: { onLine: true },
  DeferLoad: { when: () => {} },
  URLSearchParams,
  // Stand-in for every {{ ... }} the template interpolates; `const TICKER =
  // {{ ticker }}` is the first statement in the block, so leaving it undefined
  // aborts the bootstrap before the window table is even initialised.
  JINJA: 'SMCI',
  Date, Math, JSON, Array, Object, String, Number, Boolean, isNaN, parseFloat, parseInt,
};
sandbox.globalThis = sandbox;
sandbox.window.document = sandbox.document;

const ctx = vm.createContext(sandbox);
try {
  vm.runInContext(js, ctx, { filename: 'history.html:inline' });
} catch (e) {
  // Top-level page bootstrap may reference things a headless run has no use
  // for. The builders are hoisted function declarations, so they are defined
  // regardless — only bail if the one under test is missing.
  // The builders are hoisted function declarations, so a failure in the page's
  // bootstrap does not remove them -- but the window table is a top-level
  // `const`, and an exception before it leaves that binding in TDZ so every
  // later _indWindows() call throws. Treat any bootstrap error as fatal.
  console.error('history.html bootstrap failed to evaluate:', e.message);
  console.error(e.stack.split('\n').slice(1, 4).join('\n'));
  process.exit(1);
}
const { _buildConfig, _indWindows, _barUnit } = ctx;
t('inline JS exposes _buildConfig', typeof _buildConfig === 'function');

// ── Fixtures ────────────────────────────────────────────────────────────────
// A geometric-ish walk is enough: every assertion below is about how many bars a
// window consumes, which is independent of the values. Deterministic (no
// Math.random) so a failure is reproducible.
function series(nBars, stepDays, start = new Date('2025-08-25T00:00:00Z')) {
  const dates = [], prices = [], highs = [], lows = [], volumes = [];
  let p = 40;
  for (let i = 0; i < nBars; i++) {
    p = p * (1 + 0.04 * Math.sin(i * 1.7) + 0.002);
    const d = new Date(start.getTime() + i * stepDays * 86400000);
    dates.push(d.toISOString().slice(0, 10));
    prices.push(Math.round(p * 100) / 100);
    highs.push(Math.round(p * 1.03 * 100) / 100);
    lows.push(Math.round(p * 0.97 * 100) / 100);
    volumes.push(1_000_000 + i * 1000);
  }
  return { ticker: 'TEST', dates, prices, highs, lows, volumes,
           pe_history: prices.map(v => v / 3.26),
           fwd_pe_history: prices.map(v => v / 5.32),
           peg_history: prices.map(() => 1.2),
           relative_strength: prices.map(() => 100),
           avg_volume: 1_200_000, earnings_markers: [] };
}
const STATIC = { current_pe: 11.4, forward_pe: 7.0, target_price: 50,
                 call_wall: null, put_wall: null, earnings_markers: [] };

const drawn = ds => (ds?.data || []).filter(v => v != null).length;
const byLabel = (cfg, pred) => cfg.data.datasets.find(x => pred(x.label || ''));
const chip = id => els[id];
const chipHidden = id => els[id]._wrapper.style.display === 'none';

function build(key, d) {
  CHIP_IDS.forEach(id => { els[id].textContent = ''; els[id]._wrapper.style.display = ''; });
  const cfg = _buildConfig(key, d, STATIC);
  ctx._applyChips(cfg, d.dates);
  return cfg;
}

// ── 1Y weekly, the default view and the one that was broken ────────────────
// 54 weekly bars is what Yahoo actually returns for range=1y&interval=1wk.
const wk1y = series(54, 7);
console.log('\n1Y view — 54 weekly bars (the default):');
t('bar size detected as weekly', _barUnit(wk1y.dates) === 'w', `got '${_barUnit(wk1y.dates)}'`);

const hv = build('hv', wk1y);
const hvS = byLabel(hv, l => l.startsWith('HV') && hv.data.datasets.indexOf(byLabel(hv, x => x === l)) === 0);
t('HV draws two lines', hv.data.datasets.length === 2, `got ${hv.data.datasets.length}`);
hv.data.datasets.forEach(ds => {
  t(`HV "${ds.label}" is not empty`, drawn(ds) > 0, `${drawn(ds)} points`);
  t(`HV "${ds.label}" clears the 5-point floor`, drawn(ds) >= 5, `${drawn(ds)} points`);
});
t('HV labels state weeks, not days',
  hv.data.datasets.every(ds => /^HV\d+w$/.test(ds.label)),
  hv.data.datasets.map(ds => ds.label).join(', '));
t('HV legend chips match the drawn lines',
  chip('legendHvS').textContent === hv.data.datasets[0].label &&
  chip('legendHvL').textContent === hv.data.datasets[1].label,
  `${chip('legendHvS').textContent} / ${chip('legendHvL').textContent}`);
t('no HV chip left hidden', !chipHidden('legendHvS') && !chipHidden('legendHvL'));

const price = build('price', wk1y);
const maS = byLabel(price, l => /^\d+w MA$/.test(l) && l.startsWith(String(_indWindows(wk1y.dates).maS)));
const maL = byLabel(price, l => /^\d+w MA$/.test(l) && l.startsWith(String(_indWindows(wk1y.dates).maL)));
t('price chart has both moving averages', !!maS && !!maL);
t('short MA is substantial', drawn(maS) >= 40, `${drawn(maS)} of 54`);
t('long MA is substantial (was 5 of 54)', drawn(maL) >= 30, `${drawn(maL)} of 54`);
t('MA labels state weeks', /w MA$/.test(maS.label) && /w MA$/.test(maL.label),
  `${maS.label} / ${maL.label}`);
const bbU = byLabel(price, l => l === 'BB Upper');
const bbL = byLabel(price, l => l === 'BB Lower');
t('Bollinger bands present and substantial', drawn(bbU) >= 30 && drawn(bbL) >= 30,
  `${drawn(bbU)} / ${drawn(bbL)}`);
t('BB Upper still fills to the band below it', bbU.fill === '+1');
t('BB Upper is immediately followed by BB Lower',
  price.data.datasets.indexOf(bbL) === price.data.datasets.indexOf(bbU) + 1);
t('MA legend chips renamed to weeks',
  chip('legendMaS').textContent === maS.label && chip('legendMaL').textContent === maL.label,
  `${chip('legendMaS').textContent} / ${chip('legendMaL').textContent}`);

const rsi = build('rsi', wk1y);
t('RSI header says weeks, not the old "14日"', chip('rsiWin').textContent === '(14w)',
  chip('rsiWin').textContent);
t('RSI still draws', drawn(byLabel(rsi, l => l === 'RSI')) >= 35,
  `${drawn(byLabel(rsi, l => l === 'RSI'))} of 54`);

const stoch = build('stoch', wk1y);
t('Stochastic header states the unit', chip('stochWin').textContent === '(14w, 3)',
  chip('stochWin').textContent);
t('%K draws', drawn(byLabel(stoch, l => l === '%K')) >= 35);
t('%D draws', drawn(byLabel(stoch, l => l === '%D')) >= 35);

const vol = build('volume', wk1y);
t('volume MA is labelled in weeks', /w MA$/.test(byLabel(vol, l => /MA/.test(l)).label),
  byLabel(vol, l => /MA/.test(l)).label);

// ── 1M daily, where the same latent bug bit MA50 and HV60 ──────────────────
// ~21 daily bars cannot fill a 50- or 60-bar window either. Before the fix both
// were emitted as empty datasets behind visible legend chips; now they are
// dropped and their chips hidden, so the card never advertises a missing line.
const d1mo = series(21, 1);
console.log('\n1M view — 21 daily bars:');
t('bar size detected as daily', _barUnit(d1mo.dates) === 'd', `got '${_barUnit(d1mo.dates)}'`);

const hvD = build('hv', d1mo);
t('no empty HV dataset is emitted', hvD.data.datasets.every(ds => drawn(ds) >= 5),
  hvD.data.datasets.map(ds => `${ds.label}:${drawn(ds)}`).join(' ') || '(none)');
t('a dropped HV line has no legend chip',
  hvD.data.datasets.length === 0 ? (chipHidden('legendHvS') && chipHidden('legendHvL')) : true);

const priceD = build('price', d1mo);
t('no empty price dataset is emitted', priceD.data.datasets.every(ds => drawn(ds) >= 5),
  priceD.data.datasets.map(ds => `${ds.label}:${drawn(ds)}`).join(' '));
t('dropped MA lines have their chips hidden',
  priceD.data.datasets.some(ds => /MA/.test(ds.label || '')) || chipHidden('legendMaL'));
t('price line itself always survives', drawn(byLabel(priceD, l => l === 'Price')) === 21);

// ── 10Y monthly ─────────────────────────────────────────────────────────────
const mo10y = series(120, 30);
console.log('\n10Y view — 120 monthly bars:');
t('bar size detected as monthly', _barUnit(mo10y.dates) === 'm', `got '${_barUnit(mo10y.dates)}'`);
const hvM = build('hv', mo10y);
t('HV draws two lines', hvM.data.datasets.length === 2);
t('HV labels state months', hvM.data.datasets.every(ds => /^HV\d+m$/.test(ds.label)),
  hvM.data.datasets.map(ds => ds.label).join(', '));
t('no empty HV dataset', hvM.data.datasets.every(ds => drawn(ds) >= 5));
const rsiM = build('rsi', mo10y);
t('RSI header states months', chip('rsiWin').textContent === '(14m)', chip('rsiWin').textContent);

// ── Every view: nothing is ever advertised but undrawn ─────────────────────
// The general invariant, checked across all seven range buttons and all five
// price-derived cards. This is the assertion that would have caught HV60.
console.log('\nInvariant — no advertised-but-empty series on any range:');
const VIEWS = [
  ['1M', series(21, 1)], ['3M', series(63, 1)], ['6M', series(26, 7)],
  ['1Y', series(54, 7)], ['2Y', series(104, 7)], ['5Y', series(60, 30)], ['10Y', series(120, 30)],
];
let bad = [];
for (const [name, d] of VIEWS) {
  for (const key of ['price', 'volume', 'rsi', 'stoch', 'hv', 'delta']) {
    const cfg = build(key, d);
    cfg.data.datasets.forEach(ds => {
      // Constant reference lines (the 70/30 and 80/20 bands) are fill()ed
      // arrays, not series -- they are drawn at every index by construction.
      if (/^(OB|OS)\b/.test(ds.label || '')) return;
      if (drawn(ds) === 0) bad.push(`${name}/${key}: "${ds.label}" empty`);
    });
    // And the other half of the same invariant: a visible chip must correspond
    // to something on the canvas. Read the chip as _applyChips actually left it
    // rather than re-deriving the text, so the assertion covers that path too.
    Object.keys(cfg._chips || {}).forEach(id => {
      const el = els[id];
      if (!el || chipHidden(id)) return;              // hidden is a valid answer
      const text = el.textContent;
      if (!text) return;
      // Header suffixes are parenthesised windows, not dataset names, and the
      // BB chip names the band *window* while the datasets are Upper/Lower.
      const named = /^\(/.test(text)
        ? true
        : /^BB\(/.test(text)
          ? cfg.data.datasets.some(ds => (ds.label || '').startsWith('BB'))
          : cfg.data.datasets.some(ds => (ds.label || '') === text);
      if (!named) bad.push(`${name}/${key}: chip #${id} "${text}" names no dataset`);
    });
  }
}
t('no empty dataset on any range/card', bad.length === 0, bad.slice(0, 8).join(' | '));

console.log('');
if (failures.length) {
  console.error(`${failures.length} failure(s):`);
  failures.forEach(f => console.error('  - ' + f));
  process.exit(1);
}
console.log('all history indicator checks passed');
