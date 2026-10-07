/**
 * Behaviour tests for static/watchlist.js — the one watchlist behind /lookup's
 * star, the star on every /companies card and the star in /history's header.
 *
 * The failures worth catching are the quiet ones. A reader's list lost to a
 * shape change, a corrupt value or a refused write looks exactly like a list
 * they never made. And the old 12-name cap dropped their oldest picks without
 * a word. So most of what follows asserts that nothing is lost.
 *
 * The same store, under /lookup's other key, is the reader's recently viewed
 * stocks (window.RecentTickers), which three pages used to keep by hand in two
 * shapes; the old shapes must still read.
 *
 * Run: node tests/check_watchlist.mjs
 */
import { readFileSync, readdirSync } from 'fs';
import { createRequire } from 'module';
import path from 'path';
import vm from 'vm';

const root = path.resolve(import.meta.dirname, '..');
const require = createRequire(import.meta.url);
const W = require(path.join(root, 'ystocker/static/watchlist.js'));

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${detail ? '  ' + detail : ''}`);
  if (!cond) failures.push(label);
};
const eq = (label, got, want) => t(label, JSON.stringify(got) === JSON.stringify(want),
  `got ${JSON.stringify(got)} want ${JSON.stringify(want)}`);

function memoryStorage(initial = {}) {
  const data = { ...initial };
  return {
    data,
    getItem: k => (k in data ? data[k] : null),
    setItem: (k, v) => { data[k] = String(v); },
  };
}

console.log('the key and shape are /lookup\'s');
eq('same localStorage key /lookup always used', W.KEY, 'ystocker_watchlist');
{
  // Exactly what lookup.html wrote before this module existed.
  const s = memoryStorage({ ystocker_watchlist: JSON.stringify([{ ticker: 'NVDA', name: 'NVIDIA' }, { ticker: 'AAPL', name: 'Apple' }]) });
  const wl = W.create(s);
  eq('an existing /lookup list is read as-is', wl.list(), [{ ticker: 'NVDA', name: 'NVIDIA' }, { ticker: 'AAPL', name: 'Apple' }]);
  wl.add('MSFT', 'Microsoft');
  eq('and written back in the same shape, newest first',
    JSON.parse(s.data.ystocker_watchlist).map(w => w.ticker), ['MSFT', 'NVDA', 'AAPL']);
}

console.log('add / remove / toggle');
{
  const events = [];
  const wl = W.create(memoryStorage(), list => events.push(list.map(w => w.ticker)));
  t('has() on an empty list is false', !wl.has('NVDA'));
  t('add returns true', wl.add('nvda', 'NVIDIA Corp'));
  t('tickers are upper-cased and trimmed', wl.has(' NVDA ') && wl.tickers()[0] === 'NVDA');
  wl.add('AAPL', 'Apple');
  wl.add('NVDA');
  eq('re-adding moves to the front without duplicating', wl.tickers(), ['NVDA', 'AAPL']);
  eq('and keeps the name it had when given none', wl.list()[0].name, 'NVIDIA Corp');
  eq('toggle off answers false', wl.toggle('AAPL'), false);
  eq('toggle on answers true', wl.toggle('KO', 'Coca-Cola'), true);
  eq('state after toggles', wl.tickers(), ['KO', 'NVDA']);
  t('removing something absent writes nothing', !wl.remove('ZZZZ') && events.length === 5,
    `events ${events.length}`);
  eq('every write is announced with the new list', events[events.length - 1], ['KO', 'NVDA']);
  t('an empty or absurd ticker is refused', !wl.add('') && !wl.add('X'.repeat(21)) && !wl.add(null));
  wl.clear();
  eq('clear empties it', wl.list(), []);
}

console.log('nothing a reader made is lost');
{
  const wl = W.create(memoryStorage());
  for (let i = 0; i < 30; i++) wl.add('T' + i);
  eq('thirty stars stay thirty (the old cap was 12)', wl.list().length, 30);
  for (let i = 30; i < W.MAX + 5; i++) wl.add('T' + i);
  eq('the cap is MAX', wl.list().length, W.MAX);
  eq('and past it the oldest goes, not the newest', wl.tickers()[0], 'T' + (W.MAX + 4));
}
{
  const s = memoryStorage({ ystocker_watchlist: '{not json' });
  const wl = W.create(s);
  eq('a corrupt value reads as empty rather than throwing', wl.list(), []);
  wl.add('NVDA');
  eq('and the next write repairs it', JSON.parse(s.data.ystocker_watchlist), [{ ticker: 'NVDA', name: 'NVDA' }]);
}
{
  const s = memoryStorage({ ystocker_watchlist: JSON.stringify(['aapl', null, 7, { ticker: 'MSFT' }, { ticker: 'aapl', name: 'dup' }, { name: 'no ticker' }]) });
  eq('junk entries are skipped, bare strings kept, duplicates folded',
    W.create(s).list(), [{ ticker: 'AAPL', name: 'AAPL' }, { ticker: 'MSFT', name: 'MSFT' }]);
}
{
  // Safari private mode: getItem works, setItem throws QuotaExceededError.
  const s = memoryStorage();
  s.setItem = () => { throw new Error('QuotaExceededError'); };
  const wl = W.create(s);
  wl.add('NVDA');
  wl.add('AAPL');
  eq('a refused write keeps the list in memory for the visit', wl.tickers(), ['AAPL', 'NVDA']);
  t('so a star still toggles', wl.toggle('NVDA') === false && !wl.has('NVDA'));
}
{
  const wl = W.create(null);
  wl.add('NVDA');
  eq('no storage at all still works in memory', wl.tickers(), ['NVDA']);
}
{
  const s = { getItem: () => { throw new Error('SecurityError'); }, setItem: () => { throw new Error('SecurityError'); } };
  const wl = W.create(s);
  eq('storage that throws on read reads as empty', wl.list(), []);
  wl.add('KO');
  eq('and then works in memory', wl.tickers(), ['KO']);
}

console.log('names');
{
  const wl = W.create(memoryStorage());
  wl.add('BRK-B', '  Berkshire Hathaway  ');
  eq('names are trimmed', wl.list()[0].name, 'Berkshire Hathaway');
  wl.add('LONG', 'x'.repeat(500));
  eq('and bounded', wl.list()[0].name.length, 120);
  wl.add('NONAME', 42);
  eq('a non-string name falls back to the ticker', wl.list()[0].name, 'NONAME');
}

console.log('recently viewed: the same store under /lookup\'s other key');
eq('the key /lookup, the header search and /dca all wrote', W.RECENT_KEY, 'ystocker_recent_tickers');
{
  // What the old code left behind: /lookup and the header search wrote
  // {ticker, name}, and /dca wrote bare strings (dropping the records).
  const s = memoryStorage({ ystocker_recent_tickers: JSON.stringify(
    ['MSFT', { ticker: 'NVDA', name: 'NVIDIA' }, 'nvda', { ticker: 'AAPL', name: 'Apple' }]) });
  const r = W.create(s, null, { key: W.RECENT_KEY, max: W.RECENT_MAX });
  eq('both old shapes read, in order, one per ticker', r.list(),
    [{ ticker: 'MSFT', name: 'MSFT' }, { ticker: 'NVDA', name: 'NVIDIA' }, { ticker: 'AAPL', name: 'Apple' }]);
  r.add('TSM', 'Taiwan Semiconductor');
  eq('and are written back as records, newest first',
    JSON.parse(s.data.ystocker_recent_tickers).map(w => w.ticker), ['TSM', 'MSFT', 'NVDA', 'AAPL']);
  r.add('NVDA');
  eq('a stock viewed again moves to the front with its name', r.list()[0], { ticker: 'NVDA', name: 'NVIDIA' });
  r.add('MSFT', 'Microsoft');
  eq('and a real name replaces the ticker standing in for one', r.list()[0], { ticker: 'MSFT', name: 'Microsoft' });
  eq('the instance reports its own key and cap', [r.KEY, r.MAX], [W.RECENT_KEY, W.RECENT_MAX]);
}
{
  const s = memoryStorage();
  const wl = W.create(s);
  const r = W.create(s, null, { key: W.RECENT_KEY, max: W.RECENT_MAX });
  wl.add('NVDA');
  r.add('AAPL');
  eq('the two lists share storage, not a key', [wl.tickers(), r.tickers()], [['NVDA'], ['AAPL']]);
  r.clear();
  t('so clearing the history leaves the watchlist', wl.has('NVDA') && r.list().length === 0);
}
{
  const r = W.create(memoryStorage(), null, { key: W.RECENT_KEY, max: W.RECENT_MAX });
  for (let i = 0; i < W.RECENT_MAX + 6; i++) r.add('R' + i);
  eq('recently viewed keeps RECENT_MAX', r.list().length, W.RECENT_MAX);
  eq('its oldest end falls off', r.tickers()[W.RECENT_MAX - 1], 'R6');
  const long = memoryStorage({ ystocker_recent_tickers: JSON.stringify(Array.from({ length: 40 }, (_, i) => 'L' + i)) });
  eq('and the cap holds over a longer list already in storage',
    W.create(long, null, { key: W.RECENT_KEY, max: W.RECENT_MAX }).list().length, W.RECENT_MAX);
}

console.log('in a page');
{
  const src = readFileSync(path.join(root, 'ystocker/static/watchlist.js'), 'utf8');
  const fired = [];
  const listeners = {};
  const win = {
    localStorage: memoryStorage(),
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    document: { dispatchEvent: ev => fired.push([ev.type, ev.detail.list.map(w => w.ticker)]) },
  };
  vm.runInNewContext(src, { window: win });
  t('the page gets both lists', !!win.Watchlist && !!win.RecentTickers);
  win.RecentTickers.add('NVDA', 'NVIDIA');
  win.Watchlist.add('AAPL');
  eq('each change announces its own event', fired, [['recent:change', ['NVDA']], ['watchlist:change', ['AAPL']]]);
  const storage = key => { fired.length = 0; listeners.storage.forEach(fn => fn({ key })); return fired.slice(); };
  eq('another tab\'s history is the recent event', storage('ystocker_recent_tickers'), [['recent:change', ['NVDA']]]);
  eq('another tab\'s star is the watchlist event', storage('ystocker_watchlist'), [['watchlist:change', ['AAPL']]]);
  eq('storage cleared elsewhere announces both', storage(null).map(f => f[0]), ['watchlist:change', 'recent:change']);
  eq('an unrelated key announces nothing', storage('ystocker_lang'), []);
}

console.log('loaded once, from base.html, before any page script reads it');
{
  const tpl = path.join(root, 'ystocker/templates');
  const base = readFileSync(path.join(tpl, 'base.html'), 'utf8');
  const at = base.indexOf("filename='watchlist.js'");
  t('base.html loads it', at > 0);
  t('in <head>, ahead of every page\'s content', at > 0 && at < base.indexOf('</head>')
    && at < base.indexOf('{% block content %}'));
  t('and ahead of the header search, which reads both lists', at < base.indexOf("filename='navsearch.js'"));
  const copies = readdirSync(tpl, { recursive: true })
    .filter(f => f.endsWith('.html') && f !== 'base.html')
    .filter(f => readFileSync(path.join(tpl, f), 'utf8').includes("filename='watchlist.js'"));
  eq('no page loads a second copy, which would answer every change twice', copies, []);
}

console.log('who writes recently viewed');
{
  // A stock page records itself once its data has loaded, so a mistyped symbol,
  // whose page has nothing to show, is never kept; /lookup records the card it
  // draws in place. Everything else only reads: recording at the moment a
  // search navigates is what used to fill the list with typos.
  const tpl = path.join(root, 'ystocker/templates');
  const hist = readFileSync(path.join(tpl, 'history.html'), 'utf8');
  const fetchAt = hist.indexOf('const resp = await fetch(`/api/history/${encodeURIComponent(TICKER)}`);');
  const refusedAt = hist.indexOf('if (!resp.ok) {', fetchAt);
  const recordAt = hist.indexOf('window.RecentTickers.add(TICKER, data.name', fetchAt);
  t('/history records the stock it shows', fetchAt > 0 && recordAt > 0);
  t('after the request for its data and the return on a refusal',
    fetchAt < refusedAt && refusedAt < recordAt && hist.slice(refusedAt, recordAt).includes('return;'));
  t('in the same function, beside the name it records', recordAt - fetchAt < 1500
    && hist.slice(fetchAt, recordAt).includes("getElementById('stockName').textContent"));
  const files = readdirSync(tpl, { recursive: true }).filter(f => f.endsWith('.html')).map(f => path.join(tpl, f))
    .concat(readdirSync(path.join(root, 'ystocker/static')).filter(f => f.endsWith('.js'))
      .map(f => path.join(root, 'ystocker/static', f)));
  const writers = files.filter(f => /RecentTickers\.(add|toggle)\(/.test(readFileSync(f, 'utf8')))
    .map(f => path.relative(root, f)).sort();
  eq('and only /history and /lookup write it', writers,
    ['ystocker/templates/history.html', 'ystocker/templates/lookup.html']);
}

if (failures.length) {
  console.log(`\n${failures.length} FAILED`);
  process.exit(1);
}
console.log('\nall passed');
