// Checks the options walls on /history (templates/history.html): the tiles
// _loadOptionsData writes into #optionsWallsGrid, and the labels the price
// chart draws on its wall lines.
//
// A wall is open interest: the strike where the most call (put) contracts are
// still open, summed over the nearest _OPTIONS_MAX_EXPIRATIONS expiries
// (routes.api_options). Reported 2026-10-07 from /history/INTC?lang=zh ("call
// wall and put wall should be 未平仓的期权吧"): the Chinese page called them
// 认购墙/认沽墙, which says nothing about open interest, and 认购 reads as an
// offering subscription, which is what it means on /insiders. The chart drew the
// lines as English "Call Wall $130.00", the copy claimed "all expirations" when
// only the nearest 12 count, and the spread tile read "44.1占现价", its % lost.
// All of it is composed in JavaScript, where I18n.apply() does not reach.
//
// Both pieces are extracted from the template rather than copied (a copy agrees
// on the day it is written and drifts after), and run against the real i18n.js,
// loaded as a classic script so `const I18n` is script-scoped as on the page.
//
// Run: node tests/check_history_walls.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', 'ystocker');
const tpl = readFileSync(join(root, 'templates', 'history.html'), 'utf8');
const i18nSrc = readFileSync(join(root, 'static', 'i18n.js'), 'utf8');
const routesSrc = readFileSync(join(root, 'routes.py'), 'utf8');
const researchSrc = readFileSync(join(root, 'research.py'), 'utf8');

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${cond || !detail ? '' : '  — ' + detail}`);
  if (!cond) failures.push(label);
};
const has = (label, text, needle) => t(label, text.includes(needle), `"${needle}" not in: ${text}`);
const lacks = (label, text, needle) => t(label, !text.includes(needle), `"${needle}" in: ${text}`);

// ── The code under test, cut out of the template ────────────────────────────
function blockEnd(src, open) {
  let depth = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) return j + 1;
  }
  throw new Error('unbalanced braces');
}
const LOAD_AT = tpl.indexOf('async function _loadOptionsData() {');
if (LOAD_AT < 0) throw new Error('_loadOptionsData not found in history.html');
const LOAD = tpl.slice(LOAD_AT, blockEnd(tpl, tpl.indexOf('{', LOAD_AT)));

const CALL_LINE = tpl.indexOf('drawRef(staticData.call_wall');
const AFTER_AT = tpl.lastIndexOf('afterDraw(chart) {', CALL_LINE);
if (CALL_LINE < 0 || AFTER_AT < 0) throw new Error("the price chart's wall lines were not found in history.html");
const AFTER_BODY = tpl.slice(tpl.indexOf('{', AFTER_AT), blockEnd(tpl, tpl.indexOf('{', AFTER_AT)));

// ── A page, in one language ─────────────────────────────────────────────────
function el(id) {
  return { id, style: {}, textContent: '', className: '', innerHTML: '' };
}
function page(lang) {
  const els = Object.fromEntries(['statPutCallRatio', 'statPutCallLabel', 'optionsWallsCard',
    'optionsWallsGrid', 'legendCallWall', 'legendPutWall', 'pcChartSection', 'pcByExpiryChart']
    .map(id => [id, el(id)]));
  const store = new Map();
  const listeners = {};
  let payload = null;
  const ctx = {
    console, JSON, URL, URLSearchParams, encodeURIComponent, Number, Math,
    document: {
      documentElement: { lang: 'en' },
      getElementById: id => els[id] || null,
      addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
      dispatchEvent: () => true,
      querySelectorAll: () => [], querySelector: () => null,
    },
    location: { search: `?lang=${lang}`, pathname: '/history/INTC', hostname: 'trade-agents.com',
                origin: 'https://trade-agents.com', href: `https://trade-agents.com/history/INTC?lang=${lang}` },
    localStorage: { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) },
    Event: class { constructor(type) { this.type = type; } },
    fetch: async () => ({ ok: true, json: async () => payload }),
    TICKER: 'INTC',
    CT: { c: v => v },
    Chart: function Chart() {},
    _staticData: {},
    _chartInstances: {},
    _periodCache: { '1y': { prices: [100, null, 113.3] } },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(i18nSrc, ctx, { filename: 'i18n.js' });
  const load = vm.runInContext(`(${LOAD.replace('async function _loadOptionsData()', 'async function ()')})`,
    ctx, { filename: 'history.html (_loadOptionsData)' });
  const afterDraw = vm.runInContext(`(function (chart, staticData) ${AFTER_BODY})`,
    ctx, { filename: 'history.html (price afterDraw)' });
  return {
    els, ctx,
    I18n: vm.runInContext('I18n', ctx),
    async load(p) { payload = p; await load(); },
    // What the chart writes on its reference lines.
    labels(staticData) {
      const texts = [];
      const noop = () => {};
      const c = { save: noop, restore: noop, beginPath: noop, moveTo: noop, lineTo: noop, stroke: noop,
                  setLineDash: noop, fill: noop, closePath: noop, fillText: s => texts.push(s) };
      afterDraw({ ctx: c, chartArea: { left: 0, right: 100, top: 0, bottom: 100 },
                  scales: { y: { getPixelForValue: () => 50 }, x: { getPixelForValue: () => 0 } },
                  data: { labels: [] } }, { earnings_markers: [], ...staticData });
      return texts;
    },
  };
}

