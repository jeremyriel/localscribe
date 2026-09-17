/* The validation workbench: audio transport, time ribbon, and segment editor.

   Three ideas carry most of the design:

   1. Every word is its own span carrying a start time, so clicking a word
      replays from that word. This is the core review loop -- hear it again
      without touching the player.
   2. The text is only re-rendered as word spans when it is NOT being edited.
      While a segment has focus it stays plain text, so the caret never jumps
      and typing is never interrupted by a re-render.
   3. Edits save automatically but timings are not guessed on the fly. Editing
      marks the segment as needing a re-timestamp, which the user triggers
      once, deliberately, when the human pass is done. */

(() => {
  const D = window.DOC;
  if (!D) return;

  const api = `/api/projects/${encodeURIComponent(D.slug)}/documents/${encodeURIComponent(D.id)}`;

  const audio = document.getElementById('audio');
  const host = document.getElementById('segments');
  const ribbon = document.getElementById('ribbon');
  const saveState = document.getElementById('save-state');
  const staleWarning = document.getElementById('stale-warning');
  const staleText = document.getElementById('stale-text');
  const timeReadout = document.getElementById('time-readout');
  const loopToggle = document.getElementById('loop-seg');
  const followToggle = document.getElementById('follow-play');

  let transcript = D.transcript;
  let duration = Number(transcript.duration) || 0;
  let peaks = [];
  let activeSegment = -1;
  let editingSegment = -1;
  let loopRange = null;        // {start, end} while looping a segment
  const pendingEdits = new Map();   // segment id -> {text?, speaker?}

  /* ------------------------------------------------------------ rendering */

  function words(seg) {
    return seg.words || [];
  }

  function stamp(value) {
    return LS.clock(value, false);
  }

  function segmentHtml(seg) {
    const w = words(seg);
    if (!w.length) {
      // No word timings yet (edited and not re-timestamped, or hand typed).
      return LS.escapeHtml(seg.text || '');
    }
    return w.map((word, index) =>
      `<span class="w${(word.prob !== undefined && word.prob < D.lowConfidence) ? ' low' : ''}"` +
      ` data-start="${word.start}" data-end="${word.end}" data-wi="${index}"` +
      ` title="${word.prob !== undefined ? Math.round(word.prob * 100) + '% confidence' : ''} - click to hear">` +
      `${LS.escapeHtml(word.w)}</span>`
    ).join(' ');
  }

  function render() {
    host.innerHTML = '';
    const fragment = document.createDocumentFragment();

    transcript.segments.forEach((seg) => {
      const el = document.createElement('div');
      el.className = 'seg';
      el.dataset.id = seg.id;
      if (seg.edited) el.classList.add('edited');
      if (seg.stale_timings) el.classList.add('stale');

      const length = (seg.end - seg.start) || 0;
      el.innerHTML = `
        <div class="seg-time">
          <button type="button" class="seg-stamp" data-play="${seg.start}"
                  title="Play from ${stamp(seg.start)}">${stamp(seg.start)}</button>
          <span class="seg-dur">${length.toFixed(1)}s</span>
          <div class="seg-tools">
            <button type="button" data-act="loop" title="Loop this segment">loop</button>
            <button type="button" data-act="merge" title="Merge with the next segment">merge</button>
          </div>
        </div>
        <div class="seg-body">
          <input class="seg-speaker" type="text" placeholder="Speaker"
                 value="${LS.escapeHtml(seg.speaker || '')}"
                 aria-label="Speaker for segment at ${stamp(seg.start)}">
          <div class="seg-text" contenteditable="true" spellcheck="true"
               role="textbox" aria-multiline="true"
               aria-label="Transcript text at ${stamp(seg.start)}">${segmentHtml(seg)}</div>
        </div>`;
      fragment.appendChild(el);
    });

    host.appendChild(fragment);
    updateStaleWarning();
  }

  function segmentElement(id) {
    return host.querySelector(`.seg[data-id="${id}"]`);
  }

  function segmentById(id) {
    return transcript.segments.find((s) => Number(s.id) === Number(id));
  }

  function updateStaleWarning() {
    const stale = transcript.segments.filter((s) => s.stale_timings).length;
    if (!staleWarning) return;
    staleWarning.hidden = stale === 0;
    if (stale && staleText) {
      staleText.textContent =
        ` ${LS.plural(stale, 'segment has', 'segments have')} been edited, so the ` +
        `word timings and caption files no longer match the text.`;
    }
    document.querySelectorAll('.seg').forEach((el) => {
      const seg = segmentById(el.dataset.id);
      el.classList.toggle('stale', Boolean(seg && seg.stale_timings));
      el.classList.toggle('edited', Boolean(seg && seg.edited));
    });
  }

  /* --------------------------------------------------------------- saving */

  function setSaveState(text, cls) {
    if (!saveState) return;
    saveState.textContent = text;
    saveState.className = `save-state ${cls || ''}`.trim();
  }

  const flush = LS.debounce(async () => {
    if (!pendingEdits.size) return;

    const edits = [...pendingEdits.entries()].map(([id, change]) => ({ id: Number(id), ...change }));
    pendingEdits.clear();
    setSaveState('Saving...');

    try {
      const result = await LS.put(`${api}/transcript`, { edits });
      setSaveState('Saved', 'saved');

      // Reflect the authoritative summary rather than guessing locally.
      const low = document.getElementById('fact-low');
      const reviewed = document.getElementById('fact-reviewed');
      if (low) low.textContent = result.summary.low_confidence;
      if (reviewed) reviewed.textContent = result.summary.human_edited ? 'yes' : 'not yet';
      updateStaleWarning();
    } catch (err) {
      setSaveState('Not saved', 'dirty');
      LS.toast(`Could not save your edit: ${err.message}`, 'error', 12000);
      // Put the edits back so the next flush retries them.
      edits.forEach((edit) => {
        const { id, ...change } = edit;
        pendingEdits.set(id, { ...(pendingEdits.get(id) || {}), ...change });
      });
    }
  }, 1400);

  function queueEdit(id, change) {
    pendingEdits.set(Number(id), { ...(pendingEdits.get(Number(id)) || {}), ...change });
    setSaveState('Unsaved changes', 'dirty');
    flush();
  }

  // Never lose an edit to a closed tab or a navigation.
  window.addEventListener('beforeunload', (event) => {
    if (!pendingEdits.size && !flush.pending()) return;
    flush.flush();
    event.preventDefault();
    event.returnValue = '';
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flush.flush();
  });

  /* ---------------------------------------------------------- interaction */

  host.addEventListener('click', (event) => {
    const stampBtn = event.target.closest('[data-play]');
    if (stampBtn) {
      playFrom(Number(stampBtn.dataset.play));
      return;
    }

    const tool = event.target.closest('button[data-act]');
    if (tool) {
      const segEl = tool.closest('.seg');
      const id = Number(segEl.dataset.id);
      if (tool.dataset.act === 'loop') toggleLoop(id);
      if (tool.dataset.act === 'merge') mergeSegment(id);
      return;
    }

    // Clicking a word replays from that word -- but not while editing that
    // segment, where a click must place the caret instead.
    const word = event.target.closest('.w');
    if (word) {
      const segEl = word.closest('.seg');
      if (Number(segEl.dataset.id) === editingSegment) return;
      playFrom(Number(word.dataset.start));
      markPlayingWord(word);
    }
  });

  function markPlayingWord(word) {
    document.querySelectorAll('.w.playing').forEach((el) => el.classList.remove('playing'));
    if (word) word.classList.add('playing');
  }

  /* Editing: swap word spans for plain text on focus so the caret behaves,
     and restore the spans on blur. */
  host.addEventListener('focusin', (event) => {
    const textEl = event.target.closest('.seg-text');
    if (!textEl) return;
    const segEl = textEl.closest('.seg');
    editingSegment = Number(segEl.dataset.id);
    textEl.classList.add('editing');
    const seg = segmentById(editingSegment);
    if (seg) textEl.textContent = seg.text || textEl.textContent.trim();
  });

  host.addEventListener('focusout', (event) => {
    const textEl = event.target.closest('.seg-text');
    if (textEl) {
      const segEl = textEl.closest('.seg');
      const id = Number(segEl.dataset.id);
      const seg = segmentById(id);
      const next = textEl.textContent.replace(/\s+/g, ' ').trim();

      if (seg && next !== (seg.text || '').trim()) {
        seg.text = next;
        seg.edited = true;
        // Emptying a segment clears its words outright; otherwise the timings
        // are simply stale until re-timestamped.
        if (next) seg.stale_timings = true;
        else { seg.words = []; delete seg.stale_timings; }
        queueEdit(id, { text: next });
      }

      textEl.classList.remove('editing');
      editingSegment = -1;
      if (seg) textEl.innerHTML = segmentHtml(seg);
      updateStaleWarning();
      return;
    }

    const speaker = event.target.closest('.seg-speaker');
    if (speaker) {
      const id = Number(speaker.closest('.seg').dataset.id);
      const seg = segmentById(id);
      const next = speaker.value.trim();
      if (seg && next !== (seg.speaker || '')) {
        seg.speaker = next;
        queueEdit(id, { speaker: next });
      }
    }
  });

  host.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && event.target.closest('.seg-text, .seg-speaker')) {
      event.target.blur();
    }
  });

  /* -------------------------------------------------------------- playback */

  function playFrom(seconds) {
    if (!audio) return;
    audio.currentTime = Math.max(0, Math.min(seconds, duration || audio.duration || 0));
    const promise = audio.play();
    if (promise && promise.catch) promise.catch(() => { /* autoplay refusal */ });
  }

  function toggleLoop(id) {
    const seg = segmentById(id);
    if (!seg) return;
    if (loopRange && loopRange.id === id) {
      loopRange = null;
      if (loopToggle) loopToggle.checked = false;
      LS.toast('Loop off.', 'info', 2200);
      return;
    }
    loopRange = { id, start: seg.start, end: seg.end };
    if (loopToggle) loopToggle.checked = true;
    playFrom(seg.start);
    LS.toast(`Looping ${stamp(seg.start)} to ${stamp(seg.end)}.`, 'info', 2600);
  }

  if (loopToggle) {
    loopToggle.addEventListener('change', () => {
      if (!loopToggle.checked) { loopRange = null; return; }
      const seg = segmentById(activeSegment >= 0 ? activeSegment : 0);
      if (seg) loopRange = { id: seg.id, start: seg.start, end: seg.end };
    });
  }

  if (audio) {
    audio.addEventListener('timeupdate', () => {
      const t = audio.currentTime;

      if (loopRange && t >= loopRange.end) {
        audio.currentTime = loopRange.start;
        return;
      }

      if (timeReadout) {
        timeReadout.textContent =
          `${LS.clock(t, false)} / ${LS.clock(duration || audio.duration || 0, false)}`;
      }

      highlight(t);
      drawRibbon(t);
    });

    audio.addEventListener('loadedmetadata', () => {
      if (!duration || !Number.isFinite(duration)) duration = audio.duration;
      drawRibbon(0);
    });

    audio.addEventListener('error', () => {
      LS.toast(
        'The audio could not be loaded. If this is an unusual format, the ' +
        'browser-playable preview may be missing; re-transcribing the file ' +
        'regenerates it.', 'error', 14000);
    });

    const playBtn = document.getElementById('btn-play');
    if (playBtn) {
      playBtn.addEventListener('click', () => (audio.paused ? audio.play() : audio.pause()));
      audio.addEventListener('play', () => { playBtn.textContent = 'Pause'; });
      audio.addEventListener('pause', () => { playBtn.textContent = 'Play'; });
    }
    const back = document.getElementById('btn-back');
    const fwd = document.getElementById('btn-fwd');
    if (back) back.addEventListener('click', () => { audio.currentTime -= 2; });
    if (fwd) fwd.addEventListener('click', () => { audio.currentTime += 2; });

    const rate = document.getElementById('rate');
    if (rate) rate.addEventListener('change', () => { audio.playbackRate = Number(rate.value); });
  }

  function highlight(t) {
    let found = -1;
    for (const seg of transcript.segments) {
      if (t >= seg.start && t <= seg.end) { found = seg.id; break; }
    }
    if (found === activeSegment) {
      highlightWord(t);
      return;
    }

    if (activeSegment >= 0) {
      const previous = segmentElement(activeSegment);
      if (previous) previous.classList.remove('active');
    }
    activeSegment = found;
    if (found < 0) return;

    const el = segmentElement(found);
    if (!el) return;
    el.classList.add('active');

    // Follow the audio, unless the user is typing -- scrolling the text out
    // from under someone mid-edit is infuriating.
    if (followToggle && followToggle.checked && editingSegment < 0) {
      const box = el.getBoundingClientRect();
      if (box.top < 90 || box.bottom > window.innerHeight - 120) {
        el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
    }
    highlightWord(t);
  }

  function highlightWord(t) {
    if (activeSegment < 0) return;
    const el = segmentElement(activeSegment);
    if (!el) return;
    const spans = el.querySelectorAll('.w');
    let current = null;
    spans.forEach((span) => {
      const start = Number(span.dataset.start);
      const end = Number(span.dataset.end);
      if (t >= start && t <= end) current = span;
    });
    if (current) markPlayingWord(current);
  }

  /* --------------------------------------------------------------- ribbon */

  async function loadWaveform() {
    try {
      const data = await LS.get(`${api}/waveform`);
      peaks = data.peaks || [];
      if (data.duration) duration = data.duration;
    } catch (err) {
      peaks = [];
    }
    drawRibbon(0);
  }

  function drawRibbon(position) {
    if (!ribbon) return;
    const ratio = window.devicePixelRatio || 1;
    const width = ribbon.clientWidth;
    const height = ribbon.clientHeight || 72;

    if (ribbon.width !== Math.floor(width * ratio)) {
      ribbon.width = Math.floor(width * ratio);
      ribbon.height = Math.floor(height * ratio);
    }

    const ctx = ribbon.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const total = duration || 1;
    const mid = height / 2;

    // Waveform
    if (peaks.length) {
      ctx.fillStyle = '#c7d2d4';
      const step = width / peaks.length;
      const barWidth = Math.max(1, step * 0.85);
      peaks.forEach((peak, index) => {
        const h = Math.max(1, peak * (height * 0.82));
        ctx.fillRect(index * step, mid - h / 2, barWidth, h);
      });
    } else {
      ctx.fillStyle = '#e7e5da';
      ctx.fillRect(0, mid - 1, width, 2);
    }

    // Segment boundaries, so the ribbon shows the structure of the transcript
    ctx.strokeStyle = 'rgba(15,118,110,.22)';
    ctx.lineWidth = 1;
    transcript.segments.forEach((seg) => {
      const x = (seg.start / total) * width;
      ctx.beginPath();
      ctx.moveTo(x, 4);
      ctx.lineTo(x, height - 4);
      ctx.stroke();
    });

    // Active segment shading
    const active = segmentById(activeSegment);
    if (active) {
      const x1 = (active.start / total) * width;
      const x2 = (active.end / total) * width;
      ctx.fillStyle = 'rgba(15,118,110,.13)';
      ctx.fillRect(x1, 0, Math.max(2, x2 - x1), height);
    }

    // Loop region
    if (loopRange) {
      const x1 = (loopRange.start / total) * width;
      const x2 = (loopRange.end / total) * width;
      ctx.strokeStyle = 'rgba(154,91,0,.75)';
      ctx.lineWidth = 2;
      ctx.strokeRect(x1, 2, Math.max(3, x2 - x1), height - 4);
    }

    // Playhead
    const x = (Math.max(0, position) / total) * width;
    ctx.strokeStyle = '#a32020';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
    ctx.stroke();
  }

  if (ribbon) {
    ribbon.addEventListener('click', (event) => {
      const box = ribbon.getBoundingClientRect();
      const fraction = (event.clientX - box.left) / box.width;
      playFrom(fraction * (duration || 0));
    });
    window.addEventListener('resize', LS.debounce(() => {
      drawRibbon(audio ? audio.currentTime : 0);
    }, 120));
  }

  /* ------------------------------------------------------------- shortcuts */

  document.addEventListener('keydown', (event) => {
    const typing = event.target.matches('input, textarea, [contenteditable="true"]');
    if (typing) return;
    if (!audio) return;

    if (event.code === 'Space') {
      event.preventDefault();
      if (audio.paused) audio.play(); else audio.pause();
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      audio.currentTime -= event.shiftKey ? 5 : 2;
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      audio.currentTime += event.shiftKey ? 5 : 2;
    }
  });

  /* ----------------------------------------------------- uncertain words */

  let lowIndex = -1;
  const jumpLow = document.getElementById('jump-low');
  if (jumpLow) {
    jumpLow.addEventListener('click', () => {
      const spans = [...document.querySelectorAll('.w.low')];
      if (!spans.length) {
        LS.toast('No low-confidence words in this transcript.', 'info');
        return;
      }
      lowIndex = (lowIndex + 1) % spans.length;
      const span = spans[lowIndex];
      span.scrollIntoView({ block: 'center', behavior: 'smooth' });
      markPlayingWord(span);
      playFrom(Number(span.dataset.start));
      LS.toast(`Uncertain word ${lowIndex + 1} of ${spans.length}.`, 'info', 2400);
    });
  }

  /* --------------------------------------------------- segment operations */

  async function mergeSegment(id) {
    flush.flush();
    try {
      const result = await LS.post(`${api}/segment/${id}/merge`);
      transcript = result.transcript;
      render();
      LS.toast('Segments merged.', 'ok', 2400);
    } catch (err) {
      LS.toast(err.message, 'error');
    }
  }

  /* -------------------------------------------------------- re-timestamp */

  const retimestampBtn = document.getElementById('retimestamp');
  if (retimestampBtn) {
    retimestampBtn.addEventListener('click', async () => {
      flush.flush();
      retimestampBtn.disabled = true;
      retimestampBtn.textContent = 'Re-timestamping...';
      if (window.LSConsole) LSConsole.open();

      try {
        // Give any in-flight autosave a moment to land first.
        await new Promise((resolve) => setTimeout(resolve, 250));
        const result = await LS.post(`${api}/retimestamp`);
        transcript = result.transcript;
        render();
        refreshOutputs(result.outputs);

        const report = result.report;
        LS.toast(
          `Timings rebuilt: ${report.preserved} words kept their exact timing ` +
          `(${report.exact_pct}%), ${report.redistributed} redistributed, ` +
          `${report.interpolated} interpolated. Caption files rewritten.`,
          'ok', 11000);

        const note = document.getElementById('retimestamp-note');
        if (note) note.textContent = 'Re-timestamped just now.';
        if (report.review_segments && report.review_segments.length) {
          LS.toast(
            `${LS.plural(report.review_segments.length, 'segment')} had large ` +
            `rewrites and are worth spot-checking against the audio.`,
            'warn', 12000);
        }
      } catch (err) {
        LS.toast(`Re-timestamp failed: ${err.message}`, 'error', 12000);
      } finally {
        retimestampBtn.disabled = false;
        retimestampBtn.textContent = 'Re-timestamp and rewrite captions';
      }
    });
  }

  function refreshOutputs(outputs) {
    const list = document.getElementById('outputs');
    if (!list || !outputs) return;
    list.innerHTML = outputs.map((f) =>
      `<li><span class="fmt">.${LS.escapeHtml(f.format)}</span>` +
      `<a href="/outputs/${encodeURIComponent(D.slug)}/${encodeURIComponent(D.id)}/${encodeURIComponent(f.name)}" download>${LS.escapeHtml(f.name)}</a>` +
      `<span class="size">${(f.size_bytes / 1024).toFixed(1)} KB</span></li>`
    ).join('');
  }

  /* -------------------------------------------------------- side actions */

  const reveal = document.getElementById('reveal');
  if (reveal) {
    reveal.addEventListener('click', async () => {
      try {
        const result = await LS.post(`${api}/reveal`);
        if (!result.opened) {
          LS.toast(`Could not open a file manager. The folder is at: ${result.path}`, 'warn', 14000);
        }
      } catch (err) {
        LS.toast(err.message, 'error');
      }
    });
  }

  const reexport = document.getElementById('reexport');
  if (reexport) {
    reexport.addEventListener('click', async () => {
      flush.flush();
      reexport.disabled = true;
      try {
        const result = await LS.post(`${api}/export`);
        refreshOutputs(result.outputs);
        LS.toast('Output files rewritten from the current transcript.', 'ok');
      } catch (err) {
        LS.toast(err.message, 'error');
      } finally {
        reexport.disabled = false;
      }
    });
  }

  document.querySelectorAll('[data-restore]').forEach((button) => {
    button.addEventListener('click', async () => {
      if (!confirm(
        'Restore this earlier version?\n\nThe current transcript is saved as a ' +
        'new revision first, so this can be undone.'
      )) return;
      try {
        const result = await LS.post(`${api}/revisions/${button.dataset.restore}/restore`);
        transcript = result.transcript;
        render();
        LS.toast('Earlier version restored.', 'ok');
      } catch (err) {
        LS.toast(err.message, 'error');
      }
    });
  });

  /* ------------------------------------------------------------- start up */

  render();
  loadWaveform();
  setSaveState('Saved', 'saved');
})();
