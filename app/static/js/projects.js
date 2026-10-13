/* Project list page: create, import. */

(() => {
  const form = document.getElementById('new-project');
  const openButtons = ['new-project-btn', 'empty-new']
    .map((id) => document.getElementById(id))
    .filter(Boolean);

  function show(open) {
    if (!form) return;
    form.hidden = !open;
    if (open) {
      const title = document.getElementById('np-title');
      if (title) title.focus();
    }
  }

  openButtons.forEach((button) => button.addEventListener('click', () => show(true)));
  const cancel = document.getElementById('np-cancel');
  if (cancel) cancel.addEventListener('click', () => show(false));

  if (form) {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const data = new FormData(form);
      const title = (data.get('title') || '').toString().trim();
      if (!title) {
        LS.toast('A project needs a title.', 'warn');
        return;
      }
      const submit = form.querySelector('button[type=submit]');
      submit.disabled = true;
      try {
        const result = await LS.post('/api/projects', {
          title,
          principal_investigator: (data.get('principal_investigator') || '').toString(),
          irb_protocol: (data.get('irb_protocol') || '').toString(),
          description: (data.get('description') || '').toString(),
          data_sensitivity: data.get('data_sensitivity').toString(),
        });
        location.href = `/project/${result.project.slug}`;
      } catch (err) {
        LS.toast(`Could not create the project: ${err.message}`, 'error');
        submit.disabled = false;
      }
    });
  }

  /* --------------------------------------------------------------- import */

  const importBtn = document.getElementById('import-btn');
  const importInput = document.getElementById('import-input');

  if (importBtn && importInput) {
    importBtn.addEventListener('click', () => importInput.click());

    importInput.addEventListener('change', async () => {
      const file = importInput.files && importInput.files[0];
      if (!file) return;

      const body = new FormData();
      body.append('file', file);
      const replace = confirm(
        'If a project with this archive’s name already exists, replace it?\n\n' +
        'Choose Cancel to keep both projects by importing a renamed copy.'
      );
      const mode = replace ? 'replace' : 'rename';
      let snapshotBeforeReplace = null;
      if (replace) {
        snapshotBeforeReplace = confirm(
          'Create a local recovery snapshot before replacing an existing project?\n\n' +
          'Choose Cancel to skip the snapshot.'
        ) ? 'yes' : 'no';
        if (snapshotBeforeReplace === 'no' && !confirm(
          'Replace an existing project without a recovery snapshot?'
        )) {
          importInput.value = '';
          return;
        }
      }
      body.append('mode', mode);
      if (snapshotBeforeReplace) {
        body.append('snapshot_before_replace', snapshotBeforeReplace);
      }

      importBtn.disabled = true;
      importBtn.textContent = 'Importing...';
      if (window.LSConsole) LSConsole.open();

      try {
        const result = await LS.request('POST', '/api/projects/import', body);
        const project = result.project;
        const integrity = project.archive_integrity_verified
          ? 'Archive integrity verified.'
          : `Archive format v${project.archive_format_version || 1} has no verifiable checksums.`;
        const missing = project.missing_media_documents || [];
        const media = project.archive_includes_media === false
          ? ' This transcript-only archive did not include source media.'
          : '';
        const missingDetail = missing.length
          ? ` ${missing.length} recording${missing.length === 1 ? ' is' : 's are'} missing source media.`
          : '';
        window.alert(`Imported "${project.title}". ${integrity}${media}${missingDetail}`);
        location.href = `/project/${result.project.slug}`;
      } catch (err) {
        LS.toast(`Import failed: ${err.message}`, 'error', 10000);
        importBtn.disabled = false;
        importBtn.textContent = 'Import project';
        importInput.value = '';
      }
    });
  }

  const restoreBtn = document.getElementById('restore-library-snapshot-btn');
  if (restoreBtn) {
    restoreBtn.addEventListener('click', async () => {
      const select = document.getElementById('recovery-snapshot-select');
      const option = select && select.selectedOptions[0];
      const mode = document.getElementById('recovery-restore-mode').value;
      if (!option || mode === 'abort') {
        LS.toast('No restore was performed.', 'info');
        return;
      }
      if (mode === 'replace' && !confirm(
        'Replace the existing project with this snapshot? A safety snapshot ' +
        'will be created first.'
      )) return;

      restoreBtn.disabled = true;
      try {
        const result = await LS.post(
          `/api/snapshots/${encodeURIComponent(option.dataset.slug)}/` +
          `${encodeURIComponent(option.value)}/restore`,
          { mode }
        );
        if (result.aborted) {
          LS.toast('Restore cancelled.', 'info');
        } else {
          location.href = `/project/${encodeURIComponent(result.project.slug)}`;
        }
      } catch (err) {
        LS.toast(`Could not restore the snapshot: ${err.message}`, 'error', 10000);
        restoreBtn.disabled = false;
      }
    });
  }
})();
