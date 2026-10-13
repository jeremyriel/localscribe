/* Settings: collect the schema-generated form, model downloads, GPU setup. */

(() => {
  const form = document.getElementById('settings-form');
  const state = document.getElementById('settings-state');

  /* Live slider readouts. */
  document.querySelectorAll('input[type=range]').forEach((slider) => {
    const output = slider.parentElement.querySelector('output');
    const sync = () => {
      if (!output) return;
      // 0 means "no cap" for the token budget; say so rather than showing 0.
      output.textContent = (slider.name === 'max_new_tokens' && slider.value === '0')
        ? 'no cap'
        : slider.value;
    };
    slider.addEventListener('input', sync);
    sync();
  });

  function collect() {
    const values = {};
    const multi = {};

    form.querySelectorAll('[data-kind]').forEach((el) => {
      const kind = el.dataset.kind;
      const key = el.name;
      if (!key) return;

      if (kind === 'multiselect') {
        if (!multi[key]) multi[key] = [];
        if (el.checked) multi[key].push(el.value);
        return;
      }
      if (kind === 'bool') { values[key] = el.checked; return; }
      if (kind === 'slider' || kind === 'number') { values[key] = Number(el.value); return; }
      values[key] = el.value;
    });

    Object.assign(values, multi);
    return values;
  }

  function setState(text, cls) {
    if (!state) return;
    state.textContent = text;
    state.className = `save-state ${cls || ''}`.trim();
    const bar = document.getElementById('settings-actions');
    if (bar) bar.classList.toggle('is-dirty', cls === 'dirty');
  }

  if (form) {
    form.addEventListener('input', () => setState('Unsaved changes', 'dirty'));

    form.addEventListener('submit', async (event) => {
      event.preventDefault();

      const values = collect();

      // Validate the advanced JSON here so the user sees the problem next to
      // the field rather than as a server error.
      const advanced = document.getElementById('f-advanced');
      if (advanced && advanced.value.trim()) {
        try {
          const parsed = JSON.parse(advanced.value);
          if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
            throw new Error('must be a JSON object, e.g. {"key": value}');
          }
        } catch (err) {
          setState('Advanced parameters are not valid JSON', 'dirty');
          LS.toast(`Advanced parameters: ${err.message}`, 'error', 9000);
          advanced.focus();
          return;
        }
      }

      const submit = document.querySelector('button[type=submit][form="settings-form"]');
      submit.disabled = true;
      setState('Saving...');

      try {
        const result = await LS.post('/api/settings', { values });
        setState(
          result.changed.length
            ? `Saved (${LS.plural(result.changed.length, 'change')})`
            : 'Saved - nothing had changed',
          'saved');
        LS.renderEnginePill(result.engine);

        const needsReload = ['model', 'device', 'compute_type', 'cpu_threads', 'num_workers']
          .some((k) => result.changed.includes(k));
        if (needsReload) {
          LS.toast('Engine settings changed. The model reloads on the next transcription.', 'info');
        }
        if (result.changed.includes('offline_lock')) {
          // The model table's download buttons depend on the lock state.
          setTimeout(() => location.reload(), 900);
        }
      } catch (err) {
        setState('Not saved', 'dirty');
        LS.toast(`Could not save settings: ${err.message}`, 'error', 10000);
      } finally {
        submit.disabled = false;
      }
    });
  }

  const recheckHealth = document.getElementById('recheck-health');
  if (recheckHealth) {
    recheckHealth.addEventListener('click', async () => {
      recheckHealth.disabled = true;
      recheckHealth.textContent = 'Checking...';
      try {
        await LS.get('/api/health');
        location.reload();
      } catch (err) {
        LS.toast(err.message, 'error');
        recheckHealth.disabled = false;
        recheckHealth.textContent = 'Re-check';
      }
    });
  }

  /* ------------------------------------------------------ restore defaults */

  const resetBtn = document.getElementById('reset-settings');
  if (resetBtn) {
    resetBtn.addEventListener('click', async () => {
      if (!confirm(
        'Restore every setting to its default?\n\nThis includes the selected ' +
        'model, decoding options, and the offline lock. Downloaded models are ' +
        'not deleted.'
      )) return;
      try {
        await LS.post('/api/settings/reset');
        LS.toast('Settings restored to defaults.', 'ok');
        location.reload();
      } catch (err) {
        LS.toast(err.message, 'error');
      }
    });
  }

  /* ------------------------------------------------------- recommendation */

  /* Two recommendations: Turbo is the right transcription default but cannot
     translate, so translation has its own. Either only fills the form; nothing
     changes until Save settings is pressed. */
  [['apply-rec', 'Transcription'], ['apply-rec-translate', 'Translation']].forEach(([id, kind]) => {
    const button = document.getElementById(id);
    if (!button) return;
    button.addEventListener('click', () => {
      const setField = (fieldId, value) => {
        const el = document.getElementById(fieldId);
        if (!el) return;
        el.value = value;
        el.dispatchEvent(new Event('change', { bubbles: true }));
      };
      setField('f-model', button.dataset.model);
      setField('f-compute_type', button.dataset.compute);
      setField('f-device', button.dataset.device === 'cuda' ? 'auto' : button.dataset.device);
      setState(`${kind} recommendation applied - press Save settings`, 'dirty');
      LS.toast(`${kind} recommendation filled in. Press Save settings to apply it.`, 'info');
    });
  });

  const recheck = document.getElementById('recheck-hw');
  if (recheck) {
    recheck.addEventListener('click', async () => {
      recheck.disabled = true;
      recheck.textContent = 'Checking...';
      try {
        await LS.get('/api/models?refresh=true');
        location.reload();
      } catch (err) {
        LS.toast(err.message, 'error');
        recheck.disabled = false;
        recheck.textContent = 'Re-check hardware';
      }
    });
  }

  /* ------------------------------------------------------------ GPU setup */

  const installGpu = document.getElementById('install-gpu');
  if (installGpu) {
    installGpu.addEventListener('click', async () => {
      if (!confirm(
        'Install the CUDA support libraries (cuBLAS 12 and cuDNN 9) with pip?\n\n' +
        'This downloads roughly 700 MB from the Python package index into this ' +
        "app's virtual environment. It requires network access."
      )) return;

      installGpu.disabled = true;
      installGpu.textContent = 'Installing, this takes a few minutes...';
      if (window.LSConsole) LSConsole.open();

      try {
        const result = await LS.post('/api/engine/gpu-support');
        if (result.restart_required) {
          LS.toast(
            'GPU support installed. Restart Local Scribe to enable the GPU.',
            'ok', 15000);
          installGpu.textContent = 'Installed - restart to enable';
        } else if (result.hardware.cuda_usable) {
          LS.toast('GPU support installed. CUDA is now available.', 'ok', 9000);
          setTimeout(() => location.reload(), 1200);
        } else {
          LS.toast(
            'Libraries installed, but CUDA is still unavailable. See the console.',
            'warn', 12000);
          installGpu.disabled = false;
          installGpu.textContent = 'Install GPU support';
        }
      } catch (err) {
        LS.toast(err.message, 'error', 12000);
        installGpu.disabled = false;
        installGpu.textContent = 'Install GPU support';
      }
    });
  }

  /* ---------------------------------------------------------------- updates */

  const checkUpdates = document.getElementById('check-updates');
  if (checkUpdates) {
    checkUpdates.addEventListener('click', async () => {
      checkUpdates.disabled = true;
      checkUpdates.textContent = 'Checking...';
      try {
        const result = await LS.post('/api/update-check/refresh');
        LS.toast(
          result.update.available
            ? `Version ${result.update.latest} is available.`
            : 'Local Scribe is up to date.',
          'ok', 6000);
        location.reload();
      } catch (err) {
        LS.toast(err.message, 'error', 10000);
        checkUpdates.disabled = false;
        checkUpdates.textContent = 'Check now';
      }
    });
  }

  const applyUpdate = document.getElementById('apply-update');
  if (applyUpdate) {
    applyUpdate.addEventListener('click', async () => {
      if (!confirm(
        'Pull the latest version from GitHub?\n\n' +
        "This only updates this folder's source code - your projects, " +
        'models, and settings are untouched. Local Scribe needs to be ' +
        'restarted afterwards to finish.'
      )) return;

      applyUpdate.disabled = true;
      applyUpdate.textContent = 'Updating...';
      if (window.LSConsole) LSConsole.open();

      try {
        const result = await LS.post('/api/update/apply');
        LS.toast(result.message, 'ok', 12000);
      } catch (err) {
        LS.toast(err.message, 'error', 14000);
      } finally {
        applyUpdate.disabled = false;
        applyUpdate.textContent = 'Update now';
      }
    });
  }

  /* -------------------------------------------------------- model actions */

  const modelTable = document.querySelector('.model-table');
  if (modelTable) {
    modelTable.addEventListener('click', async (event) => {
      const button = event.target.closest('button[data-act]');
      if (!button) return;
      const key = button.dataset.model;
      const act = button.dataset.act;

      if (act === 'download') {
        const downloadBytes = Number(button.dataset.downloadBytes) || 0;
        const requiredBytes = Number(button.dataset.requiredBytes) || downloadBytes;
        if (!confirm(
          `Download ${key}?\n\nThe download is estimated at about ` +
          `${LS.bytes(downloadBytes)}. Keep about ${LS.bytes(requiredBytes)} ` +
          'free on the model storage drive for the download and normal app use.'
        )) return;
        button.disabled = true;
        button.textContent = 'Starting...';
        if (window.LSConsole) LSConsole.open();
        try {
          const result = await LS.post(`/api/models/${encodeURIComponent(key)}/download`);
          if (result.already) {
            LS.toast('That model is already on disk.', 'info');
            location.reload();
            return;
          }
          button.textContent = 'Downloading...';
          LS.toast(
            `Download queued; reserve about ${LS.bytes(requiredBytes)} free. ` +
            'Progress appears in the console.',
            'ok');
          watchDownload(key, button);
        } catch (err) {
          LS.toast(err.message, 'error', 10000);
          button.disabled = false;
          button.textContent = 'Download';
        }
        return;
      }

      if (act === 'delete') {
        if (!confirm(
          `Delete the ${key} weights from disk?\n\nYou can download them again ` +
          `later, which needs network access.`
        )) return;
        button.disabled = true;
        try {
          const result = await LS.del(`/api/models/${encodeURIComponent(key)}`);
          LS.toast(`Deleted, freeing ${LS.bytes(result.freed_bytes)}.`, 'ok');
          location.reload();
        } catch (err) {
          LS.toast(err.message, 'error');
          button.disabled = false;
        }
        return;
      }

      if (act === 'select') {
        try {
          const result = await LS.post('/api/settings', { values: { model: key } });
          LS.renderEnginePill(result.engine);
          LS.toast(`${key} selected.`, 'ok');
          location.reload();
        } catch (err) {
          LS.toast(err.message, 'error');
        }
      }
    });
  }

  /* Reload the page when a queued download finishes, so the table updates. */
  function watchDownload(key, button) {
    const timer = setInterval(async () => {
      try {
        const status = await LS.get('/api/jobs');
        const current = status.current;
        const stillRunning = current && current.kind === 'download';
        if (stillRunning) {
          button.textContent = `${Math.round((current.progress || 0) * 100)}%`;
          return;
        }
        const last = (status.jobs || []).find((j) => j.kind === 'download');
        if (last && last.status === 'done') {
          clearInterval(timer);
          LS.toast('Model downloaded and ready.', 'ok');
          location.reload();
        } else if (last && (last.status === 'failed' || last.status === 'cancelled')) {
          clearInterval(timer);
          button.disabled = false;
          button.textContent = 'Download';
          LS.toast(`Download ${last.status}: ${last.error || 'see the console'}`, 'error', 12000);
        }
      } catch (err) {
        clearInterval(timer);
      }
    }, 2000);
  }
})();
