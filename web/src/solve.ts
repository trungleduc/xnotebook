// Solve the environment (mambajs/rattler, in wasm) and download every package.
// All requests target real upstream URLs; the host firewall rewrites them to the
// local caching proxy, which verifies each package against the lock's sha256.

import { create } from '@emscripten-forge/mambajs';
import { computePackageUrl, formatChannels, ILock } from '@emscripten-forge/mambajs-core';
import { debug, log, step } from './host';

export interface IPackageFile {
  filename: string;
  url: string;
  kind: 'conda' | 'pip';
  sha256?: string;
  md5?: string;
  size?: number;
}

export interface IDownloaded extends IPackageFile {
  data: ArrayBuffer;
}

const logger = {
  log: (...m: any[]) => debug(m.join(' ')),
  warn: (...m: any[]) => debug('warning: ' + m.join(' ')),
  error: (...m: any[]) => log('error: ' + m.join(' '))
};

export function channelUrls(channels: string[]): string[] {
  const info = formatChannels(channels).channelInfo;
  const urls: string[] = [];
  for (const mirrors of Object.values(info)) {
    for (const m of mirrors) {
      urls.push(m.url);
    }
  }
  return urls;
}

export function lockChannelUrls(lock: ILock): string[] {
  const urls: string[] = [];
  for (const mirrors of Object.values(lock.channelInfo)) {
    for (const m of mirrors) {
      urls.push(m.url);
    }
  }
  return urls;
}

export async function solveEnv(yml: string): Promise<ILock> {
  try {
    return await create({ yml, logger });
  } catch (e: any) {
    throw new Error(`failed to solve the environment:\n${yml}\n${e?.message ?? e}`);
  }
}

export function lockFiles(lock: ILock): IPackageFile[] {
  const files: IPackageFile[] = [];
  for (const [filename, pkg] of Object.entries(lock.packages)) {
    files.push({
      filename,
      url: computePackageUrl(pkg, filename, lock.channelInfo),
      kind: 'conda',
      sha256: pkg.hash?.sha256,
      md5: pkg.hash?.md5,
      size: pkg.size
    });
  }
  for (const [filename, pkg] of Object.entries(lock.pipPackages ?? {})) {
    files.push({
      filename,
      url: pkg.url,
      kind: 'pip',
      sha256: pkg.hash?.sha256,
      md5: pkg.hash?.md5,
      size: pkg.size
    });
  }
  return files;
}

async function sha256Hex(data: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', data);
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}

async function fetchOne(file: IPackageFile, attempts = 3): Promise<IDownloaded> {
  let lastErr: any;
  for (let i = 0; i < attempts; i++) {
    try {
      const resp = await fetch(file.url);
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data = await resp.arrayBuffer();
      if (file.sha256) {
        const got = await sha256Hex(data);
        if (got !== file.sha256) {
          throw new Error(`sha256 mismatch (expected ${file.sha256}, got ${got})`);
        }
      }
      return { ...file, data };
    } catch (e) {
      lastErr = e;
    }
  }
  throw new Error(`failed to download ${file.filename} from ${file.url}: ${lastErr?.message ?? lastErr}`);
}

export async function downloadAll(files: IPackageFile[], concurrency = 8): Promise<IDownloaded[]> {
  const out: IDownloaded[] = new Array(files.length);
  let next = 0;
  let done = 0;
  const workers = Array.from({ length: Math.min(concurrency, files.length) }, async () => {
    while (next < files.length) {
      const i = next++;
      out[i] = await fetchOne(files[i]);
      done++;
      step(`fetched ${done}/${files.length} ${files[i].filename} (${formatBytes(out[i].data.byteLength)})`);
    }
  });
  await Promise.all(workers);
  return out;
}

export function formatBytes(n: number): string {
  if (n >= 1 << 20) {
    return `${(n / (1 << 20)).toFixed(1)} MB`;
  }
  return `${Math.max(1, Math.round(n / 1024))} KB`;
}
