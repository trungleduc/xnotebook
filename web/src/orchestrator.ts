// Page entry point: job -> merge deps -> solve -> download -> SEAL -> boot kernel -> run cells
// (all cells of the job, or one at a time from the host in an interactive session).

import { ILock } from '@emscripten-forge/mambajs-core';
import { computeLockId } from '@emscripten-forge/mambajs-core';
import { debug, emit, progress, request, send, step } from './host';
import { condaName, IMergedEnv, mergeEnv } from './deps';
import { channelUrls, downloadAll, formatBytes, lockChannelUrls, lockFiles, solveEnv } from './solve';
import { IKernelMessage, KernelClient, makeMessage, normalizeEname, OutputTracker } from './executor';
import { ensureCellIds, parseNotebook, scriptToNotebook, sourceText } from './formats';
import { WidgetStateTracker } from './widgets';
import { within } from './agent/paths';
import { ICell, IJob, IMount, INotebook, IRunResult, Output } from './types';

/** Control words (state, length) followed by the JSON of the input_reply. */
const STDIN_BUFFER_BYTES = 8 + (1 << 20);

async function fetchBytes(url: string): Promise<ArrayBuffer> {
  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(`failed to load ${url}: HTTP ${resp.status}`);
  }
  return resp.arrayBuffer();
}

async function main(): Promise<void> {
  const job: IJob = await (await fetch('job.json')).json();
  // Everything the sealed phase needs from the bundle is loaded now. In agent mode the
  // kernel runs under a supervisor (so that it can be restarted after the seal), next to
  // the agent worker.
  const worker = new Worker(job.agent ? 'kernel.host.worker.js' : 'kernel.worker.js');
  const agentWorker = job.agent ? new Worker('agent.worker.js') : null;
  const loaded = job.agent ? Promise.all([loadedSignal(worker, 'the kernel supervisor'), loadedSignal(agentWorker as Worker, 'the agent worker')]) : null;
  if (job.agent) {
    worker.postMessage({ xnb: 'source', source: await (await fetch('kernel.worker.js')).text() });
  }
  worker.addEventListener('message', ev => {
    if (ev.data?.xnb === 'step') {
      (ev.data.info ? progress : step)(ev.data.message);
    }
  });
  const untarWasm = await fetchBytes('unpack.wasm');

  const inputNb = job.format === 'ipynb' ? parseNotebook(job.content) : null;
  const nb: INotebook = inputNb ?? scriptToNotebook(job.path, job.content);
  const env = mergeEnv(job, inputNb);
  debug(`environment:\n${env.yml}`);
  progress(`environment: ${env.specs.concat(env.pipSpecs.map(p => `pip:${p}`)).join(', ')}`);
  step(`kernel: ${env.kernelName || '?'} (${env.kernelPackage})`);
  step(`channels: ${env.channels.join(', ')}`);

  // --- setup: solve (or reuse a lock) and fetch packages through the proxy ---
  let lock: ILock | null = job.lock ?? null;
  const lockId = computeLockId(env.yml);
  const urls = lock ? lockChannelUrls(lock) : channelUrls(env.channels);
  const hasPip = env.pipSpecs.length > 0 || Object.keys(lock?.pipPackages ?? {}).length > 0;
  const envReply = await request<{ lock: ILock | null }>('env', { channelUrls: urls, hasPip, lockId });
  let solved = false;
  if (job.lock) {
    step('using the lock given with --lock');
  } else if (!lock && envReply.lock) {
    lock = envReply.lock;
    step(`using cached lock ${lockId}`);
  }
  if (!lock) {
    progress('solving environment...');
    const t = performance.now();
    lock = await solveEnv(env.yml);
    solved = true;
    step(`solved in ${((performance.now() - t) / 1000).toFixed(1)}s`);
  }
  const files = lockFiles(lock);
  for (const [filename, pkg] of Object.entries(lock.packages)) {
    step(`  ${pkg.name} ${pkg.version} (${pkg.channel}) ${filename}`);
  }
  for (const [filename, pkg] of Object.entries(lock.pipPackages ?? {})) {
    step(`  ${pkg.name} ${pkg.version} (pip) ${filename}`);
  }
  await request('lock', { lock, lockId, solved, files });
  const totalSize = files.reduce((n, f) => n + (f.size ?? 0), 0);
  progress(`fetching ${files.length} packages (${formatBytes(totalSize)})...`);
  const tFetch = performance.now();
  const downloaded = await downloadAll(files);
  step(`fetched in ${((performance.now() - tFetch) / 1000).toFixed(1)}s`);

  // --- seal: from here on, no network for anyone, the proxy included ---
  await loaded;
  await request('seal');
  progress('sealed: network disabled');
  progress(`starting kernel ${env.kernelName || env.kernelPackage}...`);
  const tBoot = performance.now();

  // Bridge mode: input() blocks the worker on this buffer until the frontend answers.
  const stdinBuffer = job.bridge && crossOriginIsolated ? new SharedArrayBuffer(STDIN_BUFFER_BYTES) : null;
  const kernel = job.agent ? new KernelClient(worker, () => worker.postMessage({ xnb: 'kill' })) : new KernelClient(worker);
  kernel.onDebug = m => debug(m);
  const ready = waitReady(worker);
  worker.postMessage(
    {
      xnb: 'boot',
      packages: downloaded.map(d => ({ filename: d.filename, kind: d.kind, data: d.data })),
      lockPackages: lock.packages,
      untarWasm,
      kernelName: env.kernelName,
      kernelPackage: env.kernelPackage,
      stdin: job.stdin ?? null,
      mounts: job.mounts ?? [],
      cwd: job.cwd ?? null,
      stdinBuffer
    },
    [...downloaded.map(d => d.data), untarWasm]
  );
  const spec = await ready;
  progress(`kernel ready: ${spec.display_name} (${((performance.now() - tBoot) / 1000).toFixed(1)}s)`);
  emit({ kind: 'kernel_ready', spec });

  const result = job.bridge
    ? await bridge(job, nb, kernel, stdinBuffer)
    : job.agent
      ? await agentSession(job, nb, worker, kernel, spec, agentWorker as Worker, requestedPackages(env, lock))
      : job.interactive
      ? await interactive(job, nb, kernel, spec)
      : await execute(job, nb, kernel, spec, lock);
  send({ type: 'done', result });
}

