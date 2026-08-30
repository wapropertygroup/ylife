/**
 * Behaviour tests for static/autorefresh.js.
 *
 * The risk here is not a refresh that fails to happen — the reader can always
 * reload. It is a reload that happens when nobody asked, or worse, a reload
 * *loop*: two gunicorn workers can disagree about the cache stamp under
 * --preload, so a naive "stamp differs -> reload" ping-pongs forever between a
 * worker holding the new payload and one still serving the inherited snapshot.
 * Most of what follows asserts that no reload happens: same stamp, older stamp,
 * a stamp already claimed, a reader mid-page, offline, or a hidden tab.
 *
 * Run: node tests/check_autorefresh.mjs
 */
import fs from 'fs';
import path from 'path';

const root = path.resolve(import.meta.dirname, '..');
const SRC = path.join(root, 'ystocker/static/autorefresh.js');

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${detail ? '  ' + detail : ''}`);
  if (!cond) failures.push(label);
};

// Faked for the whole run, not just around the eval: the module reads Date.now
// on every poll and every interaction, so a clock restored after load would make
// `idle()` and the MIN_GAP_MS assertions silently compare real timestamps and
// pass or fail for reasons the test never stated. The binding below is the same
// `clock` that load() resets, so the arrow tracks it.
Date.now = () => clock;

// ── Minimal DOM ────────────────────────────────────────────────────────────

function makeEl(tag = 'DIV') {
  const el = {
    nodeType: 1,
    tagName: tag,
    style: {},
    children: [],
    textContent: '',
    className: '',
    type: '',
    onclick: null,
    classes: new Set(),
    attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
  };
  el.classList = {
    add: (...c) => c.forEach(x => el.classes.add(x)),
    remove: (...c) => c.forEach(x => el.classes.delete(x)),
    contains: c => el.classes.has(c),
  };
  return el;
}

let handlers, reloads, timers, store, fetchCalls, metaReply, pillRoot, clock;

/**
 * Load a fresh copy of the module against a fresh fake environment.
 *
 * Re-evaluated per scenario rather than reset, because `watched` is module-level
 * state that deliberately refuses a second watch() on the same URL — reusing one
 * instance would make every test after the first a no-op.
 */
function load({ hidden = false, onLine = true, sessionStorage = true } = {}) {
  handlers = {};
  reloads = 0;
  timers = [];
  fetchCalls = 0;
  store = {};
  clock = 1_000_000;
  metaReply = { fetched_at: 0, warming: false };

  const body = makeEl('BODY');
  const head = makeEl('HEAD');
  pillRoot = body;

  const doc = {
    hidden,
    head,
    body,
    readyState: 'complete',
    activeElement: null,
    createElement: makeEl,
    getElementById: () => null,
    querySelector: () => null,
    addEventListener(ev, fn) { (handlers[ev] ||= []).push(fn); },
    removeEventListener() {},
  };

  const global_ = {
    document: doc,
    navigator: { onLine },
    location: { reload() { reloads += 1; } },
    // Split by delay, which cleanly separates the module's two uses of the
    // timer. The short one is reload()'s settle beat: run it inline so a reload
    // is observable in the same tick as the poll that caused it. The long one is
    // the next scheduled poll: collect it, or firing it here would recurse
    // forever.
    setTimeout(fn, ms) {
      if (ms <= 500) { fn(); return 0; }
      timers.push({ fn, ms });
      return timers.length;
    },
    clearTimeout() {},
    addEventListener(ev, fn) { (handlers[ev] ||= []).push(fn); },
    fetch(url) {
      fetchCalls += 1;
      return Promise.resolve({ json: () => Promise.resolve(metaReply) });
    },
    I18n: { t: k => ({ 'autorefresh.new_data': '有更新的数据',
                       'autorefresh.refresh': '刷新',
                       'autorefresh.dismiss': '忽略' })[k] || k },
  };
  if (sessionStorage) {
    global_.sessionStorage = {
      getItem: k => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
    };
  }
  // Date.now is stubbed for the whole run (see the top of this file), so the
  // module's own now() reads `clock` and a test can place an interaction "70
  // seconds ago" without sleeping.
  const src = fs.readFileSync(SRC, 'utf8');
  const fn = new Function('window', src);
  fn(global_);
  return global_;
}

/** Run the pending timer callbacks once (the scheduled poll). */
async function runTimers() {
  const due = timers.splice(0, timers.length);
  for (const { fn } of due) fn();
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
}

async function fire(ev) {
  for (const fn of handlers[ev] || []) fn({ persisted: true });
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
}

const pill = () => pillRoot.children.find(c => c.className === 'ystk-ar');

/**
 * Advance past INTERACTION_MS.
 *
 * A freshly loaded page seeds lastActivity to "now", so for the first minute of
 * its life a stamp change is *offered* rather than taken — correct behaviour
 * (the render is seconds old, and someone is plainly looking at it), but it
 * means every test of the automatic path has to place itself after that window.
 * In production the gap is the poll interval, which is 30 minutes.
 */
const idle = () => { clock += 70 * 1000; };

// ── Source-level invariants ────────────────────────────────────────────────
console.log('\nsource');
{
  const src = fs.readFileSync(SRC, 'utf8');
  t('polls the stamp endpoint, never /api/housing itself',
    !src.includes("'/api/housing'") && !src.includes('"/api/housing"'));
  t('guards the loop with sessionStorage, not a variable',
    src.includes('sessionStorage') && src.includes('claim'));
  t('only trusts onLine in the false direction',
    src.includes('onLine === false'));
  t('CSS uses shared --t-* tokens (Tailwind is compiled here)',
    src.includes('var(--t-surface') && src.includes('var(--t-accent'));
  t('no hardcoded Chinese or English-only user string outside LABELS',
    (src.match(/textContent = '(?!↻ |×)/g) || []).length === 0);
}

// ── Does not reload when it should not ─────────────────────────────────────
console.log('\nquiet cases');
{
  const g = load();
  g.AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 5000 });
  metaReply = { fetched_at: 5000, warming: false };
  await runTimers();
  t('same stamp does not reload', reloads === 0);

  metaReply = { fetched_at: 4000, warming: false };
  await runTimers();
  t('older stamp does not reload', reloads === 0, '(worker serving a stale snapshot)');
}
{
  const g = load({ onLine: false });
  g.AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 5000 });
  metaReply = { fetched_at: 9000, warming: false };
  await runTimers();
  t('offline does not reload', reloads === 0, '(sw.js would serve offline.html)');
  t('offline does not even poll', fetchCalls === 0);
}
{
  const g = load({ hidden: true });
  g.AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 5000 });
  metaReply = { fetched_at: 9000, warming: false };
  await runTimers();
  t('hidden tab does not poll on its timer', fetchCalls === 0);
}
{
  const g = load();
  g.AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 5000 });
  g.AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 5000 });
  metaReply = { fetched_at: 9000, warming: false };
  idle();
  await runTimers();
  t('a second watch() on the same URL is refused', reloads === 1,
    `(reloads=${reloads}, one watcher not two)`);
}

// ── The loop guard ─────────────────────────────────────────────────────────
console.log('\nloop guard');
{
  const g = load();
  g.AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 5000 });
  metaReply = { fetched_at: 9000, warming: false };
  idle();
  await runTimers();
  t('newer stamp reloads once', reloads === 1);

  // The reload lands on a worker that still holds the old payload: same
  // rendered stamp, same newer meta stamp. Only the claim carried through
  // sessionStorage can tell this apart from genuinely new news.
  const carried = { ...store };
  const g2 = load();
  Object.assign(store, carried);
  g2.AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 5000 });
  metaReply = { fetched_at: 9000, warming: false };
  idle();
  await runTimers();
  t('a claimed stamp never reloads twice', reloads === 0,
    '(the two-worker ping-pong)');
}
{
  // A one-minute interval, so the poll cadence itself does not clear the gap.
  const g = load();
  g.AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 5000, intervalMs: 60 * 1000 });
  metaReply = { fetched_at: 9000, warming: false };
  idle();
  await runTimers();
  t('first reload fires', reloads === 1);
  metaReply = { fetched_at: 12000, warming: false };
  await runTimers();
  t('MIN_GAP_MS blocks a second reload inside 5 min', reloads === 1);
  clock += 6 * 60 * 1000;
  await runTimers();
  t('and allows it once the gap has passed', reloads === 2);
}

// ── Offer instead of reload while someone is reading ───────────────────────
console.log('\nactive reader');
{
  const g = load();
  g.AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 5000 });
  await fire('pointerdown');                     // interaction "just now"
  metaReply = { fetched_at: 9000, warming: false };
  await runTimers();
  t('does not reload out from under an active reader', reloads === 0);
  t('offers a pill instead', !!pill() && pill().classes.has('ystk-ar-on'));
  t('pill text is translated', pill()?.children[1]?.textContent === '有更新的数据');
  t('pill action is translated', pill()?.children[2]?.textContent === '↻ 刷新');

  pill().children[2].onclick();                  // Refresh
  await runTimers();
  t('the pill Refresh action reloads', reloads === 1);
}
{
  const g = load();
  g.AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 5000 });
  await fire('pointerdown');
  metaReply = { fetched_at: 9000, warming: false };
  await runTimers();
  t('pill shown', !!pill());
  pill().children[3].onclick();                  // ×
  t('dismiss hides it', !pill().classes.has('ystk-ar-on'));
  await runTimers();
  t('a dismissed stamp does not come back', reloads === 0 && !pill().classes.has('ystk-ar-on'));
}
{
  // The pill's text comes from JS, so I18n.apply() cannot reach it: without a
  // langchange listener an English pill would sit on a page switched to Chinese.
  const g = load();
  g.AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 5000 });
  await fire('pointerdown');
  metaReply = { fetched_at: 9000, warming: false };
  await runTimers();
  t('pill offered', !!pill() && pill().classes.has('ystk-ar-on'));
  g.I18n.t = k => ({ 'autorefresh.new_data': 'Newer data is available',
                     'autorefresh.refresh': 'Refresh' })[k] || k;
  await fire('i18n:langchange');
  t('pill retranslates on a language switch',
    pill().children[1].textContent === 'Newer data is available' &&
    pill().children[2].textContent === '↻ Refresh');
}
{
  const g = load();
  g.AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 5000 });
  await fire('pointerdown');
  clock += 70 * 1000;                            // interaction now stale
  metaReply = { fetched_at: 9000, warming: false };
  await runTimers();
  t('an idle reader is reloaded automatically', reloads === 1,
    '(INTERACTION_MS has lapsed)');
}

// ── The path this module exists for ────────────────────────────────────────
console.log('\ntab returned to');
{
  const g = load();
  g.AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 5000 });
  await fire('pointerdown');                     // was being read before hiding
  metaReply = { fetched_at: 9000, warming: false };
  await fire('visibilitychange');
  t('becoming visible polls immediately', fetchCalls === 1);
  t('and reloads despite the pre-hide interaction', reloads === 1,
    '(a returned-to tab has nothing to lose yet)');
}
{
  const g = load();
  g.AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 5000 });
  metaReply = { fetched_at: 9000, warming: false };
  await fire('pageshow');
  t('back/forward cache restore reloads on a newer stamp', reloads === 1);
}
{
  // visibilitychange fires in both directions. Without the doc.hidden guard,
  // switching *away* from the tab would poll and could reload a page nobody is
  // looking at — and the reader would return to a scroll position they lost for
  // no visible reason.
  const g = load({ hidden: true });
  g.AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 5000 });
  metaReply = { fetched_at: 9000, warming: false };
  await fire('visibilitychange');
  t('going hidden neither polls nor reloads', fetchCalls === 0 && reloads === 0);
}

// ── Cadence ────────────────────────────────────────────────────────────────
console.log('\ncadence');
{
  const g = load();
  g.AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 5000 });
  metaReply = { fetched_at: 5000, warming: false };
  await runTimers();
  t('idle cadence is 30 min', timers[0]?.ms === 30 * 60 * 1000, `got ${timers[0]?.ms}`);

  metaReply = { fetched_at: 5000, warming: true };
  await runTimers();
  t('polls faster while the server is rebuilding', timers[0]?.ms === 60 * 1000,
    `got ${timers[0]?.ms}`);
}
{
  const g = load();
  g.AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 5000 });
  g.fetch = () => Promise.reject(new Error('boom'));
  await runTimers();
  t('a failed poll backs off rather than surfacing an error',
    timers[0]?.ms > 30 * 60 * 1000, `got ${timers[0]?.ms}`);
  t('and never reloads', reloads === 0);
}
{
  // Safari private mode throws on sessionStorage. The module must still work,
  // falling back to MIN_GAP_MS as its only loop guard.
  const g = load({ sessionStorage: false });
  g.AutoRefresh.watch({ meta: '/api/housing/meta', stamp: 5000 });
  metaReply = { fetched_at: 9000, warming: false };
  idle();
  await runTimers();
  t('survives sessionStorage being unavailable', reloads === 1);
}

console.log(`\n${failures.length ? 'FAILURES: ' + failures.join('; ') : 'all passed'}\n`);
process.exit(failures.length ? 1 : 0);
