// Kernel supervisor (agent mode). It never runs user code: it holds the kernel worker's
// source and the package bytes, runs the kernel as a nested worker, and starts a new one
// when the agent restarts the kernel.
//
// After the seal the page cannot load kernel.worker.js again (that is a network request),
// and a blob worker created by the page would get the page's CSP. A blob worker created
// here inherits this worker's CSP instead (`connect-src 'none'`).

const ctx = self as any;
let sourceUrl: string | null = null;
let boot: any = null;
let inner: Worker | null = null;

function spawn(): void {
  if (!sourceUrl || !boot) {
    ctx.postMessage({ xnb: 'fatal', message: 'kernel supervisor: no kernel source or boot message' });
    return;
  }
  const w = new Worker(sourceUrl);
  inner = w;
  // Messages from a kernel that was replaced are dropped.
  w.onmessage = ev => {
    if (inner === w) {
      ctx.postMessage(ev.data);
    }
  };
  w.onerror = ev => {
    ev.preventDefault();
    if (inner === w) {
      ctx.postMessage({ xnb: 'fatal', message: `kernel worker error: ${ev.message}` });
    }
  };
  // Keep the originals for the next restart; each kernel gets (and consumes) a copy.
  const packages = boot.packages.map((p: any) => ({ ...p, data: p.data.slice(0) }));
  const untarWasm = boot.untarWasm.slice(0);
  w.postMessage({ ...boot, packages, untarWasm }, [...packages.map((p: any) => p.data), untarWasm]);
}

ctx.onmessage = (ev: MessageEvent) => {
  const msg = ev.data;
  switch (msg?.xnb) {
    case 'source':
      sourceUrl = URL.createObjectURL(new Blob([msg.source], { type: 'text/javascript' }));
      return;
    case 'boot':
      boot = msg;
      spawn();
      return;
    case 'restart':
      inner?.terminate();
      inner = null;
      if (msg.mounts) {
        boot = { ...boot, mounts: msg.mounts };
      }
      spawn();
      return;
    case 'kill':
      inner?.terminate();
      inner = null;
      return;
    default:
      inner?.postMessage(msg);
  }
};

ctx.postMessage({ xnb: 'loaded' });
