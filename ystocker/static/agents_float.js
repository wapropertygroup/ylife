(() => {
  'use strict';

  const root = document.getElementById('agentsFloatingRoot');
  if (!root) return;

  const launcher = document.getElementById('agentsFloatingLauncher');
  const panel = document.getElementById('agentsFloatingPanel');
  const handle = document.getElementById('agentsFloatingHandle');
  const maximizeButton = document.getElementById('agentsFloatingMaximize');
  const minimizeButton = document.getElementById('agentsFloatingMinimize');
  const closeButton = document.getElementById('agentsFloatingClose');
  const resizer = document.getElementById('agentsFloatingResizer');
  const frame = document.getElementById('agentsFloatingFrame');
  const loading = document.getElementById('agentsFloatingLoading');
  const compact = window.matchMedia('(max-width: 700px)');
  const openKey = 'ystocker_agents_float_open';
  const geometryKey = 'ystocker_agents_float_geometry';
  // Written by an older build that expanded the panel to a full-screen overlay
  // in place. Expanding now navigates to /agents instead, so the value is only
  // read to clear it -- left behind it would restore a stuck full-screen panel.
  const legacyMaximizedKey = 'ystocker_agents_float_maximized';
  let frameLoaded = false;
  let geometryRestored = false;
  let interaction = null;
  // Report currently on screen inside the frame, published by the embedded page.
  // Used so expanding lands on that report rather than the bare index.
  let currentJobId = null;

  function readStorage(key) {
    try { return localStorage.getItem(key); } catch (_) { return null; }
  }

  function writeStorage(key, value) {
    try { localStorage.setItem(key, value); } catch (_) {}
  }

  function currentLanguage() {
    return typeof I18n !== 'undefined' && I18n.getLang() === 'zh' ? 'zh' : 'en';
  }

  function syncAccessibleLabels() {
    if (typeof I18n === 'undefined') return;
    const expandLabel = I18n.t('agents.float_expand') || 'Open full page';
    maximizeButton.dataset.i18nTitle = 'agents.float_expand';
    maximizeButton.setAttribute('aria-label', expandLabel);
    maximizeButton.title = expandLabel;
    minimizeButton.setAttribute('aria-label', I18n.t('agents.float_minimize') || 'Minimize');
    closeButton.setAttribute('aria-label', I18n.t('agents.float_close') || 'Close');
  }

  function syncFrameLanguage() {
    if (!frameLoaded || !frame.contentWindow) return;
    frame.contentWindow.postMessage({
      type: 'ystocker:agents-language',
      lang: currentLanguage(),
    }, window.location.origin);
  }

  function loadFrame() {
    if (frameLoaded) return;
    const source = new URL(frame.dataset.src, window.location.origin);
    if (currentLanguage() === 'zh') source.searchParams.set('lang', 'zh');
    else source.searchParams.delete('lang');
    loading.hidden = false;
    frame.src = source.pathname + source.search;
    frameLoaded = true;
  }

  function clamp(value, minimum, maximum) {
    return Math.min(Math.max(value, minimum), Math.max(minimum, maximum));
  }

  function clampPanel() {
    if (compact.matches || panel.hidden) return;
    const rect = panel.getBoundingClientRect();
    const width = Math.min(rect.width, window.innerWidth - 16);
    const height = Math.min(rect.height, window.innerHeight - 16);
    panel.style.width = `${width}px`;
    panel.style.height = `${height}px`;
    panel.style.left = `${clamp(rect.left, 8, window.innerWidth - width - 8)}px`;
    panel.style.top = `${clamp(rect.top, 8, window.innerHeight - height - 8)}px`;
    panel.style.right = 'auto';
    panel.style.bottom = 'auto';
  }

  function restoreGeometry() {
    if (geometryRestored || compact.matches) return;
    geometryRestored = true;
    const raw = readStorage(geometryKey);
    if (!raw) return;
    try {
      const geometry = JSON.parse(raw);
      const width = clamp(Number(geometry.width) || 560, 390, window.innerWidth - 16);
      const height = clamp(Number(geometry.height) || 760, 360, window.innerHeight - 16);
      panel.style.width = `${width}px`;
      panel.style.height = `${height}px`;
      panel.style.left = `${clamp(Number(geometry.left) || 8, 8, window.innerWidth - width - 8)}px`;
      panel.style.top = `${clamp(Number(geometry.top) || 8, 8, window.innerHeight - height - 8)}px`;
      panel.style.right = 'auto';
      panel.style.bottom = 'auto';
    } catch (_) {}
  }

  function saveGeometry() {
    if (compact.matches || panel.hidden) return;
    const rect = panel.getBoundingClientRect();
    writeStorage(geometryKey, JSON.stringify({
      left: Math.round(rect.left),
      top: Math.round(rect.top),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
    }));
  }

  function openPanel(moveFocus = true) {
    loadFrame();
    restoreGeometry();
    panel.hidden = false;
    launcher.hidden = true;
    launcher.setAttribute('aria-expanded', 'true');
    writeStorage(openKey, '1');
    if (moveFocus) minimizeButton.focus({ preventScroll: true });
  }

  function minimizePanel() {
    panel.hidden = true;
    launcher.hidden = false;
    launcher.setAttribute('aria-expanded', 'false');
    writeStorage(openKey, '0');
    launcher.focus({ preventScroll: true });
  }

  function closePanel() {
    minimizePanel();
    frame.removeAttribute('src');
    frameLoaded = false;
    loading.hidden = false;
  }

  // Expanding leaves the panel behind and goes to the real page. The previous
  // behaviour grew the iframe to a full-screen overlay, which looked like full
  // screen but kept every limitation of being framed: no address bar to copy or
  // bookmark, the browser back button belonging to the host page, and a nested
  // scroll container. Navigating carries the report being read across, so the
  // expanded view opens on the same thing rather than the index.
  function expandToPage() {
    const url = new URL(frame.dataset.src, window.location.origin);
    url.searchParams.delete('embed');
    if (currentJobId) url.searchParams.set('job', currentJobId);
    if (currentLanguage() === 'zh') url.searchParams.set('lang', 'zh');
    // Leave the panel closed, or coming back would reopen it over the page.
    writeStorage(openKey, '0');
    window.location.href = url.pathname + url.search;
  }

  function startInteraction(event, type) {
    if (compact.matches || event.button !== 0) return;
    if (type === 'drag' && event.target.closest('button')) return;
    event.preventDefault();
    const rect = panel.getBoundingClientRect();
    interaction = {
      type,
      startX: event.clientX,
      startY: event.clientY,
      left: rect.left,
      top: rect.top,
      width: rect.width,
      height: rect.height,
    };
    panel.style.left = `${rect.left}px`;
    panel.style.top = `${rect.top}px`;
    panel.style.right = 'auto';
    panel.style.bottom = 'auto';
    document.body.classList.add('agents-float-interacting');
  }

  function moveInteraction(event) {
    if (!interaction) return;
    event.preventDefault();
    const deltaX = event.clientX - interaction.startX;
    const deltaY = event.clientY - interaction.startY;
    if (interaction.type === 'drag') {
      panel.style.left = `${clamp(interaction.left + deltaX, 8, window.innerWidth - interaction.width - 8)}px`;
      panel.style.top = `${clamp(interaction.top + deltaY, 8, window.innerHeight - interaction.height - 8)}px`;
      return;
    }
    const width = clamp(interaction.width + deltaX, 390, window.innerWidth - interaction.left - 8);
    const height = clamp(interaction.height + deltaY, 360, window.innerHeight - interaction.top - 8);
    panel.style.width = `${width}px`;
    panel.style.height = `${height}px`;
  }

  function endInteraction() {
    if (!interaction) return;
    interaction = null;
    document.body.classList.remove('agents-float-interacting');
    saveGeometry();
  }

  launcher.addEventListener('click', () => openPanel());
  maximizeButton.addEventListener('click', expandToPage);
  minimizeButton.addEventListener('click', minimizePanel);
  closeButton.addEventListener('click', closePanel);
  handle.addEventListener('pointerdown', event => startInteraction(event, 'drag'));
  resizer.addEventListener('pointerdown', event => startInteraction(event, 'resize'));
  window.addEventListener('pointermove', moveInteraction);
  window.addEventListener('pointerup', endInteraction);
  window.addEventListener('pointercancel', endInteraction);
  window.addEventListener('resize', clampPanel);
  frame.addEventListener('load', () => {
    loading.hidden = true;
    syncFrameLanguage();
  });
  // The framed page reports which report it is showing, so expanding can open
  // the same one. Origin-checked: this listener is on every page of the site.
  window.addEventListener('message', event => {
    if (event.origin !== window.location.origin) return;
    if (event.data && event.data.type === 'ystocker:agents-job') {
      currentJobId = event.data.id || null;
    }
  });
  document.addEventListener('i18n:langchange', () => {
    syncAccessibleLabels();
    syncFrameLanguage();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !panel.hidden) minimizePanel();
  });

  // Drop the stale full-screen flag from the previous behaviour.
  if (readStorage(legacyMaximizedKey) !== null) {
    try { localStorage.removeItem(legacyMaximizedKey); } catch (_) {}
  }
  maximizeButton.removeAttribute('aria-pressed');
  syncAccessibleLabels();
  // Only desktop reopens itself. On a phone the panel is a full-screen sheet
  // (inset: 0), so restoring it on every page load covered whatever the visitor
  // had navigated to -- including /login, where it hid the sign-in button
  // entirely and there was no way to get past it. The launcher is still there;
  // one tap reopens.
  if (!compact.matches && readStorage(openKey) === '1') openPanel(false);
})();
