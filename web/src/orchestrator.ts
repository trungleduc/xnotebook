// Page entry point: job -> merge deps -> solve -> download -> SEAL -> boot kernel -> run cells.

import { ILock } from '@emscripten-forge/mambajs-core';
import { computeLockId } from '@emscripten-forge/mambajs-core';
import { debug, emit, progress, request, send, step } from './host';
import { mergeEnv } from './deps';
import { channelUrls, downloadAll, formatBytes, lockChannelUrls, lockFiles, solveEnv } from './solve';
import { KernelClient, makeMessage, normalizeEname, OutputTracker } from './executor';
import { parseNotebook, scriptToNotebook, sourceText } from './formats';
import { IJob, INotebook, IRunResult, Output } from './types';

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

  const result = await execute(job, nb, kernel, spec, lock);
  send({ type: 'done', result });
}

async function execute(
  job: IJob,
  nb: INotebook,
  kernel: KernelClient,
  spec: any,
  lock: ILock
): Promise<IRunResult> {
  const tracker = new OutputTracker((cell, output, kind) => emit({ kind: 'output', cell, output, mode: kind }));

  // kernel_info for language_info
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

  let status: IRunResult['status'] = 'ok';
  let failedCell: number | null = null;
  let error: string | null = null;
  const cellTimeout = job.cellTimeout ? job.cellTimeout * 1000 : null;

  for (let i = 0; i < nb.cells.length; i++) {
    const cell = nb.cells[i];
    if (cell.cell_type !== 'code') {
      continue;
    }
    cell.outputs = [];
    cell.execution_count = null;
    if (status !== 'ok') {
      continue; // cells after a failure are left unexecuted
    }
    const code = sourceText(cell.source);
    emit({ kind: 'cell_start', cell: i, source: code });
    const tCell = performance.now();
    const outputs: Output[] = cell.outputs;
    const state = { clearPending: false };
    const msg = makeMessage('execute_request', {
      code,
      silent: false,
      store_history: true,
      user_expressions: {},
      allow_stdin: true,
      stop_on_error: !job.allowErrors
    });
    const res = await kernel.request(
      msg,
      m => {
        if (m.header.msg_type === 'execute_input') {
          cell.execution_count = m.content.execution_count ?? cell.execution_count;
          return;
        }
        tracker.apply(i, outputs, m, state);
      },
      cellTimeout
    );
    if (res.status === 'ok') {
      await kernel.sync(); // top-level await: let the pending promise settle
    }
    if (res.reply) {
      cell.execution_count = res.reply.content.execution_count ?? cell.execution_count;
    }
    emit({
      kind: 'cell_end',
      cell: i,
      status: res.reply?.content?.status ?? res.status,
      seconds: (performance.now() - tCell) / 1000,
      outputs: outputs.length
    });
    if (res.status === 'timeout') {
      kernel.terminate(`cell ${i} timed out`);
      outputs.push({
        output_type: 'error',
        ename: 'TimeoutError',
        evalue: `cell execution timed out after ${job.cellTimeout}s`,
        traceback: [`TimeoutError: cell execution timed out after ${job.cellTimeout}s`]
      });
      status = 'timeout';
      failedCell = i;
      error = `cell ${i} timed out after ${job.cellTimeout}s`;
    } else if (res.status === 'dead') {
      status = 'error';
      failedCell = i;
      error = `kernel died: ${res.reason}`;
      outputs.push({ output_type: 'error', ename: 'KernelDied', evalue: String(res.reason), traceback: [] });
    } else if (res.reply?.content?.status === 'error' && !job.allowErrors) {
      status = 'error';
      failedCell = i;
      error = `${normalizeEname(res.reply.content.ename)}: ${res.reply.content.evalue}`;
    }
  }

  let mounts: IRunResult['mounts'] = [];
  if (!kernel.dead && (job.mounts ?? []).some(m => m.mode === 'rw')) {
    mounts = await new Promise(resolve => {
      const onMsg = (ev: MessageEvent) => {
        if (ev.data?.xnb === 'collected') {
          kernel.worker.removeEventListener('message', onMsg);
          resolve(ev.data.mounts);
        }
      };
      kernel.worker.addEventListener('message', onMsg);
      kernel.worker.postMessage({ xnb: 'collect' });
    });
  }
  void lock;
  return { notebook: nb, status, failedCell, error, mounts };
}

main().catch(e => {
  send({ type: 'error', message: String(e?.stack ?? e?.message ?? e) });
});
