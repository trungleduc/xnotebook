// POSIX paths of the kernel filesystem, resolved the way pi's tools resolve them.

const UNICODE_SPACES = /[  -   　]/g;
const HOME = '/home/xnb';

export function normalize(path: string): string {
  const out: string[] = [];
  for (const part of path.split('/')) {
    if (part === '' || part === '.') {
      continue;
    }
    if (part === '..') {
      out.pop();
    } else {
      out.push(part);
    }
  }
  return '/' + out.join('/');
}

/** An absolute path for what the model wrote: `@` prefix and odd spaces dropped, `~` expanded. */
export function resolvePath(input: string, cwd: string): string {
  let p = input.replace(UNICODE_SPACES, ' ').trim();
  if (p.startsWith('@')) {
    p = p.slice(1);
  }
  if (p === '~' || p.startsWith('~/')) {
    p = HOME + p.slice(1);
  }
  return normalize(p.startsWith('/') ? p : `${cwd}/${p}`);
}

export function dirname(path: string): string {
  const i = path.lastIndexOf('/');
  return i <= 0 ? '/' : path.slice(0, i);
}

/** True when `path` is `root` or inside it. */
export function within(path: string, root: string): boolean {
  const r = root.replace(/\/+$/, '');
  return path === r || path.startsWith(r + '/');
}
