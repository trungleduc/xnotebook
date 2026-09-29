import { test } from 'node:test';
import assert from 'node:assert/strict';
import YAML from 'yaml';
import { condaName, mergeEnv, parseTomlSubset, pep723Source, pipName, resolveKernel } from '../src/deps';
import { IJob } from '../src/types';

function job(over: Partial<IJob> = {}): IJob {
  return {
    path: 'nb.ipynb',
    format: 'ipynb',
    content: '',
    deps: [],
    pip: [],
    channels: [],
    mounts: [],
    allowErrors: false,
    ...over
  };
}

test('spec names', () => {
  assert.equal(condaName('conda-forge::NumPy >=1.2'), 'numpy');
  assert.equal(condaName('pandas=2.*'), 'pandas');
  assert.equal(pipName('Typing_Extensions>=4'), 'typing-extensions');
});

test('later sources win, channels keep order, defaults appended', () => {
  const env = mergeEnv(
    job({
      envYaml: 'channels: [https://example.org/ch]\ndependencies: [numpy=1.0, scipy, {pip: [six==1.0]}]\n',
      deps: ['numpy>=2'],
      pip: ['six==1.17'],
      channels: ['conda-forge']
    }),
    { nbformat: 4, nbformat_minor: 5, metadata: { xnb: { dependencies: ['pandas', 'scipy=1.11'] } }, cells: [] }
  );
  assert.deepEqual(env.channels, ['https://example.org/ch', 'conda-forge', 'https://prefix.dev/emscripten-forge-4x']);
  assert.deepEqual(env.specs, ['pandas', 'scipy=1.11', 'numpy>=2', 'xeus-python']);
  assert.deepEqual(env.pipSpecs, ['six==1.17']);
  const parsed = YAML.parse(env.yml);
  assert.deepEqual(parsed.dependencies.at(-1), { pip: ['six==1.17'] });
});

test('kernel resolution', () => {
  assert.deepEqual(resolveKernel(job({ kernel: 'xr' }), null), { name: 'xr', pkg: 'xeus-r' });
  assert.deepEqual(resolveKernel(job({ kernel: 'xeus-lua' }), null), { name: 'xlua', pkg: 'xeus-lua' });
  assert.equal(resolveKernel(job({ format: 'script', path: 'a.lua' }), null).pkg, 'xeus-lua');
  const nb = { nbformat: 4, nbformat_minor: 5, metadata: { kernelspec: { name: 'python3' } }, cells: [] };
  assert.equal(resolveKernel(job(), nb).name, 'xpython');
  assert.throws(() => resolveKernel(job({ kernel: 'nope' }), null));
});

test('PEP 723', () => {
  const src = [
    '#!/usr/bin/env python',
    '# /// script',
    '# requires-python = ">=3.11"',
    '# dependencies = [',
    '#   "requests<3",  # comment',
    "#   'rich',",
    '# ]',
    '# [tool.xnb]',
    '# channels = ["https://example.org/ch"]',
    '# dependencies = ["numpy"]',
    '# ///',
    'print(1)'
  ].join('\n');
  assert.deepEqual(pep723Source(src), {
    channels: ['https://example.org/ch'],
    dependencies: ['numpy', { pip: ['requests<3', 'rich'] }]
  });
  assert.equal(pep723Source('print(1)'), null);
  assert.deepEqual(parseTomlSubset('a = true\n[x.y]\nb = 2'), { a: true, x: { y: { b: 2 } } });
});
