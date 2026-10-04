/**
 * Behaviour tests for static/agent_calendar.js — the arithmetic behind the
 * decision calendar on /agents.
 *
 * The failures worth catching all draw a plausible calendar: a month that
 * starts on the wrong weekday shifts every chip a day, a missing rating read
 * as Hold paints a steady view on days the desk said nothing, and a change
 * measured against an unrated run marks an upgrade that never happened.
 *
 * Run: node tests/check_agent_calendar.mjs
 */
import { createRequire } from 'module';
import path from 'path';

const root = path.resolve(import.meta.dirname, '..');
const require = createRequire(import.meta.url);
const AC = require(path.join(root, 'ystocker/static/agent_calendar.js'));

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${detail ? '  ' + detail : ''}`);
  if (!cond) failures.push(label);
};

// Real decisions from the showcase on 2026-10-04, and a few around them.
const RUNS = [
  { id: 'a', ticker: 'MSFT', date: '2026-10-02', level: -1, decision: 'Underweight', status: 'done', created_at: '2026-10-04T10:00:00Z' },
  { id: 'b', ticker: 'MSFT', date: '2026-09-29', level: 0, decision: 'Hold', status: 'done', created_at: '2026-09-29T21:00:00Z' },
  { id: 'c', ticker: 'MSFT', date: '2026-09-29', level: 1, decision: 'Overweight', status: 'done', created_at: '2026-09-30T08:00:00Z' },
  { id: 'd', ticker: 'MSFT', date: '2026-09-15', level: null, decision: '', status: 'error', created_at: '2026-09-15T20:00:00Z' },
  { id: 'e', ticker: 'MSFT', date: '2026-09-01', level: 1, decision: 'Overweight', status: 'done', created_at: '2026-09-01T20:00:00Z' },
  { id: 'f', ticker: 'NFLX', date: '2026-09-15', level: -1, decision: 'Underweight', status: 'done', created_at: '2026-09-15T21:00:00Z' },
  { id: 'g', ticker: 'NFLX', date: '2026-08-28', level: 1, decision: 'Overweight', status: 'done', created_at: '2026-08-28T21:00:00Z' },
];

console.log('levelKey');
t('the five steps and none', ['buy', 'ow', 'hold', 'uw', 'sell'].join() === [2, 1, 0, -1, -2].map(AC.levelKey).join());
t('null is none, not hold', AC.levelKey(null) === 'none' && AC.levelKey(undefined) === 'none');
t('an unknown level is none', AC.levelKey(7) === 'none');

console.log('monthGrid');
const oct = AC.monthGrid(2026, 10);           // 2026-10-01 is a Thursday
t('October 2026 opens on Monday 28 September', oct[0][0].date === '2026-09-28' && oct[0][0].out);
t('the 1st sits on Thursday', oct[0][3].date === '2026-10-01' && !oct[0][3].out && oct[0][3].day === 1);
t('whole weeks, seven days each', oct.every(w => w.length === 7));
t('it ends on a Sunday, the 1st of November', oct[oct.length - 1][6].date === '2026-11-01');
const feb = AC.monthGrid(2027, 2);            // Feb 2027 starts on a Monday, 28 days
t('a month starting Monday has no leading days', feb[0][0].date === '2027-02-01' && !feb[0][0].out);
t('February 2027 is exactly four weeks', feb.length === 4);
const leap = AC.monthGrid(2028, 2);
t('a leap February holds the 29th', leap.flat().some(c => c.date === '2028-02-29' && !c.out));

console.log('shiftMonth');
t('across a year end', AC.shiftMonth('2026-12', 1) === '2027-01' && AC.shiftMonth('2027-01', -1) === '2026-12');

console.log('byDate');
const all = AC.byDate(RUNS, '');
t('two runs on one day, oldest first', all['2026-09-29'].map(r => r.id).join() === 'b,c');
t('the ticker filter', Object.keys(AC.byDate(RUNS, 'NFLX')).sort().join() === '2026-08-28,2026-09-15');
t('a run is placed by the date it analysed, not when it ran', !!all['2026-10-02'] && !all['2026-10-04']);

console.log('months and latestMonth');
t('newest month of all', AC.latestMonth(RUNS, '') === '2026-10');
t('newest month of one ticker', AC.latestMonth(RUNS, 'NFLX') === '2026-09');
t('no runs, no month', AC.latestMonth([], '') === null);
t('the months that hold a run', AC.months(RUNS, 'MSFT').join() === '2026-09,2026-10');

console.log('timeline');
const msft = AC.timeline(RUNS, 'MSFT');
t('in date order', msft.map(r => r.id).join() === 'e,d,b,c,a');
t('the first rated run is "first"', msft[0].change === 'first');
t('an unrated run has no change of its own', msft[1].change === null);
t('a change skips the unrated run: Overweight -> Hold is down', msft[2].change === 'down');
t('the same day, re-run: Hold -> Overweight is up', msft[3].change === 'up');
t('Overweight -> Underweight is down', msft[4].change === 'down');
const flat = AC.timeline([{ ticker: 'X', date: '2026-01-02', level: 0 }, { ticker: 'X', date: '2026-01-05', level: 0 }], 'X');
t('an unchanged rating is "same"', flat[1].change === 'same');

console.log('summary');
const s = AC.summary(RUNS, 'MSFT');
t('counts by step', s.counts.ow === 2 && s.counts.hold === 1 && s.counts.uw === 1 && s.counts.none === 1);
t('rated and total', s.rated === 4 && s.total === 5);
t('the latest rated run', s.latest && s.latest.id === 'a');
const sameDay = AC.summary([RUNS[1], RUNS[2]], 'MSFT');
t('on one day the later run is the latest', sameDay.latest.id === 'c');
t('nothing rated, no latest', AC.summary([RUNS[3]], 'MSFT').latest === null);

if (failures.length) {
  console.log(`\n${failures.length} FAILED`);
  process.exit(1);
}
console.log('\nall ok');
