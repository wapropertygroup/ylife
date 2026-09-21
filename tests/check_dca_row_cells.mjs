// Checks the row-rendering helpers in templates/dca_index.html.
//
// These decide what a reader is told when a row cannot be scored, or when an
// overlay multiplier sits at 1.00x. Both were previously communicated only
// through `title` attributes, which do not exist on a touch device — so the
// logic that replaced them is worth testing rather than eyeballing.
//
// The functions are extracted from the template itself rather than copied here:
// a copy would agree on the day it was written and drift afterwards, and this
// file exists precisely to catch the case where the page says something
// different from what it should.
//
// Run: node tests/check_dca_row_cells.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const tpl = readFileSync(join(here, '..', 'ystocker', 'templates', 'dca_index.html'), 'utf8');

function extract(name) {
  const start = tpl.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`function ${name} not found in dca_index.html`);
  // Brace-match from the first "{" after the signature.
  let i = tpl.indexOf('{', start), depth = 0;
  for (let j = i; j < tpl.length; j++) {
    if (tpl[j] === '{') depth++;
    else if (tpl[j] === '}' && --depth === 0) return tpl.slice(start, j + 1);
  }
  throw new Error(`unbalanced braces in ${name}`);
}

// Stubs for what the page provides.
//
// `I18n.t` resolves from a table rather than returning null. A null stub looks
// simpler and is worse: it sends every lookup down the English fallback branch,
// so the *keys* are never exercised and a typo'd or collapsed key — the exact
// bug that would ship an untranslated or wrong string to the Chinese page —
// passes the check. Verified: with a null stub, rewriting `dcx.why_ + reason`
// to a single constant key was not caught.
const STRINGS = {
  'dcx.why_young': 'history too short',
  'dcx.why_no_peer_group': 'no peer group',
  'dcx.why_factors': 'too few factors',
  'dcx.filings': 'filings',
  'dcx.years_unit': 'y',
  'dcx.dropped': 'dropped',
  'dcx.already_scored': 'scored',
  'dcx.not_scorable': 'A fund publishes no statements, so it has no valuation history to score.',
};
const asked = [];
const I18n = { t: (k) => { asked.push(k); return STRINGS[k] ?? null; } };
const esc = (s) => String(s ?? '');
const label = (prefix, key) => `${prefix}${key ?? 'unknown'}`;

const src = ['overlayCell', 'noScoreTag', 'coverageCell'].map(extract).join('\n');
const { overlayCell, noScoreTag, coverageCell } = new Function(
  'I18n', 'esc', 'label',
  `${src}; return { overlayCell, noScoreTag, coverageCell };`
)(I18n, esc, label);

let failed = 0;
const check = (name, cond, detail = '') => {
  if (cond) { console.log(`  ok   ${name}`); }
  else { console.log(`  FAIL ${name}${detail ? ' — ' + detail : ''}`); failed++; }
};

console.log('overlayCell');
{
  // The number must always be present: a reader checking
  // amount = base x M_val x M_earn x M_port needs the factor that was applied,
  // even when it was applied because nothing could be measured.
  const unknown = overlayCell(1, 'unknown', 'dca.earn_');
  check('keeps the applied multiplier when nothing was measured', unknown.includes('1.00×'));
  check('names the reason inline, not only in a title attribute',
        unknown.includes('dca.earn_unknown') && !unknown.includes('title='));

  const measured = overlayCell(1.08, 'raised', 'dca.earn_');
  check('shows a measured band', measured.includes('1.08×') && measured.includes('dca.earn_raised'));
  check('measured value is not dimmed', !measured.includes('text-slate-400'));

  // "analysts did not move" and "there is no data" are different statements and
  // must not render identically — this is the whole point of the change.
  const neutralMeasured = overlayCell(1, 'stable', 'dca.earn_');
  check('measured-neutral differs from unmeasured',
        neutralMeasured !== unknown);
  check('measured-neutral is not italicised as absent',
        !neutralMeasured.includes('italic') && unknown.includes('italic'));

  // Sixty identical "not signed in" labels is noise; the banner says it once.
  const suppressed = overlayCell(1, 'unknown', 'dca.port_', true);
  check('suppressed variant drops the repeated sub-label',
        !suppressed.includes('dca.port_unknown'));
  check('suppressed variant still shows the multiplier', suppressed.includes('1.00×'));
}

