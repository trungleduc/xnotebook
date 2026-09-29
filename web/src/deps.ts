// Merge dependency sources into a single environment.yml.
//
// Sources, applied in order (a later source wins for the same package name):
//   1. environment.yaml given with -e
//   2. notebook `metadata.xnb` (same schema) or a PEP 723 `# /// script` block
//   3. CLI flags (-d / --pip / -c)
// Channels are a union that keeps order; default channels are appended when missing.

import YAML from 'yaml';
import { IJob, INotebook } from './types';

export const DEFAULT_CHANNELS = ['https://prefix.dev/emscripten-forge-4x', 'conda-forge'];

/** kernelspec name -> conda package */
export const KERNELS: Record<string, string> = {
  xpython: 'xeus-python',
  xr: 'xeus-r',
  xlua: 'xeus-lua',
  xjavascript: 'xeus-javascript',
  xcpp20: 'xeus-cpp',
  xcpp17: 'xeus-cpp',
  xcpp23: 'xeus-cpp',
  xnelson: 'xeus-nelson',
  xoctave: 'xeus-octave'
};

/** file extension -> kernelspec name */
export const EXTENSIONS: Record<string, string> = {
  py: 'xpython',
  r: 'xr',
  lua: 'xlua',
  js: 'xjavascript',
  cpp: 'xcpp20',
  m: 'xoctave'
};

export interface IEnvSource {
  channels?: string[];
  dependencies?: (string | { pip?: string[] })[];
}

export interface IMergedEnv {
  channels: string[];
  specs: string[];
  pipSpecs: string[];
  kernelName: string;
  kernelPackage: string;
  yml: string;
}

export function condaName(spec: string): string {
  // "conda-forge::numpy >=1.2" -> "numpy"
  let s = spec.trim();
  const colons = s.lastIndexOf('::');
  if (colons >= 0) {
    s = s.slice(colons + 2);
  }
  const m = s.match(/^[A-Za-z0-9_.\-]+/);
  return (m ? m[0] : s).toLowerCase();
}

export function pipName(spec: string): string {
  const m = spec.trim().match(/^[A-Za-z0-9_.\-]+/);
  return (m ? m[0] : spec).toLowerCase().replace(/[-_.]+/g, '-');
}

function asList(value: unknown, what: string): any[] {
  if (value === undefined || value === null) {
    return [];
  }
  if (!Array.isArray(value)) {
    throw new Error(`${what} must be a list`);
  }
  return value;
}

export function splitSource(src: IEnvSource | null | undefined, what: string): {
  channels: string[];
  specs: string[];
  pip: string[];
} {
  const channels: string[] = [];
  const specs: string[] = [];
  const pip: string[] = [];
  if (!src) {
    return { channels, specs, pip };
  }
  for (const c of asList(src.channels, `${what}: channels`)) {
    channels.push(String(c));
  }
  for (const d of asList(src.dependencies, `${what}: dependencies`)) {
    if (typeof d === 'string') {
      specs.push(d);
    } else if (d && typeof d === 'object' && 'pip' in d) {
      for (const p of asList(d.pip, `${what}: pip`)) {
        pip.push(String(p));
      }
    } else {
      throw new Error(`${what}: unsupported dependency entry ${JSON.stringify(d)}`);
    }
  }
  return { channels, specs, pip };
}

// -- PEP 723 ---------------------------------------------------------------

const PEP723 = /^# \/\/\/ ([a-zA-Z0-9-]+)[ \t]*$\r?\n((?:^#(?:| .*)$\r?\n)+)^# \/\/\/[ \t]*$/gm;

/** Extract the `script` metadata block of a PEP 723 file as TOML text. */
export function pep723Block(source: string): string | null {
  let found: string | null = null;
  for (const m of source.matchAll(PEP723)) {
    if (m[1] !== 'script') {
      continue;
    }
    if (found !== null) {
      throw new Error('multiple PEP 723 `script` blocks');
    }
    found = m[2]
      .split(/\r?\n/)
      .map(line => (line.startsWith('# ') ? line.slice(2) : line.slice(1)))
      .join('\n');
  }
  return found;
}

/**
 * A tiny TOML subset parser: tables, string / string-array / bool / number values.
 * Enough for PEP 723 (`dependencies`, `requires-python`, `[tool.xnb]`).
 */
export function parseTomlSubset(text: string): Record<string, any> {
  const root: Record<string, any> = {};
  let table = root;
  const lines = text.split('\n');
  const stripComment = (s: string) => {
    let inStr: string | null = null;
    for (let i = 0; i < s.length; i++) {
      const ch = s[i];
      if (inStr) {
        if (ch === '\\' && inStr === '"') {
          i++;
        } else if (ch === inStr) {
          inStr = null;
        }
      } else if (ch === '"' || ch === "'") {
        inStr = ch;
      } else if (ch === '#') {
        return s.slice(0, i);
      }
    }
    return s;
  };
  const parseValue = (raw: string): any => {
    const v = raw.trim();
    if (v.startsWith('[')) {
      const items: any[] = [];
      const re = /"((?:[^"\\]|\\.)*)"|'([^']*)'/g;
      for (const m of v.matchAll(re)) {
        items.push(m[1] !== undefined ? JSON.parse(`"${m[1]}"`) : m[2]);
      }
      return items;
    }
    if (v.startsWith('"')) {
      return JSON.parse(v);
    }
    if (v.startsWith("'")) {
      return v.slice(1, -1);
    }
    if (v === 'true' || v === 'false') {
      return v === 'true';
    }
    const n = Number(v);
    return Number.isNaN(n) ? v : n;
  };
  for (let i = 0; i < lines.length; i++) {
    let line = stripComment(lines[i]).trim();
    if (!line) {
      continue;
    }
    const header = line.match(/^\[([^\[\]]+)\]$/);
    if (header) {
      table = root;
      for (const part of header[1].split('.').map(p => p.trim().replace(/^"|"$/g, ''))) {
        table = table[part] = table[part] ?? {};
      }
      continue;
    }
    const eq = line.indexOf('=');
    if (eq < 0) {
      throw new Error(`invalid TOML line: ${lines[i]}`);
    }
    const key = line.slice(0, eq).trim().replace(/^"|"$/g, '');
    let value = line.slice(eq + 1).trim();
    if (value.startsWith('[')) {
      // multi-line array: accumulate until brackets balance (strings don't contain brackets here)
      while (!balanced(value) && i + 1 < lines.length) {
        i++;
        line = stripComment(lines[i]);
        value += '\n' + line;
      }
    }
    table[key] = parseValue(value);
  }
  return root;
}

