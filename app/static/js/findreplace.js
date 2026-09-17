/* Find and replace across the open transcript.

   Matches are painted with the CSS Custom Highlight API rather than by
   wrapping text in <mark> elements. That matters here: the transcript is
   contenteditable, and mutating the DOM under a caret moves it, loses the
   selection, and fights the autosave. Highlights are drawn over Ranges and
   touch nothing.

   Replacements go through the editor's ordinary edit queue, so they inherit
   the revision snapshot, the edited/stale-timings flags and the re-timestamp
   prompt. There is no second persistence path to keep in step. */

(() => {
  const D = window.DOC;
  if (!D || !window.LSEditor) return;

  const bar = document.getElementById('findbar');
  const toggle = document.getElementById('find-toggle');
  const input = document.getElementById('find-input');
  const replaceInput = document.getElementById('replace-input');
  const countEl = document.getElementById('find-count');
  const note = document.getElementById('find-note');
  if (!bar || !input) return;

  const caseBox = document.getElementById('find-case');
  const wordBox = document.getElementById('find-word');
  const regexBox = document.getElementById('find-regex');

  const supportsHighlights = typeof CSS !== 'undefined' && 'highlights' in CSS;

  let matches = [];      // {segId, start, end, text}
  let current = -1;

  /* ------------------------------------------------------------- searching */

  function buildPattern() {
    const needle = input.value;
    if (!needle) return null;

    let source = needle;
    if (!regexBox.checked) source = needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    if (wordBox.checked) source = `\\b(?:${source})\\b`;

    try {
      const pattern = new RegExp(source, caseBox.checked ? 'g' : 'gi');
      if (note) { note.textContent = ''; note.classList.remove('bad'); }
      return pattern;
    } catch (err) {
      // An invalid regular expression is a normal thing to type halfway
      // through; report it in place rather than throwing.
      if (note) {
        note.textContent = `Invalid expression: ${err.message}`;
        note.classList.add('bad');
      }
      return null;
    }
  }

  function search() {
    matches = [];
    const pattern = buildPattern();

    if (pattern) {
      window.LSEditor.segments().forEach((seg) => {
        const text = seg.text || '';
        pattern.lastIndex = 0;
        let hit;
        while ((hit = pattern.exec(text)) !== null) {
          if (hit[0] === '') { pattern.lastIndex += 1; continue; }
          matches.push({
            segId: Number(seg.id),
            start: hit.index,
            end: hit.index + hit[0].length,
            text: hit[0],
          });
          if (matches.length > 5000) break;   // pathological pattern guard
        }
      });
    }

    if (current >= matches.length) current = matches.length - 1;
    if (current < 0 && matches.length) current = 0;
    paint();
    updateCount();
  }

  function updateCount() {
    if (!countEl) return;
    if (!input.value) countEl.textContent = '';
    else if (!matches.length) countEl.textContent = 'no matches';
    else countEl.textContent = `${current + 1} of ${matches.length}`;
  }

  /* ------------------------------------------------------------ highlights */

  /* Map a character offset inside a segment onto a DOM Range, walking the
     text nodes of its word spans. */
  function rangeFor(match) {
    const el = window.LSEditor.segmentElement(match.segId);
    if (!el) return null;
    const textEl = el.querySelector('.seg-text');
    if (!textEl) return null;

    const walker = document.createTreeWalker(textEl, NodeFilter.SHOW_TEXT);
    let consumed = 0;
    let startNode = null; let startOffset = 0;
    let endNode = null; let endOffset = 0;
    let node;

    while ((node = walker.nextNode())) {
      const length = node.nodeValue.length;

      if (startNode === null && consumed + length > match.start) {
        startNode = node;
        startOffset = match.start - consumed;
      }
      if (startNode !== null && consumed + length >= match.end) {
        endNode = node;
        endOffset = match.end - consumed;
        break;
      }
      consumed += length;

      // Word spans are joined by a space in the markup but the rendered text
      // carries it as a separate node, so no adjustment is needed here; the
      // walker sees exactly the characters the user sees.
    }

    if (!startNode || !endNode) return null;
    const range = document.createRange();
    try {
      range.setStart(startNode, Math.max(0, startOffset));
      range.setEnd(endNode, Math.min(endNode.nodeValue.length, endOffset));
    } catch (err) {
      return null;
    }
    return range;
  }

  function paint() {
    if (!supportsHighlights) return;
    const all = [];
    const focused = [];

    matches.forEach((match, index) => {
      const range = rangeFor(match);
      if (!range) return;
      if (index === current) focused.push(range);
      else all.push(range);
    });

    CSS.highlights.set('ls-find', new Highlight(...all));
    CSS.highlights.set('ls-find-current', new Highlight(...focused));
  }

  function clearPaint() {
    if (!supportsHighlights) return;
    CSS.highlights.delete('ls-find');
    CSS.highlights.delete('ls-find-current');
  }

  /* -------------------------------------------------------------- stepping */

  function step(delta) {
    if (!matches.length) return;
    current = (current + delta + matches.length) % matches.length;
    paint();
    updateCount();
    reveal();
  }

  function reveal() {
    const match = matches[current];
    if (!match) return;
    const el = window.LSEditor.segmentElement(match.segId);
    if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  /* ------------------------------------------------------------ replacing */

  function replacementFor(match) {
    const value = replaceInput ? replaceInput.value : '';
    if (!regexBox.checked) return value;
    // With regex on, allow $1-style back-references by re-running the match.
    const pattern = buildPattern();
    if (!pattern) return value;
    pattern.lastIndex = 0;
    return match.text.replace(new RegExp(pattern.source, pattern.flags.replace('g', '')), value);
  }

  function applyToSegment(segId, edits) {
    const seg = window.LSEditor.segmentById(segId);
    if (!seg) return false;

    // Apply right to left so earlier offsets stay valid.
    let text = seg.text || '';
    edits.sort((a, b) => b.start - a.start).forEach((edit) => {
      text = text.slice(0, edit.start) + edit.value + text.slice(edit.end);
    });
    if (text === seg.text) return false;

    seg.text = text;
    seg.edited = true;
    seg.stale_timings = true;
    window.LSEditor.queueEdit(segId, { text });
    return true;
  }

  function replaceCurrent() {
    const match = matches[current];
    if (!match) return;
    const changed = applyToSegment(match.segId,
      [{ start: match.start, end: match.end, value: replacementFor(match) }]);
    if (changed) {
      window.LSEditor.render();
      search();
      LS.toast('Replaced.', 'ok', 1800);
    }
  }

  async function replaceAll() {
    if (!matches.length) return;
    const value = replaceInput ? replaceInput.value : '';
    if (!confirm(
      `Replace all ${matches.length} occurrences of "${input.value}" with ` +
      `"${value}"?\n\nThe current transcript is snapshotted first, so this can ` +
      `be undone from the edit history. Word timings will need re-timestamping ` +
      `afterwards.`
    )) return;

    // Snapshot before a bulk change, so "undo" means restoring a revision.
    try {
      await LS.put(`${window.LSEditor.api}/transcript`, { edits: [], revision: true });
    } catch (err) {
      LS.toast(`Could not snapshot before replacing: ${err.message}`, 'error');
      return;
    }

    const bySegment = new Map();
    matches.forEach((match) => {
      if (!bySegment.has(match.segId)) bySegment.set(match.segId, []);
      bySegment.get(match.segId).push({
        start: match.start, end: match.end, value: replacementFor(match),
      });
    });

    let segmentsChanged = 0;
    bySegment.forEach((edits, segId) => {
      if (applyToSegment(segId, edits)) segmentsChanged += 1;
    });

    window.LSEditor.render();
    window.LSEditor.flush();
    const total = matches.length;
    search();

    LS.toast(
      `Replaced ${LS.plural(total, 'occurrence')} across ` +
      `${LS.plural(segmentsChanged, 'segment')}. Re-timestamp when you have ` +
      `finished editing so the captions match.`, 'ok', 11000);
  }

  /* ---------------------------------------------------------------- wiring */

  function open() {
    bar.hidden = false;
    input.focus();
    input.select();
    search();
  }

  function close() {
    bar.hidden = true;
    clearPaint();
    matches = [];
    current = -1;
    if (countEl) countEl.textContent = '';
  }

  if (toggle) toggle.addEventListener('click', () => (bar.hidden ? open() : close()));
  document.getElementById('find-close').addEventListener('click', close);
  document.getElementById('find-next').addEventListener('click', () => step(1));
  document.getElementById('find-prev').addEventListener('click', () => step(-1));
  document.getElementById('replace-one').addEventListener('click', replaceCurrent);
  document.getElementById('replace-all').addEventListener('click', replaceAll);

  const debouncedSearch = LS.debounce(() => { current = matches.length ? current : 0; search(); }, 180);
  input.addEventListener('input', debouncedSearch);
  [caseBox, wordBox, regexBox].forEach((box) => {
    if (box) box.addEventListener('change', search);
  });

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      step(event.shiftKey ? -1 : 1);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      close();
    }
  });

  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') {
      // Deliberately overriding the browser's own find: it cannot see the
      // segment structure, cannot replace, and scrolling it through a
      // contenteditable transcript fights the editor.
      event.preventDefault();
      open();
    } else if (event.key === 'Escape' && !bar.hidden) {
      close();
    }
  });

  // Re-rendering rebuilds the DOM, so the ranges must be rebuilt too.
  document.addEventListener('ls:rendered', () => {
    if (!bar.hidden) paint();
  });

  if (!supportsHighlights && countEl) {
    countEl.title = 'This browser cannot highlight matches in place; '
      + 'stepping through them still works.';
  }
})();
