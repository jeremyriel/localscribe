/* Assistive, explicitly reversible de-identification review. */

(() => {
  const D = window.DOC;
  if (!D || !window.LSEditor) return;

  const panel = document.getElementById('deidentification-review');
  if (!panel) return;
  const api = `${LSEditor.api}/deidentification`;
  const flags = [...panel.querySelectorAll('[data-deid-flag]')];
  const patterns = document.getElementById('deid-custom-patterns');
  const scanButton = document.getElementById('deid-scan');
  const status = document.getElementById('deid-status');
  const counts = document.getElementById('deid-counts');
  const list = document.getElementById('deid-items');
  let busy = false;

  function patternValues() {
    return patterns.value.split(/\r?\n/).map((line) => {
      const colon = line.indexOf(':');
      if (colon < 1) return null;
      const label = line.slice(0, colon).trim();
      const pattern = line.slice(colon + 1).trim();
      return label && pattern ? { label, pattern } : null;
    }).filter(Boolean);
  }

  function setPreferences(report) {
    if (report.flags) {
      flags.forEach((input) => {
        input.checked = report.flags[input.dataset.deidFlag] !== false;
      });
    }
    if (Array.isArray(report.custom_patterns)) {
      patterns.value = report.custom_patterns
        .map((item) => `${item.label}: ${item.pattern}`).join('\n');
    }
  }

  function render(report) {
    setPreferences(report);
    counts.replaceChildren();
    ['flagged', 'resolved', 'skipped'].forEach((kind) => {
      const chip = document.createElement('span');
      chip.textContent = `${kind[0].toUpperCase()}${kind.slice(1)}: ${report.counts?.[kind] || 0}`;
      counts.appendChild(chip);
    });
    list.replaceChildren();
    (report.items || []).forEach((item) => {
      const row = document.createElement('li');
      row.className = 'review-item';
      const description = document.createElement('span');
      description.className = 'review-item-text';
      description.textContent = `${item.label} · “${item.text}” · segment ${Number(item.segment_id) + 1}`;
      const badge = document.createElement('span');
      badge.className = 'small muted';
      badge.textContent = item.status === 'resolved'
        ? `Replaced with ${item.replacement}`
        : item.status === 'skipped' ? `Skipped${item.note ? ` · ${item.note}` : ''}` : 'Flagged';
      row.append(description, badge);
      if (item.status === 'flagged') {
        row.append(
          actionButton('Replace', () => runAction('replace', item.id)),
          actionButton('Skip', () => runAction('skip', item.id)),
        );
      } else if (item.status === 'resolved') {
        row.append(actionButton('Undo replacement', () => runAction('undo', item.id)));
      }
      list.appendChild(row);
    });
  }

  function actionButton(label, handler) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-small';
    button.textContent = label;
    button.disabled = busy;
    button.addEventListener('click', handler);
    return button;
  }

  async function runAction(action, itemId) {
    if (busy) return;
    busy = true;
    status.textContent = action === 'replace'
      ? 'Saving a reversible replacement…' : 'Saving review status…';
    try {
      if (action !== 'skip') await LSEditor.flush();
      const result = await LS.post(`${api}/${action}`, { item_id: itemId });
      if (result.transcript) LSEditor.setTranscript(result.transcript);
      render(result);
      status.textContent = action === 'replace'
        ? 'Replacement saved to transcript and edit history.'
        : action === 'undo' ? 'Replacement undone and recorded in edit history.' : 'Finding skipped.';
    } catch (err) {
      status.textContent = '';
      LS.toast(`Could not update de-identification review: ${err.message}`, 'error');
    } finally {
      busy = false;
      [...list.querySelectorAll('button')].forEach((button) => { button.disabled = false; });
    }
  }

  scanButton.addEventListener('click', async () => {
    if (busy) return;
    busy = true;
    scanButton.disabled = true;
    status.textContent = 'Saving edits and scanning…';
    try {
      await LSEditor.flush();
      const selected = Object.fromEntries(flags.map((input) => [
        input.dataset.deidFlag, input.checked,
      ]));
      const result = await LS.post(`${api}/scan`, {
        flags: selected,
        custom_patterns: patternValues(),
      });
      render(result);
      status.textContent = 'Scan complete. Review each result before replacing or skipping it.';
    } catch (err) {
      status.textContent = '';
      LS.toast(`Could not scan transcript: ${err.message}`, 'error');
    } finally {
      busy = false;
      scanButton.disabled = false;
    }
  });

  LS.get(api).then(render).catch((err) => {
    status.textContent = `Could not load local review report: ${err.message}`;
  });
})();
