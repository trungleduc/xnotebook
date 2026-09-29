// Record ipywidgets state from the kernel's comm messages and save it in the
// notebook metadata (application/vnd.jupyter.widget-state+json), like nbclient.
//
// Each widget sends comm_open with its full state, then comm_msg {method: "update"}
// with partial state; binary values travel in msg.buffers at `buffer_paths`.
// Closed widgets (comm_close) are dropped. The result is a static snapshot.

import { IKernelMessage } from './executor';
import { INotebook } from './types';

export const WIDGET_STATE_MIME = 'application/vnd.jupyter.widget-state+json';

interface IBuffer {
  path: (string | number)[];
  encoding: 'base64';
  data: string;
}

function toBytes(b: any): Uint8Array | null {
  if (b instanceof ArrayBuffer) {
    return new Uint8Array(b);
  }
  if (ArrayBuffer.isView(b)) {
    return new Uint8Array(b.buffer, b.byteOffset, b.byteLength);
  }
  return null;
}

function base64(bytes: Uint8Array): string {
  let s = '';
  const CH = 0x8000;
  for (let i = 0; i < bytes.length; i += CH) {
    s += String.fromCharCode(...bytes.subarray(i, i + CH));
  }
  return btoa(s);
}

export class WidgetStateTracker {
  private state = new Map<string, Record<string, any>>();
  private buffers = new Map<string, Map<string, IBuffer>>();

  /** Feed every kernel message; only comm_* messages are used. */
  handle(msg: IKernelMessage): void {
    const t = msg.header?.msg_type;
    if (t !== 'comm_open' && t !== 'comm_msg' && t !== 'comm_close') {
      return;
    }
    const content = msg.content ?? {};
    const commId: string | undefined = content.comm_id;
    if (!commId) {
      return;
    }
    if (t === 'comm_close') {
      this.state.delete(commId);
      this.buffers.delete(commId);
      return;
    }
    if (t === 'comm_open' && content.target_name && content.target_name !== 'jupyter.widget') {
      return;
    }
    const data = content.data ?? {};
    if (t === 'comm_msg' && data.method !== 'update' && data.method !== 'echo_update') {
      return;
    }
    if (t === 'comm_msg' && !this.state.has(commId)) {
      return; // not a widget comm we saw opening
    }
    if (!data.state || typeof data.state !== 'object') {
      return;
    }
    const current = this.state.get(commId) ?? {};
    Object.assign(current, data.state);
    this.state.set(commId, current);
    const paths: (string | number)[][] = data.buffer_paths ?? [];
    const raw: any[] = msg.buffers ?? [];
    if (paths.length) {
      const bufs = this.buffers.get(commId) ?? new Map<string, IBuffer>();
      paths.forEach((path, i) => {
        let encoded: string | null = null;
        const bytes = toBytes(raw[i]);
        if (bytes) {
          encoded = base64(bytes);
        } else if (typeof raw[i] === 'string') {
          encoded = raw[i]; // already base64 (serialized message)
        }
        if (encoded !== null) {
          bufs.set(JSON.stringify(path), { path, encoding: 'base64', data: encoded });
        }
      });
      this.buffers.set(commId, bufs);
    }
  }

  get size(): number {
    return this.state.size;
  }

  /** The widget-state document, or null when no live widget exists. */
  toMetadata(): Record<string, any> | null {
    const models: Record<string, any> = {};
    for (const [id, s] of this.state) {
      if (!('_model_name' in s)) {
        continue;
      }
      const entry: Record<string, any> = {
        model_name: s._model_name,
        model_module: s._model_module,
        model_module_version: s._model_module_version,
        state: s
      };
      const bufs = this.buffers.get(id);
      if (bufs && bufs.size) {
        entry.buffers = [...bufs.values()];
      }
      models[id] = entry;
    }
    if (!Object.keys(models).length) {
      return null;
    }
    return { version_major: 2, version_minor: 0, state: models };
  }

  /** Replace (or remove, when there are no widgets) the notebook's saved widget state. */
  saveTo(nb: INotebook): void {
    const doc = this.toMetadata();
    const widgets = { ...(nb.metadata.widgets ?? {}) };
    if (doc) {
      widgets[WIDGET_STATE_MIME] = doc;
    } else {
      delete widgets[WIDGET_STATE_MIME]; // state from a previous run would not match the new outputs
    }
    if (Object.keys(widgets).length) {
      nb.metadata.widgets = widgets;
    } else {
      delete nb.metadata.widgets;
    }
  }
}
