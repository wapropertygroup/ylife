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

      const el = document.createElement(tag.toLowerCase());
      for (const name of ALLOWED[tag]) {
        if (!node.hasAttribute(name)) continue;
        let value = node.getAttribute(name);
        if (name === 'href') {
          value = safeHref(value);
          if (value === null) continue;
          el.setAttribute('rel', 'noopener noreferrer nofollow');
          el.setAttribute('target', '_blank');
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
  const BLOCK_HTML = /^<(p|div|section|table|ul|ol|dl|pre|blockquote|h[1-6]|figure)\b/i;

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
  global.Markdown = { render: renderMd, escape: esc, inline: inline, sanitize: sanitize };
})(window);
