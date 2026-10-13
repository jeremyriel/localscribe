/* Small, DOM-free playback calculations shared by the editor and its tests. */
(function exposePlayback(root, factory) {
  const playback = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = playback;
  if (root) root.LSPlayback = playback;
}(typeof window === 'undefined' ? null : window, () => {
  function seekTarget(seconds, duration) {
    const total = Math.max(0, Number(duration) || 0);
    const value = Number(seconds);
    return Math.max(0, Math.min(Number.isFinite(value) ? value : 0, total));
  }

  function timeAtFraction(fraction, start, span) {
    const position = Math.max(0, Math.min(1, Number(fraction) || 0));
    return Math.max(0, Number(start) || 0) + position * Math.max(0, Number(span) || 0);
  }

  function rangeWithRoll(start, end, pre, post, duration) {
    const total = Math.max(0, Number(duration) || 0);
    const beginning = Number(start) || 0;
    const finish = Number(end);
    return {
      start: seekTarget(beginning - (Number(pre) || 0), total),
      end: Number.isFinite(finish)
        ? seekTarget(finish + (Number(post) || 0), total)
        : null,
    };
  }

  return { seekTarget, timeAtFraction, rangeWithRoll };
}));
