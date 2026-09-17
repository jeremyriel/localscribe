/* The speaker roster: how many speakers, what they are called, their colours.

   Tagging itself lives in editor.js, because it happens on the turns. This
   module owns the roster above the transcript: the count stepper, the name and
   acronym fields, and the colour choice.

   Colour is never the only signal. Every chip and row shows the acronym as
   text, so the interface reads correctly in greyscale and for anyone who
   cannot distinguish the palette. */

(() => {
  const D = window.DOC;
  if (!D || !window.LSEditor) return;

  const host = document.getElementById('speaker-rows');
  const empty = document.getElementById('speaker-empty');
  const countInput = document.getElementById('spk-count');
  const minus = document.getElementById('spk-minus');
  const plus = document.getElementById('spk-plus');
  const state = document.getElementById('spk-state');
  if (!host || !countInput) return;

  const palette = D.palette || ['#0f766e'];
  const MAX = 12;

  let roster = (D.speakers || []).map((s) => ({ ...s }));

  function setState(text, cls) {
    if (!state) return;
    state.textContent = text || '';
    state.className = `save-state ${cls || ''}`.trim();
  }

  /* Derive an acronym the way the server does, so the two agree until the
     validator overrides it. */
  function deriveShort(name, taken) {
    const text = (name || '').trim();
    if (!text) return '';
    const used = new Set((taken || []).filter(Boolean).map((t) => t.toUpperCase()));
    const parts = text.split(/[\s_-]+/).filter(Boolean);

    let candidate;
    if (parts.length >= 2) {
      candidate = /^\d+$/.test(parts[parts.length - 1])
        ? parts[0][0] + parts[parts.length - 1]
        : parts.slice(0, 3).map((p) => p[0]).join('');
    } else {
      const word = parts[0];
      candidate = (word[0] + word.slice(1).replace(/[aeiou]/gi, '')).slice(0, 3) || word.slice(0, 3);
    }

    candidate = candidate.toUpperCase().slice(0, 4);
    if (!used.has(candidate)) return candidate;
    for (let n = 2; n < 100; n += 1) {
      const probe = `${candidate.slice(0, 3)}${n}`;
      if (!used.has(probe)) return probe;
    }
    return candidate;
  }

  function nextId() {
    const used = new Set(roster.map((s) => s.id));
    for (let n = 1; n < 500; n += 1) {
      if (!used.has(`s${n}`)) return `s${n}`;
    }
    return `s${Date.now()}`;
  }

  function addSpeaker(name) {
    const index = roster.length;
    const label = name || `Speaker ${index + 1}`;
    roster.push({
      id: nextId(),
      name: label,
      short: deriveShort(label, roster.map((s) => s.short)),
      color: palette[index % palette.length],
    });
  }

  function turnsFor(speakerId) {
    return window.LSEditor.turns().filter((t) => t.speakerId === speakerId).length;
  }

  function render() {
    host.innerHTML = '';
    if (empty) empty.hidden = roster.length > 0;

    roster.forEach((speaker, index) => {
      const row = document.createElement('div');
      row.className = 'speaker-row';
      row.style.setProperty('--speaker-color', speaker.color);
      row.dataset.id = speaker.id;
      row.innerHTML = `
        <button type="button" class="swatch-btn" data-act="colour"
                title="Change this speaker's colour"
                aria-label="Colour for ${LS.escapeHtml(speaker.name)}"></button>
        <span class="speaker-key" aria-hidden="true">${index < 9 ? index + 1 : ''}</span>
        <input class="speaker-name" type="text" data-act="name"
               value="${LS.escapeHtml(speaker.name)}"
               placeholder="Full name or pseudonym"
               aria-label="Name for speaker ${index + 1}">
        <input class="speaker-short" type="text" data-act="short" maxlength="6"
               value="${LS.escapeHtml(speaker.short || '')}"
               placeholder="Tag"
               aria-label="Short tag for speaker ${index + 1}">
        <span class="speaker-turns small muted">${turnsFor(speaker.id)} turns</span>
        <button type="button" class="btn btn-small btn-quiet btn-danger"
                data-act="remove" title="Remove this speaker">Remove</button>`;
      host.appendChild(row);
    });

    countInput.value = roster.length;
  }

  /* ------------------------------------------------------------- persistence */

  const save = LS.debounce(async () => {
    setState('Saving...');
    try {
      const result = await LS.put(`${window.LSEditor.api}/speakers`, { speakers: roster });
      roster = result.speakers.map((s) => ({ ...s }));
      window.LSEditor.setRoster(roster);
      render();
      setState('Saved', 'saved');

      if (result.unassigned_segments) {
        LS.toast(
          `${LS.plural(result.unassigned_segments, 'segment')} lost its speaker ` +
          `because ${result.removed_speakers.join(', ')} was removed.`, 'warn', 9000);
      }
    } catch (err) {
      setState('Not saved', 'dirty');
      LS.toast(`Could not save the speakers: ${err.message}`, 'error', 9000);
    }
  }, 700);

  function changed() {
    setState('Unsaved changes', 'dirty');
    window.LSEditor.setRoster(roster);
    save();
  }

  /* ----------------------------------------------------------------- events */

  host.addEventListener('input', (event) => {
    const field = event.target.closest('[data-act]');
    if (!field) return;
    const row = field.closest('.speaker-row');
    const speaker = roster.find((s) => s.id === row.dataset.id);
    if (!speaker) return;

    if (field.dataset.act === 'name') {
      speaker.name = field.value;
      // Keep the acronym in step while the validator has not set one of their
      // own, but never overwrite a deliberate choice.
      const shortField = row.querySelector('[data-act="short"]');
      if (!speaker.shortEdited) {
        speaker.short = deriveShort(field.value,
          roster.filter((s) => s !== speaker).map((s) => s.short));
        if (shortField) shortField.value = speaker.short;
      }
      changed();
    } else if (field.dataset.act === 'short') {
      speaker.short = field.value.toUpperCase().slice(0, 6);
      speaker.shortEdited = true;
      field.value = speaker.short;
      changed();
    }
  });

  host.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-act]');
    if (!button) return;
    const row = button.closest('.speaker-row');
    const speaker = roster.find((s) => s.id === row.dataset.id);
    if (!speaker) return;

    if (button.dataset.act === 'colour') {
      const at = palette.indexOf(speaker.color);
      speaker.color = palette[(at + 1 + palette.length) % palette.length];
      row.style.setProperty('--speaker-color', speaker.color);
      changed();
      return;
    }

    if (button.dataset.act === 'remove') {
      const tagged = turnsFor(speaker.id);
      if (tagged && !confirm(
        `Remove ${speaker.name}?\n\n${LS.plural(tagged, 'turn')} currently ` +
        `assigned to them will become unassigned. The text itself is not ` +
        `touched, and you can re-tag afterwards.`
      )) return;
      roster = roster.filter((s) => s.id !== speaker.id);
      render();
      changed();
    }
  });

  /* ---------------------------------------------------------------- stepper */

  function setCount(next) {
    const wanted = Math.max(0, Math.min(MAX, Number(next) || 0));

    if (wanted > roster.length) {
      while (roster.length < wanted) addSpeaker();
    } else if (wanted < roster.length) {
      // Removing from the end: warn once if any of those speakers are in use.
      const doomed = roster.slice(wanted);
      const tagged = doomed.reduce((sum, s) => sum + turnsFor(s.id), 0);
      if (tagged && !confirm(
        `Reducing to ${LS.plural(wanted, 'speaker')} removes ` +
        `${doomed.map((s) => s.name).join(', ')}.\n\n` +
        `${LS.plural(tagged, 'turn')} assigned to them will become unassigned.`
      )) {
        countInput.value = roster.length;
        return;
      }
      roster = roster.slice(0, wanted);
    } else {
      return;
    }

    render();
    changed();
  }

  if (plus) plus.addEventListener('click', () => setCount(roster.length + 1));
  if (minus) minus.addEventListener('click', () => setCount(roster.length - 1));
  countInput.addEventListener('change', () => setCount(countInput.value));

  // Turn counts shown per speaker go stale when tagging changes.
  document.addEventListener('ls:rendered', () => {
    host.querySelectorAll('.speaker-row').forEach((row) => {
      const label = row.querySelector('.speaker-turns');
      if (label) label.textContent = `${turnsFor(row.dataset.id)} turns`;
    });
  });

  render();
})();
