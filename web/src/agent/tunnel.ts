// `fetch` for the model SDKs that never touches the network: each request goes to the
// page, then over the CDP bridge to the host, which adds the API key and forwards it.
// The response streams back chunk by chunk into a ReadableStream.

/** Base URL given to the SDKs; only the path and query reach the host. */
export const TUNNEL_BASE = 'https://llm.xnb.invalid';

export interface ITunnelRequest {
  reqId: number;
  method: string;
  path: string;
  headers: Record<string, string>;
  body: string;
}

/** What the host sends back, in order: head, chunks, then end or error. */
export interface ITunnelReply {
  reqId: number;
  head?: { status: number; headers: Record<string, string> };
  chunk?: string;
  end?: boolean;
  error?: string;
}

interface IPending {
  resolve: (r: Response) => void;
  reject: (e: any) => void;
  controller: ReadableStreamDefaultController<Uint8Array> | null;
  stream: ReadableStream<Uint8Array>;
  headed: boolean;
}

function abortError(): Error {
  return new DOMException('The request was aborted', 'AbortError');
}

export class Tunnel {
  private nextId = 0;
  private pending = new Map<number, IPending>();
  private encoder = new TextEncoder();

  constructor(
    private send: (req: ITunnelRequest) => void,
    private cancel: (reqId: number) => void
  ) {}

  fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const req = new Request(input as any, init);
    const url = new URL(req.url);
    const signal = init?.signal ?? req.signal;
    if (signal?.aborted) {
      throw abortError();
    }
    const body = typeof init?.body === 'string' ? init.body : await req.text();
    const headers: Record<string, string> = {};
    req.headers.forEach((v, k) => {
      headers[k] = v;
    });
    const reqId = ++this.nextId;
    return new Promise<Response>((resolve, reject) => {
      let controller: ReadableStreamDefaultController<Uint8Array> | null = null;
      const stream = new ReadableStream<Uint8Array>({
        start: c => {
          controller = c;
        },
        cancel: () => this.drop(reqId, true)
      });
      const entry: IPending = { resolve, reject, controller: null, stream, headed: false };
      entry.controller = controller;
      this.pending.set(reqId, entry);
      signal?.addEventListener('abort', () => this.fail(reqId, abortError(), true), { once: true });
      this.send({ reqId, method: req.method, path: url.pathname + url.search, headers, body });
    });
  };

  /** A reply message from the host. */
  receive(msg: ITunnelReply): void {
    const p = this.pending.get(msg.reqId);
    if (!p) {
      return;
    }
    if (msg.head) {
      p.headed = true;
      p.resolve(new Response(p.stream, { status: msg.head.status, headers: msg.head.headers }));
    }
    if (msg.chunk !== undefined && msg.chunk.length) {
      p.controller?.enqueue(this.encoder.encode(msg.chunk));
    }
    if (msg.error !== undefined) {
      this.fail(msg.reqId, new TypeError(msg.error), false);
    } else if (msg.end) {
      this.pending.delete(msg.reqId);
      p.controller?.close();
    }
  }

  /** Abort every request in flight (the agent was stopped). */
  abortAll(): void {
    for (const id of [...this.pending.keys()]) {
      this.fail(id, abortError(), true);
    }
  }

  private fail(reqId: number, error: Error, tellHost: boolean): void {
    const p = this.pending.get(reqId);
    if (!p) {
      return;
    }
    this.drop(reqId, tellHost);
    if (p.headed) {
      try {
        p.controller?.error(error);
      } catch {
        // the stream was already closed
      }
    } else {
      p.reject(error);
    }
  }

  private drop(reqId: number, tellHost: boolean): void {
    if (this.pending.delete(reqId) && tellHost) {
      this.cancel(reqId);
    }
  }
}
