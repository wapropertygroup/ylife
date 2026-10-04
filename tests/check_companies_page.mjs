/**
 * Behaviour tests for the /companies page script (templates/companies.html):
 * the sort, the Watchlist view and the star on every card.
 *
 * Each of these fails as a plausible page, not an error:
 * - a sort that interleaves the quoted companies with SEC's list draws the
 *   "every other company" divider dozens of times;
 * - a loss-making company sorted "lowest P/E first" heads the list as the
 *   cheapest stock there is;
 * - a starred ticker that is in neither list just vanishes from the
 *   Watchlist, with no card to open or unstar.
 * So this runs the page's own script, extracted from the template rather than
 * copied (a copy agrees on the day it is written and drifts after), against a
 * small fake DOM and the real static/watchlist.js.
 *
 * Run: node tests/check_companies_page.mjs
 */
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import vm from 'node:vm';

const root = path.resolve(import.meta.dirname, '..');
const require = createRequire(import.meta.url);
const W = require(path.join(root, 'ystocker/static/watchlist.js'));
const tpl = readFileSync(path.join(root, 'ystocker/templates/companies.html'), 'utf8');

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${detail ? '  ' + detail : ''}`);
  if (!cond) failures.push(label);
};
const eq = (label, got, want) => t(label, JSON.stringify(got) === JSON.stringify(want),
  `got ${JSON.stringify(got)} want ${JSON.stringify(want)}`);

// The page's inline script: the <script> block that follows watchlist.js.
const SCRIPT = (() => {
  const m = /watchlist\.js[^>]*><\/script>\s*<script>([\s\S]*?)<\/script>/.exec(tpl);
  if (!m) throw new Error('page script not found after watchlist.js in companies.html');
  return m[1];
})();

// Followed companies, as routes._company_cards builds them: largest first.
const ROWS = [
  { t: 'NVDA', n: 'NVIDIA', p: 180.1, c: 1.5, m: 4400, pe: 50.2, y: 40.1, g: ['Semis'] },
  { t: 'MSFT', n: 'Microsoft', p: 510, c: -0.4, m: 3800, pe: 36.0, y: 20.0, g: ['Software'] },
  { t: 'AAPL', n: 'Apple', p: 250, c: 0.0, m: 3700, pe: 33.0, y: -5.0, g: ['Hardware'] },
  { t: 'INTC', n: 'Intel', p: 30, c: 3.2, m: 130, pe: -12.0, y: 55.0, g: ['Semis'] },
  { t: 'QQQQ', n: 'No quote yet', p: null, c: null, m: 10, pe: null, y: null, g: ['Semis'] },
];
// SEC's list, in SEC's order: [ticker, name, exchange, also].
const DIRECTORY = [
  ['NVDA', 'NVIDIA CORP', 'Nasdaq', ''],          // followed: folds into its card
  ['ZETA', 'Zeta Global', 'NYSE', ''],
  ['ACME', 'Acme <script>alert(1)</script> Inc', 'OTC', 'ACMEW'],
  ['BETA', 'Beta Technologies', 'NYSE', ''],
];
const STRINGS = {
  'companies.watch_empty': 'WATCH-EMPTY',
  'companies.divider': 'DIVIDER-ALL',
  'companies.divider_watch': 'DIVIDER-WATCH',
  'companies.star_add': 'Add to watchlist',
  'companies.star_remove': 'Remove from watchlist',
  'companies.watch_local': 'kept in this browser',
};

function makeEl(id, extra = {}) {
  const listeners = {};
  const el = {
    id, hidden: false, textContent: '', innerHTML: '', title: '', dataset: {}, attrs: {},
    _classes: new Set(),
    setAttribute(k, v) { el.attrs[k] = String(v); },
    getAttribute(k) { return k in el.attrs ? el.attrs[k] : null; },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    fire(type, ev = {}) { (listeners[type] || []).forEach(fn => fn({ target: el, ...ev })); },
    closest() { return null; },
    ...extra,
  };
  el.classList = {
    add: c => el._classes.add(c),
    remove: c => el._classes.delete(c),
    contains: c => el._classes.has(c),
    toggle: (c, force) => {
      const on = force === undefined ? !el._classes.has(c) : !!force;
      if (on) el._classes.add(c); else el._classes.delete(c);
      return on;
    },
  };
  return el;
}

// A <select>: assigning a value it has no option for leaves it empty.
function makeSelect(id, options) {
  let value = '';
  const el = makeEl(id);
  Object.defineProperty(el, 'value', {
    get: () => value,
    set: v => { value = options.includes(String(v)) ? String(v) : ''; },
  });
  return el;
}

const unesc = s => s.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<')
  .replace(/&gt;/g, '>').replace(/&amp;/g, '&');

async function load({ search = '', withWatchlist = true, stored = null, directory = DIRECTORY } = {}) {
  const els = {
    coSearch: Object.assign(makeEl('coSearch'), { value: '' }),
    coGroup: makeSelect('coGroup', ['', 'Semis', 'Software', 'Hardware']),
    coExch: makeSelect('coExch', ['', 'NYSE', 'Nasdaq', 'CBOE', 'OTC']),
    coSort: makeSelect('coSort', ['', 'chg', 'pe', 'y52', 'az']),
    coMeta: makeEl('coMeta'), coGrid: makeEl('coGrid'), coEmpty: makeEl('coEmpty'),
    coMore: makeEl('coMore'), coWatchN: makeEl('coWatchN'),
  };
  const chips = ['all', 'watch', 'followed', 'gainers', 'losers'].map(v => {
    const c = makeEl(null);
    c.dataset.coView = v;
    c.closest = sel => (sel === '[data-co-view]' ? c : null);
    return c;
  });
  // Stars are parsed out of the grid's HTML, once per render, so a repaint in
  // place can be inspected until the next render replaces them.
  let starCache = { html: null, list: [] };
  const stars = () => {
    const html = els.coGrid.innerHTML;
    if (starCache.html !== html) {
      const list = [];
      const re = /<button type="button" class="co-star( is-on)?" data-co-star="([^"]*)"\s+aria-pressed="(true|false)"[^>]*>([^<]*)<\/button>/g;
      for (const m of html.matchAll(re)) {
        const b = makeEl(null);
        b.dataset.coStar = unesc(m[2]);
        b._classes = new Set(m[1] ? ['co-star', 'is-on'] : ['co-star']);
        b.attrs['aria-pressed'] = m[3];
        b.textContent = m[4];
        b.closest = sel => (sel === '[data-co-star]' ? b : null);
        list.push(b);
      }
      starCache = { html, list };
    }
    return starCache.list;
  };
  const docListeners = {};
  const document = {
    getElementById: id => els[id] || null,
    querySelectorAll(sel) {
      if (sel === '[data-co-view]') return chips;
      if (sel === '[data-co-star]') return stars();
      throw new Error(`unexpected selector ${sel}`);
    },
    querySelector(sel) {
      const m = /^\[data-co-view="(\w+)"\]$/.exec(sel);
      if (m) return chips.find(c => c.dataset.coView === m[1]) || null;
      throw new Error(`unexpected selector ${sel}`);
    },
    addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
    dispatchEvent(ev) { (docListeners[ev.type] || []).forEach(fn => fn(ev)); },
  };
  const storage = {
    data: stored ? { ystocker_watchlist: JSON.stringify(stored) } : {},
    getItem(k) { return k in this.data ? this.data[k] : null; },
    setItem(k, v) { this.data[k] = String(v); },
  };
  const watchlist = withWatchlist
    ? W.create(storage, list => document.dispatchEvent({ type: 'watchlist:change', detail: { list } }))
    : undefined;
  const href = `https://stock.li-family.us/companies${search}`;
  const urls = [];
  let release;
  const gate = new Promise(r => { release = r; });
  const ctx = {
    document, console, URL, URLSearchParams, setTimeout, Promise, Map, Set,
    location: { search, href, pathname: '/companies' },
    history: { state: null, replaceState: (s, _, u) => urls.push(String(u)) },
    I18n: { t: k => STRINGS[k] ?? null, stockName: (tk, n) => n, datetime: s => `@${s}` },
    // The directory answers only when the test says so, so the moment before
    // SEC's list arrives can be inspected too.
    fetch: async () => {
      await gate;
      return { status: 200, json: async () => ({ companies: directory, as_of: '2026-10-04T00:00:00Z' }) };
    },
  };
  ctx.window = { Watchlist: watchlist, CT: undefined };
  const code = SCRIPT
    .replace('{{ companies | tojson }}', JSON.stringify(ROWS))
    .replace('{{ updated | tojson }}', 'null')
    .replace('{{ warming | tojson }}', 'false');
  vm.runInContext(code, vm.createContext(ctx));
  const page = {
    els, chips, urls, watchlist, storage, stars,
    async ready() { release(); for (let i = 0; i < 5; i++) await new Promise(r => setTimeout(r, 0)); },
    order: () => [...els.coGrid.innerHTML.matchAll(/<span class="co-ticker">([^<]*)<\/span>/g)].map(m => m[1]),
    dividers: () => (els.coGrid.innerHTML.match(/class="co-divider"/g) || []).length,
    click(target) { (docListeners.click || []).forEach(fn => fn({ target })); },
    chip(v) { page.click(chips.find(c => c.dataset.coView === v)); },
    star(tk) {
      const b = stars().find(s => s.dataset.coStar === tk);
      if (!b) throw new Error(`no star for ${tk}`);
      page.click(b);
      return b;
    },
    sort(v) { els.coSort.value = v; els.coSort.fire('change'); },
  };
  return page;
}

