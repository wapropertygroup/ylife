/**
 * Checks `_longHistory()` in templates/history.html: the Fundamentals payload
 * cut down to what the deep-research report reads (research.py renders it as
 * "Ten-year history from the company's filings").
 *
 * Its failures are numbers a model would quote with confidence: a percentile
 * of 0 when the current P/E is simply missing reads as "the cheapest it has
 * been in ten years", and billions off by a thousand read as a collapse. So
 * these check the figures, extracted from the template rather than copied (a
 * copy agrees on the day it is written and drifts after).
 *
 * Run: node tests/check_research_long_history.mjs
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const root = path.resolve(import.meta.dirname, '..');
const tpl = readFileSync(path.join(root, 'ystocker/templates/history.html'), 'utf8');

function extract(name) {
  const start = tpl.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`function ${name} not found in history.html`);
  let depth = 0;
  for (let j = tpl.indexOf('{', start); j < tpl.length; j++) {
    if (tpl[j] === '{') depth++;
    else if (tpl[j] === '}' && --depth === 0) return tpl.slice(start, j + 1);
  }
  throw new Error(`unbalanced braces in ${name}`);
}
const ctx = vm.createContext({});
vm.runInContext(extract('_longHistory'), ctx);
const longHistory = fd => JSON.parse(JSON.stringify(vm.runInContext('_longHistory', ctx)(fd)));

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${detail ? '  ' + detail : ''}`);
  if (!cond) failures.push(label);
};
const eq = (label, got, want) => t(label, JSON.stringify(got) === JSON.stringify(want),
  `got ${JSON.stringify(got)} want ${JSON.stringify(want)}`);

// Twelve fiscal years and forty-four quarters, oldest first, as xbrl.assemble emits.
const years = Array.from({ length: 12 }, (_, i) => 2015 + i);
const quarters = Array.from({ length: 44 }, (_, i) => i);
function payload(over = {}) {
  return {
    source: 'sec', basis: { currency: 'USD' }, latest: { filed: '2026-08-26', form: '10-Q' },
    annual: {
      end: years.map(y => `${y}-01-31`), label: years.map(y => `FY${y}`),
      values: {
        revenue: years.map(y => (y - 2000) * 1e10),        // 150B ... 260B
        revenue_yoy: years.map((_, i) => (i ? 0.0734 : null)),
        gross_margin: years.map(() => 0.6512), operating_margin: years.map(() => 0.3),
        net_margin: years.map(() => 0.25), eps: years.map(y => y - 2010),
        fcf: years.map(() => 1.23456e9), roe: years.map(() => 0.181), shares: years.map(() => 2.45e10),
      },
    },
    quarterly: {
      end: quarters.map(i => `q${String(i).padStart(2, '0')}`), label: quarters.map(i => `Q${(i % 4) + 1}`),
      values: {},
    },
    ttm: {
      values: {
        revenue: quarters.map(i => 1e11 + i * 1e9), revenue_yoy: quarters.map(() => 0.05),
        net_income: quarters.map(() => 2.5e10), fcf: quarters.map(() => 2e10),
        gross_margin: quarters.map(() => 0.6), roe: quarters.map(() => 0.2),
      },
    },
    valuation: { pe: quarters.map(i => 10 + i), now: { pe: 30 } },   // 10..53
    ...over,
  };
}

console.log('the decade');
{
  const lh = longHistory(payload());
  eq('ten fiscal years, newest first', lh.annual.map(r => r.fy), years.slice(-10).reverse().map(y => `FY${y}`));
  eq('in billions to two places', lh.annual[0].revenue, 260);
  eq('fractions become percentages to one place', [lh.annual[0].gross_margin, lh.annual[0].revenue_yoy], [65.1, 7.3]);
  eq('small billions keep their cents', lh.annual[0].fcf, 1.23);
  eq('EPS is left as reported', lh.annual[0].eps, 16);
  eq('eight TTM quarters, newest first', lh.ttm.map(r => r.end), ['q43', 'q42', 'q41', 'q40', 'q39', 'q38', 'q37', 'q36']);
  eq('source, currency and the latest filing ride along', [lh.source, lh.currency, lh.latest.form], ['sec', 'USD', '10-Q']);
}

console.log('the P/E range');
{
  const lh = longHistory(payload());
  // The last forty quarters: 14..53, so 30 sits at 17 of 40.
  eq('range over the last forty quarters', [lh.pe_10y.low, lh.pe_10y.high, lh.pe_10y.quarters], [14, 53, 40]);
  eq('median of the sorted range', lh.pe_10y.median, 34);
  eq('where today sits in it', lh.pe_10y.percentile, 43);
  const none = longHistory(payload({ valuation: { pe: quarters.map(i => 10 + i) } }));
  eq('no current P/E is no percentile, not the 0th', [none.pe_10y.now, none.pe_10y.percentile], [null, null]);
  const short = longHistory(payload({ valuation: { pe: [12, 13, 14, null, -3, 15, 16], now: { pe: 14 } } }));
  eq('fewer than eight real quarters is no range at all', short.pe_10y, null);
  const losses = longHistory(payload({ valuation: { pe: quarters.map(i => (i % 2 ? -5 : null)), now: { pe: 20 } } }));
  eq('loss quarters do not count as cheap ones', losses.pe_10y, null);
}

console.log('nothing to report');
eq('an unavailable payload sends nothing', longHistory({ unavailable: 'not_a_company' }), null);
eq('a missing one too', longHistory(null), null);
{
  const thin = longHistory(payload({
    annual: { end: ['2025-12-31'], label: [''], values: { revenue: [null], eps: [null] } },
  }));
  eq('a year with neither revenue nor EPS is dropped', thin.annual, []);
}

if (failures.length) {
  console.log(`\n${failures.length} FAILED`);
  process.exit(1);
}
console.log('\nall passed');
