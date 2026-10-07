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
// passes the check. Verified on the no-score tag this page used to draw: with a
// null stub, rewriting `dcx.why_ + reason` to a single constant key was not
// caught.
const STRINGS = {
  'dcx.filings': 'filings',
  'dcx.years_unit': 'y',
  'dcx.dropped': 'dropped',
  'dcx.already_scored': 'scored',
  'dcx.not_scorable': 'A fund publishes no statements, so it has no valuation history to score.',
  'dcx.fold_show': '{n} more with V below 60 — show them',
  'dcx.fold_hide': 'Fold the {n} with V below 60',
  'dcx.fold_card': '{n} names with V below 60 are folded.',
  'dcx.fold_open': 'Show them',
};
const asked = [];
const I18n = { t: (k) => { asked.push(k); return STRINGS[k] ?? null; } };
const esc = (s) => String(s ?? '');
const label = (prefix, key) => `${prefix}${key ?? 'unknown'}`;

const src = ['overlayCell', 'coverageCell'].map(extract).join('\n');
const { overlayCell, coverageCell } = new Function(
  'I18n', 'esc', 'label',
  `${src}; return { overlayCell, coverageCell };`
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

/* No row without a valid V is drawn (2026-10-07), so the tag that said why a
   row had none went with it. The reasons still show on the detail page's peer
   panel, which has its own checks. */
console.log('no-score tag gone; coverage strings by key');
{
  check('the overview no longer carries a no-score tag', !tpl.includes('function noScoreTag'));
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
   and XTL were both being suggested. Three entry points reach a company's DCA tab — the
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
  check('a company still navigates, to its DCA tab', navigated === '/history/MSFT?tab=dca');

  // RECENT_KEY is shared with navsearch and /lookup, so a fund opened on
  // /history arrives here as a chip linking to a page that cannot score it.
  fns.renderRecent();
  check('a fund is dropped from the recent chips',
        !dom.recentList.innerHTML.includes('SPY'));
  check('companies stay in the recent chips',
        dom.recentList.innerHTML.includes('MSFT'));
}

/* ── Names below V 60 start folded ─────────────────────────────────────────
   Asked for 2026-10-05, absorbing the 2026-10-04 rule that left V ≤ 20 off the
   page. The cut is made on V as the table shows it, so a 59.6 that reads "60"
   is not folded under a rule that says "below 60", and a name that could not be
   scored is not "below 60". The threshold and the functions are read from the
   template, so this cannot pass against a copy of them. */
console.log('\nV below 60 folded');
{
  const below = Number((/const FOLD_BELOW_V = (\d+);/.exec(tpl) || [])[1]);
  check('the threshold is 60', below === 60);
  const fold = new Function('I18n', 'esc',
    `const FOLD_BELOW_V = ${below};
     ${['folded', 'foldRow', 'foldNote', 'tableBody', 'hasV'].map(extract).join('\n')}
     return { folded, foldRow, foldNote, tableBody };`)(I18n, esc);
  check('V 59.4 is folded', fold.folded({ V: 59.4 }));
  check('a 59.6 that reads "60" stays open', !fold.folded({ V: 59.6 }));
  check('V exactly 60 stays open', !fold.folded({ V: 60 }));
  check('V ≤ 20 is folded too', fold.folded({ V: 20 }) && fold.folded({ V: 0 }));
  check('a cheap name stays open', !fold.folded({ V: 93.3 }));
  check('an unscored name is not "below 60"', !fold.folded({ V: null }) && !fold.folded({}));

  const rows = [
    { ticker: 'LOW', V: 31 }, { ticker: 'NONE', V: null }, { ticker: 'TOP', V: 92 },
    { ticker: 'MID', V: 61 }, { ticker: 'DEAR', V: 4 },
  ];
  const byV = (a, b) => (a.V == null) - (b.V == null) || (b.V ?? 0) - (a.V ?? 0);
  const cell = (r) => `[${r.ticker}]`;
  const closed = fold.tableBody(rows, false, byV, cell);
  check('folded: the open names, then the fold, and no row for a name with no V',
        /^\[TOP\]\[MID\]<tr class="fold-row">.*<\/tr>$/.test(closed) && !closed.includes('[NONE]'));
  check('folded: nothing below 60 is drawn',
        !closed.includes('[LOW]') && !closed.includes('[DEAR]'));
  check('the fold counts what it holds and says it is closed',
        closed.includes('2 more with V below 60') && closed.includes('aria-expanded="false"'));
  const open = fold.tableBody(rows, true, byV, cell);
  check('unfolded: the folded names follow the fold, in order, and still no row with no V',
        open.startsWith('[TOP][MID]<tr') && /<\/tr>\[LOW\]\[DEAR\]$/.test(open)
          && !open.includes('[NONE]'));
  check('a V that is not a finite number is no V either',
        fold.tableBody([{ ticker: 'NAN', V: NaN }, { ticker: 'STR', V: '70' },
                        { ticker: 'INF', V: Infinity }, { ticker: 'OK', V: 65 }], true, byV, cell) === '[OK]');
  check('unfolded: the fold offers to close again',
        open.includes('Fold the 2 with V below 60') && open.includes('aria-expanded="true"'));
  check('with nothing below 60 there is no fold row',
        !fold.tableBody([{ ticker: 'A', V: 70 }, { ticker: 'B', V: null }], false, byV, cell)
          .includes('fold-row'));
  const note = fold.foldNote(29);
  check('the card note says how many and carries the same toggle',
        note.includes('29 names with V below 60 are folded.') && note.includes('data-fold-toggle'));
  check('every fold string is asked for by key',
        ['dcx.fold_show', 'dcx.fold_hide', 'dcx.fold_card', 'dcx.fold_open'].every((k) => asked.includes(k)));
}

console.log(failed ? `\n${failed} check(s) failed` : '\nall checks passed');
process.exit(failed ? 1 : 0);