console.log('default order');
{
  const p = await load();
  eq('before SEC\'s list: the followed companies, largest first', p.order(), ['NVDA', 'MSFT', 'AAPL', 'INTC', 'QQQQ']);
  await p.ready();
  eq('then SEC\'s list after them, in SEC\'s order, the followed NVDA folded in',
    p.order(), ['NVDA', 'MSFT', 'AAPL', 'INTC', 'QQQQ', 'ZETA', 'ACME', 'BETA']);
  eq('one divider where the quotes end', p.dividers(), 1);
  t('a name from SEC is escaped', p.els.coGrid.innerHTML.includes('Acme &lt;script&gt;')
    && !p.els.coGrid.innerHTML.includes('<script>'));
  eq('a star on every card', p.stars().length, 8);
  eq('the address is left clean for the default view', p.urls[p.urls.length - 1], 'https://stock.li-family.us/companies');
}

console.log('sorting');
{
  const p = await load();
  await p.ready();
  p.sort('pe');
  eq('lowest P/E first; a loss (INTC) and no P/E (QQQQ) go last, not first',
    p.order().slice(0, 5), ['AAPL', 'MSFT', 'NVDA', 'INTC', 'QQQQ']);
  eq('SEC\'s rows keep their order behind the quoted ones', p.order().slice(5), ['ZETA', 'ACME', 'BETA']);
  eq('still exactly one divider', p.dividers(), 1);
  eq('the sort rides in the address', p.urls[p.urls.length - 1], 'https://stock.li-family.us/companies?sort=pe');
  p.sort('chg');
  eq('best today first; no quote last', p.order().slice(0, 5), ['INTC', 'NVDA', 'AAPL', 'MSFT', 'QQQQ']);
  p.sort('y52');
  eq('best 52 weeks first', p.order().slice(0, 5), ['INTC', 'NVDA', 'MSFT', 'AAPL', 'QQQQ']);
  p.sort('az');
  eq('A-Z sorts each part on its own', p.order(), ['AAPL', 'INTC', 'MSFT', 'NVDA', 'QQQQ', 'ACME', 'BETA', 'ZETA']);
  eq('so the divider is still drawn once', p.dividers(), 1);
  p.sort('');
  eq('back to largest first', p.order().slice(0, 5), ['NVDA', 'MSFT', 'AAPL', 'INTC', 'QQQQ']);
}

