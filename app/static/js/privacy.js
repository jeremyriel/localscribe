/* Activity & Diagnostics: preview sanitized diagnostics, then save only on request. */

(() => {
  const previewButton = document.getElementById('preview-diagnostics');
  const preview = document.getElementById('diagnostics-preview');
  const inspected = document.getElementById('diagnostics-inspected');
  const saveButton = document.getElementById('save-diagnostics');
  const status = document.getElementById('diagnostics-status');
  let diagnosticText = '';

  if (!previewButton || !preview || !inspected || !saveButton) return;

  const updateSaveState = () => {
    saveButton.disabled = !diagnosticText || !inspected.checked;
  };

  previewButton.addEventListener('click', async () => {
    previewButton.disabled = true;
    previewButton.textContent = 'Preparing preview...';
    inspected.checked = false;
    inspected.disabled = true;
    diagnosticText = '';
    updateSaveState();
    try {
      const data = await LS.get('/api/diagnostics');
      diagnosticText = `${JSON.stringify(data, null, 2)}\n`;
      preview.value = diagnosticText;
      inspected.disabled = false;
      status.textContent = 'Preview ready. Review the JSON before enabling save.';
    } catch (err) {
      preview.value = '';
      status.textContent = `Could not prepare diagnostics: ${err.message}`;
    } finally {
      previewButton.disabled = false;
      previewButton.textContent = 'Generate preview';
    }
  });

  inspected.addEventListener('change', updateSaveState);

  saveButton.addEventListener('click', () => {
    if (!diagnosticText || !inspected.checked) return;
    const blob = new Blob([diagnosticText], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'localscribe-diagnostics.json';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
    status.textContent = 'Diagnostics saved from the inspected preview.';
  });
})();
