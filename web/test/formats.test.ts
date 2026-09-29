import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseNotebook, scriptToNotebook } from '../src/formats';
import { normalizeEname, OutputTracker } from '../src/executor';

test('percent-format scripts', () => {
  const nb = scriptToNotebook(
    'a.py',
    '# /// script\n# dependencies = []\n# ///\n# %%\nx = 1\n\n# %% [markdown]\n# # Title\n# %%\nprint(x)\n'
  );
  assert.deepEqual(
    nb.cells.map(c => [c.cell_type, c.source]),
    [
      ['code', 'x = 1'],
      ['markdown', '# Title'],
      ['code', 'print(x)']
    ]
  );
  assert.equal(new Set(nb.cells.map(c => c.id)).size, 3);
});

test('scripts without markers are one cell; lua comments', () => {
  assert.equal(scriptToNotebook('a.py', 'a = 1\nb = 2\n').cells.length, 1);
  const lua = scriptToNotebook('a.lua', '-- %%\nprint(1)\n-- %%\nprint(2)\n');
  assert.deepEqual(lua.cells.map(c => c.source), ['print(1)', 'print(2)']);
});

test('notebooks get ids and nbformat 4.5', () => {
  const nb = parseNotebook(
    JSON.stringify({ nbformat: 4, nbformat_minor: 2, metadata: {}, cells: [{ cell_type: 'code', source: '', metadata: {} }, { cell_type: 'code', source: '', metadata: {}, id: 'dup' }, { cell_type: 'code', source: '', metadata: {}, id: 'dup' }] })
  );
  assert.equal(nb.nbformat_minor, 5);
  assert.equal(new Set(nb.cells.map(c => c.id)).size, 3);
  assert.equal(nb.cells[1].id, 'dup');
  assert.throws(() => parseNotebook('{}'));
});

test('ename normalisation', () => {
  assert.equal(normalizeEname("<class 'FileNotFoundError'>"), 'FileNotFoundError');
  assert.equal(normalizeEname("<class 'pkg.mod.MyError'>"), 'MyError');
  assert.equal(normalizeEname('ValueError'), 'ValueError');
});

function msg(type: string, content: any) {
  return { header: { msg_id: 'x', msg_type: type }, parent_header: {}, metadata: {}, content };
}

test('output tracking: streams, display updates, clear_output(wait)', () => {
  const t = new OutputTracker(() => {});
  const a: any[] = [];
  const b: any[] = [];
  const st = { clearPending: false };
  t.apply(0, a, msg('stream', { name: 'stdout', text: 'x' }), st);
  t.apply(0, a, msg('stream', { name: 'stdout', text: 'y' }), st);
  t.apply(0, a, msg('display_data', { data: { 'text/plain': 'v1' }, transient: { display_id: 'd' } }), st);
  t.apply(1, b, msg('update_display_data', { data: { 'text/plain': 'v2' }, transient: { display_id: 'd' } }), st);
  assert.equal(a[0].text, 'xy');
  assert.equal(a[1].data['text/plain'], 'v2');
  const st2 = { clearPending: false };
  t.apply(1, b, msg('stream', { name: 'stdout', text: 'old' }), st2);
  t.apply(1, b, msg('clear_output', { wait: true }), st2);
  assert.equal(b.length, 1);
  t.apply(1, b, msg('stream', { name: 'stdout', text: 'new' }), st2);
  assert.deepEqual(b, [{ output_type: 'stream', name: 'stdout', text: 'new' }]);
});
