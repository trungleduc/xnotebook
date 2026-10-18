import { test } from 'node:test';
import assert from 'node:assert/strict';
import { reportToContent } from '../src/agent/content';
import { resolvePath, within } from '../src/agent/paths';
import { makeTools } from '../src/agent/tools';
import { Tunnel, TUNNEL_BASE, type ITunnelRequest } from '../src/agent/tunnel';

const b64 = (s: string) => Buffer.from(s).toString('base64');

test('reportToContent: text, images, errors and limits', () => {
  const png = { output_type: 'display_data', data: { 'image/png': 'iVBO\nRw0K', 'text/plain': '<Figure>' } };
  const r = reportToContent({
    status: 'ok',
    outputs: [{ output_type: 'stream', name: 'stdout', text: 'a\n' }, png, { output_type: 'execute_result', data: { 'text/plain': '42' } }]
  });
  assert.equal(r.isError, false);
  assert.deepEqual(r.content, [
    { type: 'text', text: 'a\n' },
    { type: 'image', data: 'iVBORw0K', mimeType: 'image/png' },
    { type: 'text', text: '42\n' }
  ]);
  const noImages = reportToContent({ status: 'ok', outputs: [png] }, { images: false });
  assert.deepEqual(noImages.content, [{ type: 'text', text: '[image/png omitted]\n' }]);
  const dead = reportToContent({ status: 'timeout', error: 'cell 1 timed out', outputs: [] });
  assert.equal(dead.isError, true);
  assert.match((dead.content[0] as any).text, /call restart_kernel/);
  const long = reportToContent({ status: 'ok', outputs: [{ output_type: 'stream', text: 'x'.repeat(50_000) }] });
  assert.match((long.content[0] as any).text, /characters truncated/);
  assert.deepEqual(reportToContent({ status: 'ok', outputs: [] }).content, [{ type: 'text', text: '(no output)' }]);
});

test('resolvePath and within', () => {
  assert.equal(resolvePath('a/../b.py', '/home/xnb'), '/home/xnb/b.py');
  assert.equal(resolvePath('@/data/x', '/home/xnb'), '/data/x');
  assert.equal(resolvePath('~/notes.md', '/data'), '/home/xnb/notes.md');
  assert.equal(within('/data/x/y', '/data'), true);
  assert.equal(within('/database', '/data'), false);
});

test('Tunnel: a streamed reply becomes a Response', async () => {
  const sent: ITunnelRequest[] = [];
  const tunnel = new Tunnel(r => sent.push(r), () => {});
  const pending = tunnel.fetch(`${TUNNEL_BASE}/v1/messages?beta=true`, {
    method: 'POST',
    headers: { 'x-api-key': 'xnb', 'content-type': 'application/json' },
    body: '{"model":"m"}'
  });
  await new Promise(r => setTimeout(r, 0));
  assert.equal(sent.length, 1);
  assert.equal(sent[0].path, '/v1/messages?beta=true');
  assert.equal(sent[0].body, '{"model":"m"}');
  const id = sent[0].reqId;
  tunnel.receive({ reqId: id, head: { status: 200, headers: { 'content-type': 'text/event-stream' } } });
  tunnel.receive({ reqId: id, chunk: 'data: 1\n\n' });
  tunnel.receive({ reqId: id, chunk: 'data: 2\n\n' });
  tunnel.receive({ reqId: id, end: true });
  const resp = await pending;
  assert.equal(resp.status, 200);
  assert.equal(await resp.text(), 'data: 1\n\ndata: 2\n\n');
});

test('Tunnel: abort tells the host and rejects', async () => {
  const cancelled: number[] = [];
  const tunnel = new Tunnel(() => {}, id => cancelled.push(id));
  const ctl = new AbortController();
  const pending = tunnel.fetch(`${TUNNEL_BASE}/v1/messages`, { method: 'POST', body: '{}', signal: ctl.signal });
  await new Promise(r => setTimeout(r, 0));
  ctl.abort();
  await assert.rejects(pending, (e: any) => e.name === 'AbortError');
  assert.deepEqual(cancelled, [1]);
});