console.log('noScoreTag');
{
  check('a scored row gets no tag', noScoreTag({ no_score_reason: null }) === '');
  const young = noScoreTag({ no_score_reason: 'young' });
  const factors = noScoreTag({ no_score_reason: 'factors' });
  check('"not yet" and "cannot" produce different text', young !== factors);
  check('young says history is short', /history too short/.test(young));
  check('factors says factors', /too few factors/.test(factors));
  check('no peer group is its own reason',
        /no peer group/.test(noScoreTag({ no_score_reason: 'no_peer_group' })));
  // The server owns this decision; the page must not re-derive it from `years`,
  // or the row and the detail page would eventually disagree.
  check('an unrecognised reason renders nothing rather than guessing',
        noScoreTag({ no_score_reason: 'something_new' }).includes('></div>'));

  // Assert the *key*, not just the rendered text. Every one of these strings has
  // an English fallback in the template, so a wrong or collapsed i18n key still
  // renders correct-looking English — and ships that English to the Chinese
  // page, which is the bug. Only checking which key was requested catches it.
  asked.length = 0;
  noScoreTag({ no_score_reason: 'young' });
  check('looks up the per-reason key', asked.includes('dcx.why_young'),
        `asked for ${JSON.stringify(asked)}`);
  asked.length = 0;
  noScoreTag({ no_score_reason: 'factors' });
  check('a different reason looks up a different key', asked.includes('dcx.why_factors'),
        `asked for ${JSON.stringify(asked)}`);

  asked.length = 0;
  coverageCell({ years: 3.5, vintages: 9, dropped: ['pe'] });
  check('the year unit is translated, not a hardcoded "y"',
        asked.includes('dcx.years_unit'), `asked for ${JSON.stringify(asked)}`);
  check('the dropped label is translated',
        asked.includes('dcx.dropped'), `asked for ${JSON.stringify(asked)}`);
}

console.log('coverageCell');
{
  check('no history renders an em dash', coverageCell({ years: null }, 60) === '—');
  // `need` is passed in rather than read from a module global — the checker
  // caught exactly that, which is also why the function is easier to reason
  // about now.
  const clean = coverageCell({ years: 3.5, vintages: 9, dropped: [] }, 60);
  check('clean row shows length and vintages', clean.includes('3.5y') && clean.includes('9 filings'));
  check('clean row adds no dropped line', !clean.includes('dropped'));

  const lossy = coverageCell(
    { years: 3.5, vintages: 9, dropped: ['pe', 'peg', 'peer'],
      dropped_obs: { pe: 57, peg: 6, peer: 0 } }, 60);
  check('dropped factors are named inline, not hidden in a tooltip',
        lossy.includes('dca.f_pe') && lossy.includes('dca.f_peg') && lossy.includes('dca.f_peer'));
  check('dropped line is not a bare count', !/>−3</.test(lossy));

  // A factor three weeks from qualifying and one forty weeks short render
  // identically without this, and across the universe both shapes are common.
  check('a short factor shows how short', lossy.includes('57/60'));
  check('a factor with no series at all shows no count',
        !lossy.includes('0/60'), 'zero of sixty implies it is merely short');
}

/* ── The fund filter ──────────────────────────────────────────────────────
   A fund publishes no statements, so following one from this page costs six
   Yahoo reads and about a minute to arrive at "no published statements". SPY
   and XTL were both being suggested. Three entry points reach /dca/<t> — the
   suggestion list, the Enter key and the Score button — and the last two take
   whatever is in the box, so filtering the list alone leaves two ways through.
   That is what these checks are for. */
