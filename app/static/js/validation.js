/* Explicit local validation sign-off and reopening controls. */

(() => {
  const D = window.DOC;
  const panel = document.getElementById('validation-panel');
  if (!D || !panel) return;

  const api = `/api/projects/${encodeURIComponent(D.slug)}/documents/${encodeURIComponent(D.id)}`;
  const reviewer = document.getElementById('validation-reviewer');
  const notes = document.getElementById('validation-notes');
  const status = document.getElementById('workflow-status');
  const exportProvenance = document.getElementById('export-provenance');
  const lockNotice = document.getElementById('validation-lock');
  const signoff = document.getElementById('validation-signoff');
  const startButton = document.getElementById('review-start');
  const validateButton = document.getElementById('validation-sign');
  const reopenButton = document.getElementById('validation-reopen');
  const history = document.getElementById('validation-history');
  const historySummary = panel.querySelector('.validation-history summary');
  let workflow = D.workflow || D.transcript.validation || {
    status: 'transcribed', reviewer: '', validated_at: null, notes: '', history: [],
  };
  let busy = false;
  reviewer.value = workflow.reviewer || reviewer.value;

  function render() {
    panel.dataset.status = workflow.status;
    status.textContent = workflow.label || workflow.status;
    lockNotice.hidden = !['validated', 'exported'].includes(workflow.status);
    const exported = workflow.export || {};
    exportProvenance.textContent = exported.exported_at
      ? `Latest export: ${exported.status} · ${exported.exported_at}` +
        (exported.formats && exported.formats.length
          ? ` · ${exported.formats.join(', ')}` : '')
      : 'No export recorded yet.';
    startButton.hidden = workflow.status !== 'transcribed';
    validateButton.hidden = workflow.status !== 'in_review';
    reopenButton.hidden = !['validated', 'exported'].includes(workflow.status);
    if (workflow.validated_at) {
      signoff.textContent = `Validated by ${workflow.reviewer || 'unknown reviewer'} · ${workflow.validated_at}` +
        (workflow.notes ? ` · ${workflow.notes}` : '');
    } else {
      signoff.textContent = 'No validation sign-off recorded.';
      signoff.classList.add('muted');
    }
    signoff.classList.toggle('muted', !workflow.validated_at);
    history.replaceChildren();
    (workflow.history || []).slice().reverse().forEach((event) => {
      const item = document.createElement('li');
      const heading = document.createElement('strong');
      heading.textContent = String(event.action || '').replaceAll('_', ' ')
        .replace(/\b\w/g, (letter) => letter.toUpperCase());
      item.append(heading, document.createTextNode(
        ` · ${event.reviewer || 'Reviewer not recorded'} · ${event.at || ''}`,
      ));
      if (event.notes) {
        item.append(document.createElement('br'), document.createTextNode(event.notes));
      }
      if (event.formats && event.formats.length) {
        item.append(document.createElement('br'),
          document.createTextNode(`Formats: ${event.formats.join(', ')}`));
      }
      history.appendChild(item);
    });
    historySummary.textContent = `Validation and export history (${(workflow.history || []).length})`;
    document.querySelectorAll('.workflow-stages li').forEach((item, index) => {
      const stage = workflow.stages && workflow.stages[index];
      item.classList.toggle('active', Boolean(stage && stage.active));
      if (stage && stage.active) item.setAttribute('aria-current', 'step');
      else item.removeAttribute('aria-current');
    });
    [startButton, validateButton, reopenButton].forEach((button) => {
      button.disabled = busy;
    });
  }

  async function transition(action) {
    if (busy) return;
    const name = reviewer.value.trim();
    const comment = notes.value.trim();
    if (action !== 'start' && !name) {
      LS.toast('Enter the reviewer’s name before continuing.', 'error');
      reviewer.focus();
      return;
    }
    if (action === 'reopen') {
      if (!comment) {
        LS.toast('Enter a reason before reopening validation.', 'error');
        notes.focus();
        return;
      }
      if (!window.confirm('Reopen validation and return this transcript to In review?')) return;
    }
    busy = true;
    render();
    try {
      const result = await LS.post(`${api}/validation`, {
        action, reviewer: name, notes: comment,
      });
      workflow = result.validation;
      if (action === 'validate') notes.value = '';
      render();
      LS.toast(
        action === 'validate' ? 'Validation sign-off saved locally.' :
          action === 'reopen' ? 'Validation reopened and recorded.' : 'Review started.',
        'ok',
      );
    } catch (err) {
      LS.toast(`Could not update validation: ${err.message}`, 'error');
    } finally {
      busy = false;
      render();
    }
  }

  startButton.addEventListener('click', () => transition('start'));
  validateButton.addEventListener('click', () => transition('validate'));
  reopenButton.addEventListener('click', () => transition('reopen'));
  document.addEventListener('ls:workflow', (event) => {
    if (event.detail) {
      workflow = event.detail;
      render();
    }
  });
  render();
})();
