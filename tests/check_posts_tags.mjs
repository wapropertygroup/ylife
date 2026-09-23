// Checks the title-derived grouping in templates/posts.html: `laneKey`, which
// decides the board's lanes, and `autoTags`, which labels a post with a series
// and a cadence nobody actually sent.
//
// Both are heuristics over free text, and the cost of a wrong one is a lane too
// many or a chip that reads as a classification when it is a coincidence. That
// is a cheap failure, which is exactly why it needs a test: nothing else will
// notice. Every case below was taken from the live feed rather than invented —
// the two bugs this file pins were both found by running the rules over the
// real 44 titles and reading the output, not by reasoning about the regexes.
//
// The functions are extracted from the template rather than copied, matching
// check_dca_row_cells.mjs: a copy agrees on the day it is written and drifts
// afterwards, and drift is the whole thing being guarded against.
//
// Run: node tests/check_posts_tags.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const tpl = readFileSync(join(here, '..', 'ystocker', 'templates', 'posts.html'), 'utf8');

function extract(name, kind = 'function') {
  const sig = kind === 'function' ? `function ${name}(` : `const ${name} = [`;
  const start = tpl.indexOf(sig);
  if (start < 0) throw new Error(`${name} not found in posts.html`);
  const open = kind === 'function' ? '{' : '[';
  const close = kind === 'function' ? '}' : ']';
  let i = tpl.indexOf(open, start + sig.length - 1), depth = 0;
  for (let j = i; j < tpl.length; j++) {
    if (tpl[j] === open) depth++;
    else if (tpl[j] === close && --depth === 0) return tpl.slice(start, j + 1);
  }
  throw new Error(`unbalanced ${open}${close} in ${name}`);
}

// Identity stub. Unlike check_dca_row_cells.mjs a table is not needed here:
// these keys are asserted for existence in i18n.js by the loop at the bottom,
// which is the failure that actually matters (a missing zh string on a page
// that is mostly read in Chinese).
const I18n = { t: (k) => k };
const MAX_KINDS = Number(/const MAX_KINDS = (\d+)/.exec(tpl)?.[1]);

const src = [extract('KIND_RULES', 'const'), extract('autoTags'), extract('laneKey')].join('\n');
const { autoTags, laneKey, KIND_RULES } =
  new Function('I18n', 'MAX_KINDS', `${src}\nreturn {autoTags, laneKey, KIND_RULES};`)(I18n, MAX_KINDS);

