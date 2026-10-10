// Page <-> Python host bridge. The host injects the `__xnbSend` CDP binding before
// navigation and answers requests by evaluating `__xnbReply(...)` in the page.

type Pending = { resolve: (v: any) => void; reject: (e: Error) => void };

declare global {
  interface Window {
    __xnbSend?: (payload: string) => void;
    __xnbReply?: (msg: { id: number; value: any; error: string | null }) => void;
    /** Bridge mode: the host delivers a Jupyter message for the kernel. */
    __xnbKernel?: (msg: any) => void;
  }
}

let nextId = 0;
const pending = new Map<number, Pending>();

window.__xnbReply = ({ id, value, error }) => {
  const p = pending.get(id);
  if (!p) {
    return;
  }
  pending.delete(id);
  if (error) {
    p.reject(new Error(error));
  } else {
    p.resolve(value);
  }
};

export function send(msg: Record<string, any>): void {
  const binding = window.__xnbSend;
  if (!binding) {
    console.log('[xnb]', JSON.stringify(msg).slice(0, 500));
    return;
  }
  binding(JSON.stringify(msg));
}

export function request<T = any>(type: string, payload: Record<string, any> = {}): Promise<T> {
  const id = ++nextId;
  return new Promise<T>((resolve, reject) => {
    pending.set(id, { resolve, reject });
    send({ ...payload, type, id });
  });
}

export function log(message: string): void {
  send({ type: 'log', message });
}

/** A phase of the run (shown unless --quiet). */
export function progress(message: string): void {
  send({ type: 'progress', level: 'info', message });
}

/** A detailed step (shown with --verbose). */
export function step(message: string): void {
  send({ type: 'progress', level: 'verbose', message });
}

export function debug(message: string): void {
  send({ type: 'debug', message });
}

export function emit(event: Record<string, any>): void {
  send({ type: 'event', event });
}