/** A fake page: a kernel filesystem in a Map. */
function fakeRpc(files: Map<string, string>, readOnly = '/ro') {
  const calls: string[] = [];
  const rpc = async (op: string, args: any): Promise<any> => {
    calls.push(op);
    if (op === 'fs') {
      const f = files.get(args.path);
      if (args.op === 'stat') {
        const dir = [...files.keys()].some(k => k.startsWith(args.path + '/'));
        return f !== undefined || dir ? { value: { dir: f === undefined, file: f !== undefined, size: f?.length ?? 0 } } : { error: 'no such file', code: 'ENOENT' };
      }
      if (args.op === 'read') {
        return f === undefined ? { error: 'no such file', code: 'ENOENT' } : { value: b64(f) };
      }
      if (args.op === 'list') {
        return { value: [...files.keys()].filter(k => k.startsWith(args.path + '/')).map(k => ({ name: k.slice(args.path.length + 1), dir: false, size: files.get(k)!.length })) };
      }
    }
    if (op === 'write') {
      if (args.path.startsWith(readOnly + '/')) {
        throw new Error(`${args.path} is in the read-only mount ${readOnly}`);
      }
      files.set(args.path, args.text);
      return ['./saved'];
    }
    throw new Error(`unexpected ${op}`);
  };
  return { rpc, calls };
}

test('edit tool: pi edit engine, line endings, errors', async () => {
  const files = new Map([['/data/a.py', 'x = 1\r\ny = 2\r\n']]);
  const { rpc } = fakeRpc(files);
  const tools = Object.fromEntries(makeTools(rpc, '/data', true).map(t => [t.name, t]));
  const r = await tools.edit.execute('1', { path: 'a.py', edits: [{ oldText: 'y = 2', newText: 'y = 3' }] });
  assert.equal(files.get('/data/a.py'), 'x = 1\r\ny = 3\r\n'); // CRLF kept
  assert.match((r.content[0] as any).text, /Successfully replaced 1 block\(s\) in a.py. Saved to the host./);
  assert.match(r.details.patch, /-y = 2\n\+y = 3/);
  await assert.rejects(tools.edit.execute('2', { path: 'a.py', edits: [{ oldText: 'nope', newText: 'z' }] }), /Could not find/);
  await assert.rejects(tools.edit.execute('3', { path: 'missing.py', edits: [{ oldText: 'a', newText: 'b' }] }), /Could not edit file: missing.py/);
  // a single edit object, as some models send it
  const prepared = tools.edit.prepareArguments!({ path: 'a.py', edits: { oldText: 'x', newText: 'X' } });
  assert.deepEqual(prepared.edits, [{ oldText: 'x', newText: 'X' }]);
});

test('read and write tools', async () => {
  const lines = Array.from({ length: 3000 }, (_, i) => `line ${i + 1}`).join('\n');
  const files = new Map([['/data/big.txt', lines], ['/data/img.png', 'PNG']]);
  const { rpc } = fakeRpc(files);
  const tools = Object.fromEntries(makeTools(rpc, '/data', true).map(t => [t.name, t]));
  const head = await tools.read.execute('1', { path: 'big.txt' });
  assert.match((head.content[0] as any).text, /Showing lines 1-2000 of 3000. Use offset=2001/);
  const tail = await tools.read.execute('2', { path: 'big.txt', offset: 2999 });
  assert.equal((tail.content[0] as any).text, 'line 2999\nline 3000');
  const img = await tools.read.execute('3', { path: 'img.png' });
  assert.equal((img.content[1] as any).type, 'image');
  const dir = await tools.read.execute('4', { path: '/data' });
  assert.match((dir.content[0] as any).text, /big.txt/);
  const w = await tools.write.execute('5', { path: 'out/new.txt', content: 'hi' });
  assert.equal(files.get('/data/out/new.txt'), 'hi');
  assert.equal(w.details.bytes, 2);
  await assert.rejects(tools.write.execute('6', { path: '/ro/x', content: 'no' }), /read-only mount/);
  const textOnly = Object.fromEntries(makeTools(rpc, '/data', false).map(t => [t.name, t]));
  const noImg = await textOnly.read.execute('7', { path: 'img.png' });
  assert.match((noImg.content[0] as any).text, /does not take images/);
});
