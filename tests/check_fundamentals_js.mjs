/**
 * Behaviour tests for static/fundamentals.js — the arithmetic behind the
 * Fundamentals tab on /history/<ticker>.
 *
 * Every failure here renders as a plausible card: "+56% YoY" against the
 * wrong quarter reads as growth, a P/E of 1,149 left uncapped flattens ten
 * years of 20-40x into the floor, and a 10Y window counted in bars rather
 * than dates shows nine years when a quarter is missing. So these check the
 * numbers a card would print, not just that something prints.
 *
 * Run: node tests/check_fundamentals_js.mjs
 */
import { createRequire } from 'module';
import path from 'path';

const root = path.resolve(import.meta.dirname, '..');
const require = createRequire(import.meta.url);
const F = require(path.join(root, 'ystocker/static/fundamentals.js'));

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${detail ? '  ' + detail : ''}`);
  if (!cond) failures.push(label);
};
const eq = (label, got, want) => t(label, JSON.stringify(got) === JSON.stringify(want),
  `got ${JSON.stringify(got)} want ${JSON.stringify(want)}`);

console.log('format');
eq('money in billions, three figures', F.format(46743000000, 'money', 'USD'), '$46.7B');
eq('money in trillions', F.format(1.2e12, 'money', 'USD'), '$1.20T');
eq('negative money keeps its sign before the symbol', F.format(-8.82e9, 'money', 'USD'), '-$8.82B');
eq('a foreign currency uses its own symbol', F.format(2.89431e12, 'money', 'TWD'), 'NT$2.89T');
eq('an unknown currency falls back to its code', F.format(5e9, 'money', 'SEK'), 'SEK 5.00B');
eq('split-adjusted EPS keeps three decimals below ten cents', F.format(0.0823, 'eps', 'USD'), '$0.082');
eq('EPS keeps two decimals above', F.format(2.46, 'eps', 'USD'), '$2.46');
eq('a loss per share is signed', F.format(-2.16, 'eps', 'USD'), '-$2.16');
eq('a margin is a percentage', F.format(0.7498, 'pct'), '75.0%');
eq('a multiple has one decimal', F.format(24.84, 'x'), '24.8×');
eq('a share count carries no currency', F.format(24.29e9, 'shares'), '24.3B');
eq('a missing value is a dash, never 0', F.format(null, 'money', 'USD'), '—');

console.log('axis');
eq('axis drops trailing zeros', F.axis(5e9, 'money', 'USD'), '$5B');
eq('axis percent is whole', F.axis(0.75, 'pct'), '75%');
eq('axis multiple is whole', F.axis(30, 'x'), '30×');

console.log('tick');
eq('a quarter is month and year', F.tick('2026-07-26', 'Q2 FY2027', 'quarterly'), "Jul '26");
eq('a year is FY and two digits', F.tick('2026-01-25', 'FY2026', 'annual'), 'FY26');

console.log('window');
const quarters = [];
for (let y = 2014; y <= 2026; y++) for (const md of ['-01-25', '-04-26', '-07-26', '-10-25']) quarters.push(y + md);
const last10 = F.window(quarters, '10y');
eq('10Y is forty quarters', last10.length, 40);
eq('10Y ends at the newest quarter', quarters[last10[last10.length - 1]], '2026-10-25');
eq('5Y is twenty quarters', F.window(quarters, '5y').length, 20);
eq('Max is everything', F.window(quarters, 'max').length, quarters.length);
// A quarter missing in the middle must not stretch the window to 41 bars' worth of dates.
const holed = quarters.filter(d => d !== '2020-04-26');
eq('a missing quarter shortens 10Y by one bar, not by a year', F.window(holed, '10y').length, 39);
const trailingEmpty = F.window(quarters, '3y', i => i < quarters.length - 2);
eq('trailing empty periods are not "now"', quarters[trailingEmpty[trailingEmpty.length - 1]], '2026-04-26');
eq('annual 5Y is five years', F.window(['2020-01-26', '2021-01-31', '2022-01-30', '2023-01-29', '2024-01-28', '2025-01-26', '2026-01-25'], '5y').length, 5);

console.log('yearAgo / change');
const ends = ['2024-04-28', '2024-07-28', '2024-10-27', '2025-01-26', '2025-04-27'];
eq('a year ago is the same quarter last year', F.yearAgo(ends, 4, 'quarterly'), 0);
eq('nothing a year back is null', F.yearAgo(ends, 2, 'quarterly'), null);
// The quarter a year back is missing: four bars back is now the wrong quarter.
const gap = ['2024-01-28', '2024-07-28', '2024-10-27', '2025-01-26', '2025-04-27'];
eq('a missing quarter is not replaced by its neighbour', F.yearAgo(gap, 4, 'quarterly'), null);
const g = F.change([26.044, 30.040, 35.082, 39.331, 44.062], ends, 4, 'quarterly', 'money');
t('growth is relative to a year ago', Math.abs(g.rel - (44.062 / 26.044 - 1)) < 1e-9, JSON.stringify(g));
eq('growth from a loss is not a percentage', F.change([-1, 0, 0, 0, 2], ends, 4, 'quarterly', 'money'), null);
const pp = F.change([0.646, 0.70, 0.74, 0.76, 0.605], ends, 4, 'quarterly', 'pct');
t('a margin moves in percentage points', Math.abs(pp.pp - (60.5 - 64.6)) < 1e-9, JSON.stringify(pp));
eq('annual compares with the year before', F.yearAgo(['2024-01-28', '2025-01-26', '2026-01-25'], 2, 'annual'), 1);

console.log('capRatios / median');
eq('median of an even count', F.median([10, 20, 30, 40]), 25);
eq('median ignores gaps', F.median([null, 10, undefined, 30]), 20);
const c = F.capRatios([20, 25, 30, 1149, 35]);
eq('one near-zero-earnings quarter is capped at 4x the median', c.cap, 120);
eq('only that bar is marked capped', c.clipped, [false, false, false, true, false]);
eq('nothing to cap is no cap', F.capRatios([20, 25, 30]).cap, null);
// With the -50 counted the median would be 25 and the cap 100; without it, 27.5 and 110.
eq('negative multiples are not part of the median', F.capRatios([-50, 20, 25, 30, 400]).cap, 110);

console.log('latest');
eq('latest skips trailing gaps', F.latest([1, 2, null], [0, 1, 2]), 1);
eq('latest within the window only', F.latest([1, 2, 3], [0, 1]), 1);
eq('nothing is null', F.latest([null, null], [0, 1]), null);

// Comparing companies (asked 2026-10-06). Fiscal years end in different
// months, so periods are placed by their midpoint on calendar quarters. Placed
// by end date, NVIDIA's quarter to late July would sit a quarter after Intel's
// to late June, when both cover the same spring.
console.log('calendarKey / calendarLabel');
eq("Intel's quarter to 27 Jun is calendar Q2", F.calendarKey('2026-03-29', '2026-06-27', 'quarterly'), '2026Q2');
eq("NVIDIA's quarter to 26 Jul is calendar Q2 too", F.calendarKey('2026-04-27', '2026-07-26', 'quarterly'), '2026Q2');
eq('by end date it would have been Q3', F.calendarKey('2026-07-01', '2026-07-26', 'quarterly'), '2026Q3');
eq("NVIDIA's FY2026 (to Jan 2026) is calendar 2025", F.calendarKey('2025-01-27', '2026-01-25', 'annual'), '2025');
eq("Intel's FY2025 is calendar 2025", F.calendarKey('2024-12-29', '2025-12-27', 'annual'), '2025');
eq('a missing start is taken as a quarter before the end', F.calendarKey('', '2026-06-27', 'quarterly'), '2026Q2');
eq('a quarter label', F.calendarLabel('2026Q2'), "Q2 '26");
eq('a year label', F.calendarLabel('2025'), '2025');

console.log('calendarWindow');
const intelKeys = ['2024Q3', '2024Q4', '2025Q1', '2025Q2', '2025Q3', '2025Q4', '2026Q1', '2026Q2'];
const nvdaKeys = ['2025Q2', '2025Q3', '2025Q4', '2026Q1', '2026Q2', '2026Q3'];
const win = F.calendarWindow([intelKeys, nvdaKeys], 'max', false);
eq('the window runs from the oldest to the newest any company has', [win[0], win[win.length - 1]], ['2024Q3', '2026Q3']);
eq('every quarter between is there', win.length, 9);
eq('3Y is twelve quarters back from the newest', F.calendarWindow([intelKeys, ['2015Q1', '2026Q3']], '3y', false),
   ['2023Q4', '2024Q1', '2024Q2', '2024Q3', '2024Q4', '2025Q1', '2025Q2', '2025Q3', '2025Q4', '2026Q1', '2026Q2', '2026Q3']);
eq('a quarter nobody filed for stays, as a gap', F.calendarWindow([['2025Q1', '2025Q3']], 'max', false),
   ['2025Q1', '2025Q2', '2025Q3']);
eq('annual windows count years', F.calendarWindow([['2019', '2025'], ['2024']], '3y', true), ['2023', '2024', '2025']);
eq('no periods, no window', F.calendarWindow([[], []], '10y', false), []);

console.log('alignTo');
eq('each key finds its period, a gap finds nothing',
   F.alignTo(['2025Q1', '2025Q2', '2025Q3'], ['2025Q1', '2025Q3'], [10, 30], ['2025-03-31', '2025-09-30']),
   [0, null, 1]);
eq('two periods in one quarter: the one with a figure wins',
   F.alignTo(['2025Q2'], ['2025Q2', '2025Q2'], [12, null], ['2025-05-31', '2025-06-28']), [0]);
eq('both with figures: the later one wins',
   F.alignTo(['2025Q2'], ['2025Q2', '2025Q2'], [12, 13], ['2025-05-31', '2025-06-28']), [1]);
eq('a key outside the company is null', F.alignTo(['2030Q1'], ['2025Q1'], [1], ['2025-03-31']), [null]);

// The tab's poll, read out of history.html rather than copied. Response.ok is
// true for a 202 as well, and the committed poll took the 202 "still reading"
// answer for the payload: it wiped the status line and then failed on the
// missing periods, so a cold ticker showed an empty tab (found 2026-10-06).
console.log('fetchFundamentals');
{
  const { readFileSync } = await import('fs');
  const tpl = readFileSync(path.join(root, 'ystocker/templates/history.html'), 'utf8');
  const start = tpl.indexOf('async function fetchFundamentals(');
  t('the poll is in the template', start >= 0);
  let i = tpl.indexOf('{', start), depth = 0, end = -1;
  for (let j = i; j < tpl.length; j++) {
    if (tpl[j] === '{') depth++;
    else if (tpl[j] === '}' && --depth === 0) { end = j + 1; break; }
  }
  const src = tpl.slice(start, end);
  const poll = (answers) => {
    const asked = [], states = [];
    const fakeFetch = async (url) => {
      asked.push(url);
      const a = answers.length > 1 ? answers.shift() : answers[0];
      if (a === 'network') throw new Error('offline');
      return { status: a.status, ok: a.status >= 200 && a.status < 300, json: async () => a.body };
    };
    const run = new Function('POLL_MS', 'fetch', 'setTimeout', src + '; return fetchFundamentals;')(
      [0, 0, 0], fakeFetch, (fn) => fn());
    return { asked, states, go: (retry) => run('INTC', retry, (s) => states.push(s)) };
  };
  const body = { quarterly: { end: ['2026-06-27'] } };
  let p = poll([{ status: 202, body: { status: 'warming', queued: false } }, { status: 200, body }]);
  let got = await p.go(false);
  eq('a 202 is waited out, not taken for the payload', got, { payload: body });
  eq('and the wait is reported', p.states, ['building']);
  p = poll([{ status: 202, body: { status: 'warming', queued: true } }, { status: 200, body }]);
  await p.go(false);
  eq('no free slot is "queued"', p.states, ['queued']);
  eq('a 400 is a ticker the filings cannot be looked up by',
     await poll([{ status: 400, body: {} }]).go(false), { state: 'invalid' });
  eq("a spent day's allowance says so",
     await poll([{ status: 503, body: { reason: 'daily_cap' } }]).go(false), { state: 'capped' });
  eq('a failure is failed', await poll([{ status: 503, body: { reason: 'x' } }]).go(false), { state: 'failed' });
  eq('so is no network', await poll(['network']).go(false), { state: 'failed' });
  p = poll([{ status: 202, body: { status: 'warming' } }]);
  eq('a poll that never ends ends on timeout', await p.go(false), { state: 'timeout' });
  eq('after a bounded number of asks', p.asked.length, 4);
  p = poll([{ status: 200, body }]);
  await p.go(true);
  t('a retry asks for a rebuild once', p.asked[0].endsWith('?retry=1'), p.asked[0]);
}

if (failures.length) {
  console.log(`\n${failures.length} FAILED`);
  process.exit(1);
}
console.log('\nall passed');
