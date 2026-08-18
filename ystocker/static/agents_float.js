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
  const maximizedKey = 'ystocker_agents_float_maximized';
  let frameLoaded = false;
  let geometryRestored = false;
  let interaction = null;

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
    const maximized = panel.classList.contains('is-maximized');
    const labelKey = maximized ? 'agents.float_restore' : 'agents.float_maximize';
    const maximizeLabel = I18n.t(labelKey)
      || (maximized ? 'Restore window' : 'Full screen');
    maximizeButton.dataset.i18nTitle = labelKey;
    maximizeButton.setAttribute('aria-label', maximizeLabel);
    maximizeButton.title = maximizeLabel;
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
    if (compact.matches || panel.hidden || panel.classList.contains('is-maximized')) return;
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
    if (compact.matches || panel.hidden || panel.classList.contains('is-maximized')) return;
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

  function toggleMaximize() {
    const maximize = !panel.classList.contains('is-maximized');
    if (maximize) saveGeometry();
    panel.classList.toggle('is-maximized', maximize);
    maximizeButton.setAttribute('aria-pressed', String(maximize));
    writeStorage(maximizedKey, maximize ? '1' : '0');
    syncAccessibleLabels();
    if (!maximize) requestAnimationFrame(clampPanel);
  }

  function startInteraction(event, type) {
    if (compact.matches || panel.classList.contains('is-maximized') || event.button !== 0) return;
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
  maximizeButton.addEventListener('click', toggleMaximize);
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
  document.addEventListener('i18n:langchange', () => {
    syncAccessibleLabels();
    syncFrameLanguage();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !panel.hidden) minimizePanel();
  });

  const startsMaximized = readStorage(maximizedKey) === '1';
  panel.classList.toggle('is-maximized', startsMaximized);
  maximizeButton.setAttribute('aria-pressed', String(startsMaximized));
  syncAccessibleLabels();
  if (readStorage(openKey) === '1') openPanel(false);
})();