console.log('gainers and losers are rankings of their own');
{
  const p = await load();
  await p.ready();
  p.sort('az');
  p.chip('gainers');
  eq('gainers by today\'s change, whatever the sort says', p.order(), ['INTC', 'NVDA']);
  t('and the sort is hidden there', p.els.coSort.hidden === true);
  p.chip('losers');
  eq('losers, worst first', p.order(), ['MSFT']);
  p.chip('followed');
  t('the sort comes back on a view it applies to', p.els.coSort.hidden === false);
  eq('with its choice intact', p.order(), ['AAPL', 'INTC', 'MSFT', 'NVDA', 'QQQQ']);
  eq('view and sort both in the address', p.urls[p.urls.length - 1], 'https://stock.li-family.us/companies?view=followed&sort=az');
}

console.log('the star and the Watchlist view');
{
  const p = await load();
  await p.ready();
  p.chip('watch');
  eq('an empty watchlist says how to fill it', [p.els.coEmpty.hidden, p.els.coEmpty.textContent], [false, 'WATCH-EMPTY']);
  t('and says where it is kept', p.els.coMeta.textContent.includes('kept in this browser'));
  p.chip('all');
  const b = p.star('MSFT');
  t('a pressed star repaints in place', b.classList.contains('is-on') && b.textContent === '★'
    && b.getAttribute('aria-pressed') === 'true' && b.title === 'Remove from watchlist');
  eq('the chip counts it', p.els.coWatchN.textContent, '1');
  eq('it is stored in /lookup\'s shape, with the card\'s name',
    JSON.parse(p.storage.data.ystocker_watchlist), [{ ticker: 'MSFT', name: 'Microsoft' }]);
  p.star('ZETA');
  p.chip('watch');
  eq('the Watchlist holds both, the quoted one first', p.order(), ['MSFT', 'ZETA']);
  eq('with the watch view\'s own divider wording',
    (p.els.coGrid.innerHTML.match(/class="co-divider">([^<]*)</) || [])[1], 'DIVIDER-WATCH');
  p.star('MSFT');
  eq('unstarring inside the Watchlist removes the card', p.order(), ['ZETA']);
  eq('and the count follows', p.els.coWatchN.textContent, '1');
  p.star('ZETA');
  eq('the last one gone, the count disappears', p.els.coWatchN.textContent, '');
}