/** Resolves when a worker reports that its script has loaded (before the seal). */
function loadedSignal(worker: Worker, name: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const onMsg = (ev: MessageEvent) => {
      if (ev.data?.xnb === 'loaded') {
        worker.removeEventListener('message', onMsg);
        resolve();
      }
    };
    worker.addEventListener('message', onMsg);
    worker.addEventListener('error', ev => reject(new Error(`${name} failed to load: ${ev.message}`)), { once: true });
  });
}

/** The kernelspec of a booting kernel, once it is ready. */
function waitReady(worker: Worker): Promise<any> {
  return new Promise<any>((resolve, reject) => {
    const onMsg = (ev: MessageEvent) => {
      if (ev.data?.xnb === 'ready') {
        worker.removeEventListener('message', onMsg);
        resolve(ev.data.spec);
      } else if (ev.data?.xnb === 'fatal') {
        worker.removeEventListener('message', onMsg);
        reject(new Error(`kernel failed to start: ${ev.data.message}`));
      }
    };
    worker.addEventListener('message', onMsg);
    worker.addEventListener('error', ev => reject(new Error(`kernel worker error: ${ev.message}`)), { once: true });
  });
}

interface IExecContext {
  kernel: KernelClient;
  tracker: OutputTracker;
  /** Per-cell timeout in seconds. */
  cellTimeout: number | null;
  stopOnError: boolean;
}

interface ICellRun {
  status: 'ok' | 'error' | 'timeout' | 'dead';
  error: string | null;
}

/** Fill in language_info and kernelspec from the running kernel. */
async function describeKernel(nb: INotebook, kernel: KernelClient, spec: any): Promise<void> {
  const info = await kernel.request(makeMessage('kernel_info_request', {}), () => {}, 60_000);
  if (info.reply) {
    nb.metadata.language_info = info.reply.content.language_info ?? nb.metadata.language_info;
  }
  nb.metadata.kernelspec = {
    name: spec.name,
    display_name: spec.display_name,
    language: spec.language ?? info.reply?.content?.language_info?.name
  };
  nb.metadata.xnb = nb.metadata.xnb ?? undefined;
  if (nb.metadata.xnb === undefined) {
    delete nb.metadata.xnb;
  }
}

