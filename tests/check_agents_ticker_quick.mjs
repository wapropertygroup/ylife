/**
 * Behaviour tests for the /agents ticker box's quick list (templates/agents.html,
 * the "Ticker suggestions" block): before anything is typed, 股票代码 offers the
 * reader's watchlist and recently viewed stocks from static/watchlist.js.
 *
 * The list now mixes headings with options, which is where the quiet failures
 * live: an option index counted over the headings highlights one row while
 * Enter picks another, and aria-activedescendant names an id nobody has. And a
 * symbol a run refuses (an index, 7203.T) offered here would cost the reader a
 * refusal for following the site's own suggestion. So this runs the block
 * itself, cut from the template rather than copied, with the real i18n.js and
 * watchlist.js in one vm context, against a small fake DOM.
 *
 * Run: node tests/check_agents_ticker_quick.mjs
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const root = path.resolve(import.meta.dirname, '..');
const read = f => readFileSync(path.join(root, f), 'utf8');
const tpl = read('ystocker/templates/agents.html');
const BLOCK = (() => {
  const from = tpl.indexOf('  // ── Ticker suggestions');
  const to = tpl.indexOf('  // A ticker the server refused for want of prices', from);
  if (from < 0 || to < 0) throw new Error('ticker suggestions block not found in agents.html');
  return tpl.slice(from, to);
})();

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${cond || !detail ? '' : '  — ' + detail}`);
  if (!cond) failures.push(label);
};
const eq = (label, got, want) => t(label, JSON.stringify(got) === JSON.stringify(want),
  `got ${JSON.stringify(got)} want ${JSON.stringify(want)}`);

function element(tag) {
  const listeners = {};
  const classes = new Set();
  const el = {
    tag, id: '', attrs: {}, children: [], hidden: false, _text: '',
    get className() { return [...classes].join(' '); },
    set className(v) { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach(c => classes.add(c)); },
    classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
                 toggle: (c, on) => ((on ?? !classes.has(c)) ? classes.add(c) : classes.delete(c)) },
    setAttribute(k, v) { el.attrs[k] = String(v); },
    getAttribute(k) { return k in el.attrs ? el.attrs[k] : null; },
    removeAttribute(k) { delete el.attrs[k]; },
    appendChild(c) { el.children.push(c); return c; },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    fire(type, ev = {}) { (listeners[type] || []).forEach(fn => fn({ preventDefault() {}, ...ev })); },
    querySelectorAll(sel) {
      if (sel !== '[role="option"]') throw new Error(`unexpected selector ${sel}`);
      return el.children.filter(c => c.attrs.role === 'option');
    },
  };
  // textContent: reading joins the children's text; writing replaces them.
  Object.defineProperty(el, 'textContent', {
    get: () => el._text + el.children.map(c => c.textContent).join(''),
    set: v => { el._text = String(v); el.children = []; },
  });
  return el;
}

function page({ saved = null, recent = null, lang = '' } = {}) {
  const store = new Map();
  if (saved) store.set('ystocker_watchlist', JSON.stringify(saved));
  if (recent) store.set('ystocker_recent_tickers', JSON.stringify(recent));
  if (lang) store.set('ystocker_lang', lang);
  const tickerEl = element('input');
  tickerEl.value = 'NVDA';                       // as the template pre-fills it
  const raise = element('div');
  tickerEl.closest = sel => (sel === '[data-sel-raise]' ? raise : null);
  const symList = element('ul');
  symList.hidden = true;
  let focused = null;
  const timers = [];
  const fetched = [];
  const document = {
    documentElement: { lang: 'en', classList: { contains: () => false, add() {}, remove() {}, toggle() {} } },
    get activeElement() { return focused; },
    getElementById: id => (id === 'agTickerList' ? symList : null),
    createElement: tag => element(tag),
    addEventListener() {}, dispatchEvent: () => true,
    querySelectorAll: () => [], querySelector: () => null,
  };
  const ctx = {
    document, console, URL, URLSearchParams,
    location: { search: '', origin: 'https://trade-agents.com', hostname: 'trade-agents.com',
                href: 'https://trade-agents.com/agents' },
    history: { replaceState() {} },
    Event: class { constructor(type) { this.type = type; } },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
    localStorage: { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) },
    addEventListener() {},
    setTimeout: fn => { timers.push(fn); return timers.length; },
    clearTimeout() {},
    fetch: async url => {
      fetched.push(url);
      return { ok: true, json: async () => ({ results: [{ ticker: 'TSM', name: 'Taiwan Semiconductor', exchange: 'NYSE' }] }) };
    },
    tickerEl, clearErr() {},
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(read('ystocker/static/i18n.js'), ctx, { filename: 'i18n.js' });
  vm.runInContext(read('ystocker/static/watchlist.js'), ctx, { filename: 'watchlist.js' });
  vm.runInContext(`(function () {\n${BLOCK}\n})();`, ctx, { filename: 'agents.html' });

  const p = {
    ctx, tickerEl, symList, raise, fetched,
    focus() { focused = tickerEl; tickerEl.fire('focus'); },
    blur() { focused = null; tickerEl.fire('blur'); },
    key(k) { tickerEl.fire('keydown', { key: k }); },
    type(v) { tickerEl.value = v; tickerEl.fire('input'); },
    async flush() { while (timers.length) timers.shift()(); for (let i = 0; i < 20; i++) await null; },
    // What the list shows: headings as "# label", options as their ticker.
    shown: () => symList.children.map(li => (li.attrs.role === 'option'
      ? li.children[0].textContent : '# ' + li.textContent)),
    options: () => symList.children.filter(li => li.attrs.role === 'option'),
  };
  return p;
}

console.log('the quick list');
{
  const p = page({
    saved: [{ ticker: 'NVDA', name: 'NVIDIA' }, '^GSPC', { ticker: 'TSM', name: 'TSM' }],
    recent: [{ ticker: 'AAPL', name: 'Apple Inc.' }, 'NVDA', '7203.T', 'MSFT', 'GC=F', '600519'],
  });
  t('closed until the box is focused', p.symList.hidden);
  p.focus();
  t('focusing the box opens it, though it holds NVDA already', !p.symList.hidden
    && p.tickerEl.attrs['aria-expanded'] === 'true' && p.raise.classList.contains('ag-raised'));
  eq('the watchlist, then recently viewed, under their headings', p.shown(),
    ['# ★ Watchlist', 'NVDA', 'TSM', '# Recently viewed', 'AAPL', 'MSFT', '600519']);
  t('an index, a Tokyo listing and a future are left out: a run would refuse them',
    !p.shown().some(x => ['^GSPC', '7203.T', 'GC=F'].includes(x)));
  t('an A-share code is a symbol a run takes', p.shown().includes('600519'));
  eq('a watchlisted stock is not offered twice', p.shown().filter(x => x === 'NVDA').length, 1);
  eq('only options carry ids, numbered over the options alone',
    p.symList.children.map(li => li.id || '-'), ['-', 'agTickerOpt0', 'agTickerOpt1', '-', 'agTickerOpt2', 'agTickerOpt3', 'agTickerOpt4']);
  t('headings are not options', p.symList.children.filter(li => li.attrs.role === 'presentation').length === 2);
  eq('a name beside each, and none where only the ticker is known',
    p.options().map(li => li.children[1].textContent), ['NVIDIA', '', 'Apple Inc.', '', '']);

  p.key('ArrowDown'); p.key('ArrowDown'); p.key('ArrowDown');
  const on = p.options().filter(li => li.classList.contains('is-active')).map(li => li.children[0].textContent);
  eq('the arrows step over the headings', on, ['AAPL']);
  eq('and the box names the highlighted option', p.tickerEl.attrs['aria-activedescendant'], 'agTickerOpt2');
  p.key('Enter');
  eq('Enter picks it', p.tickerEl.value, 'AAPL');
  t('and closes the list', p.symList.hidden && !p.raise.classList.contains('ag-raised'));

  p.tickerEl.fire('click');
  t('a click on the still-focused box opens it again', !p.symList.hidden);
  p.options().find(li => li.children[0].textContent === 'MSFT').fire('mousedown');
  eq('a pointer pick fills the box', p.tickerEl.value, 'MSFT');
  eq('picking writes nothing to recently viewed', p.ctx.RecentTickers.tickers(),
    ['AAPL', 'NVDA', '7203.T', 'MSFT', 'GC=F', '600519']);
}

console.log('typing takes over, and an emptied box brings it back');
{
  const p = page({ saved: ['NVDA'], recent: ['AAPL'] });
  p.focus();
  p.type('TS');
  await p.flush();
  eq('a typed query asks Yahoo', p.fetched, ['/api/agents/symbols?q=TS']);
  eq('and its answer replaces the quick list, without headings', p.shown(), ['TSM']);
  p.blur();
  p.focus();
  t('refocusing a box the reader typed in does not offer the quick list', p.symList.hidden);
  p.type('');
  eq('emptying it does', p.shown(), ['# ★ Watchlist', 'NVDA', '# Recently viewed', 'AAPL']);
  p.blur();
  t('leaving the box closes it', p.symList.hidden);
}

console.log('nothing saved or viewed');
{
  const p = page();
  p.focus();
  t('the list stays closed rather than showing two empty headings', p.symList.hidden && p.symList.children.length === 0);
}

console.log('a Chinese page');
{
  const p = page({ saved: [{ ticker: 'NVDA', name: 'NVIDIA' }], recent: ['AAPL'], lang: 'zh' });
  p.focus();
  eq('Chinese headings', p.shown().filter(x => x.startsWith('#')), ['# ★ 自选', '# 最近浏览']);
  eq('and Chinese names where known', p.options().map(li => li.children[1].textContent), ['英伟达', '苹果']);
}

if (failures.length) {
  console.log(`\n${failures.length} FAILED`);
  process.exit(1);
}
console.log('\nall passed');
