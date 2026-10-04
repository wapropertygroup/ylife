// Checks the masthead's hand-off to the pay page: `data-carry-prefs` in
// templates/_ta_masthead.html. A link to pay.trade-agents.com carries the
// reader's language and theme, because that page is another origin and cannot
// read this one's localStorage.
//
// It shipped reading `window.I18n`, which does not exist: i18n.js declares I18n
// as a top-level const, a script-scoped binding that is never a property of
// window. So every link said lang=en, and a reader who pressed 充值 on a Chinese
// page arrived at an English one. The first harness for it defined window.I18n
// by hand and passed. So this one loads the real i18n.js as a classic script in
// a vm context -- where that binding is exactly what a page gets, which the
// first assertions prove -- and runs the handler extracted from the template,
// not a copy of it.
//
// Run: node tests/check_carry_prefs.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', 'ystocker');
const i18nSrc = readFileSync(join(root, 'static', 'i18n.js'), 'utf8');
const tpl = readFileSync(join(root, 'templates', '_ta_masthead.html'), 'utf8');

// The handler and the line that binds it, from the template.
const from = tpl.indexOf('  const carry = e => {');
const bind = "['click', 'auxclick', 'contextmenu'].forEach(t => document.addEventListener(t, carry, true));";
const to = tpl.indexOf(bind, from);
if (from < 0 || to < 0) throw new Error('carry handler not found in _ta_masthead.html');
const handler = tpl.slice(from, to + bind.length);

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${cond || !detail ? '' : '  — ' + detail}`);
  if (!cond) failures.push(label);
};

/* Just enough of a page for i18n.js to load and switch, and for the handler to
   run: <html> with its lang and class list, localStorage (filled before
   i18n.js reads it), the address, and a document that records listeners
   rather than dispatching real events. Dark, as the site is by default. */
function page(search, stored = {}) {
  const classes = new Set(['dark']);
  const store = new Map(Object.entries(stored));
  const listeners = [];
  const html = {
    lang: 'en',
    classList: {
      contains: c => classes.has(c), add: c => classes.add(c), remove: c => classes.delete(c),
      toggle: (c, on) => ((on ?? !classes.has(c)) ? classes.add(c) : classes.delete(c)),
    },
  };
  const document = {
    documentElement: html,
    addEventListener: (type, fn, capture) => listeners.push({ type, fn, capture }),
    dispatchEvent: () => true,
    querySelectorAll: () => [],
    querySelector: () => null,
  };
  const ctx = {
    document, URL, URLSearchParams, console,
    // A real Location always has hostname: i18n.js reads it at load to pick the
    // brand, and an absent one threw before any check here could run.
    location: { search, origin: 'https://trade-agents.com', hostname: 'trade-agents.com',
                href: 'https://trade-agents.com/docs/overview' + search },
    Event: class { constructor(type) { this.type = type; } },
    history: { replaceState: () => {} },
    localStorage: { getItem: k => (store.has(k) ? store.get(k) : null),
                    setItem: (k, v) => store.set(k, String(v)) },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(i18nSrc, ctx, { filename: 'i18n.js' });
  vm.runInContext(handler, ctx, { filename: '_ta_masthead.html' });
  return { ctx, html, classes, listeners };
}

// A link as the handler sees one: `closest` hands back the element itself when
// it is marked, as the DOM would for a click on the link or anything inside it.
function anchor(href, marked = true) {
  const a = { href };
  a.closest = sel => (marked && sel === 'a[data-carry-prefs]' ? a : null);
  return a;
}
function follow(p, a, type = 'click') {
  for (const l of p.listeners) if (l.type === type) l.fn({ target: a });
  return new URL(a.href).searchParams;
}

const PAY = 'https://pay.trade-agents.com/?email=reader%40example.com&next=https%3A%2F%2Ftrade-agents.com%2Fagents';

// ── the environment is the one that broke it ───────────────────────────────
{
  const p = page('?lang=zh');
  t('i18n.js binds I18n as a script-scoped const', vm.runInContext('typeof I18n', p.ctx) === 'object');
  t('…which is not a property of window, as in a browser', typeof p.ctx.window.I18n === 'undefined');
  t('i18n.js set <html lang> from ?lang=zh', p.html.lang === 'zh-CN', p.html.lang);
  t('the handler is bound for click, middle-click and the context menu, in capture',
    ['click', 'auxclick', 'contextmenu'].every(type => p.listeners.some(l => l.type === type && l.capture)));
}

// ── a Chinese reader's Prepay ───────────────────────────────────────────────
{
  const p = page('?lang=zh');
  const a = anchor(PAY);
  const q = follow(p, a);
  t('a Chinese page sends lang=zh', q.get('lang') === 'zh', q.get('lang'));
  t('and the theme on screen', q.get('theme') === 'dark', q.get('theme'));
  t('the address and the way back are kept',
    q.get('email') === 'reader@example.com' && q.get('next') === 'https://trade-agents.com/agents');

  // Switched after the page loaded: English and light. The link says so the
  // next time it is followed, each parameter replaced rather than repeated.
  vm.runInContext("I18n.setLang('en')", p.ctx);
  p.classes.delete('dark');
  const q2 = follow(p, a);
  t('a switch to English since the page loaded rides along', q2.get('lang') === 'en', q2.get('lang'));
  t('and to light', q2.get('theme') === 'light', q2.get('theme'));
  t('each parameter once', q2.getAll('lang').length === 1 && q2.getAll('theme').length === 1, a.href);
}

// ── no ?lang= in the address: i18n.js's stored choice, or English ──────────
{
  const q = follow(page('', { ystocker_lang: 'zh' }), anchor(PAY));
  t('a reader who chose Chinese on an earlier visit sends lang=zh', q.get('lang') === 'zh', q.get('lang'));
  t('with nothing chosen, English', follow(page(''), anchor(PAY)).get('lang') === 'en');
}

// ── other links are left alone; a middle-click carries it too ──────────────
{
  const p = page('?lang=zh');
  const other = anchor('https://pay.trade-agents.com/?email=x', false);
  follow(p, other);
  follow(p, other, 'auxclick');
  t('an unmarked link is untouched', other.href === 'https://pay.trade-agents.com/?email=x', other.href);
  t('a middle-click on Prepay carries it', follow(p, anchor(PAY), 'auxclick').get('lang') === 'zh');
}

if (failures.length) {
  console.error(`\n${failures.length} failed`);
  process.exit(1);
}
console.log('\nall passed');
