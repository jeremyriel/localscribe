/* Activity history filter behaviour, kept free of page globals so it can be tested. */
(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.LSHistory = api;
})(typeof self !== 'undefined' ? self : this, () => {
  const NAMES = { all: 'events', network: 'online events', recovery: 'data safety events' };

  /* Which rows a filter shows; a row can belong to several groups. */
  function matches(filter, groups) {
    if (filter === 'all') return true;
    return String(groups || '').split(/\s+/).includes(filter);
  }

  function statusText(filter, shown, total) {
    if (filter === 'all') return `Showing all ${total} ${NAMES.all}.`;
    return `Showing ${shown} of ${total} events: ${NAMES[filter] || filter} only.`;
  }

  function init(doc) {
    const table = doc.getElementById('history-table');
    if (!table) return;
    const rows = Array.from(table.querySelectorAll('.history-row'));
    const days = Array.from(table.querySelectorAll('.history-day'));
    const empty = doc.getElementById('history-empty');
    const status = doc.getElementById('history-status');
    const technical = doc.getElementById('history-technical');
    const radios = Array.from(doc.querySelectorAll('input[name="history-type"]'));

    function apply() {
      const picked = radios.find((radio) => radio.checked);
      const filter = picked ? picked.value : 'all';
      let visible = 0;
      const shownDays = new Set();
      rows.forEach((row) => {
        const show = matches(filter, row.dataset.groups);
        row.hidden = !show;
        if (show) { visible += 1; shownDays.add(row.dataset.day); }
      });
      days.forEach((day) => { day.hidden = !shownDays.has(day.dataset.day); });
      if (empty) {
        empty.textContent = (empty.dataset && empty.dataset[`empty${filter[0].toUpperCase()}${filter.slice(1)}`]) || '';
        empty.hidden = visible > 0;
      }
      table.hidden = visible === 0;
      if (status) status.textContent = statusText(filter, visible, rows.length);
      const showDetail = !!(technical && technical.checked);
      table.querySelectorAll('.history-technical').forEach((el) => { el.hidden = !showDetail; });
    }

    radios.forEach((radio) => radio.addEventListener('change', apply));
    if (technical) technical.addEventListener('change', apply);
    apply();
    return apply;
  }

  return { matches, statusText, init };
});

if (typeof document !== 'undefined') LSHistory.init(document);
