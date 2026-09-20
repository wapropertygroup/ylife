// Checks `renderPeers()` in templates/dca.html — the peer comparison table.
//
// This panel puts several companies' V scores in one column, which is the one
// place the engine's central caveat can be lost: V ranks a company against its
// own history, so a table of them is not a ranking. Everything that keeps that
// readable — the band cell for a row with no V, the heading that says which
// P/E the column holds, the marker on a peer scored off a different template —
// is composed in JavaScript, where neither `I18n.apply()` nor any Python test
// can reach it.
//
// The function is extracted from the template rather than copied, for the
// reason check_dca_row_cells.mjs gives: a copy agrees on the day it is written
// and drifts afterwards, and drift is exactly what this file exists to catch.
//
// Run: node tests/check_dca_peers_panel.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const tpl = readFileSync(join(here, '..', 'ystocker', 'templates', 'dca.html'), 'utf8');

function extract(name) {
  const start = tpl.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`function ${name} not found in dca.html`);
  let i = tpl.indexOf('{', start), depth = 0;
  for (let j = i; j < tpl.length; j++) {
    if (tpl[j] === '{') depth++;
    else if (tpl[j] === '}' && --depth === 0) return tpl.slice(start, j + 1);
  }
  throw new Error(`unbalanced braces in ${name}`);
}

/* A table rather than a null stub, for the reason check_dca_row_cells.mjs
   records: returning null from every lookup sends each call down its English
   fallback, so the *keys* are never exercised and a collapsed or typo'd key
   still passes. Only the keys this panel composes by concatenation are here;
   the rest fall back, which is also what a missing translation would do. */
const STRINGS = {
  'dca.band_cheap': 'Cheap',
  'dca.band_expensive': 'Expensive',
  'dca.model_compounder': 'Compounder',
  'dca.model_bank': 'Bank',
  'dcx.why_factors': 'too few factors',
  'dcx.why_young': 'history too short',
  'dca.peers_col_fwd_pe': 'Fwd P/E',
  'dca.peers_col_ttm_pe': 'P/E (TTM)',
  'dca.peers_col_pe': 'P/E',
  'dca.peers_other_model': 'Scored on a different template',
  'dca.peers_r_not_built': 'No reconstruction on disk yet',
  'dca.peers_r_unavailable': 'Publishes no statements this engine can rank',
  'dca.peers_none': 'No peer in {g} has been scored yet.',
  'dca.peers_no_group': 'This ticker is in no peer group.',
  'dca.peers_failed': 'Could not load peers.',
  'dca.peers_truncated': '{n} further scored peers are not shown.',
};
const asked = [];
const I18n = { t: (k) => { asked.push(k); return STRINGS[k] ?? null; } };

// Mirrors the template's own `esc`, so a call site that drops it shows up as a
// raw value in the assertions below rather than passing silently.
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtMoney = (v) => v == null ? '—' : `$${v.toFixed(2)}`;
const pctColor = () => '#000';

function el() {
  const node = {
    textContent: '', innerHTML: '', _classes: new Set(),
    classList: {
      add: (c) => node._classes.add(c),
      remove: (c) => node._classes.delete(c),
      contains: (c) => node._classes.has(c),
      toggle: (c, force) => {
        const on = force === undefined ? !node._classes.has(c) : !!force;
        if (on) node._classes.add(c); else node._classes.delete(c);
        return on;
      },
    },
  };
  return node;
}

const IDS = ['peersLoading', 'peersWrap', 'peersEmpty', 'peersGroup', 'peersColPe',
             'peersBody', 'peersTrunc', 'peersUnscoredRow', 'peersUnscored',
             'peersUnscoredBasis'];

let dom = {};
const document = { getElementById: (id) => dom[id] };

const src = ['peersEmpty', 'renderPeers'].map(extract).join('\n');
const { renderPeers } = new Function(
  'document', 'I18n', 'esc', 'fmtMoney', 'pctColor',
  `${src}; return { renderPeers };`
)(document, I18n, esc, fmtMoney, pctColor);

