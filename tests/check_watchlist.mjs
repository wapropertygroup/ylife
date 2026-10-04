/**
 * Behaviour tests for static/watchlist.js — the one watchlist behind /lookup's
 * star, the star on every /companies card and the star in /history's header.
 *
 * The failures worth catching are the quiet ones. A reader's list lost to a
 * shape change, a corrupt value or a refused write looks exactly like a list
 * they never made. And the old 12-name cap dropped their oldest picks without
 * a word. So most of what follows asserts that nothing is lost.
 *
 * Run: node tests/check_watchlist.mjs
 */
import { createRequire } from 'module';
import path from 'path';

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

if (failures.length) {
  console.log(`\n${failures.length} FAILED`);
  process.exit(1);
}
console.log('\nall passed');
