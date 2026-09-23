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
console.log('=== the HTML-document opener rule ===');
// /posts accepts whole HTML documents from external senders, and the first real
// one rendered wrong: `<!DOCTYPE html>` matched no block opener, so the entire
// mail took the Markdown path and printed its own <html>/<head>/<body> as
// escaped text with every <img> beside it.
//
// The regex is pure, so it is tested directly rather than through render() —
// which is the only part of this fix reachable without a DOM. `sanitize()` needs
// DOMParser and this suite has none by design (see the stub above, which throws
// precisely to prove the brief path never reaches it), so the media behaviour
// was verified in a real browser instead: image rendered, `onerror` stripped,
// `javascript:` src dropped, iframe and script dropped with contents, <style>
// and <title> not leaked, nothing left escaped.
{
  const src = fs.readFileSync(path.join(root, 'ystocker/static/markdown.js'), 'utf8');
  const m = src.match(/const BLOCK_HTML\s*=\s*([\s\S]*?);\n/);
  t('the opener regex is still where this test looks for it', !!m);
  const OPENER = m ? eval(m[1]) : /$^/;

  // The bug, pinned.
  t('<!DOCTYPE html> opens a document', OPENER.test('<!DOCTYPE html>'));
  t('<html> opens a document', OPENER.test('<html lang="zh-CN">'));
  t('<body> opens a document', OPENER.test('<body style="margin:0">'));
  t('<table> still opens a block', OPENER.test('<table><tr><td>a'));
  t('<p> still opens a block', OPENER.test('<p>hello'));

  // The narrowing that shipped with it. A body *opening* with a bare <img> is
  // the shape an injected payload takes, not the shape a document takes, so it
  // must stay on the Markdown path where it is escaped and shown as text.
  t('a lone <img> is not an opener', !OPENER.test('<img src=x onerror=alert(1)>'));
  t('<center>/<font> are not openers',
    !OPENER.test('<center>x') && !OPENER.test('<font color=red>x'));
  t('prose is not an opener', !OPENER.test('WTI touched $98.6'));
  t('a Markdown heading is not an opener', !OPENER.test('## Heading'));
}

// Behaviour, on the path this suite can reach: the lone <img> stays inert.
console.log();
console.log('=== a lone <img> stays text ===');
t('it is escaped rather than rendered',
  Markdown.render('<img src=x onerror=alert(1)>').includes('&lt;img'));
t('its handler is inert',
  !EVENT_HANDLER.test(liveMarkup(Markdown.render('<img src=x onerror=alert(1)>'))));

