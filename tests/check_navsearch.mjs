/**
 * Behaviour tests for the header search's empty box (static/navsearch.js):
 * the reader's watchlist and the stocks they recently viewed, from
 * static/watchlist.js.
 *
 * What breaks here does so quietly. A row index that restarts at the second
 * list sends Enter to the wrong stock; a Clear button whose redraw detaches it
 * reads to the document's listener as a click outside, and the panel shuts on
 * the reader; a class that is not in the compiled Tailwind bundle just renders
 * unstyled. So this loads the real i18n.js, watchlist.js and navsearch.js, in
 * base.html's order, as classic scripts in one vm context (where `const I18n`
 * is script-scoped, exactly as on a page), against a small fake DOM.
 *
 * Run: node tests/check_navsearch.mjs
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const root = path.resolve(import.meta.dirname, '..');
const read = f => readFileSync(path.join(root, f), 'utf8');
const SRC = {
  i18n: read('ystocker/static/i18n.js'),
  watchlist: read('ystocker/static/watchlist.js'),
  navsearch: read('ystocker/static/navsearch.js'),
};

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${cond || !detail ? '' : '  — ' + detail}`);
  if (!cond) failures.push(label);
};
const eq = (label, got, want) => t(label, JSON.stringify(got) === JSON.stringify(want),
  `got ${JSON.stringify(got)} want ${JSON.stringify(want)}`);

function node(extra = {}) {
  const n = {
    attrs: {}, listeners: {},
    setAttribute(k, v) { n.attrs[k] = String(v); },
    removeAttribute(k) { delete n.attrs[k]; },
    hasAttribute(k) { return k in n.attrs; },
    getAttribute(k) { return k in n.attrs ? n.attrs[k] : null; },
    addEventListener(type, fn) { (n.listeners[type] = n.listeners[type] || []).push(fn); },
    ...extra,
  };
  return n;
}

const unesc = s => s.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<')
  .replace(/&gt;/g, '>').replace(/&amp;/g, '&');

/* A results box that knows which of its elements are still in the page: each
   element parsed out of its HTML carries the version it came from, and setting
   innerHTML starts a new version, which is what detaching them means. */
function resultsBox() {
  let html = '', version = 0, cache = null;
  // Accessors defined on the node itself: spreading them into node() would
  // copy their values once and lose the setter.
  const box = node();
  Object.defineProperty(box, 'innerHTML', { get: () => html, set: v => { html = v; version++; cache = null; } });
  Object.defineProperty(box, 'version', { get: () => version });
  box.querySelectorAll = sel => {
    if (sel !== '.navsearch-row') throw new Error(`unexpected selector ${sel}`);
    return parse().rows;
  };
  function parse() {
    if (cache) return cache;
    const v = version;
    const mk = (attrs, kind) => {
      const classes = new Set();
      const el = {
        kind, v, attrs, classes, box,
        getAttribute: k => (k in attrs ? attrs[k] : null),
        classList: { toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)), contains: c => classes.has(c) },
        scrollIntoView() {},
        closest: sel => {
          if (sel === '.navsearch-row') return kind === 'row' ? el : null;
          if (sel === '[data-navsearch-clear]') return kind === 'clear' ? el : null;
          return null;
        },
      };
      return el;
    };
    const rows = [...html.matchAll(/<button type="button" data-i="(\d+)" data-ticker="([^"]*)"[^>]*>([\s\S]*?)<\/button>/g)]
      .map(m => {
        const el = mk({ 'data-i': m[1], 'data-ticker': unesc(m[2]) }, 'row');
        const spans = [...m[3].matchAll(/<span[^>]*>([^<]*)<\/span>/g)].map(s => unesc(s[1]));
        el.ticker = spans[0]; el.name = spans[1] || '';
        return el;
      });
    const clear = /data-navsearch-clear/.test(html) ? mk({}, 'clear') : null;
    cache = { rows, clear };
    return cache;
  }
  box.parse = parse;
  return box;
}

/* One page: i18n.js, watchlist.js and navsearch.js, with a desktop mount (a
   toggle and a panel) and the phone menu's always-open mount. */