const text = html => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
const balanced = html => (html.match(/<div\b/g) || []).length === (html.match(/<\/div>/g) || []).length;

const INTC = { call_wall: 130, put_wall: 80, call_wall_oi: 123456, put_wall_oi: 98765,
               put_call_ratio: 0.73, pc_by_expiry: [{ exp: '2026-10-16', call_oi: 10, put_oi: 7, ratio: 0.7 }] };

// ── The environment is the browser's ────────────────────────────────────────
{
  const p = page('zh');
  t('i18n.js is script-scoped, as on the page (no window.I18n)',
    vm.runInContext('typeof window.I18n', p.ctx) === 'undefined' && typeof p.I18n === 'object');
  t('?lang=zh opens the page in Chinese', p.I18n.getLang() === 'zh');
}

// ── The tiles, in Chinese ───────────────────────────────────────────────────
{
  const p = page('zh');
  await p.load(INTC);
  const grid = p.els.optionsWallsGrid.innerHTML;
  const shown = text(grid);
  t('the walls card is shown', p.els.optionsWallsCard.style.display === 'block');
  t('the tiles are balanced markup', balanced(grid), grid);
  has('the call wall is named for open interest', shown, '看涨未平仓墙 $130.00');
  has('the put wall is named for open interest', shown, '看跌未平仓墙 $80.00');
  has('the call wall says how many contracts are open there', shown, '未平仓 123,456 张');
  has('the put wall says how many contracts are open there', shown, '未平仓 98,765 张');
  has('the call wall\'s distance from spot', shown, '+14.7% 距现价');
  has('the put wall\'s distance from spot', shown, '-29.4% 距现价');
  has('the spread keeps its percent sign', shown, '占现价 44.1%');
  lacks('no 认购 (it reads as an offering subscription)', shown, '认购');
  lacks('no 认沽', shown, '认沽');
  lacks('no unfilled placeholder', shown, '{v}');
  t('the legend chips come on', p.els.legendCallWall.style.display === 'flex'
    && p.els.legendPutWall.style.display === 'flex');
  t('the price chart is handed both walls',
    p.ctx._staticData.call_wall === 130 && p.ctx._staticData.put_wall === 80);
}

// ── The tiles, in English ───────────────────────────────────────────────────
{
  const p = page('en');
  await p.load(INTC);
  const shown = text(p.els.optionsWallsGrid.innerHTML);
  has('EN: the call wall', shown, 'Call Wall $130.00');
  has('EN: contracts open at the call wall', shown, '123,456 contracts open');
  has('EN: contracts open at the put wall', shown, '98,765 contracts open');
  has('EN: the spread reads as before', shown, '44.1% of spot');
}

