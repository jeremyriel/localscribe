/* Portable Review Mode queue for one transcript. */

(() => {
  const D = window.DOC;
  if (!D || !window.LSEditor) return;

  const labels = {
    low_confidence: 'Low confidence',
    artifact: 'Possible artifact',
    unassigned: 'Unassigned speaker',
    flagged: 'Flag',
    changed: 'Changed since validation',
  };
  const categories = Object.keys(labels);
  const artifacts = new Map();
  const filterInputs = [...document.querySelectorAll('[data-review-filter]')];
  const list = document.getElementById('review-items');
  const countsEl = document.getElementById('review-counts');
  const progressText = document.getElementById('review-progress');
  const progressBar = document.getElementById('review-progress-bar');
  const empty = document.getElementById('review-empty');
  const nextButton = document.getElementById('review-next');
  let scanComplete = !D.proofreading || !D.proofreading.available
    || (!D.proofreading.spelling && !D.proofreading.artefacts);
  let cursor = -1;
  let busy = false;

  function segmentReview(seg) {
    if (!seg.review || typeof seg.review !== 'object') {
      seg.review = { flagged: false, changed: false, resolved: [] };
    }
    if (!Array.isArray(seg.review.resolved)) seg.review.resolved = [];
    return seg.review;
  }

  function queueItems() {
    const items = [];
    LSEditor.segments().forEach((seg) => {
      const state = segmentReview(seg);
      const words = seg.words || [];
      const active = [];
      if (words.some((word) => Number(word.prob) < D.lowConfidence)) {
        active.push('low_confidence');
      }
      const checked = artifacts.get(Number(seg.id));
      if (checked && checked.text === (seg.text || '') && checked.issues.length) {
        active.push('artifact');
      }
      if (!(seg.speaker_id || '').trim()) active.push('unassigned');
      if (seg.review.flagged) active.push('flagged');
      if (seg.review.changed) active.push('changed');
      active.forEach((category) => items.push({
        segment: seg,
        category,
        resolved: state.resolved.includes(category),
      }));
    });
    return items;
  }

  function selectedCategories() {
    return new Set(filterInputs.filter((input) => input.checked)
      .map((input) => input.dataset.reviewFilter));
  }

  function render() {
    const items = queueItems();
    const categoryCounts = Object.fromEntries(categories.map((category) => [
      category, { total: 0, resolved: 0, open: 0 },
    ]));
    items.forEach((item) => {
      const count = categoryCounts[item.category];
      count.total += 1;
      count[item.resolved ? 'resolved' : 'open'] += 1;
    });
    const total = items.length;
    const done = items.filter((item) => item.resolved).length;
    const percent = total ? Math.round(100 * done / total) : 100;
    progressText.textContent = total
      ? `${done} of ${total} review items complete · ${total - done} unresolved` +
        (scanComplete ? '' : ' · checking for possible artifacts…')
      : scanComplete ? 'No review items found' : 'Checking for possible artifacts…';
    progressBar.value = percent;
    progressBar.setAttribute('aria-valuetext', `${percent}% complete`);
    countsEl.replaceChildren();
    categories.forEach((category) => {
      const count = categoryCounts[category];
      const chip = document.createElement('span');
      chip.textContent = `${labels[category]}: ${count.open} open / ${count.total}`;
      countsEl.appendChild(chip);
    });

    const visible = items.filter((item) =>
      selectedCategories().has(item.category) && !item.resolved);
    list.replaceChildren();
    visible.slice(0, 100).forEach((item, index) => {
      const row = document.createElement('li');
      row.className = 'review-item';
      const go = document.createElement('button');
      go.type = 'button';
      go.className = 'btn btn-small btn-quiet';
      go.textContent = `${labels[item.category]} · ${clock(item.segment.start)}`;
      go.title = `Go to ${labels[item.category].toLowerCase()} at ${clock(item.segment.start)}`;
      go.addEventListener('click', () => {
        cursor = index;
        jumpTo(item.segment);
      });
      const text = document.createElement('span');
      text.className = 'review-item-text';
      text.textContent = item.segment.text || '(empty segment)';
      const action = document.createElement('button');
      action.type = 'button';
      action.className = 'btn btn-small';
      action.textContent = item.category === 'flagged' ? 'Clear flag' : 'Reviewed';
      action.disabled = busy;
      action.addEventListener('click', () => resolveItem(item, index));
      row.append(go, text, action);
      list.appendChild(row);
    });
    empty.hidden = visible.length > 0;
    empty.textContent = scanComplete
      ? 'Nothing unresolved matches these filters.'
      : 'No matching items found yet; proofreading is checking the transcript…';
    if (visible.length > 100) {
      const note = document.createElement('li');
      note.className = 'small muted';
      note.textContent = `Showing 100 of ${visible.length}; use Next unresolved to continue.`;
      list.appendChild(note);
    }
    nextButton.disabled = visible.length === 0 || busy;
    return visible;
  }

  function clock(seconds) {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
    return `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`;
  }

  function jumpTo(seg) {
    const element = LSEditor.segmentElement(seg.id);
    if (element) {
      element.scrollIntoView({ block: 'center', behavior: 'smooth' });
      element.classList.add('review-target');
      setTimeout(() => element.classList.remove('review-target'), 1400);
    }
    LSEditor.playFrom(seg.start);
  }

  async function postAction(segmentId, category, action) {
    const result = await LS.post(`${LSEditor.api}/review`, {
      segment_id: segmentId, category, action,
    });
    const seg = LSEditor.segmentById(segmentId);
    if (seg) seg.review = result.review;
    return seg;
  }

  async function resolveItem(item, index) {
    if (busy) return;
    busy = true;
    cursor = index - 1;
    render();
    try {
      await postAction(
        item.segment.id, item.category, 'resolve');
      render();
      nextUnresolved();
    } catch (err) {
      LS.toast(`Could not save review progress: ${err.message}`, 'error');
    } finally {
      busy = false;
      render();
    }
  }

  async function toggleFlag(segmentId) {
    if (busy) return;
    const seg = LSEditor.segmentById(segmentId);
    if (!seg) return;
    busy = true;
    render();
    try {
      const action = segmentReview(seg).flagged ? 'resolve' : 'flag';
      await postAction(segmentId, 'flagged', action);
      render();
    } catch (err) {
      LS.toast(`Could not save review flag: ${err.message}`, 'error');
    } finally {
      busy = false;
      render();
    }
  }

  function nextUnresolved() {
    const visible = render();
    if (!visible.length) return;
    const index = (cursor + 1) % visible.length;
    cursor = index;
    jumpTo(visible[index].segment);
  }

  filterInputs.forEach((input) => input.addEventListener('change', () => {
    cursor = -1;
    render();
  }));
  nextButton.addEventListener('click', nextUnresolved);

  document.addEventListener('ls:proofread', (event) => {
    const { segmentId, text, issues } = event.detail || {};
    artifacts.set(Number(segmentId), {
      text: String(text || ''),
      issues: (issues || []).filter((issue) => issue.kind === 'artefact'),
    });
    render();
  });
  document.addEventListener('ls:proofread-complete', () => {
    scanComplete = true;
    render();
  });
  document.addEventListener('ls:rendered', render);

  window.LSReview = { toggleFlag, render };
  render();
})();
