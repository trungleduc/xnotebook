// Kernel worker (classic worker, so that `importScripts` works for emscripten output).
//
// It is created before the seal but only receives the packages afterwards; from
// then on it has no network: the page's firewall denies every request, its CSP is
// `connect-src 'none'`, and `fetch` is replaced by a lookup into preloaded bytes.

import {
  bootstrapPython,
  getPythonVersion,
  loadSharedLibs,
  saveFilesIntoEmscriptenFS,
  getSharedLibs,
  untarCondaPackage,
  waitRunDependencies
} from '@emscripten-forge/mambajs-core';
import { initUntarJS } from '@emscripten-forge/untarjs';

declare function importScripts(...urls: string[]): void;
declare function createXeusModule(options: any): Promise<any>;

const ctx = self as any;
const MEM = 'xnb-mem:///';
const mem = new Map<string, Uint8Array>();

function memFetch(input: any): Promise<Response> {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input?.url;
  const data = mem.get(url);
  if (!data) {
    return Promise.reject(new TypeError(`network access is disabled in xnb (${url})`));
  }
  const type = /\.(wasm|so)(\.|$)/.test(url) ? 'application/wasm' : 'application/octet-stream';
  return Promise.resolve(new Response(data as any, { headers: { 'Content-Type': type } }));
}
ctx.fetch = memFetch;

function post(msg: any, transfer: Transferable[] = []): void {
  ctx.postMessage(msg, transfer);
}

function step(message: string): void {
  post({ xnb: 'step', message });
}

function logDebug(message: string): void {
  post({ xnb: 'debug', message });
}

const logger = {
  log: (...m: any[]) => logDebug(m.join(' ')),
  warn: (...m: any[]) => logDebug('warning: ' + m.join(' ')),
  error: (...m: any[]) => logDebug('error: ' + m.join(' '))
};

interface IBootPackage {
  filename: string;
  kind: 'conda' | 'pip';
  data: ArrayBuffer;
}

interface IBootMessage {
  xnb: 'boot';
  packages: IBootPackage[];
  lockPackages: Record<string, any>;
  untarWasm: ArrayBuffer;
  kernelName: string;
  kernelPackage: string;
  stdin: string[] | null;
  mounts: { dst: string; mode: string; files: { path: string; data: string }[]; dirs: string[] }[];
  cwd: string | null;
}

let xserver: any = null;
let stdinLines: string[] | null = null;
let currentParent: any = null;

ctx.toplevel_promise = null;
ctx.toplevel_promise_py_proxy = null;

async function flushToplevel(): Promise<void> {
  if (ctx.toplevel_promise !== null && ctx.toplevel_promise_py_proxy !== null) {
    try {
      await ctx.toplevel_promise;
    } finally {
      ctx.toplevel_promise_py_proxy.delete();
      ctx.toplevel_promise_py_proxy = null;
      ctx.toplevel_promise = null;
    }
  }
}

function uuid(): string {
  return crypto.randomUUID().replace(/-/g, '');
}

// input() support: answer synchronously from the lines given with --stdin.
ctx.get_stdin = (request: any) => {
  const parent = request?.header ? request : currentParent;
  let value = '';
  let status = 'ok';
  if (stdinLines && stdinLines.length) {
    value = stdinLines.shift() as string;
  } else {
    status = 'error';
    post({ xnb: 'stdin-exhausted' });
  }
  return {
    header: {
      msg_id: uuid(),
      msg_type: 'input_reply',
      session: parent?.header?.session ?? '',
      username: 'xnb',
      date: new Date().toISOString(),
      version: '5.3'
    },
    parent_header: parent?.header ?? {},
    metadata: {},
    content: { value, status },
    buffers: [],
    channel: 'stdin'
  };
};

function b64decode(s: string): Uint8Array {
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) {
    out[i] = bin.charCodeAt(i);
  }
  return out;
}

function b64encode(data: Uint8Array): string {
  let s = '';
  const CH = 0x8000;
  for (let i = 0; i < data.length; i += CH) {
    s += String.fromCharCode(...data.subarray(i, i + CH));
  }
  return btoa(s);
}

function stripSlash(p: string): string {
  return p.replace(/^\/+/, '');
}

