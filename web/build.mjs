// Build the web bundle into ../xnb/_web (shipped inside the Python wheel).
import * as esbuild from 'esbuild';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const out = path.resolve(here, '../xnb/_web');
fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(out, { recursive: true });

const common = {
  bundle: true,
  minify: process.env.XNB_DEV ? false : true,
  sourcemap: false,
  target: 'es2022',
  platform: 'browser',
  logLevel: 'warning',
  // untarjs imports its wasm; we ship it next to the bundle and preload it ourselves.
  loader: { '.wasm': 'file' },
  assetNames: '[name]',
  define: { 'process.env.NODE_ENV': '"production"' },
  legalComments: 'none'
};

await esbuild.build({
  ...common,
  entryPoints: [path.join(here, 'src/orchestrator.ts')],
  outfile: path.join(out, 'orchestrator.js'),
  format: 'iife'
});
await esbuild.build({
  ...common,
  entryPoints: [path.join(here, 'src/kernel.worker.ts')],
  outfile: path.join(out, 'kernel.worker.js'),
  format: 'iife'
});
fs.copyFileSync(path.join(here, 'src/index.html'), path.join(out, 'index.html'));
fs.copyFileSync(
  path.join(here, 'node_modules/@emscripten-forge/untarjs/lib/unpack.wasm'),
  path.join(out, 'unpack.wasm')
);
for (const f of fs.readdirSync(out)) {
  console.log(`${f.padEnd(24)} ${(fs.statSync(path.join(out, f)).size / 1024).toFixed(0)} KiB`);
}