function page({ saved = null, recent = null, lang = '', withLists = true } = {}) {
  const store = new Map();
  if (saved) store.set('ystocker_watchlist', JSON.stringify(saved));
  if (recent) store.set('ystocker_recent_tickers', JSON.stringify(recent));
  if (lang) store.set('ystocker_lang', lang);
  const docListeners = {};
  const classes = new Set(['dark']);
  let focused = null;
  const navigations = [];

  function mount(withToggle) {
    const input = node({ value: '', focus() { focused = input; }, select() {}, blur() { if (focused === input) focused = null; } });
    const results = resultsBox();
    const panel = withToggle ? node({ attrs: { hidden: '' } }) : null;
    const toggle = withToggle ? node() : null;
    const full = node();
    const parts = { input, results, panel, toggle, full };
    const el = {
      parts,
      querySelector: sel => ({
        '[data-navsearch-toggle]': toggle, '[data-navsearch-panel]': panel,
        '[data-navsearch-input]': input, '[data-navsearch-results]': results, '[data-navsearch-full]': full,
      }[sel] ?? null),
      // In the panel: its fixed parts, and whatever the results box holds now.
      contains: n => [toggle, panel, input, results, full].includes(n) || (n && n.box === results && n.v === results.version),
    };
    return el;
  }
  const desktop = mount(true), phone = mount(false);

  const document = {
    readyState: 'complete',
    documentElement: { lang: 'en', classList: { contains: c => classes.has(c), add: c => classes.add(c), remove: c => classes.delete(c), toggle: () => {} } },
    get activeElement() { return focused; },
    addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); },
    dispatchEvent: ev => { (docListeners[ev.type] || []).forEach(fn => fn(ev)); return true; },
    querySelectorAll: sel => (sel === '[data-navsearch]' ? [desktop, phone] : []),
    querySelector: () => null,
  };
  const ctx = {
    document, console, URL, URLSearchParams,
    location: { search: '', origin: 'https://stock.li-family.us', hostname: 'stock.li-family.us',
                href: 'https://stock.li-family.us/markets', pathname: '/markets' },
    history: { replaceState: () => {} },
    Event: class { constructor(type) { this.type = type; } },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
    localStorage: { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) },
    requestAnimationFrame: fn => fn(),
    addEventListener: () => {},          // watchlist.js's storage listener
    AbortController: class { abort() {} },
    fetch: async () => ({ ok: true, json: async () => [] }),
    setTimeout: () => 0, clearTimeout: () => {},
  };
  ctx.window = ctx;
  // A page's navigation: window.location.href = '/history/…'.
  Object.defineProperty(ctx.location, 'href', {
    get: () => 'https://stock.li-family.us/markets', set: v => navigations.push(v), configurable: true,
  });
  vm.createContext(ctx);
  vm.runInContext(SRC.i18n, ctx, { filename: 'i18n.js' });
  if (withLists) vm.runInContext(SRC.watchlist, ctx, { filename: 'watchlist.js' });
  vm.runInContext(SRC.navsearch, ctx, { filename: 'navsearch.js' });

  const fire = (target, type, ev) => (target.listeners[type] || []).forEach(fn => fn(ev));
  const p = {
    ctx, desktop, phone, navigations, store,
    get focused() { return focused; },
    unfocus() { focused = null; },       // as a click on a button in the panel would
    open() { fire(desktop.parts.toggle, 'click', { preventDefault() {}, stopPropagation() {} }); },
    isOpen: () => !desktop.parts.panel.hasAttribute('hidden'),
    rows: (m = desktop) => m.parts.results.parse().rows,
    html: (m = desktop) => m.parts.results.innerHTML,
    key(k, m = desktop) { fire(m.parts.input, 'keydown', { key: k, preventDefault() {} }); },
    type(v, m = desktop) { m.parts.input.value = v; fire(m.parts.input, 'input', {}); },
    // A click: the results box's listener first, then the document's, unless
    // propagation was stopped -- the order a real click bubbles in.
    click(target, m = desktop) {
      let stopped = false;
      const ev = { target, stopPropagation() { stopped = true; }, preventDefault() {} };
      fire(m.parts.results, 'click', ev);
      if (!stopped) (docListeners.click || []).forEach(fn => fn(ev));
    },
    clearBtn: (m = desktop) => m.parts.results.parse().clear,
    run: code => vm.runInContext(code, ctx),
  };
  return p;
}

const NV = { ticker: 'NVDA', name: 'NVIDIA Corporation' };

console.log('the page these scripts share');
{
  const p = page();
  t('I18n is a script-scoped const, as on a page', p.run('typeof I18n') === 'object' && typeof p.ctx.I18n === 'undefined');
  t('watchlist.js put both lists on window', !!p.ctx.Watchlist && !!p.ctx.RecentTickers);
}

