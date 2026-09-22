/**
 * ystocker markdown renderer
 * ~~~~~~~~~~~~~~~~~~~~~~~~~~
 * Small block-level Markdown -> HTML converter shared by the pages that show
 * LLM output (the research report on /history, the agent report on /agents).
 *
 * Deliberately not a CDN library. The pages already load Tailwind and Chart.js
 * from CDNs; adding marked.js + DOMPurify for this would be two more blocking
 * requests and a third-party XSS surface for a document we can convert in ~70
 * lines. It handles what these reports actually contain: headings, tables,
 * ordered/unordered lists, blockquotes, rules, code spans, bold and italic.
 *
 * SAFETY: model output is not trusted. Text goes through esc() before any tag is
 * added. Where HTML *is* honoured -- the models sometimes answer in HTML rather
 * than Markdown -- it is rebuilt against an allowlist rather than passed through:
 * see sanitize(). Assign a result with innerHTML only, and never build markup by
 * concatenating unescaped input.
 */
(function (global) {
  'use strict';

  // Elements kept when honouring HTML, mapped to the attributes each may keep.
  // Everything absent is unwrapped (its text survives, the element does not), so
  // a <font color> or <center> degrades to plain text instead of vanishing.
  const ALLOWED = {
    P: [], BR: [], HR: [], DIV: [], SECTION: [], SPAN: [],
    B: [], STRONG: [], I: [], EM: [], U: [], S: [], DEL: [], INS: [], MARK: [],
    CODE: [], PRE: [], KBD: [], SUB: [], SUP: [], SMALL: [],
    UL: [], OL: ['start'], LI: [], DL: [], DT: [], DD: [],
    TABLE: [], THEAD: [], TBODY: [], TFOOT: [], CAPTION: [], TR: [],
    TH: ['colspan', 'rowspan'], TD: ['colspan', 'rowspan'],
    H1: [], H2: [], H3: [], H4: [], H5: [], H6: [], BLOCKQUOTE: [],
    A: ['href'],
    // Media. `src` is vetted by safeSrc() exactly as `href` is by safeHref(),
    // and no handler attribute is in any of these lists, so there is nothing to
    // smuggle -- an <img onerror> loses the onerror and keeps the picture.
    //
    // A remote image is a tracking pixel: loading one tells whoever chose the
    // URL that this reader opened this page, from this address. Accepted
    // knowingly, because the alternative is that an emailed digest full of
    // charts renders as a column of alt text, and the content here is already
    // trusted enough to be shown at all.
    IMG: ['src', 'alt', 'width', 'height', 'title'],
    FIGURE: [], FIGCAPTION: [],
    VIDEO: ['src', 'poster', 'controls', 'width', 'height'],
    AUDIO: ['src', 'controls'],
    SOURCE: ['src', 'type'],
  };
  // Dropped with their contents. Their text is markup or code, not prose, so
  // unwrapping them would paste a stylesheet into the middle of a report.
  const DROP_ENTIRELY = { SCRIPT: 1, STYLE: 1, IFRAME: 1, OBJECT: 1, EMBED: 1,
                          TEMPLATE: 1, NOSCRIPT: 1, SVG: 1, MATH: 1, FORM: 1,
                          INPUT: 1, BUTTON: 1, SELECT: 1, TEXTAREA: 1, LINK: 1,
                          META: 1, BASE: 1, TITLE: 1 };
  const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);

  function safeHref(value) {
    // Scheme allowlist. A relative or fragment link is fine; anything with a
    // scheme must be one of these, which excludes javascript: and data:.
    const v = String(value || '').trim();
    if (!v || /^[a-z][a-z0-9+.-]*:/i.test(v)) {
      return /^(https?|mailto):/i.test(v) ? v : null;
    }
    return v;
  }

  function safeSrc(value) {
    // Narrower than safeHref on purpose. A src is fetched by the browser
    // without the reader clicking anything, so `data:` is limited to images
    // (a data: document would be same-origin-ish script) and nothing else with
    // a scheme is allowed through at all.
    const v = String(value || '').trim();
    if (!v) return null;
    if (/^data:image\/(png|jpe?g|gif|webp|svg\+xml);base64,/i.test(v)) return v;
    if (/^[a-z][a-z0-9+.-]*:/i.test(v)) {
      if (!/^https?:/i.test(v)) return null;
      // Upgraded, not merely allowed. This page is https, so an http image is
      // mixed content: Safari and older Chrome block it outright and the picture
      // vanishes with *no* notice — worse than the refusals above, which at least
      // say something. Current Chrome happens to auto-upgrade, so doing it here
      // only makes every browser behave the way one of them already does, and a
      // host that cannot serve https was failing in that browser anyway.
      //
      // Observed on a real emailed digest: one of seven images arrived as
      // http://p1.img.cctvpic.com/..., which loads fine over https.
      return v.replace(/^http:/i, 'https:');
    }
    return v;                                   // relative or protocol-less
  }

  // Tags whose src the browser fetches unprompted. A refusal on one of these is
  // worth reporting; a refused <a href> is not, because nothing was going to be
  // fetched and the text is still there to read.
  const MEDIA = { IMG: 1, VIDEO: 1, AUDIO: 1, SOURCE: 1 };

  function schemeOf(value) {
    // The scheme alone, never the rest of the URL. A refused src can be a
    // tracking address or a local file path, and neither belongs in markup this
    // page is about to render -- the reader needs to know *why* it was refused,
    // which the scheme answers on its own.
    const m = /^([a-z][a-z0-9+.-]*):/i.exec(String(value || '').trim());
    return m ? m[1].toLowerCase() : 'relative';
  }

  function copyInto(from, to) {
    for (let node = from.firstChild; node; node = node.nextSibling) {
      if (node.nodeType === 3) {                       // text
        to.appendChild(document.createTextNode(node.nodeValue));
        continue;
      }
      if (node.nodeType !== 1) continue;               // comments, CDATA, ...
      const tag = node.tagName.toUpperCase();
      if (has(DROP_ENTIRELY, tag)) continue;
      // hasOwnProperty, not a truthiness test: ALLOWED['CONSTRUCTOR'] would
      // otherwise inherit a value from Object.prototype and let a tag through.
      if (!has(ALLOWED, tag)) { copyInto(node, to); continue; }

      // A media element whose src is refused becomes a marker rather than an
      // empty box. Skipping the attribute and keeping the element -- which is
      // what this did -- renders a broken picture frame with nothing anywhere
      // saying why, and the reader reasonably reads that as "the image failed to
      // load". It is not a failure, it is a refusal, and those need different
      // responses: one is worth retrying and the other never will be.
      //
      // The marker carries the scheme and the alt text and *no prose*. The copy
      // belongs to the page, which has i18n; a sentence composed in here would
      // arrive in English on a Chinese page, which is a trap this codebase has
      // already paid for once.
      if (has(MEDIA, tag) && node.hasAttribute('src')
          && safeSrc(node.getAttribute('src')) === null) {
        const mark = document.createElement('span');
        mark.className = 'md-src-refused';
        mark.setAttribute('data-kind', tag.toLowerCase());
        mark.setAttribute('data-scheme', schemeOf(node.getAttribute('src')));
        const alt = node.getAttribute('alt');
        if (alt) mark.setAttribute('data-alt', alt);
        to.appendChild(mark);
        continue;
      }

      const el = document.createElement(tag.toLowerCase());
      for (const name of ALLOWED[tag]) {
        if (!node.hasAttribute(name)) continue;
        let value = node.getAttribute(name);
        if (name === 'href') {
          value = safeHref(value);
          if (value === null) continue;
          el.setAttribute('rel', 'noopener noreferrer nofollow');
          el.setAttribute('target', '_blank');
        } else if (name === 'src') {
          value = safeSrc(value);
          if (value === null) continue;
          if (tag === 'IMG') {
            // Not decorative: a referrer would carry the page URL to whoever
            // chose the image, and lazy loading keeps a long digest from
            // fetching forty pictures at once.
            el.setAttribute('loading', 'lazy');
            el.setAttribute('referrerpolicy', 'no-referrer');
          }
        } else if (name === 'controls') {
          el.setAttribute('controls', '');      // boolean: value is irrelevant
          continue;
        }
        el.setAttribute(name, value);
      }
      copyInto(node, el);
      to.appendChild(el);
    }
  }

  /**
   * Rebuild untrusted HTML from an allowlist and return the result as a string.
   *
   * DOMParser is used rather than assigning to a live element: it produces an
   * inert document, so no script runs and no <img onerror> fires while parsing.
   * Nothing from the input is carried over -- every element and attribute in the
   * output was created here -- so serialising it back to a string is safe.
   */
  function sanitize(html) {
    try {
      const doc = new DOMParser().parseFromString(
        '<body><div id="ystk-root">' + String(html) + '</div></body>', 'text/html');
      const src = doc.getElementById('ystk-root');
      if (!src) return esc(html);
      const out = document.createElement('div');
      copyInto(src, out);
      return out.innerHTML;
    } catch (_) {
      return esc(html);      // no DOMParser: show it as text rather than risk it
    }
  }

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // Inline tags restored after escaping, so a <b> or <br> inside otherwise
  // Markdown prose is honoured. Only the attribute-free form is matched, so
  // there is no way to smuggle an event handler through: <b onclick=..> stays
  // escaped and is shown as text.
  const INLINE_HTML = /&lt;(\/?)(b|strong|i|em|u|s|del|ins|mark|code|sub|sup|small|br)\s*\/?&gt;/gi;

  function inline(s) {
    return esc(s)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
      .replace(INLINE_HTML, (m, slash, tag) => '<' + slash + tag.toLowerCase() + '>');
  }

  // A line opening a block-level element. Models that answer in HTML emit whole
  // <p>/<table> blocks, and feeding those to the Markdown paragraph rule would
  // wrap each <tr> in its own <p>.
  // Includes the document openers, which is what a forwarded email starts with:
  // `<!DOCTYPE html>` matched none of the tags below, so an entire HTML mail took
  // the Markdown path and printed its own <html>/<head>/<body> as escaped text
  // before dropping every <img> the same way. Observed on a real emailed digest.
  //
  // Still decided on the *first* tag rather than "contains HTML anywhere", which
  // is what keeps a Markdown report with one stray <div> on the Markdown path
  // instead of silently losing all of its formatting.
  const BLOCK_HTML =
    /^(<!doctype\s|<(html|head|body|article|main|header|footer|p|div|section|table|tbody|thead|tr|td|ul|ol|dl|pre|blockquote|h[1-6]|figure)\b)/i;
  // Deliberately NOT here: `img`, `center`, `font`. A body *opening* with a bare
  // <img> is the shape an injected payload takes, not the shape a document
  // takes, and routing it to sanitize() would render the picture (stripped of
  // its onerror, but rendered) where it is currently shown as text. An email
  // opens with <!doctype>, <html> or <table>; none of it needs a lone image to
  // be a block opener.

  function renderMd(md) {
    // A body that *opens* with a block-level tag is an HTML answer, not Markdown
    // with some HTML in it, so it is honoured whole. Deciding on the first tag
    // rather than "contains HTML anywhere" keeps a Markdown report that happens
    // to include one stray <div> on the Markdown path instead of silently
    // dropping all of its formatting.
    if (BLOCK_HTML.test(String(md).trim())) return sanitize(md);

    const lines = md.replace(/\r/g, '').split('\n');
    const out = [];
    let i = 0, listType = null;
    const closeList = () => { if (listType) { out.push(`</${listType}>`); listType = null; } };

    while (i < lines.length) {
      const line = lines[i];
      const t = line.trim();

      // An HTML block: consume to its matching close tag and hand the whole
      // thing to the allowlist. Counted rather than matched on the first close,
      // so a <table> containing <tr> nests correctly; an unclosed block runs to
      // the end of the section, which DOMParser then repairs.
      if (BLOCK_HTML.test(t)) {
        closeList();
        const name = t.match(BLOCK_HTML)[1].toLowerCase();
        const open = new RegExp('<' + name + '(?=[\\s/>])', 'gi');
        const close = new RegExp('</' + name + '\\s*>', 'gi');
        const chunk = [];
        let depth = 0;
        while (i < lines.length) {
          const raw = lines[i];
          chunk.push(raw);
          i++;
          depth += (raw.match(open) || []).length;
          depth -= (raw.match(close) || []).length;
          if (depth <= 0) break;
        }
        out.push(sanitize(chunk.join('\n')));
        continue;
      }

      // Table: header row followed by a |---|---| separator
      if (t.startsWith('|') && i + 1 < lines.length && /^\|[\s:|-]+\|$/.test(lines[i + 1].trim())) {
        closeList();
        const cells = r => r.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
        const head = cells(t);
        i += 2;
        const body = [];
        while (i < lines.length && lines[i].trim().startsWith('|')) { body.push(cells(lines[i])); i++; }
        out.push('<table><thead><tr>' + head.map(h => `<th>${inline(h)}</th>`).join('') +
                 '</tr></thead><tbody>' +
                 body.map(r => '<tr>' + r.map(c => `<td>${inline(c)}</td>`).join('') + '</tr>').join('') +
                 '</tbody></table>');
        continue;
      }
      const h = t.match(/^(#{1,6})\s+(.*)$/);
      if (h) {
        closeList();
        const lvl = Math.min(h[1].length, 4);
        out.push(`<h${lvl}>${inline(h[2])}</h${lvl}>`);
        i++; continue;
      }
      if (/^(---+|\*\*\*+|___+)$/.test(t)) { closeList(); out.push('<hr>'); i++; continue; }
      if (t.startsWith('>')) {
        closeList();
        const quote = [];
        while (i < lines.length && lines[i].trim().startsWith('>')) {
          quote.push(lines[i].trim().replace(/^>\s?/, '')); i++;
        }
        out.push(`<blockquote>${quote.map(inline).join('<br>')}</blockquote>`);
        continue;
      }
      const ul = t.match(/^[-*+]\s+(.*)$/);
      const ol = t.match(/^\d+[.)]\s+(.*)$/);
      if (ul || ol) {
        const want = ul ? 'ul' : 'ol';
        if (listType !== want) { closeList(); out.push(`<${want}>`); listType = want; }
        // Render "[ ]" / "[x]" checkboxes as plain glyphs
        const body = (ul ? ul[1] : ol[1]).replace(/^\[( |x|X)\]\s*/, (m, c) =>
          (c.toLowerCase() === 'x' ? '☑ ' : '☐ '));
        out.push(`<li>${inline(body)}</li>`);
        i++; continue;
      }
      if (!t) { closeList(); i++; continue; }
      closeList();
      // Always consume the current line first: while streaming, a table header
      // row can arrive before its |---| separator, and that line matches the
      // block-start guard below — without this the loop would never advance.
      const para = [t];
      i++;
      while (i < lines.length && lines[i].trim() &&
             !BLOCK_HTML.test(lines[i].trim()) &&
             !/^(#{1,6}\s|>|\||[-*+]\s|\d+[.)]\s|---+$)/.test(lines[i].trim())) {
        para.push(lines[i].trim()); i++;
      }
      out.push(`<p>${para.map(inline).join('<br>')}</p>`);
    }
    closeList();
    return out.join('\n');
  }
  // safeSrc/safeHref are exported so the *refusal rules* can be tested without a
  // DOM. sanitize() needs DOMParser and the test suite deliberately has none.
  global.Markdown = { render: renderMd, escape: esc, inline: inline,
                      sanitize: sanitize, safeSrc: safeSrc, safeHref: safeHref };
})(window);