console.log('\nfund filter');
{
  const dom = {};
  const el = () => {
    const node = { innerHTML: '', style: {}, _c: new Set(),
      classList: { add: c => node._c.add(c), remove: c => node._c.delete(c),
                   contains: c => node._c.has(c),
                   toggle: (c, f) => { const on = f === undefined ? !node._c.has(c) : !!f;
                                       if (on) node._c.add(c); else node._c.delete(c); return on; } } };
    return node;
  };
  for (const id of ['searchList', 'recentWrap', 'recentList']) dom[id] = el();

  let navigated = null;
  const globals = {
    document: { getElementById: id => dom[id], querySelectorAll: () => [] },
    I18n: { t: k => STRINGS[k] ?? null },
    esc: s => String(s ?? ''),
    CT: { c: v => v },
    UNSCORABLE: new Set(['SPY', 'XTL', 'IGV']),
    // A module-level const in the template, so `extract` (which only takes
    // functions) does not bring it along. Without it `getRecent` throws on an
    // undefined name, its own try/catch swallows that, and every recent-chip
    // assertion passes against an empty list — which is how the paired
    // "companies stay" check earns its keep.
    RECENT_KEY: 'ystocker_recent_tickers',
    window: { location: { set href(v) { navigated = v; } } },
    localStorage: { _v: '["SPY","MSFT"]', getItem: () => globals.localStorage._v,
                    setItem: (_k, v) => { globals.localStorage._v = v; } },
    _rows: [{ ticker: 'MSFT' }],
  };
  const names = ['cleanSymbol', 'langSuffix', 'dcaUrl', 'getRecent', 'addRecent',
                 'renderRecent', 'unscorableNote', 'goTo', 'renderSuggestions'];
  const fnSrc = names.map(extract).join('\n');
  const fns = new Function(...Object.keys(globals),
    `let _sugg = [], _active = -1; ${fnSrc}; return { ${names.join(', ')}, peek: () => _sugg };`
  )(...Object.values(globals));

  fns.renderSuggestions('SP', [
    { ticker: 'SPOT', name: 'Spotify', group: 'Streaming / Media' },
    { ticker: 'SPY',  name: 'SPDR S&P 500 ETF', group: 'US Broad ETFs' },
    { ticker: 'XTL',  name: 'SPDR Telecom ETF', group: 'Telecom' },
  ]);
  check('a fund is not suggested', !dom.searchList.innerHTML.includes('SPY'));
  // XTL is filed under "Telecom", so its group label gives nothing away — the
  // one that would slip through a rule reading only the first peer group.
  check('a fund wearing an equity group label is not suggested',
        !dom.searchList.innerHTML.includes('XTL'));
  check('companies are still suggested', dom.searchList.innerHTML.includes('SPOT'));

  // Typed in full: skipped too, but not in silence. An empty dropdown for a
  // symbol somebody spelled out reads as the search being broken.
  fns.renderSuggestions('SPY', [{ ticker: 'SPY', name: 'SPDR S&P 500 ETF', group: 'US Broad ETFs' }]);
  check('a typed fund offers nothing to click',
        !dom.searchList.innerHTML.includes('<button'));
  check('a typed fund says why', dom.searchList.innerHTML.includes(STRINGS['dcx.not_scorable']));
  check('a typed fund is unreachable by keyboard', fns.peek().length === 0,
        'arrow keys and Enter both index _sugg');

  // The Score button and the Enter key both call goTo directly.
  navigated = null;
  fns.goTo('SPY');
  check('the Score button refuses a fund', navigated === null);
  check('refusing still explains itself',
        dom.searchList.innerHTML.includes(STRINGS['dcx.not_scorable']));
  check('a refused fund is not remembered as recent',
        !JSON.parse(globals.localStorage._v).includes('QQQ'));

  navigated = null;
  fns.goTo('MSFT');
  check('a company still navigates', navigated === '/dca/MSFT');

  // RECENT_KEY is shared with navsearch and /lookup, so a fund opened on
  // /history arrives here as a chip linking to a page that cannot score it.
  fns.renderRecent();
  check('a fund is dropped from the recent chips',
        !dom.recentList.innerHTML.includes('SPY'));
  check('companies stay in the recent chips',
        dom.recentList.innerHTML.includes('MSFT'));
}

console.log(failed ? `\n${failed} check(s) failed` : '\nall checks passed');
process.exit(failed ? 1 : 0);