console.log('nothing saved, nothing viewed');
{
  const p = page();
  p.open();
  t('opening the panel shows it', p.isOpen());
  t('the watchlist heading shows even while it is empty', p.html().includes('★ Watchlist'));
  t('with the way to fill it', p.html().includes('Tap ☆ Watchlist beside a stock’s name on its page to keep it here.'));
  t('no recently viewed heading for an empty history', !p.html().includes('Recently viewed'));
  eq('and nothing to pick', p.rows().length, 0);
  p.key('Enter');
  eq('Enter on the empty box goes nowhere', p.navigations, []);
}

console.log('a watchlist and a history');
{
  const p = page({
    saved: [NV, { ticker: 'TSM', name: 'TSM' }],
    recent: [{ ticker: 'AAPL', name: 'Apple Inc.' }, 'MSFT', NV],   // 'MSFT': the bare string /dca used to write
  });
  p.open();
  const rows = p.rows();
  eq('the watchlist, then recently viewed, each newest first', rows.map(r => r.ticker), ['NVDA', 'TSM', 'AAPL', 'MSFT', 'NVDA']);
  eq('row indexes run on across the two lists', rows.map(r => r.attrs['data-i']), ['0', '1', '2', '3', '4']);
  eq('names beside them, and none where only the ticker is known', rows.map(r => r.name),
    ['NVIDIA Corporation', '', 'Apple Inc.', '', 'NVIDIA Corporation']);
  const html = p.html();
  t('the watchlist comes first', html.indexOf('★ Watchlist') < html.indexOf('Recently viewed'));
  t('recently viewed carries a Clear button', !!p.clearBtn());
  t('a short list has no "All" link', !html.includes('/companies?view='));
  t('no hint once something is saved', !html.includes('Tap ☆'));

  p.key('ArrowDown'); p.key('ArrowDown'); p.key('ArrowDown');
  t('the arrows move from the watchlist into recently viewed', p.rows()[2].classes.has('bg-brand/20')
    && p.rows().filter(r => r.classes.has('bg-brand/20')).length === 1);
  p.key('Enter');
  eq('and Enter opens the highlighted stock', p.navigations, ['/history/AAPL']);

  p.click(p.rows()[3]);
  eq('a click opens a recent one', p.navigations[1], '/history/MSFT');
  eq('picking records nothing itself: the stock page does that once it loads',
    p.ctx.RecentTickers.tickers(), ['AAPL', 'MSFT', 'NVDA']);
}

console.log('long lists');
{
  const saved = Array.from({ length: 7 }, (_, i) => ({ ticker: 'W' + i, name: 'Watch ' + i }));
  const recent = Array.from({ length: 30 }, (_, i) => 'R' + i);
  const p = page({ saved, recent });
  p.open();
  eq('five of each', p.rows().map(r => r.ticker), ['W0', 'W1', 'W2', 'W3', 'W4', 'R0', 'R1', 'R2', 'R3', 'R4']);
  t('then a link to the whole watchlist', p.html().includes('href="/companies?view=watch"') && p.html().includes('All 7 →'));
  t('and to all of recently viewed, which keeps 24',
    p.html().includes('href="/companies?view=recent"') && p.html().includes('All 24 →'));
}

console.log('a Chinese page');
{
  const p = page({ saved: [NV], recent: [{ ticker: 'AAPL', name: 'Apple Inc.' }], lang: 'zh' });
  p.open();
  const html = p.html();
  t('the headings are Chinese', html.includes('★ 自选') && html.includes('最近浏览') && html.includes('清除'));
  eq('a stock with a Chinese name shows it', p.rows().map(r => r.name), ['英伟达', '苹果']);
  p.click(p.rows()[0]);
  eq('and the stock page opens in Chinese', p.navigations, ['/history/NVDA?lang=zh']);
  const q = page({ saved: Array.from({ length: 6 }, (_, i) => 'W' + i), lang: 'zh' });
  q.open();
  t('the "All" link keeps the language', q.html().includes('href="/companies?view=watch&amp;lang=zh"') && q.html().includes('全部 6 个 →'));
  const r = page({ lang: 'zh' });
  r.open();
  t('the empty watchlist explains itself in Chinese', r.html().includes('「☆ 加自选」'));
}

console.log('Clear');
{
  const p = page({ saved: [NV], recent: ['AAPL', 'MSFT'] });
  p.open();
  p.unfocus();
  p.click(p.clearBtn());
  eq('empties recently viewed', p.ctx.RecentTickers.list(), []);
  t('and the panel stays open, though the redraw took the button out of it', p.isOpen());
  t('the history is gone from the box, the watchlist is not',
    !p.html().includes('Recently viewed') && p.rows().map(r => r.ticker).join() === 'NVDA');
  t('focus goes back to the box', p.focused === p.desktop.parts.input);
  eq('the watchlist is untouched', p.ctx.Watchlist.tickers(), ['NVDA']);
}

