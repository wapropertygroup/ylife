/* Timestamps, in the reader's own zone and saying so.
 *
 * Server timestamps are UTC ISO strings ("2026-08-20T14:19:59+00:00"), and the
 * offset is what makes them unambiguous -- Date parses it, so the getters below
 * are already local. What was missing is the label: "08/20 15:45" gives a reader
 * no way to know whether that is their clock or the server's, and the same page
 * is read from several zones.
 *
 * One copy for every page that prints a run's or a report's time. It began
 * inline in agents.html, which had itself previously carried two identical
 * formatters in separate scopes -- the duplication the shared Markdown renderer
 * exists to avoid -- and moved here when /history's Research tab started
 * listing the same runs (2026-10-01).
 */
window.YSTime = (function () {
  'use strict';
  const zone = () => {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; }
    catch (_) { return ''; }
  };
  const fmt = (iso, opts) => {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return '';
    try {
      return new Intl.DateTimeFormat(undefined, opts).format(d);
    } catch (_) {
      // No Intl: fall back to the old unlabelled form rather than nothing.
      const pad = n => String(n).padStart(2, '0');
      return pad(d.getMonth() + 1) + '/' + pad(d.getDate()) + ' ' +
             pad(d.getHours()) + ':' + pad(d.getMinutes());
    }
  };
  return {
    zone: zone,
    /* Compact, for a list row: "08/20 15:45 PDT". */
    when: iso => fmt(iso, {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
      hour12: false, timeZoneName: 'short',
    }),
    /* Full, for a tooltip or a report header: includes the year and seconds. */
    full: iso => fmt(iso, {
      year: 'numeric', month: 'short', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: false, timeZoneName: 'short',
    }),
  };
})();