function render(payload) {
  dom = Object.fromEntries(IDS.map(id => [id, el()]));
  asked.length = 0;
  renderPeers(payload);
  return dom;
}

let failed = 0;
function check(name, fn) {
  try { fn(); console.log(`  ok   ${name}`); }
  catch (e) { failed++; console.log(`  FAIL ${name}\n       ${e.message}`); }
}
function assert(cond, msg) { if (!cond) throw new Error(msg); }

const ROWS = [
  { ticker: 'ADBE', name: 'Adobe Inc.', model: 'compounder', same_model: true,
    V: 90.6, band: 'cheap', value: 18.2, multiplier: 1.41, amount: 1406.2,
    capped: false, no_score_reason: null },
  { ticker: 'MSFT', name: 'Microsoft', model: 'compounder', same_model: true,
    V: 53.3, band: 'expensive', value: 27.4, multiplier: 1.03, amount: 1033.2,
    capped: false, no_score_reason: null, self: true },
  { ticker: 'JPM', name: 'JPMorgan', model: 'bank', same_model: false,
    V: 24.1, band: 'expensive', value: 14.0, multiplier: 0.74, amount: 741.3,
    capped: false, no_score_reason: null },
  { ticker: 'LITE', name: 'Lumentum', model: 'compounder', same_model: true,
    V: null, band: null, value: 31.0, multiplier: null, amount: null,
    capped: false, no_score_reason: 'factors' },
];
const FULL = {
  ticker: 'MSFT', group: 'Tech', model: 'compounder', basis: 'forward',
  rows: ROWS, unscored: [{ ticker: 'TSLA', value: 88.1, reason: 'not_built' },
                         { ticker: 'IGV', value: null, reason: 'unavailable' }],
  scored: 3, members: 12, truncated: 0, base_dca: 1000,
};

console.log('renderPeers()');

/* Every element the panel reaches for must actually be in the template. This is
   the one failure that takes the *whole* card down rather than degrading one
   cell — `getElementById` returns null and the next property set throws — and a
   rename in the markup is exactly how it happens. */
