// Checks I18n.datetime(): the one function that turns every timestamp on this
// site into text.
//
// It is shared by seven pages — the six dashboards' "Data as of / next refresh"
// lines and every post on /posts — so getting it wrong is wrong in seven places
// at once, and wrong *quietly*: a time is always plausible. It rendered UTC for
// months and nobody could tell, because a wrong hour looks exactly like a right
// one unless you happen to compare it against your own clock.
//
// Re-exec'd under a fixed zone rather than trusting the machine's: on a box set
// to UTC every assertion below would pass against a UTC implementation, so the
// test would agree with the bug it exists to catch. America/Los_Angeles is
// chosen for having a non-zero offset *and* a DST transition, which is the
// thing a hand-rolled offset gets wrong.
//
// Run: node tests/check_i18n_datetime.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { spawnSync } from 'node:child_process';

const TZ = 'America/Los_Angeles';
if (process.env.TZ !== TZ) {
  const r = spawnSync(process.execPath, [fileURLToPath(import.meta.url)],
                      { stdio: 'inherit', env: { ...process.env, TZ } });
  process.exit(r.status ?? 1);
}

const here = dirname(fileURLToPath(import.meta.url));
const SRC = join(here, '..', 'ystocker', 'static', 'i18n.js');
const src = readFileSync(SRC, 'utf8');

function extract(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`${name} not found in i18n.js`);
  let i = src.indexOf('{', start), depth = 0;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) return src.slice(start, j + 1);
  }
  throw new Error(`unbalanced braces in ${name}`);
}
const months = /const _MONTHS_EN = \[[^\]]*\];/.exec(src)[0];
const make = (lang) => new Function('current',
  `${months}\n${extract('datetime')}\nreturn datetime;`)(lang);
const en = make('en'), zh = make('zh');

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${cond || !detail ? '' : '  — ' + detail}`);
  if (!cond) failures.push(label);
};

// 2026-09-23T14:56:00Z. Pacific Daylight Time is UTC-7 in September, so this
// is 07:56 locally — the exact case reported from /posts.
const PDT = Date.UTC(2026, 8, 23, 14, 56) / 1000;
// 2026-01-15T02:30:00Z. Pacific Standard Time is UTC-8, and the local date is
// the *previous day* — a day rollover, not just an hour shift.
const PST = Date.UTC(2026, 0, 15, 2, 30) / 1000;

console.log(`\nlocal clock (TZ=${TZ}, offset now ${-new Date().getTimezoneOffset() / 60}h)`);
t('summer instant renders local, not UTC', en(PDT) === 'Sep 23, 2026 07:56',
  `got ${en(PDT)}`);
t('…and the same in Chinese', zh(PDT) === '2026年9月23日 07:56', `got ${zh(PDT)}`);

// DST is the reason this uses the platform's zone database rather than a fixed
// offset: January is UTC-8, September UTC-7, and a hardcoded -7 would put this
// one an hour late and on the wrong side of midnight.
t('winter instant uses standard time, not summer', en(PST) === 'Jan 14, 2026 18:30',
  `got ${en(PST)}`);
t('…crossing back over midnight to the previous day', zh(PST) === '2026年1月14日 18:30',
  `got ${zh(PST)}`);

console.log('\nformat is unchanged — only the clock moved');
// The English branch has always reproduced Jinja's `%b %d, %Y %H:%M` so that
// putting a page on data-ts was typographically invisible. That contract holds.
t('English matches %b %d, %Y %H:%M', /^[A-Z][a-z]{2} \d{2}, \d{4} \d{2}:\d{2}$/.test(en(PDT)));
t('Chinese matches Y年M月D日 HH:MM', /^\d{4}年\d{1,2}月\d{1,2}日 \d{2}:\d{2}$/.test(zh(PDT)));
t('hours and minutes are zero-padded', en(Date.UTC(2026, 8, 23, 16, 5) / 1000)
  === 'Sep 23, 2026 09:05', `got ${en(Date.UTC(2026, 8, 23, 16, 5) / 1000)}`);
t('no timezone label — local is the unmarked default',
  !/UTC|GMT|[+-]\d{2}:?\d{2}$/.test(en(PDT)) && !/UTC|GMT/.test(zh(PDT)));

console.log('\nguards');
// A structural check, because the failure mode is a silent one-line revert.
t('the function reads no UTC getter', !/getUTC/.test(extract('datetime')),
  extract('datetime').match(/getUTC\w+/g)?.join(', '));
t('bad input is empty, not "Invalid Date"', en('nonsense') === '' && en(NaN) === '');
// Callers pass an absolute instant; /posts gets there via `new Date(iso)` on a
// Z-suffixed string, so the epoch is already zone-resolved before this runs.
t('epoch seconds round-trip through a Z-suffixed ISO string',
  en(Math.floor(new Date('2026-09-23T14:56:00.000Z').getTime() / 1000)) === en(PDT));

console.log(failures.length
  ? `\n${failures.length} FAILED: ${failures.join(', ')}\n`
  : '\nAll checks passed.\n');
process.exit(failures.length ? 1 : 0);
