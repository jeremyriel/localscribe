/* Run with: node tests/test_history_filter.js */
'use strict';

const assert = require('node:assert/strict');
const history = require('../app/static/js/history.js');

function row(groups, day) {
  return { dataset: { groups, day }, hidden: false };
}

function fakeDocument() {
  const rows = [
    row('network', 'Today'),
    row('network recovery', 'Today'),
    row('recovery', 'Yesterday'),
  ];
  const days = [
    { dataset: { day: 'Today' }, hidden: false },
    { dataset: { day: 'Yesterday' }, hidden: false },
  ];
  const detail = [{ hidden: true }, { hidden: true }];
  const table = {
    hidden: false,
    querySelectorAll: (sel) => ({
      '.history-row': rows, '.history-day': days, '.history-technical': detail,
    }[sel] || []),
  };
  const radios = ['all', 'network', 'recovery'].map((value) => ({
    value, checked: value === 'all', listeners: [],
    addEventListener(_type, fn) { this.listeners.push(fn); },
  }));
  const technical = { checked: false, listeners: [], addEventListener(_t, fn) { this.listeners.push(fn); } };
  const empty = { hidden: true, textContent: '', dataset: { emptyNetwork: 'No online.', emptyRecovery: 'No safety.' } };
  const status = { textContent: '' };
  const byId = { 'history-table': table, 'history-empty': empty, 'history-status': status, 'history-technical': technical };
  return {
    rows, days, detail, table, radios, technical, empty, status,
    doc: {
      getElementById: (id) => byId[id] || null,
      querySelectorAll: (sel) => (sel === 'input[name="history-type"]' ? radios : []),
    },
    pick(value) { radios.forEach((r) => { r.checked = r.value === value; }); radios.find((r) => r.checked).listeners.forEach((fn) => fn()); },
  };
}

assert.equal(history.matches('all', ''), true, 'All shows every event');
assert.equal(history.matches('network', 'network recovery'), true, 'a backup is also an online event');
assert.equal(history.matches('recovery', 'network recovery'), true, 'a backup is also a data safety event');
assert.equal(history.matches('recovery', 'network'), false, 'an update check is not data safety');

const page = fakeDocument();
history.init(page.doc);
assert.equal(page.rows.every((r) => !r.hidden), true, 'All starts with everything visible');
assert.equal(page.status.textContent, 'Showing all 3 events.');

page.pick('network');
assert.deepEqual(page.rows.map((r) => r.hidden), [false, false, true], 'Online hides data-safety-only rows');
assert.equal(page.days[1].hidden, true, 'a day with no visible rows is hidden');
assert.match(page.status.textContent, /Showing 2 of 3 events: online events only\./);

page.pick('recovery');
assert.deepEqual(page.rows.map((r) => r.hidden), [true, false, false], 'Data safety shows backups, snapshots and deletions');

page.rows.forEach((r) => { r.dataset.groups = 'network'; });
page.pick('recovery');
assert.equal(page.table.hidden, true, 'an empty result hides the table');
assert.equal(page.empty.hidden, false, 'and shows an explanation');
assert.equal(page.empty.textContent, 'No safety.', 'specific to the chosen filter');

page.pick('all');
assert.equal(page.empty.hidden, true, 'the explanation clears when events are visible again');

page.technical.checked = true;
page.technical.listeners.forEach((fn) => fn());
assert.equal(page.detail.every((el) => !el.hidden), true, 'technical details are revealed on demand');
page.technical.checked = false;
page.technical.listeners.forEach((fn) => fn());
assert.equal(page.detail.every((el) => el.hidden), true, 'and hidden again');

console.log('history filter tests passed');