check('every element it reaches for exists in the template', () => {
  const wanted = [...src.matchAll(/getElementById\('([^']+)'\)/g)].map(m => m[1]);
  assert(wanted.length > 5, `only found ${wanted.length} lookups — regex drifted`);
  for (const id of new Set(wanted)) {
    assert(tpl.includes(`id="${id}"`), `no element with id="${id}" in dca.html`);
  }
});

check('a peer links through to its own page', () => {
  const html = render(FULL).peersBody.innerHTML;
  assert(html.includes('href="/dca/ADBE"'), 'no link to the peer');
});

// The reader's own row must not be a link to the page they are already on.
check('the self row is marked and is not a link to itself', () => {
  const html = render(FULL).peersBody.innerHTML;
  assert(html.includes('peer-self'), 'self row not marked');
  assert(!html.includes('href="/dca/MSFT"'), 'self row links to itself');
});

/* A row that could not be scored must say why, not show an empty band. "Could
   not be measured" and "fairly valued" are different statements and the cell is
   the same width either way. */
check('an unscorable peer states its reason instead of a band', () => {
  const html = render(FULL).peersBody.innerHTML;
  assert(html.includes('too few factors'), 'no reason rendered');
  assert(asked.includes('dcx.why_factors'), 'reason key not composed');
});

/* The column heading is the whole defence against reading a forward multiple as
   a trailing one — the same confusion the reconstruction is built to avoid. */
check('the P/E heading names the basis it actually holds', () => {
  assert(render(FULL).peersColPe.textContent === 'Fwd P/E', 'forward heading wrong');
  assert(render({ ...FULL, basis: 'trailing' }).peersColPe.textContent === 'P/E (TTM)',
         'trailing heading wrong');
  assert(render({ ...FULL, basis: null }).peersColPe.textContent === 'P/E',
         'unknown-basis heading wrong');
});

check('a peer on a different template is marked', () => {
  const html = render(FULL).peersBody.innerHTML;
  assert(asked.includes('dca.peers_other_model'), 'mismatch tooltip not composed');
  assert(html.includes('Bank'), 'the differing model is not named');
});

// `same_model: null` means the server could not tell. Flagging every peer as a
// mismatch on no evidence is worse than saying nothing.
check('an unknown template comparison marks nothing', () => {
  const rows = ROWS.map(r => ({ ...r, same_model: null }));
  render({ ...FULL, rows, model: null });
  assert(!asked.includes('dca.peers_other_model'), 'marked a mismatch it cannot know');
});

check('unbuilt members are listed with a reason and a way to build them', () => {
  const dom = render(FULL);
  assert(!dom.peersUnscoredRow._classes.has('hidden'), 'unscored block hidden');
  assert(dom.peersUnscored.innerHTML.includes('href="/dca/TSLA"'), 'no link');
  assert(asked.includes('dca.peers_r_not_built'), 'not_built key not composed');
  assert(asked.includes('dca.peers_r_unavailable'), 'unavailable key not composed');
});

check('truncation is reported with its count substituted', () => {
  const dom = render({ ...FULL, truncated: 4 });
  assert(!dom.peersTrunc._classes.has('hidden'), 'truncation notice hidden');
  assert(dom.peersTrunc.textContent.includes('4'), 'count not substituted');
  assert(!dom.peersTrunc.textContent.includes('{n}'), 'slot left in the string');
});

check('nothing truncated says nothing', () => {
  assert(render(FULL).peersTrunc._classes.has('hidden'), 'empty notice shown');
});

/* Three terminal states that must not look alike. An empty group, a ticker with
   no group at all, and a request that failed are different facts, and only the
   last is worth reloading for. */
check('an empty group says so and names the group', () => {
  const dom = render({ ...FULL, rows: [], scored: 0 });
  assert(dom.peersWrap._classes.has('hidden'), 'table shown with no rows');
  assert(dom.peersEmpty.textContent.includes('Tech'), 'group not named');
});

check('no peer group at all is its own message', () => {
  const dom = render({ ticker: 'ZZZZ', group: null, rows: [], unscored: [] });
  assert(dom.peersGroup._classes.has('hidden'), 'empty chip left visible');
  assert(dom.peersEmpty.textContent === STRINGS['dca.peers_no_group'],
         `wrong message: ${dom.peersEmpty.textContent}`);
});

check('a failed request never reads as an empty peer group', () => {
  const dom = render({ error: 'HTTP 500' });
  assert(dom.peersEmpty.textContent === STRINGS['dca.peers_failed'],
         `wrong message: ${dom.peersEmpty.textContent}`);
  assert(dom.peersWrap._classes.has('hidden'), 'table shown on failure');
});

check('the spinner is always cleared, including on failure', () => {
  for (const payload of [FULL, { error: 'x' }, { group: null, rows: [] }]) {
    assert(render(payload).peersLoading._classes.has('hidden'),
           'a terminal state left the spinner running');
  }
});

/* Company names come from Yahoo and land in innerHTML. A test that greps for
   `onerror=` would pass on its own escaping, so this asserts on the *escaped*
   form being present and the live one absent. */
check('a hostile company name is escaped, not rendered', () => {
  const rows = [{ ...ROWS[0], name: '<img src=x onerror=alert(1)>' }];
  const html = render({ ...FULL, rows }).peersBody.innerHTML;
  assert(!html.includes('<img'), 'live markup reached the DOM');
  assert(html.includes('&lt;img'), 'the name was dropped rather than escaped');
});

console.log(failed ? `\n${failed} check(s) failed` : '\nall checks passed');
process.exit(failed ? 1 : 0);
