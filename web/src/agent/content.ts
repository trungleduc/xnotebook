// A cell report (nbformat outputs + status) as tool result content: the TS port of
// xnotebook/mcp.py report_to_content, with the same limits.

import type { ImageContent, TextContent } from '@earendil-works/pi-ai';

export const MAX_TEXT = 20_000;
export const MAX_IMAGES = 8;
const IMAGE_TYPES = ['image/png', 'image/jpeg'];
const ANSI = /\x1b\[[0-9;]*[A-Za-z]/g;

export type Content = TextContent | ImageContent;

function text(value: any): string {
  return Array.isArray(value) ? value.join('') : String(value ?? '');
}

export function truncate(s: string, limit: number): string {
  if (s.length <= limit) {
    return s;
  }
  const head = Math.floor((limit * 3) / 4);
  const tail = Math.floor(limit / 4);
  return `${s.slice(0, head)}\n... [${s.length - head - tail} characters truncated] ...\n${s.slice(s.length - tail)}`;
}

export function reportToContent(
  report: Record<string, any> | null,
  opts: { maxText?: number; maxImages?: number; images?: boolean } = {}
): { content: Content[]; isError: boolean } {
  const maxText = opts.maxText ?? MAX_TEXT;
  const maxImages = opts.images === false ? 0 : (opts.maxImages ?? MAX_IMAGES);
  const r = report ?? {};
  const items: Content[] = [];
  const texts: string[] = [];
  let images = 0;
  const flush = () => {
    if (texts.length) {
      items.push({ type: 'text', text: texts.join('') });
      texts.length = 0;
    }
  };
  for (const out of r.outputs ?? []) {
    const otype = out.output_type;
    if (otype === 'stream') {
      texts.push(text(out.text));
    } else if (otype === 'execute_result' || otype === 'display_data') {
      const data = out.data ?? {};
      const mime = IMAGE_TYPES.find(m => m in data);
      if (mime && images < maxImages) {
        flush();
        items.push({ type: 'image', data: text(data[mime]).replace(/\s/g, ''), mimeType: mime });
        images++;
      } else if (mime) {
        texts.push(maxImages ? `[${mime} omitted: more than ${maxImages} images]\n` : `[${mime} omitted]\n`);
      } else if ('text/plain' in data) {
        texts.push(text(data['text/plain']).replace(/\n+$/, '') + '\n');
      } else if (Object.keys(data).length) {
        texts.push(`[${Object.keys(data).sort().join(', ')}]\n`);
      }
    } else if (otype === 'error') {
      const tb = (out.traceback ?? []).map((t: any) => text(t).replace(ANSI, ''));
      texts.push((tb.length ? tb.join('\n') : `${out.ename}: ${out.evalue}`) + '\n');
    }
  }
  const status = r.status ?? 'ok';
  if (status === 'timeout' || status === 'dead') {
    texts.push(`kernel died: ${r.error || status}; call restart_kernel to get a fresh kernel\n`);
  }
  flush();
  let budget = maxText;
  for (const item of items) {
    if (item.type === 'text') {
      item.text = truncate(item.text, Math.max(budget, 200));
      budget -= item.text.length;
    }
  }
  if (!items.length) {
    items.push({ type: 'text', text: '(no output)' });
  }
  return { content: items, isError: status !== 'ok' };
}
