/* Header links that open a collapsed tools panel and move focus into it. */

(() => {
  document.addEventListener('click', (event) => {
    const link = event.target.closest('a[data-open-tool]');
    if (!link) return;
    const panel = document.getElementById((link.getAttribute('href') || '').slice(1));
    if (!panel) return;
    event.preventDefault();
    panel.open = true;
    const summary = panel.querySelector('summary');
    if (summary) summary.focus();
  });
})();
