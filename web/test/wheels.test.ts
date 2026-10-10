import { test } from 'node:test';
import assert from 'node:assert/strict';
import { wheelPath } from '../src/wheels';

const SP = 'lib/python3.13/site-packages';

test('wheel members land in site-packages', () => {
  assert.equal(wheelPath('nbconvert/__init__.py', SP), `${SP}/nbconvert/__init__.py`);
  assert.equal(wheelPath('nbconvert-7.17.1.dist-info/METADATA', SP), `${SP}/nbconvert-7.17.1.dist-info/METADATA`);
});

test('.data scheme directories are relocated', () => {
  assert.equal(
    wheelPath('nbconvert-7.17.1.data/data/share/jupyter/nbconvert/templates/lab/conf.json', SP),
    'share/jupyter/nbconvert/templates/lab/conf.json'
  );
  assert.equal(wheelPath('pkg-1.0.data/purelib/pkg/mod.py', SP), `${SP}/pkg/mod.py`);
  assert.equal(wheelPath('pkg-1.0.data/platlib/pkg/mod.py', SP), `${SP}/pkg/mod.py`);
  assert.equal(wheelPath('pkg-1.0.data/scripts/tool', SP), 'bin/tool');
  assert.equal(wheelPath('pkg-1.0.data/headers/pkg.h', SP), 'include/pkg.h');
  // Not a scheme directory: left where the wheel put it.
  assert.equal(wheelPath('pkg-1.0.data/other/x', SP), `${SP}/pkg-1.0.data/other/x`);
});
