// Checks the Fundamentals tab's loading panel in templates/history.html: what
// #fundStatus shows while /api/fundamentals answers 202, and how each wait ends.
//
// Every failure here is a plausible page rather than an error: a clock that
// restarts on each poll, a panel that flashes up for a company already read, a
// step ticked before the server said so, a spinner restarted every few seconds,
// a Chinese page left in English. All of it is composed in JavaScript, where
// neither I18n.apply() nor any Python test reaches.
//
// The block is extracted from the template rather than copied (a copy agrees on
// the day it is written and drifts after), together with the tab's own $, tr
// and esc, and runs against the real i18n.js -- loaded as a classic script, so
// `const I18n` is script-scoped as on the page -- a strict mini DOM that throws
// on unbalanced markup, and fake timers.
//
// Run: node tests/check_fundamentals_loading.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', 'ystocker');
const tpl = readFileSync(join(root, 'templates', 'history.html'), 'utf8');
const i18nSrc = readFileSync(join(root, 'static', 'i18n.js'), 'utf8');

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${cond || !detail ? '' : '  — ' + detail}`);
  if (!cond) failures.push(label);
};
const eq = (label, got, want) => t(label, JSON.stringify(got) === JSON.stringify(want),
  `got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);

// ── The code under test ─────────────────────────────────────────────────────
const FUND = tpl.indexOf('// ── Fundamentals tab ──');
if (FUND < 0) throw new Error('the Fundamentals tab script was not found in history.html');

function grab(re, what) {
  const m = re.exec(tpl.slice(FUND));
  if (!m) throw new Error(`${what} not found in the Fundamentals tab script`);
  return m[0];
}
const HELPERS = [
  grab(/const \$ = id => document\.getElementById\(id\);/, '$'),
  grab(/const tr = \(key, fallback\) => I18n\.t\(key\) \|\| fallback;/, 'tr'),
  grab(/const esc = s => [\s\S]*?\}\[c\]\)\);/, 'esc'),
].join('\n');

const BLOCK = (() => {
  const start = tpl.indexOf('// ── While the figures load', FUND);
  const tail = tpl.indexOf('// The panel and the endings are composed here', start);
  const call = tpl.indexOf("document.addEventListener('i18n:langchange'", tail);
  if (start < 0 || tail < 0 || call < 0) throw new Error('the loading block was not found in history.html');
  let depth = 0;
  for (let j = tpl.indexOf('{', call); j < tpl.length; j++) {
    if (tpl[j] === '{') depth++;
    else if (tpl[j] === '}' && --depth === 0) return tpl.slice(start, tpl.indexOf(';', j) + 1);
  }
  throw new Error('unbalanced braces in the loading block');
})();

