/**
 * Render real brief output through static/markdown.js.
 *
 * The brief is the first thing on the site to put a model-authored Markdown
 * *table* into innerHTML, so this checks both halves of that: the tables come
 * out as tables with their numbers intact, and untrusted output cannot smuggle
 * markup through.
 *
 * Run: node tests/check_brief_markdown.mjs
 */
import fs from 'fs';
import path from 'path';

const root = path.resolve(import.meta.dirname, '..');

// markdown.js reaches for the DOM only in sanitize(), which handles HTML-shaped
// input. Brief output is Markdown, so throwing here proves that path is unused.
global.window = global;
global.document = {
  createElement: () => { throw new Error('sanitize() reached — unexpected for Markdown'); },
};
eval(fs.readFileSync(path.join(root, 'ystocker/static/markdown.js'), 'utf8'));

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${detail ? '  ' + detail : ''}`);
  if (!cond) failures.push(label);
};

// Verbatim from a real zh generation.
const BRIEF = `2026年8月27日市场简报

## 1. 指数与市场广度

美国主要股指今日表现分化。纳斯达克上涨 **1.13%**。

| 指数 | 最新点位 | 日内涨跌幅 | 52周区间位置 | RSI14 |
|---|---|---|---|---|
| S&P 500 / 标普500 | 7,707.13 | +0.41% | 93% | 56.6 |
| Nasdaq / 纳斯达克 | 26,425.74 | +1.13% | 88% | 55.1 |
| Dow Jones / 道琼斯 | 53,434.55 | -0.05% | 87% | 53.7 |

标普500的 RSI14 高于 55。

## 4. 机构持仓（13F）

*本节数据暂不可用。*

## 9. 前瞻与风险

*   **美联储紧缩路径：** 加息概率达 86.0%。
*   **流动性收紧：** 储备余额下降 1588 亿美元。
`;

console.log('=== structure ===');
const html = Markdown.render(BRIEF);
t('renders <table>', html.includes('<table>'));
t('header row in <thead>', html.includes('<thead><tr><th>指数</th>'));
t('three data rows', (html.match(/<tbody>[\s\S]*?<\/tbody>/)[0].match(/<tr>/g) || []).length === 3);
t('five columns', (html.match(/<th>/g) || []).length === 5);
t('cell values intact', html.includes('<td>7,707.13</td>') && html.includes('<td>+0.41%</td>'));
t('negative sign preserved', html.includes('<td>-0.05%</td>'));
t('CJK preserved', html.includes('标普500'));
t('three h2 headings', (html.match(/<h2>/g) || []).length === 3);
t('bold rendered', html.includes('<strong>1.13%</strong>'));
t('italic unavailable line', html.includes('<em>本节数据暂不可用。</em>'));
t('bullets become a list', html.includes('<ul>') && html.includes('<li>'));
t('ampersand escaped once', html.includes('S&amp;P 500') && !html.includes('S&amp;amp;'));
t('no bare pipe left in prose', !/<p>[^<]*\|/.test(html));

// A header row can arrive before its |---| separator while streaming; the
// renderer must not hang or drop it.
console.log();
console.log('=== partial table (streaming) ===');
for (const partial of ['| a | b |', '| a | b |\n|---|', '| a | b |\n|---|---|', '| a | b |\n|---|---|\n| 1 |']) {
  let out = null, threw = null;
  try { out = Markdown.render(partial); } catch (e) { threw = e; }
  t(`does not throw on ${JSON.stringify(partial)}`, !threw, threw ? threw.message : '');
  t(`  produces output`, typeof out === 'string' && out.length > 0);
}

// Model output is untrusted. The check must look at real markup only: an
// escaped `&lt;img src=x onerror=...&gt;` sitting in a <td> is the correct
// outcome, and a naive substring search for "onerror" flags it as a failure.
// So strip every escaped sequence first, then look for live tags/handlers in
// what genuinely reached the DOM as markup.
console.log();
console.log('=== untrusted output cannot inject markup ===');
const liveMarkup = (html) => html.replace(/&lt;[^&]*?&gt;/g, '');
const DANGEROUS_TAG = /<\s*(script|iframe|img|svg|object|embed|form|input|link|meta|style|base)\b/i;
const EVENT_HANDLER = /\son[a-z]+\s*=/i;
const JS_URL = /(href|src)\s*=\s*["']?\s*javascript:/i;

const EVIL = [
  ['script block', '<script>alert(1)</script>'],
  ['script in table cell', '| a |\n|---|\n| <script>alert(1)</script> |'],
  ['img onerror', '<img src=x onerror=alert(1)>'],
  ['img onerror in cell', '| a |\n|---|\n| <img src=x onerror=alert(1)> |'],
  ['javascript: link', '[click](javascript:alert(1))'],
  ['iframe', '<iframe src="//evil"></iframe>'],
  ['svg onload', '<svg onload=alert(1)>'],
  ['bold with handler', '<b onclick="alert(1)">x</b>'],
  ['style block', '<style>body{display:none}</style>'],
  ['escaped-entity smuggling', '&lt;script&gt;alert(1)&lt;/script&gt;'],
];
for (const [label, src] of EVIL) {
  const out = Markdown.render(src);
  const live = liveMarkup(out);
  const hits = [
    DANGEROUS_TAG.test(live) && 'tag',
    EVENT_HANDLER.test(live) && 'handler',
    JS_URL.test(live) && 'js-url',
  ].filter(Boolean);
  t(`neutralises ${label}`, hits.length === 0,
    hits.length ? `${hits.join('+')} in: ${live.slice(0, 80)}` : '');
}

// The documented INLINE_HTML rule un-escapes attribute-free inline tags only,
// which lets a closing </b> through while its opening tag stays escaped. That
// is inert — browsers drop an unmatched close — but assert it stays that way.
console.log();
console.log('=== inline HTML allowlist ===');
t('attribute-free <b> is honoured',
  Markdown.render('<b>x</b>').includes('<b>x</b>'));
t('<b> with a handler stays escaped',
  Markdown.render('<b onclick="alert(1)">x</b>').includes('&lt;b onclick'));
t('only the orphan close tag leaks, no handler',
  !EVENT_HANDLER.test(liveMarkup(Markdown.render('<b onclick="alert(1)">x</b>'))));

console.log();
if (failures.length) {
  console.log(`RESULT: FAIL — ${failures.length}: ${failures.join(', ')}`);
  process.exit(1);
}
console.log(`RESULT: OK  (rendered ${html.length} chars)`);
