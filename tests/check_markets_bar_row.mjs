/**
 * Checks the row of page links that scrolls sideways when it does not fit:
 * trade-agents.com's Markets bar (templates/_ta_markets_bar.html) and, since
 * 2026-10-08, the yStocker header's tabs (base.html). Both run one script,
 * templates/_nav_row_script.html.
 *
 * At 390px the Markets bar shows a few of its seventeen links, so before this
 * script the pages at its far end -- Housing, Daily, Videos -- opened on
 * Markets, Valuation and DCA, and the bar said nothing about where the reader
 * was. Nothing about that is visible to a Python test, and it is easy to break
 * quietly: when Sectors and Macro were menus they were marked current twice
 * (the menu button a phone hides and the plain link it shows), so centring
 * the first current element centred one with no box and the row did not move.
 * A mouse wheel turns the row too, which a desktop needs now that the header
 * cannot show all seventeen tabs at once.
 *
 * The script is extracted from the template rather than copied, for the
 * reason check_dca_row_cells.mjs gives: a copy agrees on the day it is written
 * and drifts afterwards.
 *
 * Run: node tests/check_markets_bar_row.mjs
 */
import fs from 'fs';
import path from 'path';

const root = path.resolve(import.meta.dirname, '..');
const tpl = fs.readFileSync(path.join(root, 'ystocker/templates/_ta_markets_bar.html'), 'utf8');
const base = fs.readFileSync(path.join(root, 'ystocker/templates/base.html'), 'utf8');
const part = fs.readFileSync(path.join(root, 'ystocker/templates/_nav_row_script.html'), 'utf8');

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${detail ? '  ' + detail : ''}`);
  if (!cond) failures.push(label);
};

const SRC = part.slice(part.indexOf('<script>') + 8, part.lastIndexOf('</script>'));
if (!SRC.includes("'[data-nav-row]'")) throw new Error('row script not found in _nav_row_script.html');

// ── A row with real scroll geometry ─────────────────────────────────────────
// Links are laid out at `x` in the row's content box; what the page sees is
// that position less scrollLeft, and scrollLeft clamps as a browser's does.

function link(x, w, current = true) {
  return { x, w, current, get offsetWidth() { return this.w; } };
}

function load({ links = [], clientWidth = 288, scrollWidth = 782 } = {}) {
  const docHandlers = {}, rowHandlers = {};
  let resizeCb = null, fontsDone;
  const row = {
    clientWidth, scrollWidth, dataset: {}, _left: 0,
    get scrollLeft() { return this._left; },
    set scrollLeft(v) { this._left = Math.max(0, Math.min(v, this.scrollWidth - this.clientWidth)); },
    getBoundingClientRect() { return { left: 0, width: this.clientWidth }; },
    querySelectorAll: sel => (sel === '[aria-current="page"]' ? links.filter(l => l.current) : []),
    addEventListener: (type, fn) => { (rowHandlers[type] ||= []).push(fn); },
  };
  for (const l of links) l.getBoundingClientRect = () => ({ left: l.x - row._left, width: l.w });
  const document = {
    querySelector: sel => (sel === '[data-nav-row]' ? row : null),
    addEventListener: (type, fn) => { (docHandlers[type] ||= []).push(fn); },
    fonts: { ready: new Promise(r => { fontsDone = r; }) },
  };
  class ResizeObserver { constructor(cb) { resizeCb = cb; } observe() {} }
  const window = { ResizeObserver, addEventListener() {} };
  new Function('document', 'window', 'ResizeObserver', SRC)(document, window, ResizeObserver);
  const fire = (handlers, type, ev) => (handlers[type] || []).forEach(fn => fn(ev));
  return {
    row,
    doc: type => fire(docHandlers, type),
    on: (type, ev) => fire(rowHandlers, type, ev),
    resize: () => resizeCb && resizeCb(),
    fonts: async () => { fontsDone(); await new Promise(r => setTimeout(r, 0)); },
    centreOf: l => l.x - row._left + l.w / 2,
  };
}

// ── Where the row opens ─────────────────────────────────────────────────────
{
  const housing = link(560, 80);
  const g = load({ links: [housing] });
  t('the page\'s own link opens in the middle of the row',
    g.centreOf(housing) === 144, `centre at ${g.centreOf(housing)}, row centre 144`);
  t('with a fade on both sides, since both have more', g.row.dataset.fade === 'both', g.row.dataset.fade);
}
{
  // /housing: Macro's menu button is current too, and a phone hides it.
  const macro = link(0, 0), housing = link(560, 80);
  const g = load({ links: [macro, housing] });
  t('a current element with no box is passed over for the one shown',
    g.centreOf(housing) === 144, `centre at ${g.centreOf(housing)}`);
}
{
  const videos = link(720, 62);
  const g = load({ links: [videos] });
  t('the last link cannot pull the row past its end', g.row.scrollLeft === 782 - 288, `scrollLeft ${g.row.scrollLeft}`);
  t('and at the end the fade is on the left only', g.row.dataset.fade === 'left', g.row.dataset.fade);
}
{
  const g = load({ links: [link(6, 80)] });
  t('the first link leaves the row at its start', g.row.scrollLeft === 0);
  t('with the fade on the right only', g.row.dataset.fade === 'right', g.row.dataset.fade);
}
{
  const g = load({ links: [] });
  t('a page with no link of its own (a ticker) opens at the start', g.row.scrollLeft === 0);
  t('and still says there is more to the right', g.row.dataset.fade === 'right', g.row.dataset.fade);
}
{
  // Wide enough for every link (880px, or a desktop): nothing to scroll.
  const g = load({ links: [link(600, 80)], clientWidth: 770, scrollWidth: 770 });
  t('a row that fits is not scrolled', g.row.scrollLeft === 0);
  t('and has no fade at all', g.row.dataset.fade === 'none', g.row.dataset.fade);
}

// ── Kept centred until the reader takes over ────────────────────────────────
{
  // i18n.js swaps the labels at DOMContentLoaded, and the web font lands
  // later; each moves the link.
  const housing = link(560, 80);
  const g = load({ links: [housing] });
  housing.x = 500; housing.w = 40;
  g.doc('DOMContentLoaded');
  t('re-centred when the labels change at DOMContentLoaded', g.centreOf(housing) === 144, `centre at ${g.centreOf(housing)}`);
  housing.x = 520;
  await g.fonts();
  t('and again when the web font lands', g.centreOf(housing) === 144, `centre at ${g.centreOf(housing)}`);
}
for (const gesture of ['touchstart', 'pointerdown', 'wheel']) {
  const housing = link(560, 80);
  const g = load({ links: [housing] });
  g.on(gesture);
  g.row.scrollLeft = 40;
  g.on('scroll');
  g.doc('DOMContentLoaded');
  g.resize();
  t(`after a ${gesture} on the row, nothing pulls it back`, g.row.scrollLeft === 40, `scrollLeft ${g.row.scrollLeft}`);
}
{
  const housing = link(560, 80);
  const g = load({ links: [housing] });
  g.on('touchstart');
  g.row.scrollLeft = 0;
  housing.x = 580;
  g.doc('i18n:langchange');
  t('a language switch re-centres even so: every label has changed', g.centreOf(housing) === 144, `centre at ${g.centreOf(housing)}`);
}

// ── The fade follows the reader's scrolling ─────────────────────────────────
{
  const g = load({ links: [link(560, 80)] });
  g.on('touchstart');
  const seen = [0, 250, 494].map(x => { g.row.scrollLeft = x; g.on('scroll'); return g.row.dataset.fade; });
  t('start, middle and end fade right, both and left', seen.join() === 'right,both,left', seen.join());
}

// ── A mouse wheel turns the row ─────────────────────────────────────────────
function wheel(deltaY, { deltaX = 0, deltaMode = 0, ctrlKey = false } = {}) {
  return { deltaX, deltaY, deltaMode, ctrlKey, prevented: false,
           preventDefault() { this.prevented = true; } };
}
{
  const g = load({ links: [link(6, 80)] });
  const ev = wheel(100);
  g.on('wheel', ev);
  t('a wheel turned over the row moves it sideways', g.row.scrollLeft === 100, `scrollLeft ${g.row.scrollLeft}`);
  t('and keeps the page from scrolling under it', ev.prevented);
  t('and the fade follows', (g.on('scroll'), g.row.dataset.fade === 'both'), g.row.dataset.fade);
}
{
  const g = load({ links: [link(720, 62)] });          // opens at its end
  const ev = wheel(100);
  g.on('wheel', ev);
  t('at the end of the row the page scrolls as usual', !ev.prevented && g.row.scrollLeft === 782 - 288);
  const back = wheel(-120);
  g.on('wheel', back);
  t('and turning back moves the row the other way', back.prevented && g.row.scrollLeft === 782 - 288 - 120,
    `scrollLeft ${g.row.scrollLeft}`);
}
{
  const g = load({ links: [link(6, 80)] });
  const swipe = wheel(10, { deltaX: 60 });
  g.on('wheel', swipe);
  t('a trackpad\'s sideways swipe is left to the browser', !swipe.prevented && g.row.scrollLeft === 0);
  const zoom = wheel(100, { ctrlKey: true });
  g.on('wheel', zoom);
  t('ctrl and the wheel (a pinch, or zoom) is left alone', !zoom.prevented && g.row.scrollLeft === 0);
  const lines = wheel(3, { deltaMode: 1 });
  g.on('wheel', lines);
  t('a wheel that counts in lines moves a usable distance', g.row.scrollLeft === 54, `scrollLeft ${g.row.scrollLeft}`);
}
{
  const g = load({ links: [link(600, 80)], clientWidth: 770, scrollWidth: 770 });
  const ev = wheel(100);
  g.on('wheel', ev);
  t('a row that fits leaves the wheel to the page', !ev.prevented && g.row.scrollLeft === 0);
}

// ── Markup the phone layout depends on ──────────────────────────────────────
{
  // i18n.js's apply() sets textContent on every [data-i18n] element, which
  // deletes its children -- so on the Refresh link itself it would take the
  // icon with it, and a phone, which hides the label, would show an empty
  // button.
  const a = tpl.slice(tpl.indexOf('<a id="refreshBtn"'), tpl.indexOf('</a>', tpl.indexOf('<a id="refreshBtn"')));
  const tag = a.slice(0, a.indexOf('>'));
  t('the Refresh link carries no data-i18n of its own', !tag.includes('data-i18n'), tag);
  t('its label is a child the icon sits beside',
    a.includes('class="w-sub-refresh-icon"') && a.includes('<span class="w-sub-refresh-label" data-i18n="nav.refresh">'));
  t('the row carries the hook the script finds', tpl.includes('<div class="w-sub-links" data-nav-row>'));
  t('and the script follows the bar', tpl.trimEnd().endsWith('{% include "_nav_row_script.html" %}'));
}
{
  // The yStocker header: the tabs carry the same hook, and the script comes
  // straight after them, before anything else on the page has loaded.
  const nav = base.slice(base.indexOf('<nav data-nav="desktop"'), base.indexOf('</nav>', base.indexOf('<nav data-nav="desktop"')) + 6);
  t('the header\'s tabs carry the hook', nav.startsWith('<nav data-nav="desktop" data-nav-row '));
  const after = base.slice(base.indexOf(nav) + nav.length, base.indexOf(nav) + nav.length + 60);
  t('and the script follows them', after.trim().startsWith('{% include "_nav_row_script.html" %}'), after.trim().slice(0, 50));
  t('the current tab is the one the script looks for', nav.includes('aria-current="page"'));
}

console.log(`\n${failures.length ? 'FAILURES: ' + failures.join('; ') : 'all passed'}\n`);
process.exit(failures.length ? 1 : 0);
