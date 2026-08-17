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
 * SAFETY: every leaf goes through esc() before any tag is added, so raw HTML
 * in model output is rendered as text and never executed. Assign the result
 * with innerHTML only -- never build markup by concatenating unescaped input.
 */
(function (global) {
  'use strict';

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function inline(s) {
    return esc(s)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  }
  function renderMd(md) {
    const lines = md.replace(/\r/g, '').split('\n');
    const out = [];
    let i = 0, listType = null;
    const closeList = () => { if (listType) { out.push(`</${listType}>`); listType = null; } };

    while (i < lines.length) {
      const line = lines[i];
      const t = line.trim();

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
             !/^(#{1,6}\s|>|\||[-*+]\s|\d+[.)]\s|---+$)/.test(lines[i].trim())) {
        para.push(lines[i].trim()); i++;
      }
      out.push(`<p>${para.map(inline).join('<br>')}</p>`);
    }
    closeList();
    return out.join('\n');
  }
  global.Markdown = { render: renderMd, escape: esc, inline: inline };
})(window);
