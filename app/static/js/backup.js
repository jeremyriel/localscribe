/* Per-project backup folder, backup runs and local history on the project page. */

(() => {
  const card = document.getElementById('box-backup-card');
  if (!card || !window.PROJECT) return;
  const base = `/api/projects/${encodeURIComponent(window.PROJECT.slug)}`;

  const $ = (id) => document.getElementById(id);
  const pathText = $('backup-folder-path');
  const folderStatus = $('backup-folder-status');
  const legacyNotice = $('backup-legacy');
  const browseBtn = $('backup-browse');
  const clearBtn = $('backup-clear');
  const manual = $('backup-manual');
  const pathInput = $('backup-path-input');
  const saveBtn = $('backup-path-save');
  const actions = $('backup-actions');
  const healthField = $('box-health-field');
  const startBtn = $('start-box-backup-btn');
  const cancelBtn = $('cancel-box-backup-btn');
  const runStatus = $('box-backup-status');
  const historyWrap = $('backup-history-wrap');
  const historyEmpty = $('backup-history-empty');
  const historyBody = document.querySelector('#backup-history tbody');

  let state = window.PROJECT.backup || {};
  let jobId = null;
  let poll = null;
  let reported = null;

  function when(iso) {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? (iso || 'Unknown time') : date.toLocaleString();
  }

  function renderHistory() {
    const events = state.history || [];
    historyBody.textContent = '';
    historyWrap.hidden = events.length === 0;
    historyEmpty.hidden = events.length !== 0;
    events.forEach((event) => {
      const row = document.createElement('tr');
      const result = { complete: 'Completed', failed: 'Failed', cancelled: 'Cancelled' }[event.status] || event.status;
      const detail = event.status === 'failed' && event.error ? ` - ${event.error}` : '';
      [
        when(event.finished_at),
        result + detail,
        event.local_copy_verified
          ? `Verified by checksum${event.total_bytes ? ` (${LS.bytes(event.total_bytes)})` : ''}`
          : 'Not verified',
        'Unknown - Local Scribe cannot see Box',
      ].forEach((text) => {
        const cell = document.createElement('td');
        cell.textContent = text;
        row.appendChild(cell);
      });
      historyBody.appendChild(row);
    });
  }

  function render() {
    const configured = !!state.configured;
    pathText.textContent = configured ? state.destination : 'None chosen. Local Scribe never picks a folder for you.';
    folderStatus.textContent = !configured ? ''
      : (state.available ? 'Folder is available and writable.'
        : `Folder problem: ${state.problem || 'unavailable'}. Choose another folder or fix the problem.`);
    folderStatus.className = `small ${configured && !state.available ? 'badge-err' : 'muted'}`;
    browseBtn.textContent = configured ? 'Change folder…' : 'Browse…';
    clearBtn.hidden = !configured;
    legacyNotice.hidden = !state.legacy_available;
    actions.hidden = !(configured && state.available);
    healthField.hidden = !state.needs_box_health;
    renderHistory();
  }

  function adopt(next) {
    state = next || state;
    render();
  }

  async function saveFolder(destination, selection) {
    folderStatus.textContent = 'Saving…';
    try {
      const result = await LS.put(`${base}/backup`, { destination, selection });
      adopt(result.backup);
      LS.toast('Backup folder saved for this project.', 'ok');
      return true;
    } catch (err) {
      render();
      folderStatus.textContent = `The folder was not changed: ${err.message}`;
      folderStatus.className = 'small badge-err';
      return false;
    }
  }

  browseBtn.addEventListener('click', async () => {
    browseBtn.disabled = true;
    try {
      const picked = await LS.post('/api/folder-picker', { initial_path: state.destination || '' });
      if (!picked.path) {
        folderStatus.textContent = 'No folder was chosen; the saved folder is unchanged.';
        return;
      }
      await saveFolder(picked.path, 'picker');
    } catch (err) {
      manual.open = true;
      pathInput.focus();
      folderStatus.textContent = `A folder picker is not available here (${err.message}). Enter the path below.`;
    } finally {
      browseBtn.disabled = false;
    }
  });

  saveBtn.addEventListener('click', async () => {
    const value = pathInput.value.trim();
    if (!value) { pathInput.focus(); return; }
    if (await saveFolder(value, 'manual')) pathInput.value = '';
  });
  pathInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); saveBtn.click(); }
  });

  clearBtn.addEventListener('click', async () => {
    clearBtn.disabled = true;
    try {
      const result = await LS.del(`${base}/backup`);
      adopt(result.backup);
      LS.toast('Backup folder cleared. Earlier backup history is kept.', 'info');
    } catch (err) {
      LS.toast(err.message, 'error');
    } finally {
      clearBtn.disabled = false;
    }
  });

  $('backup-use-previous').addEventListener('click', async () => {
    try {
      const result = await LS.post(`${base}/backup/use-previous-folder`);
      adopt(result.backup);
      LS.toast('Earlier shared folder is now set for this project only.', 'ok');
    } catch (err) {
      folderStatus.textContent = `The earlier folder could not be used: ${err.message}`;
    }
  });

  async function refreshState() {
    try { adopt((await LS.get(`${base}/backup`)).backup); } catch (_) { /* keep what is shown */ }
  }

  function showJob(job) {
    if (!job) return;
    const labels = {
      queued: 'Waiting to start…',
      running: job.stage === 'building_archive'
        ? 'Building checksummed archive…'
        : `Copying archive (${LS.bytes(job.bytes_copied)} of ${LS.bytes(job.total_bytes)})…`,
      complete: `Local copy verified by checksum: ${job.filename} (${LS.bytes(job.total_bytes)}). ` +
        'Whether Box has uploaded it is not known to Local Scribe.',
      failed: `Backup failed: ${job.error || 'Retry the operation.'}`,
      cancelled: 'Backup cancelled. Any incomplete copy was removed.',
    };
    runStatus.textContent = labels[job.status] || 'Checking backup status…';
    const active = job.status === 'queued' || job.status === 'running';
    startBtn.disabled = active;
    cancelBtn.hidden = !active;
    if (!active) {
      if (poll) { clearInterval(poll); poll = null; }
      if (reported !== job.job_id) {
        reported = job.job_id;
        if (job.status === 'complete') LS.toast('Local copy written and verified. Box upload is not confirmed.', 'ok');
        refreshState();
      }
    }
  }

  async function pollJob() {
    if (!jobId) return;
    try {
      showJob((await LS.get(`${base}/box-backup?job_id=${encodeURIComponent(jobId)}`)).backup);
    } catch (err) {
      runStatus.textContent = err.message;
      if (poll) clearInterval(poll);
      poll = null;
    }
  }

  startBtn.addEventListener('click', async () => {
    if (!$('confirm-box-sync-destination').checked) {
      LS.toast('Confirm that this folder belongs to your Box Sync client.', 'warn');
      return;
    }
    startBtn.disabled = true;
    runStatus.textContent = 'Starting backup…';
    try {
      const result = await LS.post(`${base}/box-backup`, {
        confirm_sync_destination: true,
        independently_verified_box_health: $('confirm-box-health').checked,
        include_media: $('box-backup-include-media').checked,
      });
      jobId = result.backup.job_id;
      showJob(result.backup);
      if (!poll) poll = setInterval(pollJob, 700);
    } catch (err) {
      runStatus.textContent = err.message;
      startBtn.disabled = false;
    }
  });

  cancelBtn.addEventListener('click', async () => {
    if (!jobId) return;
    cancelBtn.disabled = true;
    try {
      await LS.post(`${base}/box-backup/${encodeURIComponent(jobId)}/cancel`);
      runStatus.textContent = 'Cancellation requested…';
    } catch (err) {
      LS.toast(err.message, 'warn');
    } finally {
      cancelBtn.disabled = false;
    }
  });

  render();
  LS.get(`${base}/box-backup`)
    .then((result) => {
      jobId = result.backup.job_id;
      if (result.backup.status !== 'queued' && result.backup.status !== 'running') {
        reported = jobId;       // an earlier run: show it quietly
      }
      showJob(result.backup);
      if (result.backup.status === 'queued' || result.backup.status === 'running') {
        poll = setInterval(pollJob, 700);
      }
    })
    .catch(() => {});
})();