/** Execute one code cell, filling its outputs and execution_count in place. */
async function runCell(ctx: IExecContext, index: number, cell: ICell): Promise<ICellRun> {
  const { kernel, tracker, cellTimeout } = ctx;
  cell.outputs = [];
  cell.execution_count = null;
  const code = sourceText(cell.source);
  emit({ kind: 'cell_start', cell: index, source: code });
  const tCell = performance.now();
  const outputs: Output[] = cell.outputs;
  const state = { clearPending: false };
  const msg = makeMessage('execute_request', {
    code,
    silent: false,
    store_history: true,
    user_expressions: {},
    allow_stdin: true,
    stop_on_error: ctx.stopOnError
  });
  const res = await kernel.request(
    msg,
    m => {
      if (m.header.msg_type === 'execute_input') {
        cell.execution_count = m.content.execution_count ?? cell.execution_count;
        return;
      }
      tracker.apply(index, outputs, m, state);
    },
    cellTimeout ? cellTimeout * 1000 : null
  );
  if (res.status === 'ok') {
    await kernel.sync(); // top-level await: let the pending promise settle
  }
  if (res.reply) {
    cell.execution_count = res.reply.content.execution_count ?? cell.execution_count;
  }
  emit({
    kind: 'cell_end',
    cell: index,
    status: res.reply?.content?.status ?? res.status,
    seconds: (performance.now() - tCell) / 1000,
    outputs: outputs.length
  });
  if (res.status === 'timeout') {
    kernel.terminate(`cell ${index} timed out`);
    outputs.push({
      output_type: 'error',
      ename: 'TimeoutError',
      evalue: `cell execution timed out after ${cellTimeout}s`,
      traceback: [`TimeoutError: cell execution timed out after ${cellTimeout}s`]
    });
    return { status: 'timeout', error: `cell ${index} timed out after ${cellTimeout}s` };
  }
  if (res.status === 'dead') {
    outputs.push({ output_type: 'error', ename: 'KernelDied', evalue: String(res.reason), traceback: [] });
    return { status: 'dead', error: `kernel died: ${res.reason}` };
  }
  if (res.reply?.content?.status === 'error') {
    return { status: 'error', error: `${normalizeEname(res.reply.content.ename)}: ${res.reply.content.evalue}` };
  }
  return { status: 'ok', error: null };
}

/** Send one message to the kernel worker and wait for its `reply` message. */
function workerCall(kernel: KernelClient, msg: Record<string, any>, reply: string): Promise<any> {
  return new Promise(resolve => {
    const onMsg = (ev: MessageEvent) => {
      if (ev.data?.xnb === reply) {
        kernel.worker.removeEventListener('message', onMsg);
        resolve(ev.data);
      }
    };
    kernel.worker.addEventListener('message', onMsg);
    kernel.worker.postMessage(msg);
  });
}

function hasRwMounts(job: IJob): boolean {
  return (job.mounts ?? []).some(m => m.mode === 'rw');
}

/** Copy rw mounts back out of the kernel filesystem (all files, or only changed ones). */
async function collectMounts(job: IJob, kernel: KernelClient, changed = false): Promise<IRunResult['mounts']> {
  if (kernel.dead || !hasRwMounts(job)) {
    return [];
  }
  return (await workerCall(kernel, { xnb: 'collect', changed }, 'collected')).mounts;
}