// Static guards on the allowlist. Weaker than behaviour, and the right shape for
// the regression that actually matters: somebody adding a handler attribute to a
// media tag, or dropping the src vetting, in a file six pages render through.
console.log();
console.log('=== the media allowlist has no handler in it ===');
{
  const src = fs.readFileSync(path.join(root, 'ystocker/static/markdown.js'), 'utf8');
  const allowed = src.slice(src.indexOf('const ALLOWED'), src.indexOf('DROP_ENTIRELY'));
  t('no on* attribute is allowed on anything', !/['"]on[a-z]+['"]/i.test(allowed));
  t('IMG carries a src', /IMG:\s*\[[^\]]*'src'/.test(allowed));
  t('every src goes through safeSrc', /name === 'src'[\s\S]{0,200}safeSrc\(/.test(src));
  t('safeSrc limits data: URIs to images', /\^data:image/.test(src));
  t('safeSrc allows no other scheme', /https\?:/.test(src));
  t('IFRAME is still dropped entirely', /DROP_ENTIRELY[\s\S]{0,200}IFRAME:\s*1/.test(src));
  t('SCRIPT and STYLE are still dropped entirely',
    /DROP_ENTIRELY[\s\S]{0,200}SCRIPT:\s*1/.test(src) &&
    /DROP_ENTIRELY[\s\S]{0,200}STYLE:\s*1/.test(src));
  t('no element may keep a style attribute', !/['"]style['"]/.test(allowed));
}

console.log('=== a refused media address is stated, not swallowed ===');
// Reported from the live page as "图片还是加载不出来". The picture had been a
// `cid:` reference — an inline mail attachment, which is not on this server and
// never will be — and the renderer skipped the attribute while keeping the
// element, leaving an empty picture frame with nothing anywhere saying why. An
// empty frame reads as "failed to load", which invites a retry; the truth was
// "can never load", which needs the sender changed.
//
// safeSrc is exported precisely so these rules are testable here: sanitize()
// needs DOMParser and this suite has none by design.
{
  const S = Markdown.safeSrc;
  t('cid: is refused',            S('cid:part1.abc@mail') === null);
  t('file: is refused',           S('file:///Users/me/x.png') === null);
  t('javascript: is refused',     S('javascript:alert(1)') === null);
  t('ftp: is refused',            S('ftp://h/f.png') === null);
  t('data:text/html is refused',  S('data:text/html;base64,AAA') === null);
  t('https is allowed',           S('https://i.ytimg.com/vi/a/hq.jpg') !== null);
  // Upgraded rather than merely allowed: this page is https, so an http image is
  // mixed content and Safari drops it with no notice at all.
  t('http is upgraded to https',  S('http://a.test/i.png') === 'https://a.test/i.png');
  t('https is left alone',        S('https://a.test/i.png') === 'https://a.test/i.png');
  t('the upgrade does not touch a path that merely contains http',
    S('/img/http-logo.png') === '/img/http-logo.png');
  t('data:image is allowed',      S('data:image/png;base64,AAA') !== null);
  t('a relative path is allowed', S('/static/x.png') === '/static/x.png');

  // safeHref stays *wider* than safeSrc, and deliberately so: an href is not
  // fetched until the reader clicks, so mailto belongs there and nowhere else.
  t('mailto is a link but never a src',
    Markdown.safeHref('mailto:a@b.test') !== null && S('mailto:a@b.test') === null);

  const src = fs.readFileSync(path.join(root, 'ystocker/static/markdown.js'), 'utf8');
  t('a refused media src emits a marker instead of the element',
    /md-src-refused/.test(src));
  t('the marker carries the scheme', /data-scheme/.test(src));
  t('the marker carries no prose — the page owns the copy',
    !/md-src-refused[\s\S]{0,400}(not supported|unsupported|cannot)/i.test(src));
  t('only media refusals are marked, not links',
    /const MEDIA = \{[^}]*IMG/.test(src) && !/MEDIA[^}]*\bA\b\s*:/.test(src));
  // The URL itself must not reach the markup: a refused src can be a tracking
  // address, and the scheme alone answers why it was refused.
  t('schemeOf returns only the scheme',
    /function schemeOf[\s\S]{0,400}m\[1\]\.toLowerCase\(\)/.test(src));
}

console.log('=== an href that is not a URL is not a link ===');
// Eighteen links in one day of digests were the literal string `完整URL` — a
// template placeholder the sender never substituted. It is a *valid* relative
// reference, so it rendered as a working anchor that resolved against our own
// origin and 404'd. Worse than not linking: it looks clickable, and the failure
// only shows up after the click on a page that looks like ours.
{
  const H = Markdown.safeHref;
  t('an unsubstituted placeholder is refused', H('完整URL') === null);
  t('a bare word is refused',                  H('TODO') === null);
  t('a rooted path is still a link',           H('/history/NVDA') === '/history/NVDA');
  t('a fragment is still a link',              H('#top') === '#top');
  t('a query is still a link',                 H('?tab=1') === '?tab=1');
  t('a bare host is still a link',             H('example.com/a') === 'example.com/a');
  t('https is untouched',                      H('https://a.test/x') === 'https://a.test/x');
  t('mailto is untouched',                     H('mailto:a@b.test') === 'mailto:a@b.test');
  t('javascript: is still refused',            H('javascript:alert(1)') === null);

  const src = fs.readFileSync(path.join(root, 'ystocker/static/markdown.js'), 'utf8');
  t('a refused link keeps its text and gains a marker',
    /md-href-refused/.test(src) && /tag === 'A'[\s\S]{0,300}copyInto\(node, to\)/.test(src));
}

console.log();
if (failures.length) {
  console.log(`RESULT: FAIL — ${failures.length}: ${failures.join(', ')}`);
  process.exit(1);
}
console.log(`RESULT: OK  (rendered ${html.length} chars)`);
