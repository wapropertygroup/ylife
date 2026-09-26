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
  const el = id => (els[id] ??= { id, innerHTML: '', _handlers: {},
    addEventListener(ev, fn) { this._handlers[ev] = fn; } });
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
// asserts both languages keep the placeholder.
const TEMPLATES = { 'markets.gics_asof': 'As of the {date} close.',
                    'markets.gics_note_method': 'Weights from SPY holdings ({date}).' };
const I18n = { t: k => TEMPLATES[k] ?? `[${k}]` };

function load({ fetch = async () => ({ status: 200, ok: true, json: async () => ({}) }) } = {}) {
  const page = makePage();
  let loader = null;
  const DeferLoad = { when: (_sel, fn) => { loader = fn; } };
  let clock = 0;
  const fakeDate = { now: () => clock, parse: s => Date.parse(s) };
  const fakeTimeout = (fn, ms) => { clock += ms; fn(); };
  const api = new Function('document', 'DeferLoad', 'I18n', 'fetch', 'setTimeout', 'Date', 'console',
    `${block}\nreturn { _GICS, renderGics, _gicsTone, _gicsFmt, _gicsSorted };`)(
    page.document, DeferLoad, I18n, fetch, fakeTimeout, fakeDate, { warn() {}, info() {} });
  return { ...api, page, loader: () => loader() };
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
