// Input formats: nbformat 4 notebooks and plain scripts (optionally split with `# %%`).

import { ICell, INotebook } from './types';

export function sourceText(src: string | string[] | undefined): string {
  return Array.isArray(src) ? src.join('') : (src ?? '');
}

function cellId(): string {
  return crypto.randomUUID().replace(/-/g, '').slice(0, 8);
}

/** nbformat >= 4.5 requires a unique `id` on every cell. */
export function ensureCellIds(nb: INotebook): void {
  if (nb.nbformat === 4 && nb.nbformat_minor < 5) {
    nb.nbformat_minor = 5;
  }
  const seen = new Set<string>();
  for (const cell of nb.cells) {
    let id = typeof cell.id === 'string' && /^[a-zA-Z0-9-_]{1,64}$/.test(cell.id) ? cell.id : '';
    while (!id || seen.has(id)) {
      id = cellId();
    }
    seen.add(id);
    cell.id = id;
  }
}

export function parseNotebook(text: string): INotebook {
  let nb: any;
  try {
    nb = JSON.parse(text);
  } catch (e: any) {
    throw new Error(`invalid notebook JSON: ${e.message}`);
  }
  if (!nb || typeof nb !== 'object' || !Array.isArray(nb.cells)) {
    throw new Error('not a notebook: missing "cells"');
  }
  if ((nb.nbformat ?? 4) < 4) {
    throw new Error(`unsupported nbformat ${nb.nbformat}; convert to nbformat 4 first`);
  }
  nb.metadata = nb.metadata ?? {};
  nb.nbformat = 4;
  nb.nbformat_minor = nb.nbformat_minor ?? 5;
  ensureCellIds(nb as INotebook);
  return nb as INotebook;
}

const COMMENT: Record<string, string> = {
  py: '#',
  r: '#',
  jl: '#',
  lua: '--',
  js: '//',
  ts: '//',
  cpp: '//',
  m: '%'
};

/** Split a script into cells at `<comment> %%` markers (percent format). */
export function scriptToNotebook(path: string, text: string): INotebook {
  const ext = path.split('.').pop()?.toLowerCase() ?? '';
  const comment = COMMENT[ext] ?? '#';
  const escaped = comment.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const marker = new RegExp(`^${escaped}\\s?%%(.*)$`);
  const lines = text.split(/(?<=\n)/);
  const cells: ICell[] = [];
  let current: string[] = [];
  let markdown = false;
  let seenMarker = false;
  const flush = () => {
    let src = current.join('');
    if (seenMarker) {
      src = src.replace(/^\s*\n/, '').replace(/\s+$/, '');
    }
    if (src.trim() || !seenMarker) {
      if (markdown) {
        const md = src
          .split('\n')
          .map(l => (l.startsWith(comment + ' ') ? l.slice(comment.length + 1) : l.startsWith(comment) ? l.slice(comment.length) : l))
          .join('\n');
        cells.push({ cell_type: 'markdown', source: md, metadata: {} });
      } else if (src.trim()) {
        cells.push({ cell_type: 'code', source: src, metadata: {}, outputs: [], execution_count: null });
      }
    }
    current = [];
  };
  for (const line of lines) {
    const m = line.replace(/\r?\n$/, '').match(marker);
    if (m) {
      const pending = current.join('');
      const onlyComments = pending
        .split('\n')
        .every(l => !l.trim() || l.trimStart().startsWith(comment));
      if (seenMarker || (pending.trim() && !onlyComments)) {
        flush();
      } else {
        current = []; // a comment-only preamble (e.g. a PEP 723 block) is not a cell
      }
      seenMarker = true;
      markdown = /\[\s*markdown\s*\]/.test(m[1]);
      continue;
    }
    current.push(line);
  }
  flush();
  const nb: INotebook = { nbformat: 4, nbformat_minor: 5, metadata: {}, cells };
  ensureCellIds(nb);
  return nb;
}
