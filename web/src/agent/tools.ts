// The agent's tools. `execute` and `restart_kernel` drive the kernel; `read`, `edit` and
// `write` work on the kernel filesystem with the semantics of pi's own tools. The edit
// engine (exact match, fuzzy fallback, overlap check, diff) is pi's, imported as is.
// pi's tool modules themselves call Node's fs/path directly, so they are not used.

import type { AgentTool, AgentToolResult } from '@earendil-works/pi-agent-core';
import { Type } from 'typebox';
import {
  applyEditsToNormalizedContent,
  detectLineEnding,
  generateDiffString,
  generateUnifiedPatch,
  normalizeToLF,
  restoreLineEndings
} from 'pi-edit-diff';
import { splitBom } from 'pi-text';
import { reportToContent } from './content';
import { resolvePath } from './paths';

/** Calls into the page, which runs them on the kernel. */
export type Rpc = (op: string, args: Record<string, any>) => Promise<any>;

/** A file operation result from the kernel worker. */
interface IFsResult {
  value?: any;
  error?: string;
  code?: string;
}

const MAX_LINES = 2000;
const MAX_BYTES = 50 * 1024;
const MAX_READ = 10 << 20;
const IMAGE_TYPES: Record<string, string> = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  gif: 'image/gif',
  webp: 'image/webp'
};

/** Shown to the model in the system prompt, as pi does for its tools. */
export const TOOL_GUIDELINES = [
  'Use execute to run code in the kernel; variables persist between calls, like notebook cells.',
  'Use read to examine files instead of printing them from code.',
  'Use edit for precise changes (edits[].oldText must match exactly).',
  'When changing multiple separate locations in one file, use one edit call with multiple entries in edits[] instead of multiple edit calls.',
  'Each edits[].oldText is matched against the original file, not after earlier edits are applied. Do not emit overlapping or nested edits. Merge nearby changes into one edit.',
  'Keep edits[].oldText as small as possible while still being unique in the file. Do not pad with large unchanged regions.',
  'Use write only for new files or complete rewrites.',
  'If the kernel dies (a cell timed out or crashed), call restart_kernel; variables and files outside mounts are lost.'
];

function utf8(b64: string): string {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) {
    bytes[i] = bin.charCodeAt(i);
  }
  return new TextDecoder().decode(bytes);
}