// ── A strict mini DOM ───────────────────────────────────────────────────────
// Only what the block touches: innerHTML both ways, textContent, hidden,
// dataset, classList and single simple selectors. Unbalanced markup throws, so
// a missing </div> fails here instead of quietly nesting the skeleton inside
// the panel on the page.
const VOID = new Set(['br', 'hr', 'img', 'input', 'meta', 'link']);
const decode = s => s.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
  .replace(/&#39;/g, "'").replace(/&amp;/g, '&');
const encode = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

class El {
  constructor(tag, attrs = new Map()) { this.tag = tag; this.attrs = attrs; this.children = []; this.parent = null; }
  get hidden() { return this.attrs.has('hidden'); }
  set hidden(v) { if (v) this.attrs.set('hidden', ''); else this.attrs.delete('hidden'); }
  get dataset() {
    const name = k => 'data-' + String(k).replace(/[A-Z]/g, c => '-' + c.toLowerCase());
    return new Proxy({}, {
      get: (_, k) => (this.attrs.has(name(k)) ? this.attrs.get(name(k)) : undefined),
      set: (_, k, v) => { this.attrs.set(name(k), String(v)); return true; },
    });
  }
  get classList() {
    const list = () => (this.attrs.get('class') || '').split(/\s+/).filter(Boolean);
    const write = l => this.attrs.set('class', l.join(' '));
    const token = c => { if (/\s/.test(c)) throw new Error(`InvalidCharacterError: "${c}"`); return c; };
    return {
      contains: c => list().includes(token(c)),
      add: (...cs) => write([...new Set([...list(), ...cs.map(token)])]),
      remove: (...cs) => write(list().filter(x => !cs.map(token).includes(x))),
      toggle: (c, force) => {
        const on = force === undefined ? !list().includes(token(c)) : !!force;
        write(on ? [...new Set([...list(), token(c)])] : list().filter(x => x !== c));
        return on;
      },
    };
  }
  get innerHTML() { return this.children.map(serialize).join(''); }
  set innerHTML(html) { this.children = parse(String(html)); this.children.forEach(c => { c.parent = this; }); }
  get textContent() { return this.children.map(c => (c instanceof El ? c.textContent : c.text)).join(''); }
  set textContent(s) { this.children = [{ text: String(s) }]; }
  querySelectorAll(sel) {
    const m = /^(?:\[([\w-]+)\]|\.([\w-]+)|([a-z]+))$/.exec(sel);
    if (!m) throw new Error(`the mini DOM does not do the selector "${sel}"`);
    const hit = el => (m[1] ? el.attrs.has(m[1]) : m[2] ? el.classList.contains(m[2]) : el.tag === m[3]);
    const out = [];
    const walk = el => el.children.forEach(c => { if (c instanceof El) { if (hit(c)) out.push(c); walk(c); } });
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

function parse(html) {
  const top = new El('#root');
  let cur = top;
  const re = /<(\/?)([a-zA-Z][\w-]*)((?:\s+[^\s=>/]+(?:\s*=\s*"[^"]*")?)*)\s*(\/?)>|([^<]+)/g;
  let m;
  while ((m = re.exec(html))) {
    if (m[5] !== undefined) { cur.children.push({ text: decode(m[5]) }); continue; }
    const tag = m[2].toLowerCase();
    if (m[1]) {
      if (cur.tag !== tag) throw new Error(`</${tag}> closes <${cur.tag}>`);
      cur = cur.parent;
      continue;
    }
    const attrs = new Map();
    for (const a of m[3].matchAll(/([^\s=>/]+)(?:\s*=\s*"([^"]*)")?/g)) attrs.set(a[1], a[2] === undefined ? '' : decode(a[2]));
    const el = new El(tag, attrs);
    el.parent = cur;
    cur.children.push(el);
    if (!m[4] && !VOID.has(tag)) cur = el;
  }
  if (cur !== top) throw new Error(`<${cur.tag}> is never closed`);
  return top.children;
}

function serialize(n) {
  if (!(n instanceof El)) return encode(n.text);
  const attrs = [...n.attrs].map(([k, v]) => (v === '' ? ` ${k}` : ` ${k}="${v.replace(/"/g, '&quot;')}"`)).join('');
  return `<${n.tag}${attrs}>${n.children.map(serialize).join('')}</${n.tag}>`;
}

// ── One page: a fresh context, clock and #fundStatus per scenario ──────────
function page(ticker = 'NBIS', { random = 0 } = {}) {
  let now = Date.UTC(2026, 9, 6, 18, 0, 0);
  let seq = 0;
  const timers = new Map();
  const schedule = (fn, ms, every) => { const id = ++seq; timers.set(id, { fn, at: now + (ms || 0), every }); return id; };
  const advance = (ms) => {
    const end = now + ms;
    for (;;) {
      let next = null;
      for (const [id, tm] of timers) if (tm.at <= end && (!next || tm.at < next[1].at)) next = [id, tm];
      if (!next) break;
      const [id, tm] = next;
      now = tm.at;
      if (tm.every) tm.at += tm.every; else timers.delete(id);
      tm.fn();
    }
    now = end;
  };
  class FakeDate extends Date {
    constructor(...a) { super(...(a.length ? a : [now])); }
    static now() { return now; }
  }
  const status = new El('div', new Map([['id', 'fundStatus'], ['class', 'fd-status']]));
  const listeners = {};
  const store = new Map();
  const html = { lang: 'en', classList: { contains: () => true, add() {}, remove() {}, toggle() {} } };
  const document = {
    documentElement: html,
    getElementById: id => (id === 'fundStatus' ? status : null),
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    dispatchEvent: (ev) => { (listeners[ev.type] || []).forEach(fn => fn(ev)); return true; },
    querySelectorAll: () => [], querySelector: () => null,
  };
  const ctx = {
    document, console, JSON, URL, URLSearchParams, isNaN, encodeURIComponent,
    Date: FakeDate,
    Math: Object.assign(Object.create(Math), { random: () => random }),
    location: { search: '', pathname: `/history/${ticker}`, hostname: 'trade-agents.com',
                origin: 'https://trade-agents.com',
                href: `https://trade-agents.com/history/${ticker}?tab=fundamentals` },
    history: { replaceState() {} },
    Event: class { constructor(type) { this.type = type; } },
    localStorage: { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) },
    setTimeout: (fn, ms) => schedule(fn, ms, 0),
    setInterval: (fn, ms) => schedule(fn, ms, ms),
    clearTimeout: id => timers.delete(id),
    clearInterval: id => timers.delete(id),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(i18nSrc, ctx, { filename: 'i18n.js' });
  const make = vm.runInContext(`(function (TICKER) {
    let payload = null;
    ${HELPERS}
    ${BLOCK}
    return { setStatus, setPayload(p) { payload = p; } };
  })`, ctx, { filename: 'history.html (loading block)' });
  return { ...make(ticker), el: status, I18n: vm.runInContext('I18n', ctx), advance, timers };
}

const box = p => p.el.querySelector('[data-fd-load]');
const title = p => box(p).querySelector('[data-fd-title]').textContent;
const msg = p => box(p).querySelector('[data-fd-msg]').textContent;
const clock = p => p.el.querySelector('[data-fd-time]').textContent;
const tipText = p => p.el.querySelector('[data-fd-tip]').textContent;
const steps = p => box(p).querySelector('[data-fd-steps]').querySelectorAll('li')
  .map(li => [li.attrs.get('class').replace('fd-step ', ''), li.textContent]);
const skeletons = p => p.el.querySelectorAll('.fd-skel');

// ── A company already read: the skeleton alone, gone in one request ────────
console.log('a quick answer');
{
  const p = page();
  p.setStatus('loading');
  t('#fundStatus is shown while loading', !p.el.hidden);
  t('and marked waiting, which drops its padding so the skeleton sits where the grid draws',
    p.el.classList.contains('is-waiting'));
  eq('eight skeleton cards where the charts will go', skeletons(p).length, 8);
  t('each with twelve bars, every height a percentage up to 100',
    skeletons(p).every(c => {
      const bars = c.querySelectorAll('i');
      return bars.length === 12 && bars.every(b => {
        const h = Number(/height:(\d+)%/.exec(b.attrs.get('style'))[1]);
        return h > 0 && h <= 100;
      });
    }));
  t('the skeleton is hidden from screen readers',
    p.el.children.find(c => c instanceof El && c.classList.contains('fd-grid')).attrs.get('aria-hidden') === 'true');
  t('the panel stays hidden at first, so a quick answer never flashes it', box(p).hidden);
  t('and it is the compact one', box(p).classList.contains('is-quick'));
  p.advance(100);
  p.setStatus(null);
  t('the answer hides #fundStatus', p.el.hidden && p.el.innerHTML === '');
  t('and clears the waiting class', !p.el.classList.contains('is-waiting'));
  eq('no clock or reveal timer is left running', p.timers.size, 0);
}

console.log('a slow first request');
{
  const p = page();
  p.setStatus('loading');
  p.advance(599);
  t('still hidden at 599 ms', box(p).hidden);
  p.advance(1);
  t('the compact panel appears at 600 ms', !box(p).hidden && box(p).classList.contains('is-quick'));
  eq('titled with the ticker', title(p), 'Loading NBIS’s fundamentals');
  t('with the animated bars for an icon', box(p).querySelector('[data-fd-icon]').querySelectorAll('.fd-bars').length === 1);
}

// ── A company being read for the first time ─────────────────────────────────
console.log('building');
{
  const p = page();
  p.setStatus('loading');
  p.advance(120);
  p.setStatus('building');
  t('the panel shows at once when the server says it is building', !box(p).hidden);
  t('the full panel, not the compact one', !box(p).classList.contains('is-quick'));
  eq('title', title(p), 'Reading NBIS’s filings');
  t('a message that says the first view takes a few seconds', /first view/.test(msg(p)) && /few seconds/.test(msg(p)));
  eq('steps: looked up, reading now, charts to come', steps(p),
     [['is-done', 'Look up NBIS'], ['is-now', 'Read its filings'], ['is-todo', 'Draw the charts']]);
  t('the step under way carries the spinner', box(p).querySelector('[data-fd-steps]').querySelectorAll('.fd-spin').length === 1);
  eq('the clock counts from when the tab asked, not from the 202', clock(p), '0:00');
  p.advance(880);
  eq('one second after asking', clock(p), '0:01');
  t('a tip under the label', tipText(p).startsWith('Tip') && tipText(p).length > 20, tipText(p));

  // The poll says "building" again every few seconds; a repaint would restart
  // the spinner and announce the status again.
  const before = box(p).querySelector('[data-fd-steps]').children;
  const titleNode = box(p).querySelector('[data-fd-title]').children;
  p.advance(1500);
  p.setStatus('building');
  t('a repeated state repaints nothing: the steps are the same nodes',
    box(p).querySelector('[data-fd-steps]').children === before);
  t('nor is the status text rewritten', box(p).querySelector('[data-fd-title]').children === titleNode);
  p.advance(65 * 1000 - 2500);
  eq('the clock reads minutes past the first one', clock(p), '1:05');
}

console.log('tips');
{
  const p = page('NBIS', { random: 0.99 });
  p.setStatus('loading');
  p.setStatus('building');
  const seen = [tipText(p)];
  for (let i = 0; i < 6; i++) { p.advance(7000); seen.push(tipText(p)); }
  eq('a new tip every seven seconds, through all six and round again',
     [new Set(seen.slice(0, 6)).size, seen[6] === seen[0]], [6, true]);
  t('the first tip comes from a random start: the last of six here', /median/.test(seen[0]), seen[0]);
  const q = page('NBIS', { random: 0 });
  q.setStatus('loading');
  q.advance(5000);
  q.setStatus('building');
  const first = tipText(q);
  t('and the first of six at the other end', /SEC filings/.test(first), first);
  q.advance(6999);
  eq('a tip gets its full seven seconds from when the panel opened', tipText(q), first);
}

// ── Queued for a slot, then built ───────────────────────────────────────────
console.log('queued, then building');
{
  const p = page();
  p.setStatus('loading');
  p.setStatus('queued');
  eq('title while queued', title(p), 'Waiting to read NBIS’s filings');
  t('the message says another company is being read first', /Another company/.test(msg(p)));
  t('the icon becomes a clock', box(p).querySelector('[data-fd-icon]').dataset.pic === 'clock'
    && box(p).querySelector('[data-fd-icon]').querySelectorAll('.fd-bars').length === 0);
  eq('steps: a waiting step appears, nothing under way yet', steps(p),
     [['is-done', 'Look up NBIS'], ['is-idle', 'Wait for a free slot'],
      ['is-todo', 'Read its filings'], ['is-todo', 'Draw the charts']]);
  t('and no spinner, since nothing has started', box(p).querySelectorAll('.fd-spin').length === 0);
  p.advance(4000);
  p.setStatus('building');
  eq('once the build starts, the wait is ticked and stays listed', steps(p),
     [['is-done', 'Look up NBIS'], ['is-done', 'Wait for a free slot'],
      ['is-now', 'Read its filings'], ['is-todo', 'Draw the charts']]);
  t('and the bars come back', box(p).querySelector('[data-fd-icon]').dataset.pic === 'bars');
  eq('the clock ran on through the change of state', clock(p), '0:04');
}

// ── How a wait ends ─────────────────────────────────────────────────────────
console.log('endings');
const ENDINGS = [
  // state, extra, tone, a word from the message, retry offered
  ['failed', undefined, 'is-warn', 'Couldn’t load', true],
  ['timeout', undefined, 'is-warn', 'longer than usual', true],
  ['capped', undefined, 'is-accent', 'ready tomorrow', false],
  ['invalid', undefined, 'is-dim', 'not a ticker', false],
  ['unavailable', 'not_a_company', 'is-dim', 'not a company', false],
  ['unavailable', 'no_such_reason', 'is-dim', 'No financial statements were found', false],
];
for (const [state, extra, tone, words, retry] of ENDINGS) {
  const p = page();
  p.setStatus('loading');
  p.setStatus('building');
  p.advance(3000);
  p.setStatus(state, extra);
  const note = p.el.querySelector('.fd-notice');
  t(`${state}${extra ? ' (' + extra + ')' : ''}: an icon, the message${retry ? ' and Retry' : ''}`,
    note && note.classList.contains(tone) && note.querySelectorAll('svg').length === 1
      && note.textContent.includes(words)
      && (note.querySelectorAll('[data-fund-retry]').length === 1) === retry,
    p.el.innerHTML.slice(0, 200));
  t(`${state}: the clock and every timer stop`, p.timers.size === 0 && !p.el.classList.contains('is-waiting'));
}
{
  const p = page();
  p.setStatus('loading');
  p.setStatus('building');
  p.advance(20 * 1000);
  p.setStatus('timeout');
  p.advance(5000);
  p.setStatus('loading');                   // Retry
  p.setStatus('building');
  eq('Retry starts a fresh wait, its clock from zero', clock(p), '0:00');
  t('and its steps from the beginning', steps(p)[0][0] === 'is-done' && steps(p).length === 3);
}

// ── Switching language mid-wait ─────────────────────────────────────────────
console.log('language');
{
  const p = page();
  p.setStatus('loading');
  p.setStatus('queued');
  p.advance(2000);
  p.setStatus('building');
  p.advance(3000);
  p.I18n.setLang('zh');
  eq('the title is redrawn in Chinese', title(p), '正在读取 NBIS 的财报');
  eq('so are the steps, the wait for a slot still listed', steps(p).map(s => s[1]),
     ['查询 NBIS', '等待空闲名额', '读取财报', '绘制图表']);
  t('and the tip', /[一-鿿]/.test(tipText(p)) && tipText(p).startsWith('小提示'), tipText(p));
  eq('the clock keeps counting from the first request', clock(p), '0:05');
  t('the panel stays open', !box(p).hidden);
  eq('one clock, not two', [...p.timers.values()].filter(x => x.every).length, 1);
  p.I18n.setLang('en');
  eq('and back', title(p), 'Reading NBIS’s filings');
}
{
  const p = page();
  p.setStatus('loading');
  p.I18n.setLang('zh');
  t('a switch in the first 600 ms keeps the compact panel hidden', box(p).hidden);
  p.advance(600);
  t('and it still appears on time', !box(p).hidden);
  eq('in Chinese', title(p), '正在加载 NBIS 的基本面数据');
}
{
  const p = page();
  p.setStatus('failed');
  p.I18n.setLang('zh');
  t('an ending is redrawn too: message and Retry in Chinese',
    p.el.textContent.includes('暂时无法加载财报数据') && p.el.textContent.includes('重试'), p.el.textContent);
}
{
  const p = page();
  p.setStatus('loading');
  p.setPayload({ ticker: 'NBIS', quarterly: { end: [] } });
  p.setStatus(null);
  p.I18n.setLang('zh');
  t('with a payload drawn, the switch is left to render()', p.el.hidden && p.el.innerHTML === '');
}

// ── Escaping and translations ───────────────────────────────────────────────
console.log('escaping and translations');
{
  const p = page('A<b>&');
  p.setStatus('loading');
  p.setStatus('building');
  const html = box(p).querySelector('[data-fd-steps]').innerHTML;
  t('a ticker in a step is escaped', html.includes('A&lt;b&gt;&amp;') && !html.includes('<b>'), html);
  eq('the title is text, set as text', title(p), 'Reading A<b>&’s filings');
  t('a ticker with $& in it is not read as a replacement pattern',
    (() => { const q = page('X$&Y'); q.setStatus('loading'); return title(q) === 'Loading X$&Y’s fundamentals'; })());
}
{
  const p = page();
  const used = [...new Set([...BLOCK.matchAll(/'(fund\.[a-z_]+)'/g)].map(m => m[1]))];
  const composed = ['loading', 'building', 'queued'].map(s => 'fund.load_title_' + s)
    .concat(['lookup', 'queue', 'read', 'draw'].map(s => 'fund.load_step_' + s))
    .concat([...BLOCK.match(/const TIPS = \[([^\]]*)\]/)[1].matchAll(/'(\w+)'/g)].map(m => 'fund.tip_' + m[1]));
  const all = [...new Set(used.concat(composed))].filter(k => !k.endsWith('_'));
  const missing = [];
  for (const lang of ['en', 'zh']) {
    p.I18n.setLang(lang);
    all.forEach(k => { if (!p.I18n.t(k)) missing.push(`${k}.${lang}`); });
  }
  p.I18n.setLang('en');
  eq('every key the panel names or composes exists in both languages', missing, []);
  t('the composed keys were actually found (guards the lint above)', all.length >= 20, String(all.length));
}

// ── The CSS the panel leans on ──────────────────────────────────────────────
console.log('css');
{
  const style = tpl.slice(0, tpl.indexOf('</style>', tpl.indexOf('.fd-load {')));
  t('the panel sets display, so #panelFund must still restore [hidden]',
    /#panelFund \[hidden\] \{ display: none !important; \}/.test(style));
  // The loading rules only: from their comment to the end of their
  // reduced-motion block, so another part of the tab is not held to this.
  const from = style.indexOf('/* While the figures load:');
  const motion = style.indexOf('@media (prefers-reduced-motion: reduce)', from);
  const block = style.slice(from, style.indexOf('}', style.indexOf('}', motion) + 1) + 1);
  const animated = [...block.slice(0, motion - from).matchAll(/^\s*([^{}@\n][^{}\n]*?)\s*\{[^}]*\banimation:\s*fd-/gm)]
    .map(m => m[1].trim());
  const still = (/@media \(prefers-reduced-motion: reduce\) \{\s*([^{]*)\{\s*animation: none;/.exec(block) || [])[1] || '';
  const stilled = still.split(',').map(s => s.trim());
  t('every animated part of the panel is stilled under reduced motion',
    from > 0 && animated.length >= 4 && animated.every(sel => stilled.includes(sel)),
    `animated ${JSON.stringify(animated)}, stilled ${JSON.stringify(stilled)}`);
  t('#fundStatus sits inside #panelFund',
    tpl.indexOf('id="fundStatus"') > tpl.indexOf('id="panelFund"')
      && tpl.indexOf('id="fundStatus"') < tpl.indexOf('id="panelDca"'));
}

console.log(failures.length ? `\n${failures.length} FAILED` : '\nall passed');
process.exit(failures.length ? 1 : 0);
