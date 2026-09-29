import { test } from 'node:test';
import assert from 'node:assert/strict';
import { WIDGET_STATE_MIME, WidgetStateTracker } from '../src/widgets';

function m(type: string, content: any, buffers: any[] = []) {
  return { header: { msg_id: 'x', msg_type: type }, parent_header: {}, metadata: {}, content, buffers };
}

const base = { _model_module: '@jupyter-widgets/controls', _model_module_version: '2.0.0' };

test('open + update + close', () => {
  const t = new WidgetStateTracker();
  t.handle(m('comm_open', { comm_id: 'a', target_name: 'jupyter.widget', data: { state: { ...base, _model_name: 'IntSliderModel', value: 3 }, buffer_paths: [] } }));
  t.handle(m('comm_msg', { comm_id: 'a', data: { method: 'update', state: { value: 7 }, buffer_paths: [] } }));
  t.handle(m('comm_open', { comm_id: 'b', target_name: 'jupyter.widget', data: { state: { ...base, _model_name: 'CheckboxModel' } } }));
  t.handle(m('comm_close', { comm_id: 'b' }));
  t.handle(m('comm_open', { comm_id: 'c', target_name: 'other.target', data: { state: { _model_name: 'X' } } }));
  t.handle(m('comm_msg', { comm_id: 'a', data: { method: 'custom', content: {} } }));
  const doc = t.toMetadata()!;
  assert.deepEqual(Object.keys(doc.state), ['a']);
  assert.equal(doc.state.a.model_name, 'IntSliderModel');
  assert.equal(doc.state.a.state.value, 7);
  assert.equal(doc.version_major, 2);
});

test('binary buffers are base64 encoded and replaced on update', () => {
  const t = new WidgetStateTracker();
  const open = m(
    'comm_open',
    { comm_id: 'img', target_name: 'jupyter.widget', data: { state: { ...base, _model_name: 'ImageModel' }, buffer_paths: [['value']] } },
    [new Uint8Array([1, 2, 3]).buffer]
  );
  t.handle(open);
  t.handle(m('comm_msg', { comm_id: 'img', data: { method: 'update', state: {}, buffer_paths: [['value']] } }, [new DataView(new Uint8Array([4, 5]).buffer)]));
  const doc = t.toMetadata()!;
  assert.deepEqual(doc.state.img.buffers, [{ path: ['value'], encoding: 'base64', data: 'BAU=' }]);
});

test('saveTo replaces stale state and removes it when there are no widgets', () => {
  const nb: any = { nbformat: 4, nbformat_minor: 5, cells: [], metadata: { widgets: { [WIDGET_STATE_MIME]: { stale: true } } } };
  new WidgetStateTracker().saveTo(nb);
  assert.equal(nb.metadata.widgets, undefined);
  const t = new WidgetStateTracker();
  t.handle(m('comm_open', { comm_id: 'a', data: { state: { ...base, _model_name: 'TextModel' } } }));
  t.saveTo(nb);
  assert.deepEqual(Object.keys(nb.metadata.widgets[WIDGET_STATE_MIME].state), ['a']);
});