async function boot(msg: IBootMessage): Promise<any> {
  stdinLines = msg.stdin ? [...msg.stdin] : null;
  mem.set(MEM + 'unpack.wasm', new Uint8Array(msg.untarWasm));
  const untarjs = await initUntarJS(() => MEM + 'unpack.wasm');
  const pythonVersion = getPythonVersion({ packages: msg.lockPackages } as any);

  // 1. Extract everything (pure data processing, nothing from the packages runs).
  const extracted: { filename: string; name: string; files: Record<string, Uint8Array> }[] = [];
  const t0 = performance.now();
  const secs = () => `${((performance.now() - t0) / 1000).toFixed(1)}s`;
  step(`extracting ${msg.packages.length} packages`);
  for (const pkg of msg.packages) {
    const data = new Uint8Array(pkg.data);
    let files: Record<string, Uint8Array>;
    if (pkg.kind === 'conda') {
      files = await untarCondaPackage({
        url: pkg.filename,
        data,
        untarjs,
        verbose: false,
        generateCondaMeta: false,
        pythonVersion
      });
    } else {
      if (!pythonVersion) {
        throw new Error('cannot install wheels without Python in the environment');
      }
      const raw = await untarjs.extractData(data, false);
      files = {};
      for (const [k, v] of Object.entries(raw)) {
        files[`lib/python${pythonVersion[0]}.${pythonVersion[1]}/site-packages/${k}`] = v;
      }
    }
    extracted.push({ filename: pkg.filename, name: msg.lockPackages[pkg.filename]?.name ?? pkg.filename, files });
    step(`  extracted ${pkg.filename} (${Object.keys(files).length} files)`);
  }
  step(`extracted in ${secs()}`);
  msg.packages.length = 0;

  // 2. Locate the kernel spec and binary.
  const all = new Map<string, Uint8Array>();
  for (const e of extracted) {
    for (const [k, v] of Object.entries(e.files)) {
      all.set(stripSlash(k), v);
    }
  }
  let specPath: string | null = null;
  if (msg.kernelName) {
    specPath = `share/jupyter/kernels/${msg.kernelName}/kernel.json`;
    if (!all.has(specPath)) {
      specPath = null;
    }
  }
  if (!specPath) {
    const kpkg = extracted.find(e => e.name === msg.kernelPackage);
    const candidates = [...(kpkg ? Object.keys(kpkg.files) : all.keys())].filter(k =>
      /^\/?share\/jupyter\/kernels\/[^/]+\/kernel\.json$/.test(k)
    );
    specPath = candidates.length ? stripSlash(candidates.sort()[0]) : null;
  }
  if (!specPath) {
    throw new Error(`no kernel.json found for kernel ${msg.kernelName || msg.kernelPackage}`);
  }
  const kernelSpec = JSON.parse(new TextDecoder().decode(all.get(specPath)));
  const kernelDirName = specPath.split('/')[3];
  kernelSpec.name = kernelSpec.name ?? kernelDirName;
  const binRel = stripSlash(String(kernelSpec.argv[0]));
  const js = all.get(`${binRel}.js`);
  const wasm = all.get(`${binRel}.wasm`);
  if (!js || !wasm) {
    throw new Error(`kernel binary ${binRel}.js/.wasm not found`);
  }
  mem.set(MEM + `${binRel}.wasm`, wasm);
  const dataFile = all.get(`${binRel}.data`);
  if (dataFile) {
    mem.set(MEM + `${binRel}.data`, dataFile);
  }
  const shared: Record<string, string> = kernelSpec.metadata?.shared ?? {};
  for (const [file, rel] of Object.entries(shared)) {
    const bytes = all.get(stripSlash(rel)) ?? all.get(`lib/${file}`);
    if (bytes) {
      mem.set(MEM + file, bytes);
    }
  }
  if (!('libxeus.so' in shared) && all.has('lib/libxeus.so')) {
    mem.set(MEM + 'libxeus.so', all.get('lib/libxeus.so')!);
  }
  all.clear();

  step(`kernel spec ${specPath}: ${binRel}.js / .wasm (${(wasm.byteLength / (1 << 20)).toFixed(1)} MB)`);

  // 3. Instantiate the kernel module.
  const scriptUrl = URL.createObjectURL(new Blob([js as any], { type: 'text/javascript' }));
  importScripts(scriptUrl);
  URL.revokeObjectURL(scriptUrl);
  const Module = await createXeusModule({
    locateFile: (file: string) => {
      const base = file.split('/').pop() as string;
      if (mem.has(MEM + base)) {
        return MEM + base;
      }
      if (file.endsWith('.wasm')) {
        return MEM + `${binRel}.wasm`;
      }
      if (file.endsWith('.data')) {
        return MEM + `${binRel}.data`;
      }
      return file;
    },
    print: (t: string) => logDebug(`[kernel stdout] ${t}`),
    printErr: (t: string) => logDebug(`[kernel stderr] ${t}`)
  });
  ctx.Module = Module;
  await waitRunDependencies(Module);
  step(`kernel module instantiated (${secs()})`);

  // 4. Install package files into MEMFS.
  const sharedLibs: Record<string, string[]> = {};
  let nFiles = 0;
  for (const e of extracted) {
    sharedLibs[e.name] = getSharedLibs(e.files, '');
    saveFilesIntoEmscriptenFS(Module.FS, e.files, '');
    nFiles += Object.keys(e.files).length;
  }
  extracted.length = 0;
  step(`installed ${nFiles} files into the in-memory filesystem (${secs()})`);
  for (const m of msg.mounts) {
    for (const d of m.dirs) {
      Module.FS.mkdirTree(d);
    }
    for (const f of m.files) {
      const dir = f.path.substring(0, f.path.lastIndexOf('/')) || '/';
      Module.FS.mkdirTree(dir);
      Module.FS.writeFile(f.path, b64decode(f.data));
    }
    step(`mounted ${m.files.length} files at ${m.dst} (${m.mode})`);
  }
  if (msg.cwd) {
    Module.FS.mkdirTree(msg.cwd);
    Module.FS.chdir(msg.cwd);
  }

  // 5. Start the interpreter and the kernel.
  if (kernelSpec.name === 'xpython' || msg.kernelPackage === 'xeus-python') {
    if (!pythonVersion) {
      throw new Error('Python is not installed, cannot start xeus-python');
    }
    await bootstrapPython({ prefix: '/', pythonVersion, Module });
    step(`Python ${pythonVersion.join('.')} initialized (${secs()})`);
  }
  const abi = Object.values(msg.lockPackages).find((p: any) => p.name === 'emscripten-abi') as any;
  const emMajor = abi ? parseInt(String(abi.version).split('.')[0], 10) : 0;
  if (emMajor < 4) {
    for (const k of Object.keys(sharedLibs)) {
      sharedLibs[k] = sharedLibs[k].filter(l => !Object.values(shared).includes(stripSlash(l)));
    }
    await loadSharedLibs({ sharedLibs, prefix: '/', Module, logger });
  }
  const xkernel = new Module.xkernel(kernelSpec.argv);
  xserver = xkernel.get_server();
  if (!xserver) {
    throw new Error('failed to start the kernel');
  }
  xkernel.start();
  step(`kernel started (${secs()})`);
  return {
    display_name: kernelSpec.display_name,
    language: kernelSpec.language,
    name: kernelSpec.name
  };
}

