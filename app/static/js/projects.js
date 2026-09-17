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
      body.append('mode', 'rename');

      importBtn.disabled = true;
      importBtn.textContent = 'Importing...';
      if (window.LSConsole) LSConsole.open();

      try {
        const result = await LS.request('POST', '/api/projects/import', body);
        LS.toast(`Imported "${result.project.title}".`, 'ok');
        location.href = `/project/${result.project.slug}`;
      } catch (err) {
        LS.toast(`Import failed: ${err.message}`, 'error', 10000);
        importBtn.disabled = false;
        importBtn.textContent = 'Import project';
        importInput.value = '';
      }
    });
  }
})();