function byteLength(s: string): number {
  return new TextEncoder().encode(s).length;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes}B`;
  }
  return bytes < 1 << 20 ? `${(bytes / 1024).toFixed(1)}KB` : `${(bytes / (1 << 20)).toFixed(1)}MB`;
}

function text(t: string): AgentToolResult<any>['content'] {
  return [{ type: 'text', text: t }];
}

export function makeTools(rpc: Rpc, cwd: string, images: boolean): AgentTool<any>[] {
  const fs = async (args: Record<string, any>): Promise<any> => {
    const r: IFsResult = await rpc('fs', args);
    if (r.error) {
      const e: any = new Error(r.error);
      e.code = r.code;
      throw e;
    }
    return r.value;
  };
  const readText = async (path: string): Promise<string> => utf8(await fs({ op: 'read', path, maxBytes: MAX_READ }));

  const execute: AgentTool<any> = {
    name: 'execute',
    label: 'execute',
    description:
      'Run code in the sandboxed kernel, like a notebook cell, and return its outputs (text, plots as images, errors). ' +
      'Variables persist between calls. The kernel has no network and no subprocesses.',
    parameters: Type.Object({ code: Type.String({ description: 'Code to execute.' }) }),
    async execute(_id, { code }: any) {
      const report = await rpc('execute', { code });
      const { content, isError } = reportToContent(report, { images });
      const saved: string[] = report?.saved ?? [];
      if (saved.length) {
        content.push({ type: 'text', text: `saved to the host: ${saved.join(', ')}` });
      }
      return {
        content,
        isError,
        details: { status: report?.status, executionCount: report?.executionCount, saved, error: report?.error }
      };
    }
  };

  const read: AgentTool<any> = {
    name: 'read',
    label: 'read',
    description:
      `Read the contents of a file, or list a directory. Supports text files and images (png, jpg, gif, webp)` +
      `${images ? ', which are sent as attachments' : ''}. Text is truncated to ${MAX_LINES} lines or ` +
      `${MAX_BYTES / 1024}KB (whichever is hit first). Use offset/limit for large files.`,
    parameters: Type.Object({
      path: Type.String({ description: 'Path to the file to read (relative or absolute)' }),
      offset: Type.Optional(Type.Number({ description: 'Line number to start reading from (1-indexed)' })),
      limit: Type.Optional(Type.Number({ description: 'Maximum number of lines to read' }))
    }),
    async execute(_id, { path, offset, limit }: any) {
      const abs = resolvePath(path, cwd);
      const st = await fs({ op: 'stat', path: abs });
      if (st.dir) {
        const entries: { name: string; dir: boolean; size: number }[] = await fs({ op: 'list', path: abs });
        const lines = entries
          .sort((a, b) => a.name.localeCompare(b.name))
          .map(e => (e.dir ? `${e.name}/` : `${e.name} (${formatSize(e.size)})`));
        return { content: text(`${abs}:\n${lines.join('\n') || '(empty)'}`), details: undefined };
      }
      const mime = IMAGE_TYPES[abs.split('.').pop()?.toLowerCase() ?? ''];
      if (mime) {
        if (!images) {
          return { content: text(`${abs} is an image (${mime}); this model does not take images.`), details: undefined };
        }
        const data = await fs({ op: 'read', path: abs, maxBytes: MAX_READ });
        return {
          content: [
            { type: 'text', text: `Read image file [${mime}]` },
            { type: 'image', data, mimeType: mime }
          ],
          details: undefined
        };
      }
      const all = (await readText(abs)).split('\n');
      const start = offset ? Math.max(0, offset - 1) : 0;
      if (start >= all.length) {
        throw new Error(`Offset ${offset} is beyond end of file (${all.length} lines total)`);
      }
      const end = Math.min(all.length, limit !== undefined ? start + limit : start + MAX_LINES);
      const out: string[] = [];
      let bytes = 0;
      for (let i = start; i < end; i++) {
        const n = byteLength(all[i]) + (out.length ? 1 : 0);
        if (bytes + n > MAX_BYTES) {
          break;
        }
        out.push(all[i]);
        bytes += n;
      }
      if (!out.length && start < all.length) {
        return {
          content: text(`[Line ${start + 1} is ${formatSize(byteLength(all[start]))}, more than the ${formatSize(MAX_BYTES)} limit. Read it with execute.]`),
          details: undefined
        };
      }
      let body = out.join('\n');
      const last = start + out.length;
      if (last < all.length) {
        body += `\n\n[Showing lines ${start + 1}-${last} of ${all.length}. Use offset=${last + 1} to continue.]`;
      }
      return { content: text(body), details: undefined };
    }
  };

  const edit: AgentTool<any> = {
    name: 'edit',
    label: 'edit',
    description:
      'Edit a single file using exact text replacement. Every edits[].oldText must match a unique, non-overlapping region of the original file. ' +
      'If two changes affect the same block or nearby lines, merge them into one edit instead of emitting overlapping edits. ' +
      'Do not include large unchanged regions just to connect distant changes.',
    parameters: Type.Object({
      path: Type.String({ description: 'Path to the file to edit (relative or absolute)' }),
      edits: Type.Array(
        Type.Object({
          oldText: Type.String({
            description:
              'Exact text for one targeted replacement. It must be unique in the original file and must not overlap with any other edits[].oldText in the same call.'
          }),
          newText: Type.String({ description: 'Replacement text for this targeted edit.' })
        }),
        {
          description:
            'One or more targeted replacements. Each edit is matched against the original file, not incrementally. Do not include overlapping or nested edits. If two changes touch the same block or nearby lines, merge them into one edit instead.'
        }
      )
    }),
    prepareArguments: prepareEditArguments,
    async execute(_id, { path, edits }: any) {
      if (!Array.isArray(edits) || !edits.length) {
        throw new Error('Edit tool input is invalid. edits must contain at least one replacement.');
      }
      const abs = resolvePath(path, cwd);
      let raw: string;
      try {
        raw = await readText(abs);
      } catch (e: any) {
        throw new Error(`Could not edit file: ${path}. ${e.code ? `Error code: ${e.code}` : String(e.message ?? e)}.`);
      }
      const { bom, text: content } = splitBom(raw);
      const ending = detectLineEnding(content);
      const { baseContent, newContent } = applyEditsToNormalizedContent(normalizeToLF(content), edits, path);
      const saved: string[] = (await rpc('write', { path: abs, text: bom + restoreLineEndings(newContent, ending) })) ?? [];
      const diff = generateDiffString(baseContent, newContent);
      const result = `Successfully replaced ${edits.length} block(s) in ${path}.`;
      return {
        content: text(saved.length ? `${result} Saved to the host.` : result),
        details: { diff: diff.diff, patch: generateUnifiedPatch(path, baseContent, newContent), firstChangedLine: diff.firstChangedLine, saved }
      };
    }
  };

  const write: AgentTool<any> = {
    name: 'write',
    label: 'write',
    description: "Write content to a file. Creates the file if it doesn't exist, overwrites if it does. Automatically creates parent directories.",
    parameters: Type.Object({
      path: Type.String({ description: 'Path to the file to write (relative or absolute)' }),
      content: Type.String({ description: 'Content to write to the file' })
    }),
    async execute(_id, { path, content }: any) {
      // The kernel worker creates the parent directories.
      const saved: string[] = (await rpc('write', { path: resolvePath(path, cwd), text: content })) ?? [];
      return {
        content: text(`Successfully wrote to ${path}${saved.length ? '. Saved to the host.' : ''}`),
        details: { bytes: byteLength(content), saved }
      };
    }
  };

  const restart: AgentTool<any> = {
    name: 'restart_kernel',
    label: 'restart_kernel',
    description:
      'Start a fresh kernel with the same packages. Variables are lost, and so are files outside the mounts; ' +
      'mounts are copied in again from the host. Use it after the kernel died, or to start from a clean state.',
    parameters: Type.Object({}),
    async execute() {
      const r = await rpc('restart', {});
      return { content: text(r.message), isError: !r.ok, details: r };
    }
  };

  return [execute, read, edit, write, restart];
}

/** Models sometimes send edits as a JSON string, a single object, or top-level oldText/newText (as in pi). */
function prepareEditArguments(input: unknown): any {
  if (!input || typeof input !== 'object') {
    return input;
  }
  const args: any = { ...(input as any) };
  const isEdit = (v: any) => v && typeof v === 'object' && !Array.isArray(v) && typeof v.oldText === 'string' && typeof v.newText === 'string';
  if (typeof args.edits === 'string') {
    try {
      const parsed = JSON.parse(args.edits);
      args.edits = Array.isArray(parsed) ? parsed : isEdit(parsed) ? [parsed] : args.edits;
    } catch {
      // left for the schema check to report
    }
  } else if (isEdit(args.edits)) {
    args.edits = [args.edits];
  }
  if (typeof args.oldText === 'string' && typeof args.newText === 'string') {
    args.edits = [...(Array.isArray(args.edits) ? args.edits : []), { oldText: args.oldText, newText: args.newText }];
    delete args.oldText;
    delete args.newText;
  }
  return args;
}
