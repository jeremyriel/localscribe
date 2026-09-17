/* Word-style squiggles: red for spelling, blue for transcription artefacts.

   Painted with the CSS Custom Highlight API over Ranges, not by wrapping
   words in elements. That is what lets an underline sit under text the user
   is actively typing without moving the caret or fighting the autosave -- the
   thing that makes this feel like a word processor rather than a linter.

   Checking happens on the server, where the dictionary already lives next to
   the project glossary, so the browser never downloads a word list. Only
   segments near the viewport are checked, which keeps a two-hour transcript
   responsive. */

(() => {
  const D = window.DOC;
  if (!D || !window.LSEditor) return;
  if (!D.proofreading || !D.proofreading.available) return;
  if (!D.proofreading.spelling && !D.proofreading.artefacts) return;

  const supportsHighlights = typeof CSS !== 'undefined' && 'highlights' in CSS;
  const counter = document.getElementById('issue-count');
  const jumpBtn = document.getElementById('jump-issue');

  const issues = new Map();     // segment id -> [issue]
  const checked = new Map();    // segment id -> the text that was checked
  const pending = new Set();
  let cursor = -1;
  let menu = null;

  /* ------------------------------------------------------------- requesting */

  async function checkSegments(ids) {
    const wanted = [];
    ids.forEach((id) => {
      const seg = window.LSEditor.segmentById(id);
      if (!seg) return;
      const text = seg.text || '';
      if (checked.get(id) === text) return;   // already current
      wanted.push({ id, text });
    });
    if (!wanted.length) return;

    wanted.forEach((w) => pending.add(w.id));
    try {
      const result = await LS.post(`${window.LSEditor.api}/proofread`,
        { segments: wanted });
      if (!result.enabled) return;

      Object.entries(result.issues || {}).forEach(([key, list]) => {
        const id = Number(key);
        issues.set(id, list);
        const seg = window.LSEditor.segmentById(id);
        checked.set(id, seg ? (seg.text || '') : '');
      });
      paint();
      updateCount();
    } catch (err) {
      // Proofreading is an aid, never a blocker: stay quiet and let the
      // validator work rather than throwing a toast at every keystroke.
    } finally {
      wanted.forEach((w) => pending.delete(w.id));
    }
  }

  const recheck = LS.debounce((id) => checkSegments([id]), 450);

  /* -------------------------------------------------------------- painting */

  function rangeFor(segId, issue) {
    const el = window.LSEditor.segmentElement(segId);
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
      if (startNode === null && consumed + length > issue.start) {
        startNode = node;
        startOffset = issue.start - consumed;
      }
      if (startNode !== null && consumed + length >= issue.end) {
        endNode = node;
        endOffset = issue.end - consumed;
        break;
      }
      consumed += length;
    }

    if (!startNode || !endNode) return null;
    try {
      const range = document.createRange();
      range.setStart(startNode, Math.max(0, startOffset));
      range.setEnd(endNode, Math.min(endNode.nodeValue.length, endOffset));
      return range;
    } catch (err) {
      return null;
    }
  }

  function paint() {
    if (!supportsHighlights) return;
    const spelling = [];
    const artefact = [];

    issues.forEach((list, segId) => {
      list.forEach((issue) => {
        const range = rangeFor(segId, issue);
        if (!range) return;
        (issue.kind === 'spelling' ? spelling : artefact).push(range);
      });
    });

    CSS.highlights.set('ls-spell', new Highlight(...spelling));
    CSS.highlights.set('ls-artefact', new Highlight(...artefact));
  }

  function allIssues() {
    const out = [];
    window.LSEditor.segments().forEach((seg) => {
      (issues.get(Number(seg.id)) || []).forEach((issue) => {
        out.push({ segId: Number(seg.id), issue });
      });
    });
    return out;
  }

  function updateCount() {
    if (!counter) return;
    const total = allIssues().length;
    counter.textContent = total;
    counter.className = total ? 'badge badge-warn' : 'badge';
    if (jumpBtn) {
      jumpBtn.title = total
        ? `${total} flagged word${total === 1 ? '' : 's'}. Click to step through them.`
        : 'Nothing flagged in the part of the transcript checked so far.';
    }
  }

  /* --------------------------------------------------------- lazy checking */

  /* Only check what the validator can actually see. A two-hour interview can
     run to thousands of segments, and checking them all up front would stall
     the page for no benefit. */
  const observer = new IntersectionObserver((entries) => {
    const ids = entries
      .filter((entry) => entry.isIntersecting)
      .map((entry) => Number(entry.target.dataset.id))
      .filter((id) => !pending.has(id));
    if (ids.length) checkSegments(ids);
  }, { rootMargin: '400px 0px' });

  function observeAll() {
    observer.disconnect();
    document.querySelectorAll('.seg[data-id]').forEach((el) => observer.observe(el));
  }

  /* ------------------------------------------------------------- suggestions */

  function closeMenu() {
    if (menu) { menu.remove(); menu = null; }
  }

  function issueAt(segId, offset) {
    return (issues.get(segId) || []).find(
      (issue) => offset >= issue.start && offset <= issue.end
    );
  }

  /* Offset of a click inside a segment's text, in the same character space
     the server used. */
  function offsetAt(textEl, node, nodeOffset) {
    const walker = document.createTreeWalker(textEl, NodeFilter.SHOW_TEXT);
    let consumed = 0;
    let current;
    while ((current = walker.nextNode())) {
      if (current === node) return consumed + nodeOffset;
      consumed += current.nodeValue.length;
    }
    return -1;
  }

  function showMenu(event, segId, issue) {
    closeMenu();
    menu = document.createElement('div');
    menu.className = 'proof-menu';

    const header = document.createElement('div');
    header.className = 'proof-msg';
    header.textContent = issue.message;
    menu.appendChild(header);

    (issue.suggestions || []).forEach((suggestion) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'proof-item';
      item.textContent = suggestion;
      item.addEventListener('click', () => {
        applySuggestion(segId, issue, suggestion);
        closeMenu();
      });
      menu.appendChild(item);
    });

    if (!(issue.suggestions || []).length) {
      const none = document.createElement('div');
      none.className = 'proof-none';
      none.textContent = 'No suggestions';
      menu.appendChild(none);
    }

    if (issue.kind === 'spelling') {
      menu.appendChild(divider());
      menu.appendChild(action('Add to this document', () =>
        addToDictionary(issue.word, 'document')));
      menu.appendChild(action('Add to project glossary', () =>
        addToDictionary(issue.word, 'project')));
    }
    menu.appendChild(divider());
    menu.appendChild(action('Ignore here', () => ignore(segId, issue)));

    document.body.appendChild(menu);
    const x = Math.min(event.clientX, window.innerWidth - menu.offsetWidth - 12);
    const y = Math.min(event.clientY, window.innerHeight - menu.offsetHeight - 12);
    menu.style.left = `${Math.max(8, x)}px`;
    menu.style.top = `${Math.max(8, y)}px`;
  }

  function divider() {
    const el = document.createElement('div');
    el.className = 'proof-divider';
    return el;
  }

  function action(label, fn) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'proof-action';
    button.textContent = label;
    button.addEventListener('click', () => { fn(); closeMenu(); });
    return button;
  }

  function applySuggestion(segId, issue, suggestion) {
    const seg = window.LSEditor.segmentById(segId);
    if (!seg) return;
    const text = seg.text || '';
    seg.text = text.slice(0, issue.start) + suggestion + text.slice(issue.end);
    seg.edited = true;
    seg.stale_timings = true;
    window.LSEditor.queueEdit(segId, { text: seg.text });
    checked.delete(segId);
    window.LSEditor.render();
    checkSegments([segId]);
  }

  async function addToDictionary(word, scope) {
    try {
      await LS.post(`${window.LSEditor.api}/dictionary`, { word, scope });
      LS.toast(
        scope === 'project'
          ? `"${word}" added to the project glossary. It will also help Whisper spell it on future transcriptions.`
          : `"${word}" accepted for this document.`, 'ok', 7000);
      // The server dropped its cache, so everything must be re-checked.
      checked.clear();
      issues.clear();
      observeAll();
      const visible = [...document.querySelectorAll('.seg[data-id]')]
        .map((el) => Number(el.dataset.id));
      checkSegments(visible);
    } catch (err) {
      LS.toast(`Could not add the word: ${err.message}`, 'error');
    }
  }

  function ignore(segId, issue) {
    const list = (issues.get(segId) || []).filter((i) => i !== issue);
    issues.set(segId, list);
    paint();
    updateCount();
  }

  /* Right-click rather than left: a left click on a word already means "play
     from here", and that is the more valuable gesture to keep. */
  document.addEventListener('contextmenu', (event) => {
    const textEl = event.target.closest('.seg-text');
    if (!textEl) return;
    const segEl = textEl.closest('.seg');
    if (!segEl) return;

    const point = document.caretPositionFromPoint
      ? document.caretPositionFromPoint(event.clientX, event.clientY)
      : null;
    const node = point ? point.offsetNode : null;
    if (!node) return;

    const segId = Number(segEl.dataset.id);
    const offset = offsetAt(textEl, node, point.offset);
    if (offset < 0) return;

    const issue = issueAt(segId, offset);
    if (!issue) return;

    event.preventDefault();
    showMenu(event, segId, issue);
  });

  document.addEventListener('click', (event) => {
    if (menu && !menu.contains(event.target)) closeMenu();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeMenu();
  });

  /* ---------------------------------------------------------------- jumping */

  if (jumpBtn) {
    jumpBtn.addEventListener('click', () => {
      const all = allIssues();
      if (!all.length) {
        LS.toast('Nothing flagged in the transcript checked so far.', 'info');
        return;
      }
      cursor = (cursor + 1) % all.length;
      const { segId, issue } = all[cursor];
      const el = window.LSEditor.segmentElement(segId);
      if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      LS.toast(`${cursor + 1} of ${all.length}: ${issue.message}`, 'info', 5000);
    });
  }

  /* ----------------------------------------------------------------- events */

  // Re-check a segment shortly after it stops being edited.
  document.addEventListener('input', (event) => {
    const textEl = event.target.closest('.seg-text');
    if (!textEl) return;
    const segEl = textEl.closest('.seg');
    if (!segEl) return;
    const id = Number(segEl.dataset.id);
    // The model is updated on blur, so read the live text directly.
    const seg = window.LSEditor.segmentById(id);
    if (seg) {
      checked.delete(id);
      seg.text = textEl.textContent.replace(/\s+/g, ' ').trim();
      recheck(id);
    }
  });

  document.addEventListener('ls:rendered', () => {
    observeAll();
    paint();
    updateCount();
  });

  observeAll();
  updateCount();
})();