function balanced(s: string): boolean {
  let depth = 0;
  let inStr: string | null = null;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (inStr) {
      if (ch === '\\' && inStr === '"') {
        i++;
      } else if (ch === inStr) {
        inStr = null;
      }
    } else if (ch === '"' || ch === "'") {
      inStr = ch;
    } else if (ch === '[') {
      depth++;
    } else if (ch === ']') {
      depth--;
    }
  }
  return depth === 0;
}

/** PEP 723: `dependencies` are pip specs; `[tool.xnb]` holds conda deps and channels. */
export function pep723Source(source: string): IEnvSource | null {
  const block = pep723Block(source);
  if (block === null) {
    return null;
  }
  const toml = parseTomlSubset(block);
  const xnb = toml.tool?.xnb ?? {};
  const deps: IEnvSource['dependencies'] = [...asList(xnb.dependencies, '[tool.xnb] dependencies')];
  const pip = asList(toml.dependencies, 'PEP 723 dependencies');
  if (pip.length) {
    deps.push({ pip });
  }
  return { channels: asList(xnb.channels, '[tool.xnb] channels'), dependencies: deps };
}

// -- merge -------------------------------------------------------------------

function kernelFromExtension(path: string): string | null {
  const ext = path.split('.').pop()?.toLowerCase() ?? '';
  return EXTENSIONS[ext] ?? null;
}

export function resolveKernel(job: IJob, nb: INotebook | null): { name: string; pkg: string } {
  let name =
    job.kernel || nb?.metadata?.kernelspec?.name || (job.format === 'script' ? kernelFromExtension(job.path) : null);
  if (!name && nb) {
    const lang = String(nb.metadata?.language_info?.name ?? nb.metadata?.kernelspec?.language ?? '').toLowerCase();
    name = { python: 'xpython', r: 'xr', lua: 'xlua', javascript: 'xjavascript' }[lang] ?? null;
  }
  if (!name) {
    name = 'xpython';
  }
  // Accept python3 / ipykernel-style names for Python notebooks.
  if (name === 'python3' || name === 'python' || name.startsWith('python3')) {
    name = 'xpython';
  }
  if (name in KERNELS) {
    return { name, pkg: KERNELS[name] };
  }
  // A package name given directly (e.g. --kernel xeus-python)
  const byPkg = Object.entries(KERNELS).find(([, pkg]) => pkg === name);
  if (byPkg) {
    return { name: byPkg[0], pkg: byPkg[1] };
  }
  if (name.startsWith('xeus-')) {
    return { name: '', pkg: name };
  }
  throw new Error(`unknown kernel "${name}"; pass --kernel (${Object.keys(KERNELS).join(', ')})`);
}

export function mergeEnv(job: IJob, nb: INotebook | null): IMergedEnv {
  const sources: { channels: string[]; specs: string[]; pip: string[] }[] = [];
  if (job.envYaml) {
    const parsed = YAML.parse(job.envYaml) ?? {};
    sources.push(splitSource(parsed, 'environment file'));
  }
  if (nb) {
    sources.push(splitSource(nb.metadata?.xnb, 'notebook metadata.xnb'));
  } else if (job.format === 'script') {
    sources.push(splitSource(pep723Source(job.content), 'PEP 723 block'));
  }
  sources.push({ channels: job.channels ?? [], specs: job.deps ?? [], pip: job.pip ?? [] });

  const channels: string[] = [];
  const specs = new Map<string, string>();
  const pip = new Map<string, string>();
  for (const s of sources) {
    for (const c of s.channels) {
      if (!channels.includes(c)) {
        channels.push(c);
      }
    }
    for (const spec of s.specs) {
      const name = condaName(spec);
      specs.delete(name); // keep insertion order of the winning source
      specs.set(name, spec.trim());
    }
    for (const spec of s.pip) {
      const name = pipName(spec);
      pip.delete(name);
      pip.set(name, spec.trim());
    }
  }
  for (const c of DEFAULT_CHANNELS) {
    if (!channels.includes(c)) {
      channels.push(c);
    }
  }
  const kernel = resolveKernel(job, nb);
  if (!specs.has(kernel.pkg)) {
    specs.set(kernel.pkg, kernel.pkg);
  }
  const specList = [...specs.values()];
  const pipList = [...pip.values()];
  const deps: any[] = [...specList];
  if (pipList.length) {
    deps.push({ pip: pipList });
  }
  const yml = YAML.stringify({ channels, dependencies: deps });
  return {
    channels,
    specs: specList,
    pipSpecs: pipList,
    kernelName: kernel.name,
    kernelPackage: kernel.pkg,
    yml
  };
}
