/**
 * Behaviour tests for static/pulltorefresh.js.
 *
 * The risk in pull-to-refresh is not a refresh that fails to happen — the user
 * simply pulls again. It is a reload that happens when nobody asked for one,
 * which throws away scroll position, chart state and anything typed. So most of
 * these assert that a gesture is *not* treated as a pull: too short, horizontal,
 * two-fingered, mid-page, inside a scrolled sub-list, or while a text field has
 * focus.
 *
 * Run: node tests/check_pulltorefresh.mjs
 */
import fs from 'fs';
import path from 'path';

const root = path.resolve(import.meta.dirname, '..');
const SRC = path.join(root, 'ystocker/static/pulltorefresh.js');

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${detail ? '  ' + detail : ''}`);
  if (!cond) failures.push(label);
};

// Stubbed for the whole run, not just around eval(): the module reads
// global.setTimeout when it commits, so a restored real timer would fire its
// reload 220ms later — after the assertion, and into whichever test is running
// by then. Inline makes every commit synchronous and test-local.
global.setTimeout = f => { f(); return 0; };

// ── Minimal DOM ────────────────────────────────────────────────────────────
function makeEl(tag = 'DIV') {
  const el = {
    nodeType: 1,
    tagName: tag,
    style: {},
    children: [],
    hidden: false,
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 0,
    parentNode: null,
    textContent: '',
    className: '',
    classes: new Set(),
    attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    getBoundingClientRect: () => ({ width: 200, height: 26, top: 0 }),
  };
  el.classList = {
    add: (...c) => c.forEach(x => el.classes.add(x)),
    remove: (...c) => c.forEach(x => el.classes.delete(x)),
    contains: c => el.classes.has(c),
  };
  return el;
}

let handlers, reloads, styleText, pill, sheet, header;

// Node ships a getter-only global `navigator`, so it cannot be plainly assigned.
function setNavigator(value) {
  Object.defineProperty(global, 'navigator', { value, configurable: true, writable: true });
}

function install({ scrollY = 0, active = null, sheetOpen = false } = {}) {
  handlers = {};
  reloads = 0;
  styleText = '';

  const head = makeEl('HEAD');
  const body = makeEl('BODY');
  header = makeEl('HEADER');
  header.getBoundingClientRect = () => ({ width: 400, height: 56, top: 0 });
  sheet = makeEl('SECTION');
  sheet.hidden = !sheetOpen;

  global.window = global;
  global.console = console;
  global.ontouchstart = null;                 // present => touch device
  setNavigator({ maxTouchPoints: 5 });
  global.scrollY = scrollY;
  global.location = { reload: () => { reloads++; } };
  global.document = {
    readyState: 'complete',
    documentElement: { scrollTop: scrollY },
    head, body,
    activeElement: active,
    createElement: tag => makeEl(tag.toUpperCase()),
    querySelector: sel => (sel === 'header.sticky' ? header : null),
    getElementById: id => (id === 'agentsFloatingPanel' ? sheet : null),
    addEventListener: (ev, fn) => { handlers[ev] = fn; },
    removeEventListener: (ev) => { delete handlers[ev]; },
  };
  global.addEventListener = (ev, fn) => { handlers[ev] = fn; };

  delete global.PullToRefresh;
  delete global.I18n;

  // eslint-disable-next-line no-eval
  eval(fs.readFileSync(SRC, 'utf8'));

  styleText = head.children.map(c => c.textContent).join('');
  pill = body.children[0];
  return { head, body };
}

// ── Gesture helpers ────────────────────────────────────────────────────────
const pt = (x, y) => ({ clientX: x, clientY: y });

function fire(type, touches, target) {
  const e = {
    type, touches, target: target || global.document.body,
    cancelable: true, prevented: false,
    preventDefault() { this.prevented = true; },
  };
  if (handlers[type]) handlers[type](e);
  return e;
}

/** One complete pull of `dy` px, in a few steps like a real finger. */
function pull(dy, { target, x = 100, steps = 4, end = 'touchend' } = {}) {
  const moves = [];
  fire('touchstart', [pt(x, 200)], target);
  for (let i = 1; i <= steps; i++) {
    moves.push(fire('touchmove', [pt(x, 200 + (dy * i) / steps)], target));
  }
  if (end) fire(end, [], target);
  return moves;
}

// travel = (dy - 10) * 0.55, armed at 56 => dy >= ~112.
const LONG = 140;
const SHORT = 60;

// ── The gesture works ──────────────────────────────────────────────────────
console.log('=== a deliberate pull at the top reloads ===');
install();
t('indicator built', !!pill && pill.className === 'ystk-ptr');
t('indicator is decorative to AT', pill.attrs['aria-hidden'] === 'true');
const moves = pull(LONG);
t('reloads once', reloads === 1, `got ${reloads}`);
t('shows the busy state', pill.classes.has('ystk-ptr-busy'));
t('holds back the rubber-band', moves.some(m => m.prevented));
t('binds touchstart/end/cancel', ['touchstart', 'touchend', 'touchcancel'].every(k => handlers[k]));

// ── The scroll fast path ───────────────────────────────────────────────────
// A non-passive document touchmove listener stops the browser scrolling without
// first consulting JS. On dashboards this heavy that is worth not paying except
// while a pull is actually possible.
console.log();
console.log('=== the scroll-blocking listener is bound per gesture ===');
install();
t('not bound at rest', !handlers.touchmove);
fire('touchstart', [pt(100, 200)]);
t('bound by a touchstart at the top', typeof handlers.touchmove === 'function');
fire('touchend', []);
t('unbound on touchend', !handlers.touchmove);

install({ scrollY: 800 });
fire('touchstart', [pt(100, 200)]);
t('never bound partway down a page', !handlers.touchmove);

install({ sheetOpen: true });
fire('touchstart', [pt(100, 200)]);
t('never bound behind an open sheet', !handlers.touchmove);

install();
fire('touchstart', [pt(100, 200)]);
fire('touchcancel', []);
t('unbound on touchcancel', !handlers.touchmove);

console.log();
console.log('=== replaces the native gesture rather than stacking with it ===');
install();
t('suppresses browser pull-to-refresh',
  /html\{overscroll-behavior-y:contain\}/.test(styleText));
t('ships its own CSS (Tailwind here is compiled)',
  styleText.includes('.ystk-ptr{') && styleText.includes('ystk-ptr-spin'));
t('honours prefers-reduced-motion',
  styleText.includes('prefers-reduced-motion'));

console.log();
console.log('=== the pill clears the sticky header ===');
// Parked above the viewport at rest, and by full travel it has come down past a
// 56px sticky header rather than sitting behind it.
install();
t('parked off-screen at rest', /translate\(-50%,-34px\)/.test(pill.style.transform || 'translate(-50%,-34px)'));
fire('touchstart', [pt(100, 200)]);
fire('touchmove', [pt(100, 200 + 400)]);      // way past MAX_TRAVEL
const y = Number(/translate\(-50%,([-\d.]+)px\)/.exec(pill.style.transform)[1]);
t('travels clear of the 56px header', y > 56, `y=${y}`);
fire('touchcancel', []);

console.log();
console.log('=== the label tracks the gesture ===');
install();
fire('touchstart', [pt(100, 200)]);
fire('touchmove', [pt(100, 260)]);          // engaged, below threshold
const labelBelow = pill.children[1].textContent;
fire('touchmove', [pt(100, 200 + LONG)]);   // past threshold
const labelArmed = pill.children[1].textContent;
fire('touchend', []);
t('reads "Pull to refresh" below threshold', labelBelow === 'Pull to refresh', labelBelow);
t('reads "Release to refresh" once armed', labelArmed === 'Release to refresh', labelArmed);
t('reads "Refreshing…" on commit', pill.children[1].textContent === 'Refreshing…');

console.log();
console.log('=== translated when I18n is present ===');
install();
global.I18n = { t: k => ({ 'ptr.pull': '下拉刷新', 'ptr.release': '松开即刷新', 'ptr.busy': '正在刷新…' }[k]) };
pull(LONG);
t('uses the I18n string', pill.children[1].textContent === '正在刷新…');

console.log();
console.log('=== missing I18n key falls back to English ===');
install();
global.I18n = { t: k => k };                 // some builds echo the key back
pull(LONG);
t('does not render a raw key', pill.children[1].textContent === 'Refreshing…');

// ── The cases that must NOT reload ─────────────────────────────────────────
console.log();
console.log('=== a short pull does not reload ===');
install();
pull(SHORT);
t('no reload', reloads === 0);
t('pill retracts', pill.style.opacity === '0');
t('not left in the busy state', !pill.classes.has('ystk-ptr-busy'));

console.log();
console.log('=== a pull mid-page does not reload ===');
install({ scrollY: 800 });
pull(LONG);
t('no reload while scrolled down', reloads === 0);

console.log();
console.log('=== scrolling away from the top mid-gesture does not reload ===');
// Starts legitimately at the top, but the page scrolls before the axis is
// decided — the touch belongs to the scroll, not to a refresh.
install();
fire('touchstart', [pt(100, 200)]);
global.scrollY = 400;
fire('touchmove', [pt(100, 200 + LONG)]);
fire('touchend', []);
t('no reload', reloads === 0);

console.log();
console.log('=== a horizontal swipe does not reload ===');
install();
fire('touchstart', [pt(100, 200)]);
fire('touchmove', [pt(400, 230)]);          // dx 300 >> dy 30
fire('touchmove', [pt(500, 200 + LONG)]);
fire('touchend', []);
t('no reload', reloads === 0, 'protects overflow-x tables');

console.log();
console.log('=== an upward drag is an ordinary scroll ===');
install();
fire('touchstart', [pt(100, 200)]);
const up = fire('touchmove', [pt(100, 100)]);
fire('touchmove', [pt(100, 340)]);          // reverses, but the touch is spent
fire('touchend', []);
t('no reload', reloads === 0);
t('never blocks the scroll', up.prevented === false);

console.log();
console.log('=== a second finger abandons the pull ===');
install();
fire('touchstart', [pt(100, 200)]);
fire('touchmove', [pt(100, 200 + LONG)]);
fire('touchmove', [pt(100, 200 + LONG), pt(200, 300)]);   // pinch-zoom
fire('touchend', []);
t('no reload', reloads === 0);

console.log();
console.log('=== touchcancel abandons the pull ===');
install();
pull(LONG, { end: 'touchcancel' });
t('no reload', reloads === 0);
t('pill retracts', pill.style.opacity === '0');

console.log();
console.log('=== a pull inside a scrolled sub-list belongs to that list ===');
install();
const list = makeEl();
list.scrollTop = 40;
list.scrollHeight = 400;
list.clientHeight = 200;
const row = makeEl();
row.parentNode = list;
pull(LONG, { target: row });
t('no reload', reloads === 0, 'nav search results, scrollable tables');

console.log();
console.log('=== an unscrolled sub-list still chains to the page ===');
install();
const atTop = makeEl();
atTop.scrollTop = 0;
atTop.scrollHeight = 400;
atTop.clientHeight = 200;
const row2 = makeEl();
row2.parentNode = atTop;
pull(LONG, { target: row2 });
t('reloads', reloads === 1, 'a list already at its top is not the gesture owner');

console.log();
console.log('=== a focused text field suppresses the gesture ===');
for (const tag of ['INPUT', 'TEXTAREA', 'SELECT']) {
  install({ active: makeEl(tag) });
  pull(LONG);
  t(`no reload with ${tag} focused`, reloads === 0);
}
install({ active: makeEl('BUTTON') });
pull(LONG);
t('a focused button does not suppress it', reloads === 1);

console.log();
console.log('=== an open research-desk sheet owns its own gestures ===');
install({ sheetOpen: true });
pull(LONG);
t('no reload while the sheet is open', reloads === 0);
install({ sheetOpen: false });
pull(LONG);
t('reloads once the sheet is closed', reloads === 1);

// ── Committed state ────────────────────────────────────────────────────────
console.log();
console.log('=== a committed refresh cannot fire twice ===');
install();
pull(LONG);
t('one reload', reloads === 1);
fire('touchend', []);                        // stray end
pull(LONG);                                  // impatient second pull
t('still one reload', reloads === 1, `got ${reloads}`);

console.log();
console.log('=== a back/forward restore un-sticks the indicator ===');
// Restored from the bfcache with `busy` still set, the pill would spin forever
// and the gesture would be dead for the rest of the page's life.
install();
pull(LONG);
t('busy after commit', pill.classes.has('ystk-ptr-busy'));
t('pageshow is bound', typeof handlers.pageshow === 'function');
handlers.pageshow({ persisted: true });
t('spinner cleared', !pill.classes.has('ystk-ptr-busy'));
reloads = 0;
pull(LONG);
t('gesture works again', reloads === 1);

console.log();
console.log('=== inert without touch support ===');
install();
delete global.ontouchstart;
setNavigator({ maxTouchPoints: 0 });
handlers = {};
const head2 = makeEl('HEAD');
global.document.head = head2;
// eslint-disable-next-line no-eval
eval(fs.readFileSync(SRC, 'utf8'));
t('binds nothing on a mouse-only device', Object.keys(handlers).length === 0);
t('injects no CSS, so native overscroll is untouched', head2.children.length === 0);

console.log();
if (failures.length) {
  console.log(`RESULT: FAIL — ${failures.length}: ${failures.join(', ')}`);
  process.exit(1);
}
console.log('RESULT: OK');