const failures = [];
// `detail` only on failure: printed next to "ok" it reads as a complaint about
// a passing check, which is worse than no diagnostic at all.
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${cond || !detail ? '' : '  — ' + detail}`);
  if (!cond) failures.push(label);
};
const lane = (title, source) => laneKey({ title, source });
const kinds = (title, source) =>
  autoTags({ title, source }).filter(x => x.key.startsWith('kind:')).map(x => x.key.slice(5));
const series = (title, source) =>
  autoTags({ title, source }).filter(x => x.key.startsWith('series:')).map(x => x.key.slice(7));

console.log('\nlaneKey — one lane per series, not per run');
// The delimiters that actually appear in the feed: full-width parens, a pipe,
// an interpunct, a colon of either width.
t('full-width paren',  lane('美卡论坛更新（9/22 18:48 轮）· 1 条') === '美卡论坛更新');
t('pipe',              lane('NYT + WSJ 新闻摘要 | 9 月 22 日下午 2:30（每6小时）') === 'NYT + WSJ 新闻摘要');
t('full-width colon',  lane('视频日报 9/21：油价逼近$100、美债5%警戒线') === '视频日报');
t('interpunct',        lane('Monarch 每日财务报告（9/22）· 净资产 $4.99M') === 'Monarch 每日财务报告');

// Regression: an earlier rule cut at /\s\d/, which reads as "the date starts
// here" and is wrong whenever a number is part of the name.
t('a number inside the name is not a date',
  lane('未来 3 天日历提醒（9/22）') === '未来 3 天日历提醒',
  `got ${JSON.stringify(lane('未来 3 天日历提醒（9/22）'))}`);

// Regression: keeping the whole head gave one lane per day, which on a daily
// series is the same as no grouping at all.
t('trailing m/d is dropped',
  lane('TradeAgents 视频日报 9/22：AI/半导体带队突破') === 'TradeAgents 视频日报');
t('trailing ISO date is dropped',
  lane('📊 论坛股票热度日报（2026-09-22）') === '📊 论坛股票热度日报');
t('two runs of one series share a lane',
  lane('TradeAgents 视频日报 9/22：甲') === lane('TradeAgents 视频日报 9/21：乙'));

t('untitled falls back to source', lane('', 'my-script') === 'my-script');
t('untitled with no source is empty', lane('', '') === '');
t('a one-off title is its own lane',
  lane('Question about September Upgrade Opportunity — Preferred Customer Bonus')
    === 'Question about September Upgrade Opportunity');

console.log('\nautoTags — cadence read off the head, never the content');
// The bug this pins: matching the whole title labelled a daily video digest an
// "alert", because 警戒线 (a yield threshold) appears in the headline. The
// cadence word is part of the series name; everything after the delimiter is
// that run's content.
t('警戒 in the headline is not a cadence',
  !kinds('视频日报 9/21：油价逼近$100、美债5%警戒线，川习会能否救市？').includes('alert'),
  'content matched the alert rule');
t('…and it is still correctly daily',
  kinds('视频日报 9/21：油价逼近$100、美债5%警戒线').join() === 'daily');
t('a genuine alert in the head is kept',
  kinds('NVDA 触发阈值').join() === 'alert');

t('更新 → update',    kinds('美卡论坛更新（9/22 18:48 轮）· 1 条').join() === 'update');
t('摘要 → digest',    kinds('NYT + WSJ 新闻摘要 | 9 月 22 日下午 2:30').join() === 'digest');
t('提醒 → reminder',  kinds('未来 3 天日历提醒（9/22）').join() === 'reminder');
t('日报 → daily',     kinds('账单/租金日报（9/22）').join() === 'daily');
t('每日 + 报告 → both, in rule order',
  kinds('Monarch 每日财务报告（9/22）· 净资产 $4.99M').join() === 'daily,report');

// Normalisation, not copying: the chip must be one facet across languages and
// must render in the reader's language, not the sender's.
t('English titles hit the same keys',
  kinds('Weekly Digest').join() === 'weekly,digest');
t('daily/日报 are one key', kinds('Morning Daily').join() === kinds('晨间日报').join());
// Word-boundaried, so an English rule cannot fire on a substring.
t('no substring match on English',
  kinds('Updatable schema report').join() === 'report',
  `got ${kinds('Updatable schema report').join()}`);

t('at most MAX_KINDS cadence chips',
  kinds('每日周报摘要报告提醒预警更新').length === MAX_KINDS);
t('a plain title gets none', kinds('连接测试').length === 0);

console.log('\nautoTags — series');
t('series is emitted', series('美卡论坛更新（9/22）· 1 条').join() === '美卡论坛更新');
// A chip repeating the one beside it reads as two facts rather than one.
t('series equal to the source is suppressed',
  series('', 'my-script').length === 0);
t('no series and no source emits nothing',
  autoTags({ title: '', source: '' }).length === 0);

console.log('\ni18n — every key the chips compose exists in both languages');
// These are built by string concatenation in JS, where I18n.apply() cannot
// reach them, so a missing key ships the raw key to the page.
const i18n = readFileSync(join(here, '..', 'ystocker', 'static', 'i18n.js'), 'utf8');
const need = [...KIND_RULES.map(([k]) => `inbox.kind_${k}`), 'inbox.auto_tag',
              'inbox.href_refused', 'inbox.href_refused_tip'];
need.forEach(k => {
  const m = new RegExp(`'${k.replace('.', '\\.')}'\\s*:\\s*\\{([^}]*)\\}`).exec(i18n);
  t(k, !!m && /\ben\s*:/.test(m[1]) && /\bzh\s*:/.test(m[1]),
    m ? 'missing en or zh' : 'key absent');
});

console.log(failures.length
  ? `\n${failures.length} FAILED: ${failures.join(', ')}\n`
  : '\nAll checks passed.\n');
process.exit(failures.length ? 1 : 0);
