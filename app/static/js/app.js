/* Shared helpers: fetch wrapper, toasts, formatting, engine status pill.
   No external libraries: everything here is plain ES2020 that ships with the
   app, so the interface works with the network cable unplugged. */

const LS = (() => {
  const token = document.body.dataset.token || '';

  /* ---------------------------------------------------------------- fetch */

  async function request(method, url, body, options = {}) {
    const init = {
      method,
      headers: { 'X-Instance-Token': token },
      ...options,
    };
    if (body instanceof FormData) {
      init.body = body;
    } else if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }

    let response;
    try {
      response = await fetch(url, init);
    } catch (err) {
      toast(`Could not reach Local Scribe: ${err.message}. Is the app still running?`, 'error');
      throw err;
    }

    const isJson = (response.headers.get('content-type') || '').includes('json');
    const payload = isJson ? await response.json().catch(() => null) : null;

    if (!response.ok) {
      const detail = (payload && (payload.detail || payload.error)) ||
        `${response.status} ${response.statusText}`;
      const error = new Error(detail);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  const get = (url) => request('GET', url);
  const post = (url, body) => request('POST', url, body === undefined ? {} : body);
  const put = (url, body) => request('PUT', url, body);
  const del = (url) => request('DELETE', url);

  /* --------------------------------------------------------------- toasts */

  function toast(message, kind = 'info', ms = 5200) {
    const host = document.getElementById('toasts');
    if (!host) return;
    const el = document.createElement('div');
    el.className = `toast toast-${kind}`;
    el.textContent = message;
    host.appendChild(el);
    const remove = () => { el.style.opacity = '0'; setTimeout(() => el.remove(), 200); };
    setTimeout(remove, ms);
    el.addEventListener('click', remove);
  }

  /* ----------------------------------------------------------- formatting */

  function duration(seconds) {
    if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '-';
    const total = Math.max(0, Math.round(Number(seconds)));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    if (h) return `${h}h ${String(m).padStart(2, '0')}m`;
    if (m) return `${m}m ${String(s).padStart(2, '0')}s`;
    return `${s}s`;
  }

  function clock(seconds, withMs = false) {
    const value = Math.max(0, Number(seconds) || 0);
    const h = Math.floor(value / 3600);
    const m = Math.floor((value % 3600) / 60);
    const s = value % 60;
    const pad = (n) => String(Math.floor(n)).padStart(2, '0');
    const base = h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
    if (!withMs) return base;
    return `${base}.${String(Math.round((value % 1) * 1000)).padStart(3, '0')}`;
  }

  function bytes(count) {
    if (count === null || count === undefined) return '-';
    let value = Number(count);
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
    return `${i === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[i]}`;
  }

  function plural(n, one, many) {
    return `${n} ${n === 1 ? one : (many || one + 's')}`;
  }

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text === null || text === undefined ? '' : String(text);
    return div.innerHTML;
  }

  function debounce(fn, ms) {
    let timer = null;
    const wrapped = (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => { timer = null; fn(...args); }, ms);
    };
    wrapped.flush = (...args) => {
      if (timer) { clearTimeout(timer); timer = null; fn(...args); }
    };
    wrapped.pending = () => timer !== null;
    return wrapped;
  }

  /* --------------------------------------------------- engine status pill */

  const PILL_CLASSES = ['pill-unknown', 'pill-ready', 'pill-loading', 'pill-error', 'pill-busy'];

  function renderEnginePill(engine, worker) {
    const pill = document.getElementById('engine-pill');
    if (!pill || !engine) return;
    const stateEl = document.getElementById('engine-state');
    const detailEl = document.getElementById('engine-detail');

    let cls = 'pill-unknown';
    let label = 'Engine idle';
    let detail = engine.selected_model_label || engine.selected_model || '';

    if (worker && worker.busy) {
      cls = 'pill-busy';
      label = 'Transcribing';
      detail = worker.eta_text && worker.eta_text !== 'estimating'
        ? `${Math.round((worker.progress || 0) * 100)}% - ${worker.eta_text} left`
        : `${Math.round((worker.progress || 0) * 100)}% complete`;
    } else if (engine.state === 'loading') {
      cls = 'pill-loading';
      label = 'Loading model';
    } else if (engine.state === 'error') {
      cls = 'pill-error';
      label = 'Engine problem';
      detail = 'Click for details';
    } else if (engine.ready) {
      cls = 'pill-ready';
      label = 'AI ready';
      detail = `${engine.loaded_model || engine.selected_model} on ${(engine.loaded_device || engine.target_device || '').toUpperCase()}`;
    } else if (!engine.model_downloaded) {
      cls = 'pill-error';
      label = 'No model';
      detail = 'Download one in AI Settings';
    } else {
      cls = 'pill-unknown';
      label = 'AI linked, idle';
      detail = `${engine.selected_model} ready to load`;
    }

    PILL_CLASSES.forEach((c) => pill.classList.remove(c));
    pill.classList.add(cls);
    if (stateEl) stateEl.textContent = label;
    if (detailEl) detailEl.textContent = detail;
    pill.title = engine.detail || label;
  }

  function renderEngineFacts(status) {
    const dl = document.getElementById('engine-facts');
    if (!dl || !status) return;
    const e = status.engine || {};
    const rows = [
      ['State', e.detail || e.state],
      ['Selected model', e.selected_model_label || e.selected_model],
      ['Downloaded', e.model_downloaded ? 'yes' : 'no - download it in AI Settings'],
      ['Loaded model', e.loaded_model || 'not loaded'],
      ['Device', (e.loaded_device || e.target_device || '').toUpperCase()],
      ['Precision', e.loaded_compute || e.target_compute],
      ['GPU', e.gpu_count ? `${e.gpu_count} x ${e.gpu_name}` : 'none detected'],
      ['NVIDIA driver', e.driver_version || 'n/a'],
      ['CUDA usable', e.cuda_usable ? 'yes' : 'no - running on CPU'],
      ['CPU cores', e.cpu_cores],
      ['Offline lock', e.offline_lock ? 'engaged' : 'off'],
      ['Queue', `${status.queue ? status.queue.pending : 0} pending`],
      ['Uptime', duration(status.uptime)],
    ];
    dl.innerHTML = rows
      .filter(([, v]) => v !== undefined && v !== null && v !== '')
      .map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`)
      .join('');

    const warnings = (e.warnings || []).concat(e.notes || []);
    if (warnings.length) {
      dl.insertAdjacentHTML('afterend',
        `<div class="notice notice-warn small" style="margin-top:.7rem">${
          warnings.map(escapeHtml).join('<br><br>')}</div>`);
    }
  }

  function wireEnginePanel() {
    const pill = document.getElementById('engine-pill');
    const panel = document.getElementById('engine-panel');
    if (!pill || !panel) return;

    pill.addEventListener('click', async () => {
      const showing = !panel.hidden;
      panel.hidden = showing;
      if (showing) return;
      try {
        renderEngineFacts(await get('/api/status'));
      } catch (err) {
        panel.innerHTML = `<p class="small">Could not read engine status: ${escapeHtml(err.message)}</p>`;
      }
    });

    document.addEventListener('click', (event) => {
      if (panel.hidden) return;
      if (!panel.contains(event.target) && !pill.contains(event.target)) panel.hidden = true;
    });

    panel.querySelectorAll('[data-engine-action]').forEach((button) => {
      button.addEventListener('click', async () => {
        const action = button.dataset.engineAction;
        button.disabled = true;
        try {
          const result = await post(`/api/engine/${action}`);
          renderEnginePill(result.engine);
          toast(action === 'load' ? 'Model loaded and ready.' : 'Model unloaded.', 'ok');
          renderEngineFacts(await get('/api/status'));
        } catch (err) {
          toast(err.message, 'error', 9000);
        } finally {
          button.disabled = false;
        }
      });
    });
  }

  document.addEventListener('DOMContentLoaded', wireEnginePanel);

  return {
    token, request, get, post, put, del,
    toast, duration, clock, bytes, plural, escapeHtml, debounce,
    renderEnginePill, renderEngineFacts,
  };
})();
