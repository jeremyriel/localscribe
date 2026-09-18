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
  const outputsStale = document.getElementById('outputs-stale');
  const timeReadout = document.getElementById('time-readout');
  const loopToggle = document.getElementById('loop-seg');
  const followToggle = document.getElementById('follow-play');

  let transcript = D.transcript;
  let roster = (D.speakers || []).slice();
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
    if (!w.length || seg.stale_timings) {
      // No word timings to trust: never transcribed, hand typed, or edited
      // and not yet re-timestamped. `seg.words` deliberately survives a
      // text edit (see apply_edits() in app/transcript.py) so a
      // re-timestamp pass has something to reconcile against, but that
      // means it no longer matches seg.text -- rendering it here would
      // silently show the segment's *previous* wording right after the
      // edit that just changed it.
      return LS.escapeHtml(seg.text || '');
    }
    return w.map((word, index) =>
      `<span class="w${(word.prob !== undefined && word.prob < D.lowConfidence) ? ' low' : ''}"` +
      ` data-start="${word.start}" data-end="${word.end}" data-wi="${index}"` +
      ` data-prob="${word.prob !== undefined ? word.prob : ''}"` +
      ` title="${word.prob !== undefined ? Math.round(word.prob * 100) + '% confidence' : ''} - click to hear">` +
      `${LS.escapeHtml(word.w)}</span>`
    ).join(' ');
  }

  /* Turns, mirroring app/transcript.py:turns().

     A turn is a maximal run of consecutive segments by one speaker. Because
     consecutive turns always differ in speaker, rendering the name once per
     turn means it is never repeated while the same person is still talking.
     Pauses inside a turn are recorded, not used as boundaries, so a silence
     breaks the paragraph without reprinting the name. */
  function computeTurns() {
    const gap = Number(D.pauseGap) || 1.5;
    const out = [];
    let current = null;
    let previousEnd = null;

    transcript.segments.forEach((seg) => {
      const speakerId = seg.speaker_id || '';
      const pause = previousEnd === null ? 0 : (seg.start - previousEnd);

      let boundary;
      if (!current) boundary = true;
      else if (speakerId !== current.speakerId) boundary = true;
      // Untagged text splits at pauses so there is something to click.
      else if (!speakerId && gap > 0 && pause >= gap) boundary = true;
      else boundary = false;

      if (boundary) {
        current = { speakerId, segments: [], pauses: {}, index: out.length };
        out.push(current);
      } else if (gap > 0 && pause >= gap) {
        current.pauses[seg.id] = pause;
      }

      current.segments.push(seg);
      previousEnd = seg.end;
    });
    return out;
  }

  function speakerById(id) {
    return roster.find((s) => s.id === id) || null;
  }

  function chipsHtml(turn) {
    if (!roster.length) return '';
    const chips = roster.map((speaker, index) => {
      const on = speaker.id === turn.speakerId;
      return `<button type="button" class="chip${on ? ' on' : ''}"
        data-assign="${LS.escapeHtml(speaker.id)}"
        style="--chip:${LS.escapeHtml(speaker.color)}"
        title="Assign this turn to ${LS.escapeHtml(speaker.name)}${index < 9 ? ` (key ${index + 1})` : ''}"
        aria-pressed="${on}">${LS.escapeHtml(speaker.short || speaker.name)}</button>`;
    }).join('');
    const clear = turn.speakerId
      ? `<button type="button" class="chip chip-clear" data-assign=""
           title="Clear the speaker on this turn">clear</button>`
      : '';
    return `<div class="chips">${chips}${clear}</div>`;
  }

  function turnHeaderHtml(turn) {
    const speaker = speakerById(turn.speakerId);
    const name = speaker
      ? `<span class="turn-name">${LS.escapeHtml(speaker.name)}</span>`
      : '<span class="turn-name unassigned">Unassigned</span>';
    return `<div class="turn-head">
        ${name}
        <button type="button" class="seg-stamp" data-play="${turn.segments[0].start}"
                title="Play this turn from the start">${stamp(turn.segments[0].start)}</button>
        ${chipsHtml(turn)}
      </div>`;
  }

  function segmentHtmlBlock(seg, turn) {
    const pause = turn.pauses[seg.id];
    const marker = pause
      ? `<div class="pause-marker" title="Silence in the recording">
           <span>${pause.toFixed(1)}s pause</span></div>`
      : '';
    const length = (seg.end - seg.start) || 0;
    const classes = ['seg'];
    if (seg.edited) classes.push('edited');
    if (seg.stale_timings) classes.push('stale');

    return `${marker}
      <div class="${classes.join(' ')}" data-id="${seg.id}">
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
          <div class="seg-text" contenteditable="true" spellcheck="false"
               role="textbox" aria-multiline="true"
               aria-label="Transcript text at ${stamp(seg.start)}">${segmentHtml(seg)}</div>
        </div>
      </div>`;
  }

  function render() {
    host.innerHTML = '';
    const fragment = document.createDocumentFragment();

    computeTurns().forEach((turn) => {
      const speaker = speakerById(turn.speakerId);
      const el = document.createElement('div');
      el.className = `turn${speaker ? ' tagged' : ''}`;
      el.dataset.turn = turn.index;
      el.dataset.ids = turn.segments.map((s) => s.id).join(',');
      if (speaker) el.style.setProperty('--speaker-color', speaker.color);
      el.innerHTML = turnHeaderHtml(turn)
        + turn.segments.map((seg) => segmentHtmlBlock(seg, turn)).join('');
      fragment.appendChild(el);
    });

    host.appendChild(fragment);
    updateStaleWarning();
    document.dispatchEvent(new CustomEvent('ls:rendered'));
  }

  function segmentElement(id) {
    return host.querySelector(`.seg[data-id="${id}"]`);
  }

  function segmentById(id) {
    return transcript.segments.find((s) => Number(s.id) === Number(id));
  }

  function updateStaleWarning() {
    const stale = transcript.segments.filter((s) => s.stale_timings).length;
    if (staleWarning) {
      staleWarning.hidden = stale === 0;
      if (stale && staleText) {
        staleText.textContent =
          ` ${LS.plural(stale, 'segment has', 'segments have')} been edited, so the ` +
          `word timings and caption files no longer match the text.`;
      }
    }
    // The downloaded/exported files (outputs/) are only rewritten on an
    // explicit Re-timestamp or Re-export, so a pending edit makes them
    // stale too -- flag that where a validator would actually go looking
    // for them, not only in the word-timing warning above.
    if (outputsStale) outputsStale.hidden = stale === 0;
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

    const chip = event.target.closest('[data-assign]');
    if (chip) {
      const turnEl = chip.closest('.turn');
      assignTurn(turnEl, chip.dataset.assign);
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

  /* ------------------------------------------------------- word inspector */

  /* Hovering a word for a couple of seconds surfaces a small card with its
     model confidence and how many other times the same word appears in this
     transcript -- context for deciding whether a find-and-replace across the
     whole document is worth it. It floats to the right of the transcript,
     fixed in place, and disappears the moment the pointer leaves the word,
     rather than living in the side panel where its appearing and
     disappearing would shove the other cards around on every hover. */

  let wordCard = null;
  let wordHoverTimer = null;
  let hoveredWordEl = null;

  function wordCore(text) {
    return (text || '').replace(/^[^\p{L}\p{N}']+|[^\p{L}\p{N}']+$/gu, '');
  }

  function countOccurrences(core) {
    if (!core) return 0;
    const needle = core.toLowerCase();
    let count = 0;
    transcript.segments.forEach((seg) => {
      (seg.text || '').split(/\s+/).forEach((token) => {
        if (wordCore(token).toLowerCase() === needle) count += 1;
      });
    });
    return count;
  }

  function hideWordCard() {
    if (wordCard) { wordCard.remove(); wordCard = null; }
  }

  function showWordCard(wordEl) {
    const core = wordCore(wordEl.textContent);
    if (!core) return;
    const total = countOccurrences(core);
    const prob = wordEl.dataset.prob;
    const others = total - 1;

    // realign.py stamps prob=0.0 on any word it redistributes after a text
    // edit (_spread(..., prob=0.0)) precisely because there is no real model
    // confidence for text a human typed - it is not a genuine low score, so
    // showing it as "0% model confidence" would read backwards.
    const isUserEdited = prob !== '' && prob !== undefined && Number(prob) === 0;

    hideWordCard();
    wordCard = document.createElement('div');
    wordCard.className = 'word-card';
    wordCard.innerHTML =
      `<div class="word-card-word">${LS.escapeHtml(core)}</div>` +
      (isUserEdited
        ? `<div class="word-card-row">User-edited</div>`
        : prob
          ? `<div class="word-card-row">${Math.round(Number(prob) * 100)}% model confidence</div>`
          : '') +
      `<div class="word-card-row">${
        others > 0
          ? `<strong>${others}</strong> other ${others === 1 ? 'occurrence' : 'occurrences'} in this transcript`
          : 'Appears only here in this transcript'
      }</div>` +
      `<div class="word-card-hint">Double-click the word to find &amp; replace it everywhere.</div>`;
    document.body.appendChild(wordCard);

    // Anchored just to the right of the transcript column, not the far
    // viewport edge, so it stays close to what is being read; clamped so it
    // never overlaps the word itself or runs off the browser window.
    const editorCard = wordEl.closest('.card');
    const editorRect = (editorCard || host).getBoundingClientRect();
    const wordRect = wordEl.getBoundingClientRect();
    const left = Math.min(
      editorRect.right + 12,
      window.innerWidth - wordCard.offsetWidth - 8
    );
    wordCard.style.left = `${Math.max(8, left)}px`;

    const top = Math.min(
      Math.max(8, wordRect.top - 8),
      window.innerHeight - wordCard.offsetHeight - 8
    );
    wordCard.style.top = `${top}px`;
  }

  host.addEventListener('mouseover', (event) => {
    const wordEl = event.target.closest('.w');
    if (!wordEl || wordEl === hoveredWordEl) return;
    hoveredWordEl = wordEl;
    clearTimeout(wordHoverTimer);
    hideWordCard();
    wordHoverTimer = setTimeout(() => {
      if (hoveredWordEl === wordEl) showWordCard(wordEl);
    }, 1000);
  });

  host.addEventListener('mouseout', (event) => {
    const wordEl = event.target.closest('.w');
    if (!wordEl || wordEl !== hoveredWordEl) return;
    // Only clear once the pointer has actually left the word, not merely
    // moved between nested elements inside it.
    if (wordEl.contains(event.relatedTarget)) return;
    clearTimeout(wordHoverTimer);
    hoveredWordEl = null;
    hideWordCard();
  });

  /* The first click of a double-click lands on `mousedown` before `dblclick`
     ever fires, and on a contenteditable that click already focuses the
     field and selects the word natively -- by the time `dblclick` runs, the
     segment has already flipped into edit mode. `event.detail` carries the
     click count on both `mousedown` and `click`, so the second `mousedown`
     of the pair is where this has to be headed off. */
  host.addEventListener('mousedown', (event) => {
    if (event.detail < 2) return;
    const wordEl = event.target.closest('.w');
    if (!wordEl) return;
    const segEl = wordEl.closest('.seg');
    if (segEl && Number(segEl.dataset.id) === editingSegment) return;
    event.preventDefault();
  });

  host.addEventListener('dblclick', (event) => {
    const wordEl = event.target.closest('.w');
    if (!wordEl) return;
    const segEl = wordEl.closest('.seg');
    if (segEl && Number(segEl.dataset.id) === editingSegment) return;
    const core = wordCore(wordEl.textContent);
    if (!core) return;
    event.preventDefault();
    clearTimeout(wordHoverTimer);
    hoveredWordEl = null;
    hideWordCard();
    if (window.LSFindReplace) window.LSFindReplace.open(core);
  });

  // A full re-render replaces the DOM, so a card anchored to an element
  // that no longer exists would be left stranded on screen.
  document.addEventListener('ls:rendered', () => {
    clearTimeout(wordHoverTimer);
    hoveredWordEl = null;
    hideWordCard();
  });

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

  });

  host.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && event.target.closest('.seg-text, .seg-speaker')) {
      event.target.blur();
    }
  });

  /* ------------------------------------------------------- speaker tagging */

  async function assignTurn(turnEl, speakerId) {
    if (!turnEl) return;
    const ids = (turnEl.dataset.ids || '')
      .split(',').filter(Boolean).map(Number);
    if (!ids.length) return;

    // Apply locally first so tagging feels instant, then persist.
    ids.forEach((id) => {
      const seg = segmentById(id);
      if (!seg) return;
      seg.speaker_id = speakerId || '';
      const speaker = speakerById(seg.speaker_id);
      seg.speaker = speaker ? speaker.name : '';
    });
    render();

    try {
      await LS.post(`${api}/assign`, { segments: ids, speaker_id: speakerId || '' });
      setSaveState('Saved', 'saved');
    } catch (err) {
      LS.toast(`Could not save the speaker: ${err.message}`, 'error');
      setSaveState('Not saved', 'dirty');
    }
  }

  /* The turn under the caret, or the one playing, or the one in view. */
  function focusedTurn() {
    if (editingSegment >= 0) {
      const el = segmentElement(editingSegment);
      if (el) return el.closest('.turn');
    }
    if (activeSegment >= 0) {
      const el = segmentElement(activeSegment);
      if (el) return el.closest('.turn');
    }
    const turnsEls = [...host.querySelectorAll('.turn')];
    return turnsEls.find((el) => {
      const box = el.getBoundingClientRect();
      return box.bottom > 120 && box.top < window.innerHeight * 0.6;
    }) || turnsEls[0] || null;
  }

  document.addEventListener('keydown', (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.target.matches('input, textarea, [contenteditable="true"]')) return;
    if (!/^[1-9]$/.test(event.key)) return;

    const speaker = roster[Number(event.key) - 1];
    if (!speaker) return;
    const turnEl = focusedTurn();
    if (!turnEl) return;
    event.preventDefault();
    assignTurn(turnEl, speaker.id);
    turnEl.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    LS.toast(`Turn assigned to ${speaker.name}.`, 'info', 1600);
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

  /* The same action is triggered from several places -- the sidebar's
     primary button, and a smaller one inline in each yellow "this needs
     re-timestamping" notice, so a validator does not have to go hunting for
     the sidebar every time an edit makes them stale. All of them share the
     js-retimestamp class and are kept in lockstep (busy state, label
     restored to whatever each button originally said). */
  const retimestampButtons = [...document.querySelectorAll('.js-retimestamp')];
  if (retimestampButtons.length) {
    const originalLabel = new Map(retimestampButtons.map((b) => [b, b.textContent]));
    const setBusy = (busy) => {
      retimestampButtons.forEach((b) => {
        b.disabled = busy;
        b.textContent = busy ? 'Re-timestamping...' : originalLabel.get(b);
      });
    };

    const runRetimestamp = async () => {
      flush.flush();
      setBusy(true);
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
        setBusy(false);
      }
    };

    retimestampButtons.forEach((b) => b.addEventListener('click', runRetimestamp));
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
        // The files themselves are fresh again even though word timings may
        // still be stale (that is a separate, still-true warning above).
        if (outputsStale) outputsStale.hidden = true;
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

  /* The surface the sibling modules use.

     speakers.js, findreplace.js and proofread.js each own one concern and
     drive the editor through this, rather than reaching into its internals or
     duplicating its state. `ls:rendered` fires after every re-render so they
     can reattach to the new DOM. */
  window.LSEditor = {
    api,
    get transcript() { return transcript; },
    get roster() { return roster; },
    setRoster(next) {
      roster = (next || []).slice();
      // Names may have changed, so refresh the resolved display names.
      transcript.segments.forEach((seg) => {
        const speaker = speakerById(seg.speaker_id || '');
        seg.speaker = speaker ? speaker.name : '';
        if (seg.speaker_id && !speaker) seg.speaker_id = '';
      });
      render();
    },
    setTranscript(next) {
      transcript = next;
      duration = Number(transcript.duration) || duration;
      render();
    },
    segments() { return transcript.segments; },
    segmentById,
    segmentElement,
    speakerById,
    turns: computeTurns,
    render,
    queueEdit,
    flush: () => flush.flush(),
    setSaveState,
    refreshOutputs,
    playFrom,
  };

  render();
  loadWaveform();
  setSaveState('Saved', 'saved');
})();
