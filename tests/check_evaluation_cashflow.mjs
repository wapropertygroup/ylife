// Checks the cash-flow columns added to the /evaluation heatmap table.
//
// The page cannot be rendered in this checkout — the dev server will not start
// (the broken Homebrew pyexpat CLAUDE.md records) — so the two things that
// would be obvious on screen are asserted here instead: that the header and the
// body agree on how many columns there are, and that a *derived* forward
// multiple never renders like the measured one beside it.
//
// The formatters are extracted from the template rather than copied: a copy
// would agree the day it was written and drift afterwards.
//
// Run: node tests/check_evaluation_cashflow.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const tpl = readFileSync(join(here, '..', 'ystocker', 'templates', 'index.html'), 'utf8');

let failed = 0;
const check = (name, cond, detail = '') => {
  if (cond) console.log(`  ok   ${name}`);
  else { console.log(`  FAIL ${name}${detail ? ' — ' + detail : ''}`); failed++; }
};

// ── Column parity ───────────────────────────────────────────────────────────
// A header and a body that disagree slide every value one column left from the
// mismatch onward, and the table still renders: P/E under the PEG heading reads
// as a plausible number, not as a fault.
console.log('column parity');
{
  const headBlock = tpl.slice(tpl.indexOf('id="heatmapBody"') - 4000,
                              tpl.indexOf('id="heatmapBody"'));
  const heads = [...headBlock.matchAll(/data-col="([a-z_]+)"/g)].map(m => m[1]);

  const rowStart = tpl.indexOf('<a href="/history/${d.ticker}"');
  const rowEnd = tpl.indexOf('</tr>`;', rowStart);
  const rowBlock = tpl.slice(rowStart, rowEnd);
  // +1 for the ticker cell, whose opening <td> sits above the anchor.
  const cells = (rowBlock.match(/<td /g) || []).length + 1;

  check('header declares the expected columns', heads.length === 12,
        `found ${heads.length}: ${heads.join(',')}`);
  check('body emits one cell per header column', cells === heads.length,
        `${cells} cells vs ${heads.length} headers`);
  check('the new columns are present',
        heads.includes('pfcf') && heads.includes('fwd_pfcf'));
  check('the empty-state colspan matches',
        tpl.includes(`colspan="${heads.length}"`),
        'colspan must equal the column count or the "no results" row misaligns');
}

// ── Formatters ──────────────────────────────────────────────────────────────
function extract(name) {
  const start = tpl.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`${name} not found`);
  let i = tpl.indexOf('{', start), depth = 0;
  for (let j = i; j < tpl.length; j++) {
    if (tpl[j] === '{') depth++;
    else if (tpl[j] === '}' && --depth === 0) return tpl.slice(start, j + 1);
  }
  throw new Error(`unbalanced braces in ${name}`);
}

const asked = [];
const I18n = { t: (k) => { asked.push(k); return null; } };   // exercise fallbacks
const src = ['esc', 'fmtPfcf', 'fmtFwdPfcf'].map(extract).join('\n');
const { esc, fmtPfcf, fmtFwdPfcf } = new Function(
  'I18n', `${src}; return { esc, fmtPfcf, fmtFwdPfcf };`)(I18n);

// Assert on rendered *text*, not on the HTML string.
//
// Earned: `!html.includes('x')` matched the `text-rose-500` class and
// `!html.includes('0')` matched `slate-600`, so two correct outputs failed. The
// same shape as the `onerror=` trap CLAUDE.md records — a naive substring test
// on markup is testing the markup.
const text = (html) => html.replace(/<[^>]*>/g, '');

console.log('fmtPfcf');
{
  check('renders a measured multiple plainly',
        fmtPfcf({ cash: { pfcf: 21.4, fcf_yield: 4.7 } }).includes('21.4x'));
  check('a measured multiple is not marked as an estimate',
        !fmtPfcf({ cash: { pfcf: 21.4 } }).includes('~'));

  // Price over negative cash flow is not a multiple; as one it sorts to the top
  // of a column a reader scans as cheapest.
  const burn = fmtPfcf({ cash: { pfcf: null, fcf_yield: -3.2, reason: 'negative_fcf' } });
  check('a cash burner shows a negative yield, not a multiple',
        text(burn).includes('-3.2%') && !text(burn).includes('x'),
        `rendered "${text(burn)}"`);
  check('the burn is tinted as a warning', /rose-/.test(burn));

  check('nothing measurable renders an em dash',
        fmtPfcf({ cash: {} }).includes('—'));
  check('a row with no cash block does not throw',
        typeof fmtPfcf({}) === 'string');
}

console.log('fmtFwdPfcf');
{
  const est = fmtFwdPfcf({ cash: { forward_pfcf: 18.2, growth: 1.16, forward_source: 'consensus' } });
  check('an estimate carries a tilde', est.includes('~18.2x'));
  // The whole point: a derived number must not sit in the same weight as the
  // measured column to its left.
  check('an estimate is visually marked as derived', /italic/.test(est));
  check('an estimate names its growth factor and source',
        est.includes('1.16') && est.includes('consensus'));

  const refused = fmtFwdPfcf({ cash: { forward_pfcf: null, reason: 'growth_out_of_band' } });
  check('a refusal renders an em dash, not a zero',
        text(refused).trim() === '—', `rendered "${text(refused)}"`);
  check('the refusal reason is carried in the tooltip',
        refused.includes('growth_out_of_band'));

  check('no cash block does not throw', typeof fmtFwdPfcf({}) === 'string');

  // Keys, not just rendered text: every one of these has a raw-identifier
  // fallback, so a wrong key still renders something plausible in English while
  // shipping an untranslated string to the Chinese page.
  asked.length = 0;
  fmtFwdPfcf({ cash: { forward_pfcf: 18.2, growth: 1.16, forward_source: 'consensus' } });
  check('looks up the per-source key', asked.includes('fcf.src_consensus'),
        `asked ${JSON.stringify(asked)}`);
  asked.length = 0;
  fmtFwdPfcf({ cash: { forward_pfcf: null, reason: 'no_growth' } });
  check('looks up the per-reason key', asked.includes('fcf.why_no_growth'),
        `asked ${JSON.stringify(asked)}`);
}

console.log('escaping');
{
  check('quotes are escaped for a title attribute',
        esc('a "b" \'c\' <d> &').includes('&quot;') && esc('<d>').includes('&lt;'));
  check('null is not rendered as the string null', esc(null) === '');
}

console.log(failed ? `\n${failed} check(s) failed` : '\nall checks passed');
process.exit(failed ? 1 : 0);