async function execute(
  job: IJob,
  nb: INotebook,
  kernel: KernelClient,
  spec: any,
  lock: ILock
): Promise<IRunResult> {
  const tracker = new OutputTracker((cell, output, kind) => emit({ kind: 'output', cell, output, mode: kind }));
  const widgets = new WidgetStateTracker();
  if (job.widgetState !== false) {
    kernel.onAnyMessage = m => widgets.handle(m);
  }
  await describeKernel(nb, kernel, spec);

  let status: IRunResult['status'] = 'ok';
  let failedCell: number | null = null;
  let error: string | null = null;
  const ctx: IExecContext = { kernel, tracker, cellTimeout: job.cellTimeout ?? null, stopOnError: !job.allowErrors };

  for (let i = 0; i < nb.cells.length; i++) {
    const cell = nb.cells[i];
    if (cell.cell_type !== 'code') {
      continue;
    }
    if (status !== 'ok') {
      cell.outputs = []; // cells after a failure are left unexecuted
      cell.execution_count = null;
      continue;
    }
    const run = await runCell(ctx, i, cell);
    if (run.status === 'timeout') {
      status = 'timeout';
    } else if (run.status === 'dead' || (run.status === 'error' && !job.allowErrors)) {
      status = 'error';
    }
    if (status !== 'ok') {
      failedCell = i;
      error = run.error;
    }
  }

  const mounts = await collectMounts(job, kernel);
  void lock;
  if (job.widgetState !== false) {
    widgets.saveTo(nb);
    if (widgets.size) {
      step(`saved state of ${widgets.size} widget models`);
    }
  }
  return { notebook: nb, status, failedCell, error, mounts };
}

/** What the host gets back for each cell of an interactive session. */
interface ICellReport {
  cell: number;
  status: ICellRun['status'];
  error: string | null;
  executionCount: number | null;
  outputs: Output[];
  /** Files under rw mounts that changed during the cell. */
  mounts?: IRunResult['mounts'];
}

/** A host command for an interactive session; exactly one field is set. */
interface ICommand {
  code?: string;
  /** Read a file (or list a directory) of the kernel filesystem. */
  read?: string;
  maxBytes?: number;
  close?: boolean;
}

/**
 * Interactive session: ask the host for one command at a time (`next`) until it says
 * close or the kernel dies. Every `next` request carries the result of the previous one.
 */
async function interactive(job: IJob, nb: INotebook, kernel: KernelClient, spec: any): Promise<IRunResult> {
  // Outputs travel back with the cell report; no need to stream them as events too.
  const tracker = new OutputTracker(() => {});
  await describeKernel(nb, kernel, spec);
  nb.cells = [];
  const ctx: IExecContext = { kernel, tracker, cellTimeout: job.cellTimeout ?? null, stopOnError: false };

  let last: ICellReport | Record<string, any> | null = null;
  let status: IRunResult['status'] = 'ok';
  let failedCell: number | null = null;
  let error: string | null = null;
  for (;;) {
    const next: ICommand = await request<ICommand>('next', { result: last });
    if (next.close) {
      last = null;
      break;
    }
    if (next.read !== undefined) {
      last = (await workerCall(kernel, { xnb: 'read', path: next.read, maxBytes: next.maxBytes ?? 0 }, 'read-result')).result;
      continue;
    }
    const cell: ICell = { cell_type: 'code', source: next.code ?? '', metadata: {}, outputs: [], execution_count: null };
    const index = nb.cells.push(cell) - 1;
    const run = await runCell(ctx, index, cell);
    const report: ICellReport = {
      cell: index,
      status: run.status,
      error: run.error,
      executionCount: cell.execution_count ?? null,
      outputs: cell.outputs ?? []
    };
    if (hasRwMounts(job) && !kernel.dead) {
      report.mounts = await collectMounts(job, kernel, true);
    }
    last = report;
    if (run.status === 'timeout' || run.status === 'dead') {
      status = run.status === 'timeout' ? 'timeout' : 'error';
      failedCell = index;
      error = run.error;
      break; // the kernel is gone; `last` goes back with `done`
    }
  }
  ensureCellIds(nb);
  const mounts = await collectMounts(job, kernel, true);
  return { notebook: nb, status, failedCell, error, mounts, last };
}

