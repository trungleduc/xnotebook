// Execute cells on the kernel worker and turn iopub messages into nbformat outputs.

import { Output } from './types';

export interface IKernelMessage {
  header: { msg_id: string; msg_type: string; session?: string; [k: string]: any };
  parent_header: { msg_id?: string; [k: string]: any };
  metadata: Record<string, any>;
  content: Record<string, any>;
  buffers?: any[];
  channel?: string;
}

export interface ICellResult {
  status: 'ok' | 'error' | 'timeout' | 'dead';
  executionCount: number | null;
  outputs: Output[];
  error?: string;
}

type Waiter = {
  msgId: string;
  onMessage: (msg: IKernelMessage) => void;
};

/** xeus-python reports `<class 'pkg.Error'>`; nbformat/ipykernel use the class name. */
export function normalizeEname(ename: unknown): string {
  const s = String(ename ?? '');
  const m = s.match(/^<class '(?:[\w.]*\.)?([^'.]+)'>$/);
  return m ? m[1] : s;
}

function uuid(): string {
  return crypto.randomUUID().replace(/-/g, '');
}

const SESSION = uuid();

export function makeMessage(msgType: string, content: Record<string, any>, channel = 'shell'): IKernelMessage {
  return {
    header: {
      msg_id: uuid(),
      msg_type: msgType,
      session: SESSION,
      username: 'xnb',
      date: new Date().toISOString(),
      version: '5.3'
    },
    parent_header: {},
    metadata: {},
    content,
    buffers: [],
    channel
  };
}

/** Normalise mime bundles: nbformat stores text as strings (or list of strings). */
function mimeBundle(data: Record<string, any> | undefined): Record<string, any> {
  const out: Record<string, any> = {};
  for (const [k, v] of Object.entries(data ?? {})) {
    out[k] = v;
  }
  return out;
}

/**
 * Tracks outputs of all cells so `update_display_data` can reach earlier cells.
 */
export class OutputTracker {
  private displays = new Map<string, Output[]>();

  constructor(private onOutput: (cell: number, output: Output, kind: 'add' | 'update' | 'clear') => void) {}

  apply(cell: number, outputs: Output[], msg: IKernelMessage, state: { clearPending: boolean }): void {
    const t = msg.header.msg_type;
    const c = msg.content;
    const clearIfPending = () => {
      if (state.clearPending) {
        outputs.length = 0;
        state.clearPending = false;
        this.onOutput(cell, {}, 'clear');
      }
    };
    switch (t) {
      case 'stream': {
        clearIfPending();
        const last = outputs[outputs.length - 1];
        if (last && last.output_type === 'stream' && last.name === c.name) {
          last.text += c.text;
        } else {
          outputs.push({ output_type: 'stream', name: c.name, text: c.text });
        }
        this.onOutput(cell, { output_type: 'stream', name: c.name, text: c.text }, 'add');
        break;
      }
      case 'display_data':
      case 'execute_result': {
        clearIfPending();
        const out: Output =
          t === 'execute_result'
            ? {
                output_type: 'execute_result',
                execution_count: c.execution_count ?? null,
                data: mimeBundle(c.data),
                metadata: c.metadata ?? {}
              }
            : { output_type: 'display_data', data: mimeBundle(c.data), metadata: c.metadata ?? {} };
        outputs.push(out);
        const id = c.transient?.display_id;
        if (id) {
          const list = this.displays.get(id) ?? [];
          list.push(out);
          this.displays.set(id, list);
        }
        this.onOutput(cell, out, 'add');
        break;
      }
      case 'update_display_data': {
        const id = c.transient?.display_id;
        for (const out of (id && this.displays.get(id)) || []) {
          out.data = mimeBundle(c.data);
          out.metadata = c.metadata ?? {};
          this.onOutput(cell, out, 'update');
        }
        break;
      }
      case 'error': {
        clearIfPending();
        const out = {
          output_type: 'error',
          ename: normalizeEname(c.ename),
          evalue: String(c.evalue ?? ''),
          traceback: (c.traceback ?? []).map(String)
        };
        outputs.push(out);
        this.onOutput(cell, out, 'add');
        break;
      }
      case 'clear_output': {
        if (c.wait) {
          state.clearPending = true;
        } else {
          outputs.length = 0;
          state.clearPending = false;
          this.onOutput(cell, {}, 'clear');
        }
        break;
      }
      default:
        break;
    }
  }
}

export class KernelClient {
  private waiters = new Map<string, Waiter>();
  private syncs = new Map<number, () => void>();
  private syncId = 0;
  private deadHandlers: ((reason: string) => void)[] = [];
  dead: string | null = null;
  onDebug: (m: string) => void = () => {};
  onStdinExhausted: () => void = () => {};

  constructor(public worker: Worker) {
    worker.addEventListener('message', ev => this.handle(ev.data));
    worker.addEventListener('error', ev => this.die(`kernel worker error: ${ev.message}`));
  }

  private handle(data: any): void {
    if (data && data.xnb) {
      switch (data.xnb) {
        case 'synced':
          this.syncs.get(data.id)?.();
          this.syncs.delete(data.id);
          break;
        case 'debug':
          this.onDebug(data.message);
          break;
        case 'stdin-exhausted':
          this.onStdinExhausted();
          break;
        case 'fatal':
          this.die(data.message);
          break;
      }
      return;
    }
    if (data && data.header) {
      const parent = data.parent_header?.msg_id;
      const w = parent ? this.waiters.get(parent) : undefined;
      if (w) {
        w.onMessage(data as IKernelMessage);
      }
    }
  }

  onDead(cb: (reason: string) => void): void {
    this.deadHandlers.push(cb);
  }

  die(reason: string): void {
    if (this.dead) {
      return;
    }
    this.dead = reason;
    for (const cb of this.deadHandlers) {
      cb(reason);
    }
  }

  terminate(reason: string): void {
    this.worker.terminate();
    this.die(reason);
  }

  sync(): Promise<void> {
    const id = ++this.syncId;
    return new Promise(resolve => {
      this.syncs.set(id, resolve);
      this.worker.postMessage({ xnb: 'sync', id });
    });
  }

  /** Send a request and collect messages until both the reply and status idle arrived. */
  request(
    msg: IKernelMessage,
    onMessage: (m: IKernelMessage) => void,
    timeoutMs?: number | null
  ): Promise<{ reply: IKernelMessage | null; status: 'ok' | 'timeout' | 'dead'; reason?: string }> {
    return new Promise(resolve => {
      let reply: IKernelMessage | null = null;
      let idle = false;
      let timer: any = null;
      let settled = false;
      const finish = (status: 'ok' | 'timeout' | 'dead', reason?: string) => {
        if (settled) {
          return;
        }
        settled = true;
        if (timer) {
          clearTimeout(timer);
        }
        this.waiters.delete(msg.header.msg_id);
        resolve({ reply, status, reason });
      };
      this.waiters.set(msg.header.msg_id, {
        msgId: msg.header.msg_id,
        onMessage: m => {
          if (m.header.msg_type === 'status') {
            if (m.content.execution_state === 'idle') {
              idle = true;
            }
          } else if (m.header.msg_type.endsWith('_reply')) {
            reply = m;
          } else {
            onMessage(m);
          }
          if (idle && reply) {
            finish('ok');
          }
        }
      });
      this.onDead(reason => finish('dead', reason));
      if (this.dead) {
        finish('dead', this.dead);
        return;
      }
      if (timeoutMs) {
        timer = setTimeout(() => finish('timeout'), timeoutMs);
      }
      this.worker.postMessage({ xnb: 'msg', msg });
    });
  }
}
