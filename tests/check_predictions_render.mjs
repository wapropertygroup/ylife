// Checks static/predictions.js, the renderer behind /predictions and the
// "three ways" card on /fedwatch, without a browser.
//
// Loads the real i18n.js and the real predictions.js as classic scripts in one
// vm context -- where `const I18n` and `const PM` are script-scoped bindings,
// exactly as a page gets them, not properties of window (the trap
// check_carry_prefs.mjs records: a harness that hand-set window.I18n passed
// while the page shipped broken).
//
// What it pins:
//   * the formatting a reader compares across rows (whole percents on cards,
//     one decimal in the side-by-side tables, signed points);
//   * which outcomes a collapsed card shows: the rungs nearest 50% on a
//     ladder, the likeliest buckets of a bucketed field in strike order --
//     the first live render showed "Anthropic IPO Closing Market Cap" as four
//     buckets under 1% with its 94% one hidden behind "Show all";
//   * filtering and sorting the board;
//   * every string from a venue or a model escaped before it reaches
//     innerHTML, asserted on live markup only (escaped text is stripped first,
//     so `&lt;img onerror&gt;` cannot satisfy or fail a check by itself);
//   * the AI panel's states, each ending somewhere a reader can act;
//   * both languages, through the real I18n.setLang.
//
// Run: node tests/check_predictions_render.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', 'ystocker');
const i18nSrc = readFileSync(join(root, 'static', 'i18n.js'), 'utf8');
const pmSrc = readFileSync(join(root, 'static', 'predictions.js'), 'utf8');

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${cond || !detail ? '' : '  — ' + detail}`);
  if (!cond) failures.push(label);
};

function page() {
  const store = new Map();
  const html = { lang: 'en', classList: { contains: () => true, add() {}, remove() {}, toggle() {} } };
  const document = {
    documentElement: html,
    addEventListener() {}, dispatchEvent: () => true,
    querySelectorAll: () => [], querySelector: () => null, getElementById: () => null,
  };
  const ctx = {
    document, URL, URLSearchParams, console, Date, Math, JSON, isNaN, encodeURIComponent,
    location: { search: '', pathname: '/predictions', hostname: 'stock.li-family.us', origin: 'https://stock.li-family.us',
                href: 'https://stock.li-family.us/predictions' },
    Event: class { constructor(type) { this.type = type; } },
    history: { replaceState() {} },
    localStorage: { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) },
    setTimeout, setInterval, clearTimeout,
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(i18nSrc, ctx, { filename: 'i18n.js' });
  vm.runInContext(pmSrc, ctx, { filename: 'predictions.js' });
  return { ctx, PM: vm.runInContext('PM', ctx), I18n: vm.runInContext('I18n', ctx) };
}

/** Live markup only: escaped text (&lt;…&gt;) is removed before searching. */
const live = html => html.replace(/&lt;[\s\S]*?&gt;/g, '');

const { ctx, PM, I18n } = page();

console.log('bindings');
t('PM is reachable by name, as the page reaches it', typeof PM === 'object' && typeof PM.boot === 'function');
t('PM is not a property of window', ctx.window.PM === undefined);
t('i18n.js loaded for real', typeof I18n.t === 'function' && I18n.t('pm.title') === 'Prediction Markets');

console.log('formatting');
t('a card rounds to whole percents', PM.pct(0.825) === '83%' && PM.pct(0.175) === '18%', PM.pct(0.825));
t('the ends stay honest', PM.pct(0.004) === '<1%' && PM.pct(0.996) === '>99%' && PM.pct(0) === '0%');
t('tables carry one decimal', PM.pct1(0.8156) === '81.6%' && PM.pct1(null) === '—');
t('changes are signed points', PM.pp(0.04) === '+4.0pp' && PM.pp(-0.035) === '−3.5pp' && PM.pp(0) === '±0.0pp');
t('money', PM.money(24391674) === '$24.4M' && PM.money(181358) === '$181k' && PM.money(950) === '$950'
  && PM.money(2.1e9) === '$2.1B' && PM.money(null) === '—');
const now = Date.parse('2026-10-03T22:00:00Z');
t('closes in days', PM.closesIn('2026-10-29T03:59:00Z', now) === 'closes in 25 days', PM.closesIn('2026-10-29T03:59:00Z', now));
t('closes in hours', PM.closesIn('2026-10-04T12:00:00Z', now) === 'closes in 14h');
t('closed', PM.closesIn('2026-10-01T00:00:00Z', now) === 'closed');

console.log('which outcomes a collapsed card shows');
const ladder = { kind: 'ladder', outcomes: [0.995, 0.99, 0.975, 0.905, 0.595, 0.175, 0.035, 0.02]
  .map((p, i) => ({ label: `Above ${i / 10}%`, p })) };
t('a ladder shows the rungs around 50%',
  PM.visibleOutcomes(ladder).map(o => o.p).join() === '0.975,0.905,0.595,0.175',
  PM.visibleOutcomes(ladder).map(o => o.p).join());
const buckets = { kind: 'exclusive', numeric: true, outcomes: [
  { label: '<100B', p: 0.004 }, { label: '100–200B', p: 0.003 }, { label: '200–300B', p: 0.002 },
  { label: '300–400B', p: 0.001 }, { label: '400–600B', p: 0.01 }, { label: '600B+', p: 0.94 },
  { label: 'No IPO by December 31, 2027', p: 0.03 }] };
t('a bucketed field shows its likeliest, in strike order',
  PM.visibleOutcomes(buckets).map(o => o.label).join('|') === '<100B|400–600B|600B+|No IPO by December 31, 2027',
  PM.visibleOutcomes(buckets).map(o => o.label).join('|'));
const names = { kind: 'exclusive', numeric: false, outcomes: ['NVIDIA', 'Apple', 'Alphabet', 'Microsoft', 'Broadcom']
  .map((label, i) => ({ label, p: 0.9 / (i + 1) })) };
t('named outcomes keep their order', PM.visibleOutcomes(names).map(o => o.label).join() === 'NVIDIA,Apple,Alphabet,Microsoft');
t('expanded shows everything', PM.visibleOutcomes(buckets, true).length === 7);

console.log('the board');
const events = [
  { key: 'a', title: 'Fed Decision in October?', topic: 'rates', platform: 'polymarket', volume_24h: 159000,
    closes: '2026-10-29T03:59:00Z', outcomes: [{ label: 'No change', p: 0.825, d1: null }] },
  { key: 'b', title: 'CPI in September', topic: 'inflation', platform: 'kalshi', volume_24h: 11000,
    closes: '2026-10-14T12:25:00Z', outcomes: [{ label: 'Above 0.5%', p: 0.595, d1: 0.01 }] },
  { key: 'c', title: 'What price will Bitcoin hit in October?', topic: 'crypto', platform: 'polymarket',
    volume_24h: 262000, closes: '2026-11-01T04:00:00Z', outcomes: [{ label: '↑ 100,000', p: 0.095, d1: -0.08 }] },
];
t('busiest first by default', PM.filterSort(events, {}).map(e => e.key).join() === 'c,a,b');
t('closing soonest', PM.filterSort(events, { sort: 'closing' }).map(e => e.key).join() === 'b,a,c');
t('biggest move', PM.filterSort(events, { sort: 'moved' }).map(e => e.key).join() === 'c,b,a');
t('by topic', PM.filterSort(events, { topic: 'inflation' }).map(e => e.key).join() === 'b');
t('by venue', PM.filterSort(events, { platform: 'polymarket' }).map(e => e.key).join() === 'c,a');
t('the filter reads outcomes too', PM.filterSort(events, { q: 'no change' }).map(e => e.key).join() === 'a');
t('topic counts', JSON.stringify(PM.topicCounts(events)) === '{"rates":1,"inflation":1,"crypto":1}');

console.log('escaping');
const evil = '<img src=x onerror=alert(1)>';
const card = PM.renderCard({ key: 'k"><b>', title: evil, subtitle: evil, url: 'https://polymarket.com/event/x',
  platform: 'polymarket', topic: 'rates', kind: 'binary', closes: '2026-11-01T00:00:00Z', volume: 1, volume_24h: 1,
  liquidity: 1, outcomes: [{ label: evil, p: 0.5, d1: 0.1 }] }, { now });
t('a venue title, subtitle and label cannot inject markup', !/<img/i.test(live(card)) && !/onerror/i.test(live(card)));
t('a key cannot break out of its attribute', !live(card).includes('"><b>'));
const movers = PM.renderMovers([{ key: 'm', title: evil, label: evil, url: 'https://kalshi.com/x', platform: 'kalshi', p: 0.3, d1: 0.05 }]);
t('movers escape too', !/<img/i.test(live(movers)));
const read = { key: 'k', model: 'gemini-2.5-flash', created_ts: now / 1000 - 3600, confidence: 'medium',
  evidence_date: '2026-10-02', kind: 'exclusive', n_outcomes: 2,
  outcomes: [{ label: evil, market_p: 0.825, ai_p: 0.70, gap_pp: -12.5 }, { label: 'Hike', market_p: 0.175, ai_p: 0.30, gap_pp: 12.5 }],
  en: { summary: '<script>alert(1)</script>', drivers: [evil], watch: [evil] },
  zh: { summary: '<script>alert(2)</script>', drivers: [], watch: [] },
  sources: [{ title: evil, uri: 'https://www.reuters.com/"onmouseover="x' }], queries: [evil] };
const done = PM.renderRead({ status: 'done', fresh: true, read, rules: evil }, {}, { enabled: true, signedIn: true, canAsk: true, now });
t('a model\'s words cannot inject markup', !/<script/i.test(live(done)) && !/<img/i.test(live(done)));
t('a source link stays inside its href', !/onmouseover="x/.test(live(done)) || live(done).includes('&quot;onmouseover'));
t('a fresh read offers no second ask', !done.includes('data-pm-ask'));
t('the gap is signed, in points', done.includes('−12.5pp') && done.includes('+12.5pp'));

console.log('the AI panel ends somewhere a reader can act');
const ev = {};
const signedOut = PM.renderRead({ status: 'none', rules: 'r' }, ev, { enabled: true, signedIn: false });
t('signed out: a sign-in link that comes back here', signedOut.includes('/login?next=%2Fpredictions'));
const signedIn = PM.renderRead({ status: 'none' }, ev, { enabled: true, signedIn: true, canAsk: true });
t('signed in: an ask button', signedIn.includes('data-pm-ask'));
const off = PM.renderRead({ status: 'none' }, ev, { enabled: false, signedIn: true });
t('switched off: says so, offers nothing', off.includes('switched off') && !off.includes('data-pm-ask'));
const running = PM.renderRead({ status: 'running' }, ev, { enabled: true, signedIn: true });
t('running: the thinking line', running.includes('pm-thinking'));
const slow = PM.renderRead({ status: 'slow' }, ev, { enabled: true, signedIn: true });
t('slow: a check-again button, not a spinner for ever', slow.includes('data-pm-check') && !slow.includes('pm-thinking'));
const parseErr = PM.renderRead({ status: 'error', error: 'sum' }, ev, { enabled: true, signedIn: true, canAsk: true });
t('an unreadable answer: says so and offers a retry', parseErr.includes('could not be read') && parseErr.includes('data-pm-ask'));
const quota = PM.renderRead({ status: 'refused', reason: 'user', quota: { limit: 5 } }, ev, { enabled: true, signedIn: true, canAsk: true });
t('past the allowance: names the limit, no retry', quota.includes('(5)') && !quota.includes('data-pm-ask'));
const stale = PM.renderRead({ status: 'done', fresh: false, read: { ...read, created_ts: now / 1000 - 20 * 3600 } }, ev,
  { enabled: true, signedIn: true, canAsk: true, now });
t('an old read: dated, with an ask-again', stale.includes('may have moved') && stale.includes('data-pm-ask'));
t('loading: a spinner only', PM.renderRead({ status: 'loading' }, ev, {}).includes('pm-spin'));

console.log('the Fed, three ways');
const fed = [{ date: '2026-12-09', label: 'Dec 2026', spread_pp: 12.8, spread_bucket: '1', agree: true, sources: [
  { source: 'futures', buckets: { '-2': 0, '-1': 0, '0': 0.184, '1': 0.816, '2': 0 }, expected_bp: 20.4 },
  { source: 'polymarket', buckets: { '-2': 0.003, '-1': 0.017, '0': 0.233, '1': 0.729, '2': 0.017 }, expected_bp: 18.5,
    overround: 1.0085, url: 'https://polymarket.com/event/fed-decision-in-december' },
  { source: 'kalshi', buckets: { '-2': 0.005, '-1': 0.015, '0': 0.268, '1': 0.688, '2': 0.024 }, expected_bp: 17.8,
    overround: 1.025, url: 'https://kalshi.com/markets/kxfeddecision/kxfeddecision-26dec' }] }];
const fedHtml = PM.renderFed(fed);
t('every source gets a row', (fedHtml.match(/<tr/g) || []).length === 4);
t('the futures name no tails', (fedHtml.match(/pm-zero/g) || []).length === 3);
t('the widest gap\'s column is marked', fedHtml.includes('pm-gap-col'));
t('the overround is on hover', fedHtml.includes('Quoted prices sum to 102.5%'));
t('the expected move is signed', fedHtml.includes('+20.4bp') && fedHtml.includes('+17.8bp'));
t('the verdict names the likeliest', fedHtml.includes('<b>Hike 25</b>') && fedHtml.includes('widest gap 12.8pp on Hike 25'));
t('nothing to compare: says so', PM.renderFed([]).includes('No upcoming meeting'));

console.log('the track record');
t('empty: says what will appear', PM.renderLedger({ summary: { recorded: 0 }, rows: [] }).includes('No AI reads yet'));
const ledger = PM.renderLedger({ summary: { recorded: 2, settled: 1, brier_ai: 0.09, brier_market: 0.0306, ai_better: 0 },
  rows: [{ title: 'Fed Decision in October?', platform: 'polymarket', url: 'https://polymarket.com/event/x',
           created_at: '2026-10-03T22:00:00Z', status: 'settled', brier_ai: 0.09, brier_market: 0.0306,
           outcomes: [{ label: 'No change', ai_p: 0.7, market_p: 0.825, result: 1 }] }] });
t('scores to three places', ledger.includes('0.090') && ledger.includes('0.031'));
t('the worse score is marked as such', ledger.includes('pm-down'));
t('unsettled scores read as unknown, never 0', PM.renderLedger({ summary: { recorded: 1, settled: 0 }, rows: [] }).includes('—'));

console.log('both languages');
I18n.setLang('zh');
const fedZh = PM.renderFed(fed);
t('the Fed table speaks Chinese', fedZh.includes('维持') && fedZh.includes('加息25') && fedZh.includes('预期变动'));
t('so does the verdict', fedZh.includes('各来源最可能的结果一致') && fedZh.includes('个百分点'));
t('and the closing time', PM.closesIn('2026-10-29T03:59:00Z', now) === '25 天后截止');
const doneZh = PM.renderRead({ status: 'done', fresh: true, read }, {}, { enabled: true, signedIn: true, now });
t('a read shows its Chinese summary on a Chinese page', doneZh.includes('alert(2)') && !doneZh.includes('alert(1)</script>'));
I18n.setLang('en');
t('and back', PM.closesIn('2026-10-29T03:59:00Z', now) === 'closes in 25 days');

console.log(failures.length ? `\n${failures.length} FAILED` : '\nall ok');
process.exit(failures.length ? 1 : 0);