/** The packages the user asked for, with their solved versions (for the agent's prompt). */
function requestedPackages(env: IMergedEnv, lock: ILock): string[] {
  const conda = new Map(Object.values(lock.packages).map(p => [p.name.toLowerCase(), p.version]));
  const pip = new Map(Object.values(lock.pipPackages ?? {}).map((p: any) => [String(p.name).toLowerCase(), p.version]));
  const out: string[] = [];
  for (const spec of env.specs) {
    const name = condaName(spec);
    out.push(conda.has(name) ? `${name} ${conda.get(name)}` : spec);
  }
  for (const spec of env.pipSpecs) {
    const name = condaName(spec);
    out.push(pip.has(name) ? `${name} ${pip.get(name)} (pip)` : `${spec} (pip)`);
  }
  return out;
}

/**
 * Agent session: the agent worker drives the kernel. The host sends chat and control
 * commands through `__xnbAgent` and model replies through `__xnbLlm`; the page sends the
 * agent's events, its model requests (`llm`) and file write-backs to the host.
 */
async function agentSession(
  job: IJob,
  nb: INotebook,
  worker: Worker,
  first: KernelClient,
  spec: any,
  agentWorker: Worker,
  packages: string[]
): Promise<IRunResult> {
  const config = job.agent!;
  const cwd = job.cwd ?? '/home/xnb';
  /** Events for the `xnb agent` terminal (cell events from runCell go to `emit`). */
  const agentEvent = (event: Record<string, any>) => send({ type: 'agent', event });
  let kernel = first;
  let restarts = 0;
  const tracker = new OutputTracker(() => {});
  await describeKernel(nb, kernel, spec);
  nb.cells = [];

  // File operations, answered by the kernel worker; a dead kernel fails them all.
  let fsId = 0;
  const fsPending = new Map<number, (r: any) => void>();
  worker.addEventListener('message', ev => {
    if (ev.data?.xnb === 'fs-result') {
      const done = fsPending.get(ev.data.id);
      fsPending.delete(ev.data.id);
      done?.(ev.data);
    }
  });
  const dead = () => ({ error: `the kernel is dead (${kernel.dead}); call restart_kernel`, code: 'EDEAD' });
  const watch = (k: KernelClient) =>
    k.onDead(reason => {
      for (const done of fsPending.values()) {
        done(dead());
      }
      fsPending.clear();
      if (reason !== 'restarted') {
        agentEvent({ kind: 'kernel_dead', reason });
      }
    });
  watch(kernel);
  const fsCall = (args: Record<string, any>): Promise<any> =>
    kernel.dead
      ? Promise.resolve(dead())
      : new Promise(resolve => {
          const id = ++fsId;
          fsPending.set(id, resolve);
          worker.postMessage({ xnb: 'fs', id, ...args });
        });
  const readOnly = (path: string): IMount | undefined =>
    (job.mounts ?? []).find(m => m.mode === 'ro' && within(path, m.dst));

  /** Send files changed under rw mounts to the host; returns the host paths it saved. */
  const saveChanges = async (): Promise<string[]> => {
    if (!hasRwMounts(job) || kernel.dead) {
      return [];
    }
    const changed = await Promise.race([
      collectMounts(job, kernel, true),
      new Promise<IRunResult['mounts']>(resolve => kernel.onDead(() => resolve([])))
    ]);
    if (!changed?.some(m => m.files.length)) {
      return [];
    }
    return (await request<{ saved: string[] }>('writeback', { mounts: changed })).saved ?? [];
  };

  const restart = async (): Promise<{ ok: boolean; message: string }> => {
    if (restarts >= config.maxRestarts) {
      return { ok: false, message: `restart limit reached (--max-restarts ${config.maxRestarts}); the kernel cannot be restarted again` };
    }
    restarts++;
    const fresh = await request<{ mounts: IMount[] }>('remount');
    const t = performance.now();
    kernel.dispose();
    kernel.die('restarted');
    kernel = new KernelClient(worker, () => worker.postMessage({ xnb: 'kill' }));
    kernel.onDebug = m => debug(m);
    watch(kernel);
    const ready = waitReady(worker);
    worker.postMessage({ xnb: 'restart', mounts: fresh.mounts });
    try {
      await ready;
    } catch (e: any) {
      kernel.die(String(e?.message ?? e));
      return { ok: false, message: String(e?.message ?? e) };
    }
    const seconds = (performance.now() - t) / 1000;
    agentEvent({ kind: 'kernel_restarted', seconds });
    return {
      ok: true,
      message: `fresh kernel started in ${seconds.toFixed(1)}s; variables are gone, and the mounts were copied in again from the host`
    };
  };

  const ops: Record<string, (args: any) => Promise<any>> = {
    execute: async ({ code }) => {
      const cell: ICell = { cell_type: 'code', source: code ?? '', metadata: {}, outputs: [], execution_count: null };
      if (kernel.dead) {
        return { status: 'dead', error: kernel.dead, outputs: [], executionCount: null };
      }
      const index = nb.cells.push(cell) - 1;
      const ctx: IExecContext = { kernel, tracker, cellTimeout: job.cellTimeout ?? null, stopOnError: false };
      const run = await runCell(ctx, index, cell);
      const saved = await saveChanges();
      return { cell: index, status: run.status, error: run.error, executionCount: cell.execution_count ?? null, outputs: cell.outputs ?? [], saved };
    },
    fs: async args => (args.op === 'read' || args.op === 'stat' || args.op === 'list' ? fsCall(args) : { error: `unknown file operation ${args.op}`, code: 'EINVAL' }),
    write: async ({ path, text }) => {
      const ro = readOnly(path);
      if (ro) {
        throw new Error(`${path} is in the read-only mount ${ro.dst}; changes there are never saved. Write somewhere else.`);
      }
      const r = await fsCall({ op: 'write', path, text });
      if (r.error) {
        throw new Error(r.error);
      }
      return saveChanges();
    },
    restart
  };

  // One operation at a time: the agent's tools and the host's /restart share the kernel.
  let queue: Promise<any> = Promise.resolve();
  const run = (op: string, args: any): Promise<any> => {
    const fn = ops[op];
    const p = queue.then(() => (fn ? fn(args) : Promise.reject(new Error(`unknown operation ${op}`))));
    queue = p.catch(() => undefined);
    return p;
  };

  agentWorker.addEventListener('message', ev => {
    const m = ev.data;
    switch (m?.xnb) {
      case 'agent-event':
        send({ type: 'agent', event: m.event });
        break;
      case 'call':
        run(m.op, m.args).then(
          value => agentWorker.postMessage({ xnb: 'reply', id: m.id, value }),
          e => agentWorker.postMessage({ xnb: 'reply', id: m.id, error: String(e?.message ?? e) })
        );
        break;
      case 'llm':
        send({ type: 'llm', reqId: m.reqId, method: m.method, path: m.path, headers: m.headers, body: m.body });
        break;
      case 'llm-abort':
        send({ type: 'llm_abort', reqId: m.reqId });
        break;
    }
  });
  window.__xnbLlm = reply => agentWorker.postMessage({ xnb: 'llm-reply', reply });

  const started = new Promise<void>((resolve, reject) => {
    const onMsg = (ev: MessageEvent) => {
      if (ev.data?.xnb === 'agent-ready') {
        agentWorker.removeEventListener('message', onMsg);
        resolve();
      } else if (ev.data?.xnb === 'agent-fatal') {
        agentWorker.removeEventListener('message', onMsg);
        reject(new Error(`the agent failed to start: ${ev.data.message}`));
      }
    };
    agentWorker.addEventListener('message', onMsg);
    agentWorker.addEventListener('error', ev => reject(new Error(`agent worker error: ${ev.message}`)), { once: true });
  });
  const mounts = (job.mounts ?? []).map(m => ({ dst: m.dst, mode: m.mode }));
  agentWorker.postMessage({ xnb: 'init', init: { config, cwd, mounts, kernel: spec, packages } });
  await started;

  const closed = new Promise<void>(resolve => {
    window.__xnbAgent = (cmd: any) => {
      switch (cmd?.type) {
        case 'chat':
          agentWorker.postMessage({ xnb: 'chat', text: String(cmd.text ?? '') });
          break;
        case 'abort':
        case 'reset':
          agentWorker.postMessage({ xnb: cmd.type });
          break;
        case 'restart':
          run('restart', {}).then(
            r => agentEvent({ kind: 'restart_result', ...r }),
            e => agentEvent({ kind: 'restart_result', ok: false, message: String(e?.message ?? e) })
          );
          break;
        case 'notebook':
          ensureCellIds(nb);
          send({ type: 'agent', event: { kind: 'notebook', notebook: nb } });
          break;
        case 'close':
          resolve();
          break;
      }
    };
  });
  agentEvent({ kind: 'agent_ready', spec, packages, cwd, mounts });
  await closed;
  agentWorker.postMessage({ xnb: 'abort' });
  window.__xnbAgent = undefined;
  window.__xnbLlm = undefined;
  ensureCellIds(nb);
  await queue.then(saveChanges).catch(() => undefined);
  return { notebook: nb, status: 'ok', mounts: [] };
}

