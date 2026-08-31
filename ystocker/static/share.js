/**
 * Share a finished agent report with somebody, by email, SMS or WeChat.
 *
 * Drives templates/_share_modal.html. Loaded on every yStocker page from
 * base.html, so the cost on a page nobody shares from has to be ~nothing: this
 * file binds one click handler and fetches the report list on *first open*, never
 * on load. There is no work at parse time beyond wiring.
 *
 * All three channels mint the same capability link through /api/agents/share;
 * only "email" asks the server to send anything. "sms" hands the link to the
 * device's own Messages app via a `sms:` URI (smsHref()) and never collects a
 * phone number. "wechat" has no URL scheme to hand off to at all — WeChat's
 * real share integration needs a verified Official Account and its JS-SDK,
 * which this box has no part of — so it shows a QR code
 * (/api/agents/shared/<token>/qr.png) for WeChat's own scanner to read
 * instead. See share.py's create() docstring for why both are a deliberate
 * scope limit, not a missing feature.
 *
 * Exposes `window.Share.open(jobId)` so a page that knows which report is in
 * front of the reader can preselect it. /agents needs no such call — it already
 * keeps `?job=<id>` in the URL (its own shareUrl()/replaceState), so reading the
 * query string covers that page without touching its ~3000 lines.
 */
