/**
 * Behaviour tests for static/deferload.js.
 *
 * The risk in deferred loading is not that a fetch fires late — it is that it
 * never fires at all, leaving a panel permanently empty with no error anywhere.
 * So these focus on the cases that could swallow a loader: a target that is
 * display:none (no box, so IntersectionObserver never fires), a missing target,
 * and an environment without IntersectionObserver.
 *
 * Run: node tests/check_deferload.mjs
 */
import fs from 'fs';
import path from 'path';

const root = path.resolve(import.meta.dirname, '..');

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${detail ? '  ' + detail : ''}`);
  if (!cond) failures.push(label);
};

// ── Minimal DOM ────────────────────────────────────────────────────────────
let observers = [];

function makeEl({ hidden = false } = {}) {
  return {
    nodeType: 1,
    // display:none => no offsetParent and a zero-size rect.
    offsetParent: hidden ? null : { nodeType: 1 },
    getBoundingClientRect: () => (hidden
      ? { width: 0, height: 0 }
      : { width: 400, height: 200 }),
  };
}

function install({ withIO = true } = {}) {
  observers = [];
  const els = {};
  global.window = global;
  global.document = { querySelector: sel => els[sel] || null };
  global.console = console;
  if (withIO) {
    global.IntersectionObserver = class {
      constructor(cb, opts) {
        this.cb = cb; this.opts = opts; this.observed = []; this.disconnected = false;
        observers.push(this);
      }
      observe(el) { this.observed.push(el); }
      disconnect() { this.disconnected = true; }
      // test helper
      fire(isIntersecting = true) { this.cb(this.observed.map(() => ({ isIntersecting }))); }
    };
  } else {
    delete global.IntersectionObserver;
  }
  delete global.DeferLoad;
  // readyState 'complete' + a synchronous rAF stub means scheduleSettle() runs
  // inline, so each test observes against its own stub layout immediately.
  global.document.readyState = 'complete';
  global.document.addEventListener = () => {};
  global.requestAnimationFrame = f => { f(); return 0; };
  const realTimeout = global.setTimeout;
  global.setTimeout = f => { f(); return 0; };
  // eslint-disable-next-line no-eval
  eval(fs.readFileSync(path.join(root, 'ystocker/static/deferload.js'), 'utf8'));
  global.setTimeout = realTimeout;
  return els;
}

// ── Deferred until visible ─────────────────────────────────────────────────
console.log('=== defers a visible target ===');
let els = install();
els['#panel'] = makeEl();
let calls = 0;
let deferred = DeferLoad.when('#panel', () => { calls++; });
t('reports deferred', deferred === true);
t('does not run on registration', calls === 0);
t('registered one observer', observers.length === 1);
t('root margin defaults to 300px', observers[0].opts.rootMargin === '300px 0px');
observers[0].fire(true);
t('runs on intersection', calls === 1);
t('disconnects after firing', observers[0].disconnected === true);
observers[0].fire(true);
t('never runs twice', calls === 1);

console.log();
console.log('=== ignores non-intersecting callbacks ===');
els = install();
els['#panel'] = makeEl();
calls = 0;
DeferLoad.when('#panel', () => { calls++; });
observers[0].fire(false);
t('does not run when not intersecting', calls === 0);
t('stays observing', observers[0].disconnected === false);

// ── The cases that could silently swallow a loader ─────────────────────────
console.log();
console.log('=== display:none target loads eagerly (cannot ever intersect) ===');
els = install();
els['#hidden'] = makeEl({ hidden: true });
calls = 0;
deferred = DeferLoad.when('#hidden', () => { calls++; });
t('runs immediately', calls === 1);
t('reports not deferred', deferred === false);
t('registers no observer', observers.length === 0);

console.log();
console.log('=== no IntersectionObserver: loads eagerly ===');
els = install({ withIO: false });
els['#panel'] = makeEl();
calls = 0;
deferred = DeferLoad.when('#panel', () => { calls++; });
t('runs immediately', calls === 1);
t('reports not deferred', deferred === false);

console.log();
console.log('=== missing target does not run the loader ===');
els = install();
calls = 0;
deferred = DeferLoad.when('#nope', () => { calls++; });
t('loader not run against absent DOM', calls === 0);
t('reports not deferred', deferred === false);

// ── Errors are contained and attributed ────────────────────────────────────
console.log();
console.log('=== a throwing loader does not break the observer ===');
els = install();
els['#panel'] = makeEl();
const logged = [];
const realErr = console.error;
console.error = (...a) => logged.push(a.join(' '));
DeferLoad.when('#panel', () => { throw new Error('boom'); }, { label: 'panel-x' });
let threw = false;
try { observers[0].fire(true); } catch (_) { threw = true; }
console.error = realErr;
t('exception contained', !threw);
t('error names the panel', logged.some(l => l.includes('panel-x') && l.includes('threw')));

console.log();
console.log('=== a rejected async loader is reported, not unhandled ===');
els = install();
els['#panel'] = makeEl();
const logged2 = [];
console.error = (...a) => logged2.push(a.join(' '));
DeferLoad.when('#panel', async () => { throw new Error('async boom'); }, { label: 'panel-y' });
observers[0].fire(true);
await new Promise(r => setTimeout(r, 10));
console.error = realErr;
t('rejection reported', logged2.some(l => l.includes('panel-y') && l.includes('rejected')));

// ── Element passed directly ────────────────────────────────────────────────
console.log();
console.log('=== accepts an element, not just a selector ===');
install();
const direct = makeEl();
calls = 0;
t('defers', DeferLoad.when(direct, () => { calls++; }) === true);
observers[0].fire(true);
t('runs', calls === 1);

// ── Registration waits for layout ──────────────────────────────────────────
// The bug this guards: registering during initial parse measures a page that is
// still skeletons, so panels destined for 6000px are briefly near the fold and
// their observers fire at once, deferring nothing.
console.log();
console.log('=== queues registrations until the layout has settled ===');
observers = [];
const lateEls = {};
global.window = global;
let domReadyCb = null;
global.document = {
  querySelector: sel => lateEls[sel] || null,
  readyState: 'loading',
  addEventListener: (ev, cb) => { if (ev === 'DOMContentLoaded') domReadyCb = cb; },
};
global.requestAnimationFrame = f => { f(); return 0; };
const realTO = global.setTimeout;
global.setTimeout = f => { f(); return 0; };
delete global.DeferLoad;
global.IntersectionObserver = class {
  constructor(cb, opts) {
    this.cb = cb; this.opts = opts; this.observed = []; this.disconnected = false;
    observers.push(this);
  }
  observe(el) { this.observed.push(el); }
  disconnect() { this.disconnected = true; }
  fire(v = true) { this.cb(this.observed.map(() => ({ isIntersecting: v }))); }
};
// eslint-disable-next-line no-eval
eval(fs.readFileSync(path.join(root, 'ystocker/static/deferload.js'), 'utf8'));

let lateCalls = 0;
// Registered while the document is still "loading" — and before the element
// even exists, as happens during parse.
const wasDeferred = DeferLoad.when('#late', () => { lateCalls++; });
t('reports deferred while unsettled', wasDeferred === true);
t('creates no observer yet', observers.length === 0, `got ${observers.length}`);
t('does not run the loader yet', lateCalls === 0);

// Element appears as the page builds, then the document becomes ready.
lateEls['#late'] = makeEl();
t('DOMContentLoaded handler was registered', typeof domReadyCb === 'function');
domReadyCb();
t('observes once settled', observers.length === 1, `got ${observers.length}`);
t('still has not run the loader', lateCalls === 0);
observers[0].fire(true);
t('runs on intersection after settling', lateCalls === 1);

// settle() is idempotent and safe to call again.
DeferLoad.settle();
t('settle() twice creates no extra observers', observers.length === 1);
global.setTimeout = realTO;

console.log();
if (failures.length) {
  console.log(`RESULT: FAIL — ${failures.length}: ${failures.join(', ')}`);
  process.exit(1);
}
console.log('RESULT: OK');
