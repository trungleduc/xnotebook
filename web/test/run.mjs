// Bundle each test/*.test.ts with esbuild and run it with node's test runner.
import * as esbuild from 'esbuild';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const out = fs.mkdtempSync(path.join(os.tmpdir(), 'xnb-web-test-'));
const files = fs.readdirSync(here).filter(f => f.endsWith('.test.ts'));
const built = [];
for (const f of files) {
  const outfile = path.join(out, f.replace(/\.ts$/, '.cjs'));
  await esbuild.build({
    entryPoints: [path.join(here, f)],
    outfile,
    bundle: true,
    platform: 'node',
    format: 'cjs',
    logLevel: 'warning',
    external: ['node:*']
  });
  built.push(outfile);
}
const r = spawnSync(process.execPath, ['--test', ...built], { stdio: 'inherit' });
fs.rmSync(out, { recursive: true, force: true });
process.exit(r.status ?? 1);