(function () {
  'use strict';

  var NOTE_MAX = 500;                    // must match share.NOTE_MAX server-side

  var dlg, msg, form, done, selJob, inTo, inNote, count, portfolioWarn,
      qrImg, qrHint, btnSend, btnSms, btnWechat, btnCancel, btnCopy, btnRevoke,
      lastFocus;
  var token = null, busy = false;

  function t(key, fallback) {
    if (window.I18n && I18n.t) {
      var s = I18n.t(key);
      if (s) return s;
    }
    return fallback;
  }

  function $(id) { return document.getElementById(id); }

  // ── Messages ──────────────────────────────────────────────────────────
  function say(kind, text) {
    msg.dataset.kind = kind;
    msg.textContent = text;
    msg.classList.remove('sd-hidden');
  }
  function clearSay() {
    msg.classList.add('sd-hidden');
    msg.textContent = '';
    delete msg.dataset.kind;
  }

  // ── Open / close ──────────────────────────────────────────────────────
  function open(jobId) {
    if (!dlg) return;
    lastFocus = document.activeElement;
    reset();
    dlg.hidden = false;
    // Two frames: the element must be laid out before the opacity transition
    // can run, or it snaps in without animating.
    requestAnimationFrame(function () { dlg.dataset.open = '1'; });
    load(jobId || jobFromUrl());
    setTimeout(function () { inTo.focus(); }, 60);
  }

  function close() {
    if (!dlg) return;
    delete dlg.dataset.open;
    // Match the CSS transition so the fade is visible rather than cut off.
    setTimeout(function () { dlg.hidden = true; }, 180);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  function reset() {
    token = null;
    busy = false;
    clearSay();
    form.classList.remove('sd-hidden');
    done.classList.add('sd-hidden');
    btnCopy.classList.add('sd-hidden');
    btnRevoke.classList.add('sd-hidden');
    btnSend.classList.remove('sd-hidden');
    btnSend.disabled = false;
    btnSend.textContent = t('share.send', 'Send');
    btnSms.classList.remove('sd-hidden');
    btnSms.disabled = false;
    btnSms.textContent = t('share.sms', 'Share via SMS');
    btnWechat.classList.remove('sd-hidden');
    btnWechat.disabled = false;
    btnWechat.textContent = t('share.wechat', 'Share to WeChat');
    btnCancel.textContent = t('share.cancel', 'Cancel');
    portfolioWarn.classList.add('sd-hidden');
    qrImg.classList.add('sd-hidden');
    qrImg.removeAttribute('src');          // drop the old token's image eagerly
    qrHint.classList.add('sd-hidden');
    inNote.value = '';
    updateCount();
    inTo.removeAttribute('aria-invalid');
  }

  function jobFromUrl() {
    try {
      return new URL(location.href).searchParams.get('job') || '';
    } catch (e) { return ''; }
  }

  // ── The report picker ─────────────────────────────────────────────────
  // Refetched on every open rather than cached after the first. A run finishes
  // while the reader is on some other page — that is the whole reason the deep
  // run emails you — so a list built once at first open would be missing exactly
  // the report they came here to send.
  function load(preferId) {
    selJob.innerHTML = '';
    var opt = document.createElement('option');
    opt.textContent = t('share.loading', 'Loading your reports…');
    selJob.appendChild(opt);
    selJob.disabled = true;

    fetch('/api/agents/jobs', { credentials: 'same-origin' })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        selJob.innerHTML = '';
        if (!res.ok) {
          selJob.disabled = true;
          say('error', res.d.error || t('share.err', 'Something went wrong.'));
          btnSend.disabled = true;
          return;
        }
        // Only finished runs with a body: a queued run has nothing to send, and
        // `status === 'done'` alone is not enough because a run can finish empty
        // (the same reason agents._listing_entry carries has_report at all).
        var jobs = (res.d.jobs || []).filter(function (j) {
          return j.status === 'done' && j.has_report;
        });
        if (!jobs.length) {
          selJob.disabled = true;
          btnSend.disabled = true;
          say('error', t('share.none', 'You have no finished reports to share yet.'));
          return;
        }
        jobs.forEach(function (j) {
          var o = document.createElement('option');
          o.value = j.id;
          var bits = [j.ticker || '?', j.date || ''];
          var d = (j.decision || '').split('\n')[0].trim();
          if (d) bits.push(d.slice(0, 28));
          o.textContent = bits.filter(Boolean).join(' · ');
          selJob.appendChild(o);
        });
        selJob.disabled = false;
        btnSend.disabled = false;
        preselect(preferId);
      })
      .catch(function (e) {
        selJob.innerHTML = '';
        selJob.disabled = true;
        btnSend.disabled = true;
        say('error', String((e && e.message) || e));
      });
  }

  function preselect(id) {
    if (!id) return;
    for (var i = 0; i < selJob.options.length; i++) {
      if (selJob.options[i].value === id) { selJob.selectedIndex = i; return; }
    }
  }

  function updateCount() {
    var n = inNote.value.length;
    count.textContent = n + ' / ' + NOTE_MAX;
    if (n >= NOTE_MAX) count.dataset.over = '1'; else delete count.dataset.over;
  }

  // ── Send ──────────────────────────────────────────────────────────────
  function send() {
    if (busy) return;
    var to = inTo.value.trim();
    var jobId = selJob.value;
    if (!to || to.indexOf('@') < 0) {
      inTo.setAttribute('aria-invalid', 'true');
      inTo.focus();
      say('error', t('share.bad_email', 'That does not look like an email address.'));
      return;
    }
    inTo.removeAttribute('aria-invalid');
    if (!jobId) {
      say('error', t('share.none', 'You have no finished reports to share yet.'));
      return;
    }

    busy = true;
    clearSay();
    btnSend.disabled = true;
    btnSend.textContent = t('share.sending', 'Sending…');

    fetch('/api/agents/share', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_id: jobId, to: to, note: inNote.value })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        busy = false;
        if (!res.ok) {
          btnSend.disabled = false;
          btnSend.textContent = t('share.send', 'Send');
          say('error', res.d.error || t('share.err', 'Something went wrong.'));
          if (res.d.reason === 'bad_recipient' || res.d.reason === 'self') inTo.focus();
          return;
        }
        token = res.d.token;
        showSent(res.d);
      })
      .catch(function (e) {
        busy = false;
        btnSend.disabled = false;
        btnSend.textContent = t('share.send', 'Send');
        say('error', String((e && e.message) || e));
      });
  }

  function showSent(d) {
    form.classList.add('sd-hidden');
    done.classList.remove('sd-hidden');
    $('shareLink').textContent = d.url || '';
    btnSend.classList.add('sd-hidden');
    btnSms.classList.add('sd-hidden');
    btnWechat.classList.add('sd-hidden');
    btnCopy.classList.remove('sd-hidden');
    btnRevoke.classList.remove('sd-hidden');
    btnCancel.textContent = t('share.done', 'Done');
    // The QR image is the share for this channel -- WeChat has no sms:-style
    // hand-off, so scanning it *is* the action, not a fallback shown after one
    // failed. Pointed at the per-token route rather than generated client-side
    // so a client-side QR library never has to ship to every page load for a
    // button most visits never click.
    if (d.channel === 'wechat' && d.token) {
      qrImg.src = '/api/agents/shared/' + encodeURIComponent(d.token) + '/qr.png';
      qrImg.classList.remove('sd-hidden');
      qrHint.classList.remove('sd-hidden');
    } else {
      qrImg.classList.add('sd-hidden');
      qrImg.removeAttribute('src');
      qrHint.classList.add('sd-hidden');
    }
    // Persistent, not part of the status line below: `say()` gets overwritten
    // by later transient messages (a revoke, a retry), but "this one talks
    // about your portfolio" should stay visible for as long as the link itself
    // is on screen, since that is the window in which it could still be sent.
    if (d.portfolio_context) {
      portfolioWarn.classList.remove('sd-hidden');
    } else {
      portfolioWarn.classList.add('sd-hidden');
    }
    // Not two outcomes, but three. An emailed share can succeed or fail to
    // send; an SMS or WeChat share never attempts a send at all --
    // routes.api_agents_share only calls send_share() for channel "email", so
    // `d.sent` is always false for the other two by design. Treating that the
    // same as a *failed* email would tell the sharer something broke when
    // nothing did.
    if (d.channel === 'wechat') {
      say('ok', t('share.wechat_sent', 'Scan the code below with WeChat to open the report.'));
    } else if (d.channel === 'sms') {
      say('ok', t('share.sms_sent', 'Opening Messages with the link ready to '
        + 'send. If nothing happens, copy the link below.'));
    } else if (d.sent) {
      say('ok', t('share.sent', 'Sent to') + ' ' + (d.to || ''));
    } else {
      say('error', t('share.unsent', 'The link is ready, but the email could not '
        + 'be sent. Copy the link and pass it on yourself.'));
    }
  }

  // ── Share via SMS ────────────────────────────────────────────────────────
  // Mints the same capability link as an email share (channel: 'sms' skips the
  // recipient checks server-side — see share.create()'s docstring) and hands
  // it to the device's own Messages app rather than mailing anything. This
  // process never learns a phone number: the sharer picks a contact inside
  // Messages after the handoff, exactly as a mailto: link would leave the
  // recipient choice to the mail client.
  function smsHref(body) {
    // iOS Safari's `sms:` handler wants `&body=`; Android's wants `?body=`.
    // There is no feature to detect for "which query-string form does this
    // platform's SMS compose intent accept" — sending the wrong one is not an
    // error, just a silently ignored parameter that opens Messages with
    // nothing pre-filled. Same class of "read what the platform actually
    // does, not what looks symmetric" trap CLAUDE.md documents for Futu's
    // universal links; UA-sniffing is what is left once there is no feature to
    // test.
    var isIOS = /iPad|iPhone|iPod/i.test(navigator.userAgent || '');
    return (isIOS ? 'sms:&body=' : 'sms:?body=') + encodeURIComponent(body);
  }

  // Shared by sendSms() and sendWechat(): both mint the same capability link
  // through /api/agents/share and differ only in the channel string and what
  // happens after showSent() renders the result -- SMS hands off to the
  // Messages app, WeChat has nothing further to do since the QR code
  // showSent() already displays *is* the share. send() (email) stays separate
  // rather than folding in here too: it has its own recipient-field validation
  // and focus handling that would only complicate this helper's signature for
  // a channel that already works.
  function mintShare(channel, btn, label, onSuccess) {
    if (busy) return;
    var jobId = selJob.value;
    if (!jobId) {
      say('error', t('share.none', 'You have no finished reports to share yet.'));
      return;
    }

    busy = true;
    clearSay();
    btn.disabled = true;
    btn.textContent = t('share.sending', 'Sending…');

    fetch('/api/agents/share', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_id: jobId, channel: channel, note: inNote.value })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        busy = false;
        btn.disabled = false;
        btn.textContent = label;
        if (!res.ok) {
          say('error', res.d.error || t('share.err', 'Something went wrong.'));
          return;
        }
        token = res.d.token;
        showSent(res.d);
        if (onSuccess) onSuccess(res.d);
      })
      .catch(function (e) {
        busy = false;
        btn.disabled = false;
        btn.textContent = label;
        say('error', String((e && e.message) || e));
      });
  }

  function sendSms() {
    mintShare('sms', btnSms, t('share.sms', 'Share via SMS'), function (d) {
      // Best-effort hand-off. On a desktop browser with no SMS handler this
      // silently does nothing, which is exactly why showSent() always leaves
      // the link on screen with a Copy button too.
      var lead = t('share.sms_lead', 'Check out this AI stock report:');
      location.href = smsHref(lead + ' ' + (d.url || ''));
    });
  }

  // ── Share to WeChat ──────────────────────────────────────────────────────
  // Mints the same capability link and renders the QR code showSent() already
  // knows how to display -- there is no hand-off step here the way SMS has
  // smsHref(), because WeChat has no URL scheme a page can invoke at all
  // (see ystocker/qr.py's module docstring). The scan *is* the share.
  function sendWechat() {
    mintShare('wechat', btnWechat, t('share.wechat', 'Share to WeChat'));
  }

  function copy() {
    var text = $('shareLink').textContent || '';
    if (!text) return;
    var ok = function () {
      btnCopy.textContent = t('share.copied', 'Copied');
      setTimeout(function () { btnCopy.textContent = t('share.copy', 'Copy link'); }, 1600);
    };
    // navigator.clipboard needs a secure context, which a plain-HTTP dev box is
    // not, so the textarea fallback is a real path and not dead code.
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(ok, fallbackCopy);
    } else {
      fallbackCopy();
    }
    function fallbackCopy() {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); ok(); } catch (e) { /* nothing to do */ }
      document.body.removeChild(ta);
    }
  }

  function revoke() {
    if (!token || busy) return;
    busy = true;
    btnRevoke.disabled = true;
    fetch('/api/agents/share/' + encodeURIComponent(token) + '/revoke',
          { method: 'POST', credentials: 'same-origin' })
      .then(function (r) { return r.ok; })
      .then(function (ok) {
        busy = false;
        btnRevoke.disabled = false;
        if (!ok) { say('error', t('share.err', 'Something went wrong.')); return; }
        token = null;
        btnRevoke.classList.add('sd-hidden');
        btnCopy.classList.add('sd-hidden');
        $('shareLink').textContent = '';
        say('ok', t('share.revoked', 'Link revoked. It no longer opens the report.'));
      })
      .catch(function () { busy = false; btnRevoke.disabled = false; });
  }

  // ── Wiring ────────────────────────────────────────────────────────────
  function init() {
    dlg = $('shareDlg');
    if (!dlg) return;                    // page did not include the partial
    msg = $('shareMsg');
    form = $('shareForm');
    done = $('shareDone');
    selJob = $('shareJob');
    inTo = $('shareTo');
    inNote = $('shareNote');
    count = $('shareCount');
    portfolioWarn = $('sharePortfolioWarn');
    qrImg = $('shareQr');
    qrHint = $('shareQrHint');
    btnSend = $('shareSend');
    btnSms = $('shareSms');
    btnWechat = $('shareWechat');
    btnCancel = $('shareCancel');
    btnCopy = $('shareCopy');
    btnRevoke = $('shareRevoke');

    btnSend.addEventListener('click', send);
    btnSms.addEventListener('click', sendSms);
    btnWechat.addEventListener('click', sendWechat);
    btnCancel.addEventListener('click', close);
    btnCopy.addEventListener('click', copy);
    btnRevoke.addEventListener('click', revoke);
    inNote.addEventListener('input', updateCount);

    // Enter sends from the address field, which is where a keyboard user ends up
    // after typing. Not from the note: a note is multi-line by nature.
    inTo.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); send(); }
    });

    // Click the backdrop, not the card, to dismiss.
    dlg.addEventListener('click', function (e) { if (e.target === dlg) close(); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && dlg.dataset.open === '1') close();
    });

    // Any element can be a trigger, so the header, the mobile menu and any page
    // that wants its own button all share one handler and none of them need JS.
    document.addEventListener('click', function (e) {
      var trig = e.target.closest && e.target.closest('[data-share-open]');
      if (!trig) return;
      e.preventDefault();
      open(trig.getAttribute('data-share-open') || '');
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.Share = { open: open, close: close };
})();
