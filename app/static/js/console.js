/* The live console: WebSocket client, heartbeat indicator, and meters.

   The console is the app's main feedback surface, so it is deliberately
   verbose and always reconnects. A visible heartbeat matters because
   transcription can spend a long time producing no new segments on a quiet
   passage, and silence should not look like a hang. */

(() => {
  const panel = document.getElementById('console');
  const log = document.getElementById('console-log');
  const toggle = document.getElementById('console-toggle');
  if (!panel || !log || !toggle) return;

  const statusEl = document.getElementById('console-status');
  const heartbeatDot = document.getElementById('heartbeat-dot');
  const barFill = document.getElementById('console-bar-fill');
  const autoscroll = document.getElementById('console-autoscroll');
  const showDebug = document.getElementById('console-debug');
  const filter = document.getElementById('console-filter');

  const MAX_ROWS = 4000;
  const SEVERITY = { debug: 0, info: 1, step: 1, stat: 1, success: 2, warn: 3, error: 4 };

  let socket = null;
  let retryDelay = 500;
  let lastSeq = 0;
  let lastBeat = 0;
  const buffer = [];   // every event received, for "Copy all" and re-filtering

  /* ------------------------------------------------------------ visibility */

  function setOpen(open) {
    panel.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    document.body.classList.toggle('console-open', open);
    try { localStorage.setItem('ls.console.open', open ? '1' : '0'); } catch (e) { /* private mode */ }
    if (open) scrollToEnd();
  }

  function scrollToEnd() {
    if (autoscroll && !autoscroll.checked) return;
    log.scrollTop = log.scrollHeight;
  }

  toggle.addEventListener('click', () => setOpen(panel.hidden));
  document.getElementById('console-close').addEventListener('click', () => setOpen(false));

  document.getElementById('console-clear').addEventListener('click', () => {
    log.innerHTML = '';
    LS.toast('Console view cleared. The full log is still in logs/localscribe.log.', 'info');
  });

  document.getElementById('console-copy').addEventListener('click', async () => {
    const text = buffer.map(formatPlain).join('\n');
    try {
      await navigator.clipboard.writeText(text);
      LS.toast(`Copied ${buffer.length} console lines to the clipboard.`, 'ok');
    } catch (e) {
      // Clipboard permission can be refused; offer a selectable fallback.
      const area = document.createElement('textarea');
      area.value = text;
      document.body.appendChild(area);
      area.select();
      LS.toast('Clipboard was blocked. The text is selected - press Ctrl+C.', 'warn', 8000);
      setTimeout(() => area.remove(), 15000);
    }
  });

  [showDebug, filter].forEach((control) => {
    if (control) control.addEventListener('change', rerender);
  });

  /* ------------------------------------------------------------- rendering */

  function visible(event) {
    if (event.level === 'heartbeat') return false;
    if (event.level === 'debug' && !(showDebug && showDebug.checked)) return false;
    const mode = filter ? filter.value : 'all';
    if (mode === 'all') return true;
    if (mode === 'stat') return event.level === 'stat';
    const floor = mode === 'warn' ? 3 : 1;
    return (SEVERITY[event.level] ?? 1) >= floor;
  }

  function timeOf(event) {
    if (!event.ts) return '';
    const parsed = new Date(event.ts);
    if (Number.isNaN(parsed.getTime())) return '';
    return parsed.toLocaleTimeString([], { hour12: false });
  }

  function formatData(data) {
    if (!data) return '';
    const parts = [];
    Object.keys(data).forEach((key) => {
      const value = data[key];
      if (value === null || value === undefined || value === '') return;
      if (typeof value === 'object') return;   // nested detail stays in the file log
      parts.push(`${key}=${value}`);
    });
    return parts.length ? `  <span class="kv">${LS.escapeHtml(parts.join(' '))}</span>` : '';
  }

  function formatPlain(event) {
    const data = event.data && Object.keys(event.data).length
      ? '  ' + Object.entries(event.data)
        .filter(([, v]) => v !== null && v !== undefined && typeof v !== 'object')
        .map(([k, v]) => `${k}=${v}`).join(' ')
      : '';
    return `${timeOf(event)} [${event.level}] ${event.message}${data}`;
  }

  function rowFor(event) {
    const li = document.createElement('li');
    li.className = `l-${event.level}`;
    li.innerHTML =
      `<span class="ts">${LS.escapeHtml(timeOf(event))}</span>` +
      `<span class="lv lv-${event.level}">${LS.escapeHtml(event.level)}</span>` +
      `<span class="msg">${LS.escapeHtml(event.message)}${formatData(event.data)}</span>`;
    return li;
  }

  function append(event) {
    if (!visible(event)) return;
    log.appendChild(rowFor(event));
    while (log.childElementCount > MAX_ROWS) log.removeChild(log.firstChild);
    scrollToEnd();
  }

  function rerender() {
    log.innerHTML = '';
    const fragment = document.createDocumentFragment();
    buffer.filter(visible).slice(-MAX_ROWS).forEach((e) => fragment.appendChild(rowFor(e)));
    log.appendChild(fragment);
    scrollToEnd();
  }

  /* ------------------------------------------------------------- heartbeat */

  function pulse() {
    if (!heartbeatDot) return;
    heartbeatDot.classList.remove('beat');
    // Force a reflow so the animation restarts on every beat.
    void heartbeatDot.offsetWidth;
    heartbeatDot.classList.add('beat');
    lastBeat = Date.now();
  }

  function setMeter(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function applyHeartbeat(payload) {
    if (!payload) return;
    pulse();
    LS.renderEnginePill(engineFromHeartbeat(payload), payload.worker);
    setMeter('m-uptime', LS.duration(payload.uptime));

    const worker = payload.worker || {};
    if (worker.busy) {
      const stats = worker.stats || {};
      setMeter('m-status', worker.label || 'working');
      setMeter('m-progress', `${Math.round((worker.progress || 0) * 100)}%`);
      setMeter('m-eta', worker.eta_text || 'estimating');
      if (barFill) barFill.style.width = `${Math.round((worker.progress || 0) * 100)}%`;
    } else {
      setMeter('m-status', worker.pending ? `${worker.pending} queued` : 'idle');
      setMeter('m-progress', '-');
      setMeter('m-eta', '-');
      if (barFill) barFill.style.width = '0%';
    }

    if (worker.worker_alive === false) {
      setMeter('m-status', 'worker stopped');
    }
  }

  function engineFromHeartbeat(payload) {
    // The heartbeat carries a compact engine view; merge it with what the
    // page was rendered with so the pill never loses the model label.
    const e = payload.engine || {};
    return {
      state: e.state,
      ready: e.ready,
      selected_model: e.model,
      selected_model_label: e.model,
      loaded_model: e.ready ? e.model : '',
      loaded_device: e.device,
      target_device: e.device,
      model_downloaded: true,
      detail: `${e.model || 'model'} on ${(e.device || '').toUpperCase()}`,
    };
  }

  /* Per-segment stats ride on 'stat' events during transcription. */
  function applyStats(data) {
    if (!data) return;
    if (data.realtime_factor !== undefined) setMeter('m-rtf', `${data.realtime_factor}x`);
    if (data.words_per_second !== undefined) setMeter('m-wps', data.words_per_second);
    if (data.tokens_per_second !== undefined) setMeter('m-tps', data.tokens_per_second);
    if (data.eta_text) setMeter('m-eta', data.eta_text);
    if (data.progress !== undefined && barFill) {
      barFill.style.width = `${Math.round(data.progress * 100)}%`;
    }
  }

  /* ------------------------------------------------------------- transport */

  function setStatus(text, cls) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.className = `console-status ${cls || ''}`.trim();
  }

  function handle(event) {
    if (event.type === 'hello') {
      buffer.length = 0;
      (event.history || []).forEach((item) => {
        buffer.push(item);
        lastSeq = Math.max(lastSeq, item.seq || 0);
      });
      rerender();
      applyHeartbeat(event.heartbeat);
      return;
    }

    if (event.level === 'heartbeat') {
      applyHeartbeat(event.data);
      return;
    }

    if (event.seq && event.seq <= lastSeq) return;   // replayed after reconnect
    lastSeq = Math.max(lastSeq, event.seq || 0);

    buffer.push(event);
    if (buffer.length > MAX_ROWS * 2) buffer.splice(0, buffer.length - MAX_ROWS);
    append(event);

    if (event.level === 'stat') applyStats(event.data);
    if (event.level === 'error') LS.toast(event.message, 'error', 10000);

    // Pages can react to console traffic, e.g. refreshing a file table when a
    // job finishes, without polling.
    document.dispatchEvent(new CustomEvent('ls:console', { detail: event }));
  }

  function connect() {
    const url = `ws://${location.host}/ws/console?token=${encodeURIComponent(LS.token)}`;
    setStatus('connecting');
    try {
      socket = new WebSocket(url);
    } catch (e) {
      setStatus('offline', 'down');
      scheduleRetry();
      return;
    }

    socket.addEventListener('open', () => {
      retryDelay = 500;
      setStatus('live', 'live');
    });

    socket.addEventListener('message', (message) => {
      try { handle(JSON.parse(message.data)); } catch (e) { /* malformed frame */ }
    });

    socket.addEventListener('close', () => {
      setStatus('reconnecting', 'down');
      scheduleRetry();
    });

    socket.addEventListener('error', () => setStatus('error', 'down'));
  }

  function scheduleRetry() {
    setTimeout(() => {
      retryDelay = Math.min(retryDelay * 2, 8000);
      connect();
    }, retryDelay);
  }

  /* If no heartbeat arrives for three intervals the backend has stopped
     responding; say so rather than leaving a stale "live" badge. */
  setInterval(() => {
    if (!lastBeat) return;
    if (Date.now() - lastBeat > 7000) setStatus('no heartbeat', 'down');
  }, 2000);

  let openByDefault = false;
  try { openByDefault = localStorage.getItem('ls.console.open') === '1'; } catch (e) { /* ignore */ }
  setOpen(openByDefault);
  connect();

  // Expose a hook so pages can open the console when they start work.
  window.LSConsole = { open: () => setOpen(true), pulse };
})();