console.log('a starred ticker in neither list');
{
  // Starred on /lookup or /history: a foreign listing SEC does not carry.
  const stored = [{ ticker: '7203.T', name: 'Toyota Motor' }, { ticker: 'AAPL', name: 'Apple' }];
  const p = await load({ search: '?view=watch', stored });
  eq('opens on the Watchlist from the address', p.chips.filter(c => c.classList.contains('is-on')).map(c => c.dataset.coView), ['watch']);
  eq('before SEC\'s list answers, no bare cards flash up', p.order(), ['AAPL']);
  await p.ready();
  eq('after it, the unknown ticker still gets a card', p.order(), ['AAPL', '7203.T']);
  t('named from the watchlist and linked to its filings',
    p.els.coGrid.innerHTML.includes('Toyota Motor') && p.els.coGrid.innerHTML.includes('/history/7203.T?tab=fundamentals'));
  p.star('7203.T');
  eq('so it can be unstarred', p.order(), ['AAPL']);
}

console.log('another tab');
{
  const p = await load();
  await p.ready();
  p.watchlist.add('NVDA', 'NVIDIA');   // what the storage event re-announces
  const b = p.stars().find(s => s.dataset.coStar === 'NVDA');
  t('a change from elsewhere repaints this page\'s star', b.classList.contains('is-on') && b.textContent === '★');
}

console.log('the address');
{
  const p = await load({ search: '?view=followed&sort=pe&lang=zh' });
  t('a sort from the address is applied', p.els.coSort.value === 'pe');
  eq('and other parameters are kept', p.urls[p.urls.length - 1], 'https://stock.li-family.us/companies?view=followed&sort=pe&lang=zh');
  const q = await load({ search: '?view=bogus&sort=constructor' });
  t('an unknown view or sort falls back to the defaults',
    q.els.coSort.value === '' && q.chips.find(c => c.dataset.coView === 'all').classList.contains('is-on'));
}

console.log('without watchlist.js');
{
  const p = await load({ search: '?view=watch', withWatchlist: false });
  await p.ready();
  eq('no stars are drawn', p.stars().length, 0);
  t('the Watchlist chip is hidden', p.chips.find(c => c.dataset.coView === 'watch').hidden === true);
  eq('and a link to it opens All instead', p.order().length, 8);
}

console.log('the strings the script composes exist in both languages');
{
  const i18n = readFileSync(path.join(root, 'ystocker/static/i18n.js'), 'utf8');
  const keys = new Set([...SCRIPT.matchAll(/tr\('([\w.]+)'/g)].map(m => m[1]));
  for (const k of ['companies.watch', 'companies.sort', 'companies.sort_mcap', 'companies.sort_chg',
                   'companies.sort_pe', 'companies.sort_y52', 'companies.sort_az']) keys.add(k);
  const missing = [...keys].filter(k => !new RegExp(`'${k.replace(/\./g, '\\.')}':\\s*\\{\\s*en:\\s*'[^']+',\\s*zh:\\s*'[^']+'`).test(i18n));
  eq('every key has an en and a zh', missing, []);
}

if (failures.length) {
  console.log(`\n${failures.length} FAILED`);
  process.exit(1);
}
console.log('\nall passed');
