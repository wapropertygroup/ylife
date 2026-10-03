/* predictions.js — /predictions, and the "three ways" card on /fedwatch.
 *
 * Everything here is drawn from /api/predictions*, so all of it is text the
 * server took from Polymarket, Kalshi or a model: every value goes through
 * esc() before it touches innerHTML, and the only hrefs are the venue URLs the
 * server built and source links it already filtered to https.
 *
 * `const PM` is script-scoped, like i18n.js's `const I18n`: the page's own
 * inline script reaches it by name, and nothing should look for window.PM.
 *
 * Strings composed here are re-read on `i18n:langchange` (render() runs again),
 * since I18n.apply() cannot reach text a script wrote.
 */
const PM = (() => {
  'use strict';

  // ── Small helpers ─────────────────────────────────────────────────────
  const L = (key, fallback) => {
    try {
      const v = typeof I18n !== 'undefined' ? I18n.t(key) : null;
      return v || fallback;
    } catch (_) { return fallback; }
  };
  const lang = () => (typeof I18n !== 'undefined' && I18n.getLang) ? I18n.getLang() : 'en';
  const fill = (s, vars) => String(s).replace(/\{(\w+)\}/g, (m, k) => (k in vars ? vars[k] : m));
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  /** A probability as a card shows it: whole percents, with the ends kept honest. */
  function pct(p) {
    if (p == null || isNaN(p)) return '—';
    const v = p * 100;
    if (v > 0 && v < 1) return '<1%';
    if (v < 100 && v > 99) return '>99%';
    return Math.round(v) + '%';
  }
  /** One decimal, for the tables where two sources sit side by side. */
  function pct1(p) {
    if (p == null || isNaN(p)) return '—';
    return (p * 100).toFixed(1) + '%';
  }
  /** A change in probability, in points. */
  function pp(d, digits = 1) {
    if (d == null || isNaN(d)) return '';
    const v = d * 100;
    return (v > 0 ? '+' : v < 0 ? '−' : '±') + Math.abs(v).toFixed(digits) + 'pp';
  }
  function money(v) {
    if (v == null || isNaN(v)) return '—';
    const a = Math.abs(v);
    if (a >= 1e9) return '$' + (v / 1e9).toFixed(1) + 'B';
    if (a >= 1e6) return '$' + (v / 1e6).toFixed(1) + 'M';
    if (a >= 1e3) return '$' + Math.round(v / 1e3) + 'k';
    return '$' + Math.round(v);
  }
  function count(v) {
    return money(v).replace('$', '');
  }
  /** "closes in 25 days", from an ISO timestamp. */
  function closesIn(iso, now) {
    if (!iso) return '';
    const ms = new Date(iso).getTime() - (now || Date.now());
    if (isNaN(ms)) return '';
    if (ms <= 0) return L('pm.closed', 'closed');
    const h = ms / 3600000;
    if (h < 48) return fill(L('pm.in_hours', 'closes in {n}h'), { n: Math.max(1, Math.round(h)) });
    return fill(L('pm.in_days', 'closes in {n} days'), { n: Math.round(h / 24) });
  }
  /** "3h", "2 天" -- a span, for "{t} ago". */
  function span(seconds) {
    const s = Math.max(0, seconds);
    if (s < 3600) return fill(L('pm.t_min', '{n}m'), { n: Math.max(1, Math.round(s / 60)) });
    if (s < 172800) return fill(L('pm.t_hour', '{n}h'), { n: Math.round(s / 3600) });
    return fill(L('pm.t_day', '{n}d'), { n: Math.round(s / 86400) });
  }
  function venue(platform) {
    if (platform === 'polymarket') return 'Polymarket';
    if (platform === 'kalshi') return 'Kalshi';
    return L('pm.futures', 'Futures (ZQ)');
  }
  function badge(platform) {
    const cls = platform === 'polymarket' || platform === 'kalshi' ? platform : 'futures';
    return `<span class="pm-badge pm-badge-${cls}">${esc(venue(platform))}</span>`;
  }
  function topicName(t) { return L('pm.topic.' + t, t); }
  function dateLabel(iso) {
    const d = new Date(String(iso).slice(0, 10) + 'T12:00:00Z');
    if (isNaN(d)) return String(iso || '');
    return d.toLocaleDateString(lang() === 'zh' ? 'zh-CN' : 'en-US',
      { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
  }
  async function getJson(url, init) {
    try {
      const r = await fetch(url, Object.assign({ credentials: 'same-origin' }, init || {}));
      let body = {};
      try { body = await r.json(); } catch (_) { body = {}; }
      return { status: r.status, body };
    } catch (e) {
      return { status: 0, body: { error: String(e && e.message || e) } };
    }
  }

  // ── The Fed, three ways ───────────────────────────────────────────────
  const BUCKETS = [['-2', 'pm.fed.cut50', 'Cut ≥50'], ['-1', 'pm.fed.cut25', 'Cut 25'],
                   ['0', 'pm.fed.hold', 'Hold'], ['1', 'pm.fed.hike25', 'Hike 25'],
                   ['2', 'pm.fed.hike50', 'Hike ≥50']];
  const bucketName = b => { const row = BUCKETS.find(x => x[0] === String(b)); return row ? L(row[1], row[2]) : String(b); };

  function renderFed(rows) {
    if (!rows || !rows.length) {
      return `<p class="pm-empty">${esc(L('pm.fed_empty', 'No upcoming meeting is listed on two sources yet.'))}</p>`;
    }
    return '<div class="pm-fed">' + rows.map(row => {
      const modal = s => BUCKETS.reduce((best, b) => ((s.buckets[b[0]] || 0) > (s.buckets[best] || 0) ? b[0] : best), '0');
      const verdict = row.agree
        ? fill(esc(L('pm.fed.agree', "Every source's likeliest: {outcome}")), { outcome: `<b>${esc(bucketName(modal(row.sources[0])))}</b>` })
        : esc(L('pm.fed.disagree', 'Sources disagree on the likeliest outcome'));
      const gap = row.spread_pp > 0
        ? ' · ' + esc(fill(L('pm.fed.gap', 'widest gap {pp}pp on {outcome}'),
                           { pp: row.spread_pp.toFixed(1), outcome: bucketName(row.spread_bucket) }))
        : '';
      const head = BUCKETS.map(b =>
        `<th${b[0] === row.spread_bucket && row.spread_pp >= 3 ? ' class="pm-gap-col"' : ''}>${esc(L(b[1], b[2]))}</th>`).join('');
      const body = row.sources.map(s => {
        const name = s.url
          ? `<a class="pm-fed-src" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${badge(s.source)}</a>`
          : `<span class="pm-fed-src">${badge(s.source)}</span>`;
        const title = s.overround
          ? ` title="${esc(fill(L('pm.overround', 'Quoted prices sum to {sum}%'), { sum: (s.overround * 100).toFixed(1) }))}"`
          : '';
        const cells = BUCKETS.map(b => {
          const p = s.buckets[b[0]] || 0;
          if (p <= 0) return '<td class="pm-zero">—</td>';
          const alpha = Math.min(0.6, p * 0.6).toFixed(3);
          return `<td style="background:rgba(var(--pm-heat),${alpha})">${pct1(p)}</td>`;
        }).join('');
        const exp = (s.expected_bp > 0 ? '+' : '') + Number(s.expected_bp).toFixed(1) + 'bp';
        return `<tr${title}><th>${name}</th>${cells}<td class="pm-exp">${esc(exp)}</td></tr>`;
      }).join('');
      return `<div class="pm-fed-meeting">
        <div class="pm-fed-head"><span class="pm-fed-date">${esc(dateLabel(row.date))}</span>
          <span class="pm-fed-verdict">${verdict}${gap}</span></div>
        <div class="pm-fed-scroll"><table class="pm-fed-table">
          <thead><tr><th></th>${head}<th>${esc(L('pm.fed.expected', 'Expected'))}</th></tr></thead>
          <tbody>${body}</tbody></table></div></div>`;
    }).join('') + '</div>';
  }

  // ── Movers ────────────────────────────────────────────────────────────
  function renderMovers(rows) {
    if (!rows || !rows.length) {
      return `<p class="pm-empty">${esc(L('pm.movers_empty', 'Nothing moved more than 3 points today.'))}</p>`;
    }
    return '<div class="pm-movers">' + rows.map(m => {
      const dir = m.d1 > 0 ? 'pm-up' : 'pm-down';
      return `<a class="pm-mover" href="${esc(m.url)}" target="_blank" rel="noopener noreferrer">
        <span class="pm-mover-q">${badge(m.platform)} ${esc(m.title)}</span>
        <span class="pm-mover-row"><span class="pm-mover-label">${esc(m.label)} · ${pct(m.p)}</span>
          <span class="pm-mover-move ${dir}">${esc(pp(m.d1))}</span></span></a>`;
    }).join('') + '</div>';
  }

  // ── The board ─────────────────────────────────────────────────────────
  function biggestMove(ev) {
    return (ev.outcomes || []).reduce((m, o) => (o.d1 != null && Math.abs(o.d1) > m ? Math.abs(o.d1) : m), 0);
  }

  /** Filter and order the board. Pure, so the node check can pin it. */
  function filterSort(events, opts) {
    const o = opts || {};
    const q = String(o.q || '').trim().toLowerCase();
    const out = (events || []).filter(ev =>
      (!o.topic || o.topic === 'all' || ev.topic === o.topic)
      && (!o.platform || o.platform === 'all' || ev.platform === o.platform)
      && (!q || String(ev.title || '').toLowerCase().includes(q)
             || (ev.outcomes || []).some(x => String(x.label || '').toLowerCase().includes(q))));
    const t = iso => { const v = new Date(iso).getTime(); return isNaN(v) ? Infinity : v; };
    if (o.sort === 'closing') out.sort((a, b) => t(a.closes) - t(b.closes));
    else if (o.sort === 'moved') out.sort((a, b) => biggestMove(b) - biggestMove(a));
    else out.sort((a, b) => (b.volume_24h || 0) - (a.volume_24h || 0));
    return out;
  }

  function topicCounts(events) {
    const n = {};
    (events || []).forEach(ev => { n[ev.topic] = (n[ev.topic] || 0) + 1; });
    return n;
  }

  const SHOWN = 4;

  /** The rows a collapsed card shows. The stored order is the reading order
   *  (strikes low to high, names by likelihood), but the first four of it are
   *  not always the four worth seeing: a bucketed field's lowest buckets are
   *  often all under 1%, and a ladder's interesting rungs are the ones near
   *  50%. Pure, so the node check can pin it. */
  function visibleOutcomes(ev, expanded) {
    const rows = ev.outcomes || [];
    if (expanded || rows.length <= SHOWN) return rows;
    if (ev.kind === 'ladder') {
      let mid = 0;
      rows.forEach((o, i) => { if (Math.abs((o.p || 0) - 0.5) < Math.abs((rows[mid].p || 0) - 0.5)) mid = i; });
      const start = Math.max(0, Math.min(mid - Math.floor(SHOWN / 2), rows.length - SHOWN));
      return rows.slice(start, start + SHOWN);
    }
    if (ev.kind === 'exclusive' && ev.numeric) {
      const keep = new Set(rows.map((o, i) => [o.p || 0, i]).sort((a, b) => b[0] - a[0]).slice(0, SHOWN).map(x => x[1]));
      return rows.filter((_, i) => keep.has(i));
    }
    return rows.slice(0, SHOWN);
  }

  function renderOutcomes(ev, expanded) {
    const rows = ev.outcomes || [];
    const shown = visibleOutcomes(ev, expanded);
    const li = shown.map(o => {
      const w = Math.max(0, Math.min(100, (o.p || 0) * 100)).toFixed(1);
      const d = o.d1 != null && Math.abs(o.d1) >= 0.005
        ? `<span class="pm-od ${o.d1 > 0 ? 'pm-up' : 'pm-down'}">${esc(pp(o.d1))}</span>`
        : '<span class="pm-od"></span>';
      return `<li class="pm-outcome"><span class="pm-olabel" title="${esc(o.label)}">${esc(o.label)}</span>
        <span class="pm-obar"><i style="width:${w}%"></i></span><span class="pm-op">${pct(o.p)}</span>${d}</li>`;
    }).join('');
    let more = '';
    if (rows.length > SHOWN) {
      more = `<button type="button" class="pm-more-btn" data-pm-more>${esc(expanded
        ? L('pm.less', 'Show fewer')
        : fill(L('pm.more', 'Show all {n}'), { n: rows.length }))}</button>`;
    }
    let partial = '';
    if ((ev.n_outcomes || 0) > rows.length && (expanded || rows.length <= SHOWN)) {
      partial = `<p class="pm-note">${esc(fill(L('pm.of_total', '{shown} of {total} outcomes shown'),
        { shown: rows.length, total: ev.n_outcomes }))}</p>`;
    }
    return `<ul class="pm-outcomes">${li}</ul>${more}${partial}`;
  }

  function renderCard(ev, ctx) {
    const c = ctx || {};
    const ready = (c.reads || new Set()).has(ev.key);
    const depth = ev.platform === 'kalshi'
      ? `<span title="${esc(L('pm.kalshi_contracts', 'Kalshi counts $1 contracts'))}">${esc(fill(L('pm.open_interest', 'open interest {v}'), { v: count(ev.open_interest) }))}</span>`
      : (ev.liquidity != null ? `<span>${esc(fill(L('pm.liquidity', 'liquidity {v}'), { v: money(ev.liquidity) }))}</span>` : '');
    const sub = ev.subtitle ? `<span class="pm-q-sub">${esc(ev.subtitle)}</span>` : '';
    const btn = `<button type="button" class="pm-ai-btn" data-pm-read${ready ? ' data-ready="1"' : ''}
        aria-expanded="${c.open ? 'true' : 'false'}">${esc(ready ? L('pm.ai_ready', '✦ AI read ready') : L('pm.ai_btn', '✦ AI read'))}</button>`;
    return `<article class="pm-card" data-key="${esc(ev.key)}"${c.open ? ' data-open="1"' : ''}>
      <div class="pm-card-top">${badge(ev.platform)}<span>${esc(topicName(ev.topic))}</span>
        <span class="pm-closes">${esc(closesIn(ev.closes, c.now))}</span></div>
      <h3 class="pm-q"><a href="${esc(ev.url)}" target="_blank" rel="noopener noreferrer"
          title="${esc(fill(L('pm.open_venue', 'Open on {venue} ↗'), { venue: venue(ev.platform) }))}">${esc(ev.title)}</a>${sub}</h3>
      ${renderOutcomes(ev, c.expanded)}
      <div class="pm-card-foot">
        ${ev.volume_24h != null ? `<span>${esc(fill(L('pm.vol_24h', '{v} today'), { v: money(ev.volume_24h) }))}</span>` : ''}
        <span>${esc(fill(L('pm.vol_total', '{v} total'), { v: money(ev.volume) }))}</span>
        ${depth}${btn}
      </div>
      <div class="pm-ai" data-pm-panel${c.open ? '' : ' hidden'}>${c.open ? (c.panel || '') : ''}</div>
    </article>`;
  }

  // ── The AI read ───────────────────────────────────────────────────────
  const PARSE_ERRORS = new Set(['no_json', 'bad_json', 'missing', 'sum', 'range']);

  function renderRead(state, ev, ctx) {
    const c = ctx || {};
    const st = state || { status: 'none' };
    if (st.status === 'loading') return '<p class="pm-wait"><span class="pm-spin"></span></p>';
    const parts = [];
    const read = st.read;

    if (read) parts.push(renderReadBody(read, ev, c));

    if (st.status === 'running') {
      parts.push(`<p class="pm-thinking"><i></i><i></i><i></i> ${esc(L('pm.ai_running',
        'Reading the contract and the latest news — usually 15 to 40 seconds…'))}</p>`);
    } else if (st.status === 'slow') {
      parts.push(`<div class="pm-ai-actions"><span>${esc(L('pm.ai_slow', 'Still working. Check back in a minute.'))}</span>
        <button type="button" class="pm-ai-btn" data-pm-check>${esc(L('pm.ai_check', 'Check again'))}</button></div>`);
    } else if (st.status === 'error' || st.status === 'refused') {
      parts.push(`<div class="pm-ai-actions"><span class="pm-down">${esc(errorText(st))}</span>${
        st.status === 'error' && c.canAsk ? `<button type="button" class="pm-ai-btn" data-pm-ask>${esc(L('pm.ai_ask_again', 'Ask again'))}</button>` : ''}</div>`);
    } else if (!read || (st.status === 'done' && !st.fresh)) {
      if (!read) {
        parts.push(`<p class="pm-ai-head"><span class="pm-ai-title">${esc(L('pm.ai_title', '✦ AI read'))}</span></p>
          <p class="pm-ai-summary">${esc(L('pm.ai_intro', 'An AI forecaster researches the question with Google Search and gives its own probability for each outcome, set beside the market\'s price. Every read is recorded and scored when the market resolves.'))}</p>`);
      }
      if (!c.enabled) {
        parts.push(`<p class="pm-muted">${esc(L('pm.ai_off', 'AI reads are switched off on this site.'))}</p>`);
      } else if (!c.signedIn) {
        const next = encodeURIComponent(location.pathname + location.search);
        parts.push(`<div class="pm-ai-actions"><a class="pm-ai-btn" href="/login?next=${next}">${esc(L('pm.ai_signin', 'Sign in to ask for an AI read'))}</a></div>`);
      } else {
        parts.push(`<div class="pm-ai-actions"><button type="button" class="pm-ai-btn" data-pm-ask>${esc(read
          ? L('pm.ai_ask_again', 'Ask again') : L('pm.ai_ask', 'Ask for an AI read'))}</button></div>`);
      }
    }

    if (st.rules) {
      parts.push(`<details><summary>${esc(L('pm.ai_rules', 'Resolution rules'))}</summary><p>${esc(st.rules)}</p></details>`);
    }
    return parts.join('');
  }

  function errorText(st) {
    if (st.status === 'refused') {
      if (st.reason === 'user') {
        return fill(L('pm.ai_err_quota_user', "You have used today's AI reads ({limit}). They reset at midnight Pacific time."),
          { limit: (st.quota && st.quota.limit) || '' });
      }
      if (st.reason === 'global') return L('pm.ai_err_quota_global', "Today's AI reads for the whole site are used up. They reset at midnight Pacific time.");
      if (st.reason === 'forbidden') return L('pm.ai_err_forbidden', 'AI reads are limited to approved accounts.');
      if (st.reason === 'auth') return L('pm.ai_signin', 'Sign in to ask for an AI read');
      if (st.reason === 'disabled') return L('pm.ai_off', 'AI reads are switched off on this site.');
      return L('pm.ai_err_generic', 'Could not start an AI read.');
    }
    if (PARSE_ERRORS.has(st.error)) return L('pm.ai_err_parse', "The model's answer could not be read as a forecast. Try again.");
    return L('pm.ai_err_model', 'The model did not answer. Try again in a minute.');
  }

  function renderReadBody(read, ev, c) {
    const now = (c && c.now) || Date.now();
    const age = (now / 1000) - (read.created_ts || 0);
    const side = (lang() === 'zh' && read.zh && read.zh.summary) ? read.zh : (read.en || {});
    const conf = read.confidence
      ? esc(fill(L('pm.ai_conf', 'confidence: {c}'), { c: L('pm.ai_conf.' + read.confidence, read.confidence) }))
      : '';
    const evidence = read.evidence_date ? esc(fill(L('pm.ai_evidence', 'evidence to {d}'), { d: read.evidence_date })) : '';
    const head = [`<span class="pm-ai-title">${esc(L('pm.ai_title', '✦ AI read'))}</span>`,
                  `<span>${esc(read.model || '')}</span>`,
                  `<span>${esc(fill(L('pm.ai_ago', '{t} ago'), { t: span(age) }))}</span>`, conf, evidence]
      .filter(Boolean).join('<span aria-hidden="true">·</span>');

    const rows = (read.outcomes || []).map(o => {
      const g = o.gap_pp;
      const cls = g >= 0.5 ? 'pm-up' : g <= -0.5 ? 'pm-down' : 'pm-muted';
      const gtxt = (g > 0 ? '+' : g < 0 ? '−' : '±') + Math.abs(g).toFixed(1) + 'pp';
      return `<tr><td>${esc(o.label)}</td><td>${pct1(o.market_p)}</td><td>${pct1(o.ai_p)}</td>
        <td class="${cls}">${esc(gtxt)}</td></tr>`;
    }).join('');
    const list = items => (items || []).map(x => `<li>${esc(x)}</li>`).join('');
    const sources = (read.sources || []).map(s => s.uri
      ? `<li><a class="pm-link" href="${esc(s.uri)}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a></li>`
      : `<li>${esc(s.title)}</li>`).join('');
    const queries = (read.queries || []).map(q => `<li>${esc(q)}</li>`).join('');
    const old = age > 12 * 3600
      ? `<p class="pm-note">${esc(fill(L('pm.ai_old', 'This read is from {t} ago; the market may have moved since.'), { t: span(age) }))}</p>`
      : '';
    const partial = (read.kind === 'exclusive' && (read.n_outcomes || 0) > (read.outcomes || []).length)
      ? `<p class="pm-note">${esc(fill(L('pm.ai_partial', 'These are the {n} most likely of {total} outcomes; the rest of the field takes the remainder.'),
          { n: (read.outcomes || []).length, total: read.n_outcomes }))}</p>`
      : '';

    return `<div class="pm-ai-head">${head}</div>${old}
      <table class="pm-ai-table"><thead><tr><th>${esc(L('pm.ai_col_outcome', 'Outcome'))}</th>
        <th>${esc(L('pm.ai_col_market', 'Market'))}</th><th>${esc(L('pm.ai_col_ai', 'AI'))}</th>
        <th>${esc(L('pm.ai_col_gap', 'Gap'))}</th></tr></thead><tbody>${rows}</tbody></table>${partial}
      ${side.summary ? `<p class="pm-ai-summary">${esc(side.summary)}</p>` : ''}
      <div class="pm-ai-cols">
        ${(side.drivers || []).length ? `<div><h4>${esc(L('pm.ai_drivers', 'What drives it'))}</h4><ul>${list(side.drivers)}</ul></div>` : ''}
        ${(side.watch || []).length ? `<div><h4>${esc(L('pm.ai_watch', 'What would change it'))}</h4><ul>${list(side.watch)}</ul></div>` : ''}
      </div>
      ${sources ? `<details><summary>${esc(fill(L('pm.ai_sources', 'Sources ({n})'), { n: (read.sources || []).length }))}</summary><ul>${sources}</ul></details>` : ''}
      ${queries ? `<details><summary>${esc(fill(L('pm.ai_queries', 'Searches it ran ({n})'), { n: (read.queries || []).length }))}</summary><ul>${queries}</ul></details>` : ''}
      <p class="pm-ai-foot">${esc(L('pm.ai_foot', 'Recorded in the track record below with the market prices beside it, and scored when the market resolves. An input to your own estimate, not an order ticket.'))}</p>`;
  }

  // ── Track record ──────────────────────────────────────────────────────
  function renderLedger(data) {
    const s = (data && data.summary) || {};
    const rows = (data && data.rows) || [];
    if (!s.recorded) {
      return `<p class="pm-empty">${esc(L('pm.ledger_none', 'No AI reads yet. The first one asked for will appear here, and be scored when its market resolves.'))}</p>`;
    }
    const fmt = v => (v == null ? '—' : Number(v).toFixed(3));
    const stat = (k, fb, v) => `<div class="pm-stat"><div class="pm-stat-k">${esc(L(k, fb))}</div><div class="pm-stat-v">${v}</div></div>`;
    const better = s.settled
      ? esc(fill(L('pm.ledger_better_v', '{a} of {n}'), { a: s.ai_better, n: s.settled }))
      : '—';
    const stats = '<div class="pm-score">'
      + stat('pm.ledger_recorded', 'Reads recorded', esc(s.recorded))
      + stat('pm.ledger_settled', 'Resolved', esc(s.settled))
      + stat('pm.ledger_brier_ai', 'AI Brier', esc(fmt(s.brier_ai)))
      + stat('pm.ledger_brier_mkt', 'Market Brier', esc(fmt(s.brier_market)))
      + stat('pm.ledger_better', 'AI beat the market', better)
      + '</div>';
    const wait = s.settled ? '' : `<p class="pm-note">${esc(L('pm.ledger_wait', 'No read has resolved yet — scores appear as markets settle.'))}</p>`;
    const body = rows.map(r => {
      const top = (r.outcomes || []).reduce((a, b) => ((b.ai_p || 0) > (a ? a.ai_p || 0 : -1) ? b : a), null);
      const call = top ? `${esc(top.label)}: AI ${pct1(top.ai_p)} / ${esc(L('pm.ai_col_market', 'Market'))} ${pct1(top.market_p)}` : '';
      let status = esc(L('pm.status_' + (r.status || 'open'), r.status || 'open'));
      if (r.status === 'settled') {
        const win = r.brier_ai < r.brier_market ? 'pm-up' : r.brier_ai > r.brier_market ? 'pm-down' : '';
        status += ` · <span class="${win}">AI ${fmt(r.brier_ai)}</span> / ${fmt(r.brier_market)}`;
      }
      return `<tr><td><a class="pm-link" href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.title)}</a>
          <span class="pm-q-sub">${esc(venue(r.platform))}${r.subtitle ? ' · ' + esc(r.subtitle) : ''}</span></td>
        <td>${esc(dateLabel(r.created_at))}</td><td>${call}</td><td>${status}</td></tr>`;
    }).join('');
    return stats + wait + `<div class="pm-ledger-scroll"><table class="pm-ledger"><thead><tr>
      <th>${esc(L('pm.ledger_col_market', 'Market'))}</th><th>${esc(L('pm.ledger_col_read', 'Read on'))}</th>
      <th>${esc(L('pm.ledger_col_call', 'Likeliest outcome, AI vs market'))}</th>
      <th>${esc(L('pm.ledger_col_status', 'Result (Brier)'))}</th></tr></thead><tbody>${body}</tbody></table></div>`;
  }

  // ── The page ──────────────────────────────────────────────────────────
  function boot(opts) {
    const $ = id => document.getElementById(id);
    const S = {
      data: null, topic: 'all', platform: 'all', sort: 'hot', q: '',
      expanded: new Set(), open: null, panels: {}, lastTouch: 0, ledger: null,
      enabled: !!(opts && opts.aiEnabled), signedIn: false,
    };
    const touch = () => { S.lastTouch = Date.now(); };

    function stamp() {
      const el = $('pmStamp');
      if (!el || !S.data) return;
      const when = new Date((S.data.fetched_at || 0) * 1000);
      const time = when.toLocaleString(lang() === 'zh' ? 'zh-CN' : 'en-US',
        { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
      el.textContent = `${L('pm.as_of', 'Odds as of')} ${time} · ${S.data.stale
        ? L('pm.stale', 'refreshing…') : L('pm.refresh_every', 'refreshed every 10 minutes')}`;
      const notes = [];
      Object.entries(S.data.sources || {}).forEach(([v, info]) => {
        if (info && info.stale_since) {
          notes.push(fill(L('pm.venue_stale', '{venue} did not answer; its odds are from {age} ago.'),
            { venue: venue(v), age: span(Date.now() / 1000 - info.stale_since) }));
        }
      });
      const warn = $('pmVenueNote');
      if (warn) { warn.textContent = notes.join(' '); warn.classList.toggle('pm-hidden', !notes.length); }
    }

    function renderTopics() {
      const el = $('pmTopics');
      if (!el || !S.data) return;
      const n = topicCounts(S.data.events);
      const topics = ['all'].concat((S.data.topics || []).filter(t => n[t]));
      el.innerHTML = topics.map(t => {
        const label = t === 'all' ? L('pm.all', 'All') : topicName(t);
        const num = t === 'all' ? (S.data.events || []).length : n[t];
        return `<button type="button" class="pm-chip" data-topic="${esc(t)}" aria-pressed="${S.topic === t}">${esc(label)}<span class="pm-chip-n">${num}</span></button>`;
      }).join('');
    }

    function renderBoard() {
      const el = $('pmBoard');
      if (!el || !S.data) return;
      const rows = filterSort(S.data.events, S);
      const reads = new Set(S.data.reads || []);
      const now = Date.now();
      el.innerHTML = rows.map(ev => renderCard(ev, {
        reads, now, expanded: S.expanded.has(ev.key), open: S.open === ev.key,
        panel: S.open === ev.key ? renderRead(S.panels[ev.key], ev, ctx()) : '',
      })).join('');
      const empty = $('pmEmpty');
      if (empty) empty.classList.toggle('pm-hidden', rows.length > 0);
    }

    const ctx = () => ({ enabled: S.enabled, signedIn: S.signedIn, canAsk: S.enabled && S.signedIn });

    function renderAll() {
      if (!S.data) return;
      stamp();
      const fed = $('pmFed');
      if (fed) fed.innerHTML = renderFed(S.data.fed);
      const movers = $('pmMovers');
      if (movers) movers.innerHTML = renderMovers(S.data.movers);
      renderTopics();
      renderBoard();
      if (S.ledger) { const l = $('pmLedger'); if (l) l.innerHTML = renderLedger(S.ledger); }
    }

    function paintPanel(key) {
      const card = document.querySelector(`.pm-card[data-key="${CSS.escape(key)}"]`);
      if (!card) return;
      const ev = (S.data.events || []).find(e => e.key === key);
      const panel = card.querySelector('[data-pm-panel]');
      if (panel && ev) panel.innerHTML = renderRead(S.panels[key], ev, ctx());
    }

    async function loadRead(key) {
      const r = await getJson('/api/predictions/read?key=' + encodeURIComponent(key));
      S.panels[key] = r.body && r.body.status ? r.body : { status: 'none' };
      return S.panels[key];
    }

    // One bounded poll per key. It must end on a rendered state: stopping the
    // timer alone would leave the "reading…" line on screen for ever.
    const polling = new Set();
    async function poll(key) {
      if (polling.has(key)) return;
      polling.add(key);
      try {
        for (let i = 0; i < 40; i++) {
          await new Promise(res => setTimeout(res, 3000));
          const st = await loadRead(key);
          if (st.status !== 'running') {
            if (st.status === 'done' && st.read) {
              S.data.reads = Array.from(new Set((S.data.reads || []).concat([key])));
            }
            if (S.open === key) renderBoard();
            return;
          }
        }
        S.panels[key] = Object.assign({}, S.panels[key], { status: 'slow' });
        if (S.open === key) paintPanel(key);
      } finally {
        polling.delete(key);
      }
    }

    async function ask(key) {
      S.panels[key] = Object.assign({}, S.panels[key], { status: 'running' });
      paintPanel(key);
      const r = await getJson('/api/predictions/read', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key }),
      });
      if (r.status === 200 || r.status === 202) {
        S.panels[key] = Object.assign({ rules: (S.panels[key] || {}).rules }, r.body);
        paintPanel(key);
        if (r.body.status === 'running') poll(key);
        else if (r.body.status === 'done') { S.data.reads = Array.from(new Set((S.data.reads || []).concat([key]))); renderBoard(); }
        return;
      }
      const reason = (r.body && r.body.reason) || (r.status === 401 ? 'auth' : r.status === 403 ? 'forbidden' : '');
      S.panels[key] = Object.assign({}, S.panels[key], { status: 'refused', reason, quota: r.body && r.body.quota });
      paintPanel(key);
    }

    async function toggle(key) {
      if (S.open === key) { S.open = null; renderBoard(); return; }
      S.open = key;
      if (!S.panels[key]) S.panels[key] = { status: 'loading' };
      renderBoard();
      const st = await loadRead(key);
      if (S.open === key) paintPanel(key);
      if (st.status === 'running') poll(key);
      const card = document.querySelector(`.pm-card[data-key="${CSS.escape(key)}"]`);
      if (card && card.scrollIntoView) card.scrollIntoView({ block: 'nearest' });
    }

    async function load(first) {
      const MAX_WAIT_MS = 90000, EVERY_MS = 3000;
      const started = Date.now();
      const wait = $('pmWait');
      for (;;) {
        const r = await getJson('/api/predictions');
        if (r.status === 200 && r.body && r.body.status === 'ok') {
          S.data = r.body;
          S.signedIn = !!(r.body.ai && r.body.ai.signed_in);
          S.enabled = !!(r.body.ai && r.body.ai.enabled);
          if (wait) wait.classList.add('pm-hidden');
          document.querySelectorAll('[data-pm-needs-data]').forEach(el => el.classList.remove('pm-hidden'));
          renderAll();
          return true;
        }
        if (!first) return false;                      // a background refresh just waits for the next one
        if (r.status !== 202 || Date.now() - started > MAX_WAIT_MS) {
          if (wait) wait.classList.add('pm-hidden');
          const err = $('pmError');
          if (err) {
            err.textContent = '⚠ ' + (r.status === 202 ? L('pm.timeout', 'Timed out waiting for the odds. Try ↻ Refresh.')
              : ((r.body && r.body.error) || ('HTTP ' + r.status)));
            err.classList.remove('pm-hidden');
          }
          return false;
        }
        await new Promise(res => setTimeout(res, EVERY_MS));
      }
    }

    async function loadLedger() {
      const r = await getJson('/api/predictions/ledger');
      if (r.status === 200) { S.ledger = r.body; const l = $('pmLedger'); if (l) l.innerHTML = renderLedger(S.ledger); }
      else { const l = $('pmLedger'); if (l) l.innerHTML = `<p class="pm-empty">${esc(L('pm.ledger_unavailable', 'The track record is unavailable right now.'))}</p>`; }
    }

    // ── Events ────────────────────────────────────────────────────────
    const topics = $('pmTopics');
    if (topics) topics.addEventListener('click', e => {
      const b = e.target.closest('[data-topic]');
      if (!b) return;
      touch();
      S.topic = b.dataset.topic;
      renderTopics();
      renderBoard();
    });
    const platform = $('pmPlatform');
    if (platform) platform.addEventListener('change', () => { touch(); S.platform = platform.value; renderBoard(); });
    const sort = $('pmSort');
    if (sort) sort.addEventListener('change', () => { touch(); S.sort = sort.value; renderBoard(); });
    const search = $('pmSearch');
    if (search) search.addEventListener('input', () => { touch(); S.q = search.value; renderBoard(); });

    const board = $('pmBoard');
    if (board) board.addEventListener('click', e => {
      const card = e.target.closest('.pm-card');
      if (!card) return;
      const key = card.dataset.key;
      if (e.target.closest('[data-pm-more]')) {
        touch();
        if (S.expanded.has(key)) S.expanded.delete(key); else S.expanded.add(key);
        renderBoard();
      } else if (e.target.closest('[data-pm-read]')) {
        touch();
        toggle(key);
      } else if (e.target.closest('[data-pm-ask]')) {
        touch();
        ask(key);
      } else if (e.target.closest('[data-pm-check]')) {
        touch();
        S.panels[key] = Object.assign({}, S.panels[key], { status: 'running' });
        paintPanel(key);
        poll(key);
      }
    });

    document.addEventListener('i18n:langchange', renderAll);

    // A tab left open keeps its odds current: the whole page re-renders from
    // one response, so no figure is ever from a different vintage than the one
    // beside it. Not while the reader is mid-interaction, and not in a hidden
    // tab (whose timers the browser throttles anyway).
    setInterval(() => {
      if (document.hidden || Date.now() - S.lastTouch < 60000) return;
      load(false);
    }, 5 * 60 * 1000);

    load(true).then(ok => {
      if (!ok) return;
      const anchor = $('pmLedgerAnchor');
      if (anchor && typeof DeferLoad !== 'undefined') DeferLoad.when(anchor, loadLedger);
      else loadLedger();
    });
  }

  // ── /fedwatch's card ──────────────────────────────────────────────────
  async function bootFed(container) {
    if (!container) return;
    let rows = null;
    const paint = () => { container.innerHTML = rows ? renderFed(rows) : ''; };
    for (let i = 0; i < 20; i++) {
      const r = await getJson('/api/predictions/fed');
      if (r.status === 200 && r.body && r.body.status === 'ok') { rows = r.body.rows || []; break; }
      if (r.status !== 202) break;
      await new Promise(res => setTimeout(res, 3000));
    }
    if (rows === null) {
      container.innerHTML = `<p class="pm-empty">${esc(L('pm.fed_unavailable', 'Prediction-market odds are unavailable right now.'))}</p>`;
      return;
    }
    paint();
    document.addEventListener('i18n:langchange', paint);
  }

  return { esc, pct, pct1, pp, money, closesIn, span, filterSort, topicCounts, visibleOutcomes,
           renderFed, renderMovers, renderCard, renderRead, renderLedger, boot, bootFed };
})();