console.log('a list that changes while the box is open');
{
  const p = page({ recent: ['AAPL'] });
  p.open();
  p.ctx.Watchlist.add('AMD', 'Advanced Micro Devices');   // a star pressed on the page
  eq('a new star shows at once', p.rows().map(r => r.ticker), ['AMD', 'AAPL']);
  p.ctx.RecentTickers.add('INTC', 'Intel');               // the stock page recorded itself
  eq('and so does a recorded view', p.rows().map(r => r.ticker), ['AMD', 'INTC', 'AAPL']);
  p.type('NV');
  const typed = p.html();
  p.ctx.Watchlist.add('KO');
  t('a box with something typed in it is left alone', p.html() === typed && p.rows()[0].ticker === 'NV');
  p.key('Enter');
  eq('Enter on a typed symbol opens it', p.navigations, ['/history/NV']);
  eq('without writing it into recently viewed', p.ctx.RecentTickers.tickers(), ['INTC', 'AAPL']);
}

console.log('the phone menu');
{
  const p = page({ saved: [NV], recent: ['AAPL'] });
  eq('its always-open box shows both lists from the start', p.rows(p.phone).map(r => r.ticker), ['NVDA', 'AAPL']);
  p.click(p.clearBtn(p.phone), p.phone);
  t('Clear works there too', p.ctx.RecentTickers.list().length === 0 && p.rows(p.phone).length === 1);
  t('without focusing the box, which would raise the keyboard', p.focused !== p.phone.parts.input);
}

console.log('without watchlist.js');
{
  const p = page({ withLists: false });
  p.open();
  t('the box just asks for a symbol', p.html().includes('Type a symbol, then press Enter.') && p.rows().length === 0);
}

console.log('every class the header search writes is in the compiled bundle');
{
  // Tailwind here is compiled: a class only this script uses is missing from
  // css/tailwind.css until build_css.sh runs, and the markup renders unstyled.
  const css = read('ystocker/static/css/tailwind.css');
  // One class attribute is spread over several string literals, with a
  // conditional in the middle: ' class="a' + (hint ? ' b' : '') + ' c"'. The
  // conditional's tokens are kept and the pieces joined into one attribute.
  const tokens = new Set();
  const joined = SRC.navsearch
    .replace(/'\s*\+\s*\(([^()]*)\)\s*\+\s*'/g, (_, expr) => {
      for (const q of expr.matchAll(/'([^']*)'/g)) q[1].split(/\s+/).forEach(c => c && tokens.add(c));
      return '';
    })
    .replace(/'\s*\+\s*'/g, '');
  for (const m of joined.matchAll(/class="([^"]+)"/g)) m[1].split(/\s+/).forEach(c => c && tokens.add(c));
  for (const m of SRC.navsearch.matchAll(/classList\.toggle\('([^']+)'/g)) tokens.add(m[1]);
  tokens.delete('navsearch-row');      // a hook for the script, styled by nothing
  // As the compiled bundle spells a selector: hover:bg-brand/20 is .hover\:bg-brand\/20.
  const inBundle = c => {
    const needle = '.' + c.replace(/([:/\[\].])/g, '\\$1');
    for (let i = css.indexOf(needle); i >= 0; i = css.indexOf(needle, i + 1)) {
      if (!/[\w\\-]/.test(css[i + needle.length] || '')) return true;
    }
    return false;
  };
  const missing = [...tokens].filter(c => !inBundle(c));
  eq('none missing', missing, []);
  t('the check saw the new markup and the conditional class',
    tokens.has('justify-between') && tokens.has('hover:text-brand') && tokens.has('italic'));
}

console.log('the strings it composes exist in both languages');
{
  const keys = new Set([...SRC.navsearch.matchAll(/t\('(nav\.[\w.]+)'/g)].map(m => m[1]));
  const missing = [...keys].filter(k => !new RegExp(`'${k.replace(/\./g, '\\.')}':\\s*\\{\\s*en:\\s*'[^']+',\\s*zh:\\s*'[^']+'`).test(SRC.i18n));
  eq('every key has an en and a zh', missing, []);
  t('including the new ones', ['nav.search_saved', 'nav.search_saved_empty', 'nav.search_clear', 'nav.search_all']
    .every(k => keys.has(k)));
}

if (failures.length) {
  console.log(`\n${failures.length} FAILED`);
  process.exit(1);
}
console.log('\nall passed');