function toBase64(data: Uint8Array): string {
  let s = '';
  for (let i = 0; i < data.length; i += 0x8000) {
    s += String.fromCharCode(...data.subarray(i, i + 0x8000));
  }
  return btoa(s);
}

function fromBase64(s: string): Uint8Array {
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) {
    out[i] = bin.charCodeAt(i);
  }
  return out;
}

/** Binary buffers cross the CDP bridge as base64 strings. */
function encodeBuffers(msg: IKernelMessage): IKernelMessage {
  const buffers = (msg.buffers ?? []).map((b: any) =>
    toBase64(b instanceof ArrayBuffer ? new Uint8Array(b) : new Uint8Array(b.buffer, b.byteOffset, b.byteLength))
  );
  return { ...msg, buffers };
}

function decodeBuffers(msg: IKernelMessage): IKernelMessage {
  const buffers = (msg.buffers ?? []).map((b: string) => {
    const bytes = fromBase64(b);
    return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  });
  return { ...msg, buffers };
}

/** Hand an input_reply to the worker blocked in get_stdin. */
function answerStdin(stdinBuffer: SharedArrayBuffer, msg: IKernelMessage): void {
  const ctrl = new Int32Array(stdinBuffer, 0, 2);
  let data = new TextEncoder().encode(JSON.stringify(msg));
  if (data.length > stdinBuffer.byteLength - 8) {
    const error = { ...msg, content: { value: '', status: 'error' } };
    data = new TextEncoder().encode(JSON.stringify(error));
  }
  new Uint8Array(stdinBuffer, 8, data.length).set(data);
  Atomics.store(ctrl, 1, data.length);
  Atomics.store(ctrl, 0, 1);
  Atomics.notify(ctrl, 0);
}

