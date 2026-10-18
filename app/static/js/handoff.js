/* Timestamped annotations and explicit offline review-package transfers. */

(() => {
  const editor = window.LSEditor;
  if (!editor) return;

  const api = `${editor.api}/handoff`;
  const annotationsList = document.getElementById('annotations-list');
  const annotationsEmpty = document.getElementById('annotations-empty');
  const annotationTime = document.getElementById('annotation-time');
  const annotationText = document.getElementById('annotation-text');
  const annotationKind = document.getElementById('annotation-kind');
  const annotationAuthor = document.getElementById('annotation-author');
  const status = document.getElementById('handoff-status');
  const conflictsHost = document.getElementById('handoff-conflicts');

  function clock(seconds) {
    return LS.clock(seconds);
  }

  function renderAnnotations() {
    const annotations = [...editor.annotations()].sort((a, b) =>
      Number(a.at) - Number(b.at) ||
      String(a.created_at).localeCompare(String(b.created_at)));
    annotationsList.replaceChildren();
    annotations.forEach((item) => {
      const row = document.createElement('li');
      row.className = 'review-item';
      const seek = document.createElement('button');
      seek.type = 'button';
      seek.className = 'btn btn-small btn-quiet';
      seek.textContent = `${item.kind === 'question' ? 'Question' : 'Comment'} · ${clock(item.at)}`;
      seek.addEventListener('click', () => editor.playFrom(Number(item.at)));
      const text = document.createElement('span');
      text.className = 'review-item-text';
      text.textContent = item.text;
      const meta = document.createElement('span');
      meta.className = 'small muted';
      meta.textContent = [
        item.author,
        item.created_at ? new Date(item.created_at).toLocaleString() : '',
      ].filter(Boolean).join(' · ');
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'btn btn-small btn-quiet';
      remove.textContent = 'Remove';
      remove.addEventListener('click', async () => {
        try {
          const result = await LS.del(
            `${editor.api}/annotations/${encodeURIComponent(item.id)}`
          );
          editor.setAnnotations(result.annotations);
          renderAnnotations();
        } catch (err) {
          LS.toast(`Could not remove this note: ${err.message}`, 'error');
        }
      });
      row.append(seek, text, meta, remove);
      annotationsList.appendChild(row);
    });
    annotationsEmpty.hidden = annotations.length > 0;
  }

  document.getElementById('annotation-use-playhead').addEventListener('click', () => {
    annotationTime.value = Number(editor.currentTime()).toFixed(1);
  });

  document.getElementById('annotation-add').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const text = annotationText.value.trim();
    if (!text) {
      LS.toast('Enter a comment or question first.', 'warn');
      annotationText.focus();
      return;
    }
    button.disabled = true;
    try {
      const result = await LS.post(`${editor.api}/annotations`, {
        kind: annotationKind.value,
        text,
        at: Number(annotationTime.value),
        author: annotationAuthor.value.trim(),
      });
      editor.setAnnotations(result.annotations);
      annotationText.value = '';
      renderAnnotations();
      LS.toast('Timestamped note saved with this transcript.', 'ok');
    } catch (err) {
      LS.toast(`Could not save this note: ${err.message}`, 'error');
    } finally {
      button.disabled = false;
    }
  });

  async function downloadPackage() {
    const button = document.getElementById('handoff-export');
    editor.flush();
    button.disabled = true;
    status.textContent = 'Preparing the review package…';
    try {
      const response = await fetch(`${api}/export`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Instance-Token': document.body.dataset.token || '',
        },
        body: JSON.stringify({
          include_media: document.getElementById('handoff-include-media').checked,
          instructions: document.getElementById('handoff-instructions').value,
        }),
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || `${response.status} ${response.statusText}`);
      }
      const blob = await response.blob();
      const disposition = response.headers.get('content-disposition') || '';
      const match = disposition.match(/filename="?([^";]+)"?/i);
      const link = document.createElement('a');
      const objectUrl = URL.createObjectURL(blob);
      link.href = objectUrl;
      link.download = match ? match[1] : 'review-package.lsreview';
      link.click();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
      status.textContent = 'Review package downloaded. Send it to the reviewer and import the returned package here.';
    } catch (err) {
      status.textContent = '';
      LS.toast(`Could not export the review package: ${err.message}`, 'error');
    } finally {
      button.disabled = false;
    }
  }

  document.getElementById('handoff-export').addEventListener('click', downloadPackage);

  const importButton = document.getElementById('handoff-import');
  const fileInput = document.getElementById('handoff-file');
  importButton.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', async () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    const body = new FormData();
    body.append('file', file);
    importButton.disabled = true;
    status.textContent = 'Importing returned review package…';
    try {
      const result = await LS.request('POST', `${api}/import`, body);
      editor.setAnnotations(result.annotations || []);
      renderAnnotations();
      if (result.conflict) {
        renderConflicts(result.conflicts || []);
        status.textContent =
          'The local transcript changed after handoff. Both versions were preserved; compare them below to resolve the conflict.';
      } else {
        status.textContent =
          'Returned transcript and annotations imported. The previous transcript is available in edit history.';
        location.reload();
      }
    } catch (err) {
      status.textContent = '';
      LS.toast(`Could not import the review package: ${err.message}`, 'error', 10000);
    } finally {
      importButton.disabled = false;
      fileInput.value = '';
    }
  });

  function renderConflicts(conflicts) {
    conflictsHost.replaceChildren();
    const unresolved = conflicts.filter((item) => item.status === 'unresolved');
    if (!unresolved.length) return;
    const title = document.createElement('h4');
    title.textContent = 'Transcript conflicts to resolve';
    conflictsHost.appendChild(title);
    unresolved.forEach((conflict) => {
      const section = document.createElement('section');
      section.className = 'notice notice-warn small';
      const description = document.createElement('p');
      description.textContent =
        'The transcript changed on both sides. Compare the saved versions; neither was discarded.';
      section.appendChild(description);
      ['local', 'incoming'].forEach((version) => {
        const link = document.createElement('a');
        link.className = 'btn btn-small btn-quiet';
        link.href = `${api}/conflicts/${encodeURIComponent(conflict.id)}/${version}`;
        link.textContent = `Download ${version === 'local' ? 'current' : 'imported'} version`;
        link.target = '_blank';
        link.rel = 'noopener';
        section.appendChild(link);
      });
      [['local', 'Keep current'], ['incoming', 'Use imported']].forEach(([choice, label]) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `btn btn-small${choice === 'incoming' ? ' btn-primary' : ''}`;
        button.textContent = label;
        button.addEventListener('click', async () => {
          if (choice === 'incoming' && !window.confirm(
            'Replace the current transcript with the imported version? Both versions remain available in the conflict archive.'
          )) return;
          button.disabled = true;
          try {
            await LS.post(`${api}/conflicts/${encodeURIComponent(conflict.id)}/resolve`, { choice });
            LS.toast(`Conflict resolved: ${label.toLowerCase()}.`, 'ok');
            location.reload();
          } catch (err) {
            button.disabled = false;
            LS.toast(`Could not resolve conflict: ${err.message}`, 'error');
          }
        });
        section.appendChild(button);
      });
      conflictsHost.appendChild(section);
    });
  }

  async function loadConflicts() {
    try {
      const result = await LS.get(`${api}/conflicts`);
      renderConflicts(result.conflicts || []);
    } catch (err) {
      LS.toast(`Could not load offline-review conflicts: ${err.message}`, 'warn');
    }
  }

  renderAnnotations();
  loadConflicts();
})();
