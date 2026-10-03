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

if (failures.length) {
  console.log(`\n${failures.length} FAILED`);
  process.exit(1);
}
console.log('\nall passed');