/**
 * Kernel bridge: every kernel message goes to the host (`kmsg`), and the host delivers
 * frontend messages through `__xnbKernel`. Runs until the kernel dies.
 */
async function bridge(
  job: IJob,
  nb: INotebook,
  kernel: KernelClient,
  stdinBuffer: SharedArrayBuffer | null
): Promise<IRunResult> {
  const rw = hasRwMounts(job);
  kernel.onAnyMessage = m => send({ type: 'kmsg', msg: encodeBuffers(m) });
  kernel.worker.addEventListener('message', ev => {
    if (ev.data?.xnb === 'input_request') {
      send({ type: 'kmsg', msg: encodeBuffers({ ...ev.data.msg, channel: 'stdin' }) });
    } else if (ev.data?.xnb === 'collected') {
      send({ type: 'mounts', mounts: ev.data.mounts });
    }
  });
  window.__xnbKernel = (msg: IKernelMessage) => {
    if (msg.channel === 'stdin') {
      if (stdinBuffer && msg.header.msg_type === 'input_reply') {
        answerStdin(stdinBuffer, msg);
      }
      return;
    }
    kernel.worker.postMessage({ xnb: 'msg', msg: decodeBuffers(msg) });
    if (rw && msg.header.msg_type === 'execute_request') {
      kernel.worker.postMessage({ xnb: 'collect', changed: true });
    }
  };
  const reason = await new Promise<string>(resolve => kernel.onDead(resolve));
  window.__xnbKernel = undefined;
  return { notebook: nb, status: 'error', error: `kernel died: ${reason}` };
}

main().catch(e => {
  send({ type: 'error', message: String(e?.stack ?? e?.message ?? e) });
});