// ── A payload without the counts, or without a wall ─────────────────────────
{
  // A copy cached before the counts existed: the tiles stay as they were.
  const p = page('zh');
  const { call_wall_oi, put_wall_oi, ...older } = INTC;
  await p.load(older);
  const shown = text(p.els.optionsWallsGrid.innerHTML);
  has('no counts: the wall is still named', shown, '看涨未平仓墙 $130.00');
  lacks('no counts: no count line', shown, '未平仓 ');
  lacks('no counts: nothing undefined', shown, 'undefined');
  lacks('no counts: no unfilled placeholder', shown, '{v}');
}
{
  // No call contracts open anywhere: the API names no call wall.
  const p = page('zh');
  await p.load({ ...INTC, call_wall: null, call_wall_oi: null });
  const grid = p.els.optionsWallsGrid.innerHTML;
  const shown = text(grid);
  t('no call wall: the tiles are balanced markup', balanced(grid), grid);
  has('no call wall: its tile says so', shown, '看涨未平仓墙 —');
  has('no call wall: the put wall still shows', shown, '看跌未平仓墙 $80.00');
  lacks('no call wall: no spread without two walls', shown, '墙宽');
  t('no call wall: its legend chip stays off', p.els.legendCallWall.style.display !== 'flex');
}

// ── The lines on the price chart ────────────────────────────────────────────
{
  const walls = { target_price: 118.05, call_wall: 130, put_wall: 80 };
  const zh = page('zh').labels(walls);
  t('ZH chart: the lines are labelled like their legend chips',
    JSON.stringify(zh) === JSON.stringify(['目标价 $118.05', '看涨未平仓墙 $130.00', '看跌未平仓墙 $80.00']),
    JSON.stringify(zh));
  const en = page('en').labels(walls);
  t('EN chart: the lines read as before',
    JSON.stringify(en) === JSON.stringify(['Target $118.05', 'Call Wall $130.00', 'Put Wall $80.00']),
    JSON.stringify(en));
  t('a missing wall draws no line', page('zh').labels({ call_wall: null }).length === 0);
}

// ── The copy says what the endpoint does ────────────────────────────────────
{
  const cap = /^_OPTIONS_MAX_EXPIRATIONS = (\d+)/m.exec(routesSrc)?.[1];
  t('found _OPTIONS_MAX_EXPIRATIONS in routes.py', !!cap);
  const zh = page('zh').I18n, en = page('en').I18n;
  for (const key of ['history.walls_desc', 'history.tip_call_wall', 'history.tip_put_wall']) {
    has(`ZH ${key} quotes the expiry cap`, zh.t(key), `最近${cap}个到期日`);
    has(`EN ${key} quotes the expiry cap`, en.t(key), `nearest ${cap} expir`);
    lacks(`EN ${key} no longer claims every expiry`, en.t(key), 'all expirations');
    has(`ZH ${key} names open interest`, zh.t(key), '未平仓');
  }
  has('the research prompt quotes the cap (EN)', researchSrc, `nearest ${cap} expiries`);
  has('the research prompt quotes the cap (ZH)', researchSrc, `最近${cap}个到期日合计`);
  // One vocabulary for calls and puts on this page, the one /markets and /daily use.
  const optionKeys = ['history.put_call_ratio', 'history.pc_title', 'history.pc_desc',
    'history.pc_chart_title', 'history.pc_chart_desc', 'history.walls_desc',
    'history.walls_call_wall', 'history.walls_put_wall', 'history.call_wall_legend',
    'history.put_wall_legend', 'history.tip_call_wall', 'history.tip_put_wall',
    'chart.pc_ratio', 'chart.axis_pc_oi_ratio'];
  const old = optionKeys.filter(k => /认购|认沽/.test(zh.t(k) || ''));
  t('no options string on /history says 认购 or 认沽', old.length === 0, old.join(', '));
}

console.log(failures.length ? `\n${failures.length} FAILED` : '\nall passed');
process.exit(failures.length ? 1 : 0);