function collectMounts(mounts: IBootMessage['mounts']): { dst: string; files: { path: string; data: string }[] }[] {
  const FS = ctx.Module?.FS;
  const out: { dst: string; files: { path: string; data: string }[] }[] = [];
  if (!FS) {
    return out;
  }
  for (const m of mounts) {
    if (m.mode !== 'rw') {
      continue;
    }
    const files: { path: string; data: string }[] = [];
    const walk = (p: string) => {
      let st: any;
      try {
        st = FS.lstat(p);
      } catch {
        return;
      }
      if (FS.isDir(st.mode)) {
        for (const name of FS.readdir(p)) {
          if (name !== '.' && name !== '..') {
            walk(`${p.replace(/\/$/, '')}/${name}`);
          }
        }
      } else if (FS.isFile(st.mode)) {
        files.push({ path: p, data: b64encode(FS.readFile(p)) });
      }
    };
    walk(m.dst);
    out.push({ dst: m.dst, files });
  }
  return out;
}

let bootMsg: IBootMessage | null = null;
let queue: Promise<void> = Promise.resolve();

ctx.onmessage = (ev: MessageEvent) => {
  const msg = ev.data;
  queue = queue.then(async () => {
    try {
      if (msg.xnb === 'boot') {
        bootMsg = { ...msg, packages: [] };
        const spec = await boot(msg);
        post({ xnb: 'ready', spec });
      } else if (msg.xnb === 'msg') {
        await flushToplevel();
        currentParent = msg.msg;
        xserver.notify_listener(msg.msg);
      } else if (msg.xnb === 'sync') {
        await flushToplevel();
        post({ xnb: 'synced', id: msg.id });
      } else if (msg.xnb === 'collect') {
        await flushToplevel();
        post({ xnb: 'collected', mounts: collectMounts(bootMsg?.mounts ?? []) });
      }
    } catch (e: any) {
      post({ xnb: 'fatal', message: String(e?.stack ?? e?.message ?? e) });
    }
  });
};
