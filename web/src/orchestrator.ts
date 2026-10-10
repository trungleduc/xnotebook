// Page entry point: job -> merge deps -> solve -> download -> SEAL -> boot kernel -> run cells
// (all cells of the job, or one at a time from the host in an interactive session).

import { ILock } from '@emscripten-forge/mambajs-core';
import { computeLockId } from '@emscripten-forge/mambajs-core';
import { debug, emit, progress, request, send, step } from './host';
import { mergeEnv } from './deps';
import { channelUrls, downloadAll, formatBytes, lockChannelUrls, lockFiles, solveEnv } from './solve';
import { KernelClient, makeMessage, normalizeEname, OutputTracker } from './executor';
import { ensureCellIds, parseNotebook, scriptToNotebook, sourceText } from './formats';
import { WidgetStateTracker } from './widgets';
import { ICell, IJob, INotebook, IRunResult, Output } from './types';

async function fetchBytes(url: string): Promise<ArrayBuffer> {
  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(`failed to load ${url}: HTTP ${resp.status}`);
  }
  return resp.arrayBuffer();
}

async function main(): Promise<void> {
  const job: IJob = await (await fetch('job.json')).json();
  // Everything the sealed phase needs from the bundle is loaded now.
  const worker = new Worker('kernel.worker.js');
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
  await request('seal');
  progress('sealed: network disabled');
  progress(`starting kernel ${env.kernelName || env.kernelPackage}...`);
  const tBoot = performance.now();

  const kernel = new KernelClient(worker);
  kernel.onDebug = m => debug(m);
  const ready = new Promise<any>((resolve, reject) => {
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
    worker.addEventListener('error', ev => reject(new Error(`kernel worker error: ${ev.message}`)));
  });
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
      cwd: job.cwd ?? null
    },
    [...downloaded.map(d => d.data), untarWasm]
  );
  const spec = await ready;
  progress(`kernel ready: ${spec.display_name} (${((performance.now() - tBoot) / 1000).toFixed(1)}s)`);
  emit({ kind: 'kernel_ready', spec });

  const result = job.interactive
    ? await interactive(job, nb, kernel, spec)
    : await execute(job, nb, kernel, spec, lock);
  send({ type: 'done', result });
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

main().catch(e => {
  send({ type: 'error', message: String(e?.stack ?? e?.message ?? e) });
});
