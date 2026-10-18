/* Run with: node tests/test_playback.js */
'use strict';

const assert = require('node:assert/strict');
const playback = require('../app/static/js/playback.js');

assert.equal(playback.timeAtFraction(0.5, 40, 20), 50,
  'waveform clicks seek to their visible timestamp');
assert.equal(playback.timeAtFraction(-1, 40, 20), 40,
  'waveform seeking clamps before the visible window');
assert.equal(playback.timeAtFraction(2, 40, 20), 60,
  'waveform seeking clamps after the visible window');
assert.equal(playback.seekTarget(127.25, 300), 127.25,
  'timestamp seeking preserves fractional seconds');
assert.equal(playback.seekTarget(-2, 300), 0,
  'timestamp seeking cannot seek before media');
assert.equal(playback.seekTarget(400, 300), 300,
  'timestamp seeking cannot seek after media');
assert.deepEqual(playback.rangeWithRoll(0.25, 4, 1, 2, 8),
  { start: 0, end: 6 }, 'pre/post-roll is clipped to the media duration');
