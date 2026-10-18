// esbuild options for the agent worker (shared by build.mjs and the tests).
import { builtinModules } from 'node:module';
import path from 'node:path';

/**
 * The agent worker bundles pi. Node built-ins some of its modules import (never called with
 * the code paths we use) become stubs that throw, so nothing can quietly reach outside.
 */
export function agentOptions(root) {
  // cross-spawn runs Node code as soon as it is imported (pi imports it for its shell tools).
  const builtins = new Set([...builtinModules, 'cross-spawn']);
  const pi = path.join(root, 'node_modules/@earendil-works/pi-coding-agent/dist');
  return {
    entryPoints: [path.join(root, 'src/agent.worker.ts')],
    alias: {
      'pi-edit-diff': path.join(pi, 'core/tools/edit-diff.js'),
      'pi-text': path.join(pi, 'utils/text.js')
    },
    plugins: [
      {
        name: 'node-stubs',
        setup(build) {
          build.onResolve({ filter: /.*/ }, args => {
            const name = args.path.replace(/^node:/, '');
            if (builtins.has(name) || builtins.has(name.split('/')[0])) {
              return { path: name, namespace: 'node-stub' };
            }
            return undefined;
          });
          build.onLoad({ filter: /.*/, namespace: 'node-stub' }, args => ({
            loader: 'js',
            contents:
              `const fail = name => () => { throw new Error('node:${args.path}.' + name + ' is not available in xnb'); };\n` +
              `module.exports = new Proxy({}, { get: (_, k) => k === '__esModule' ? false : k === 'default' ? module.exports : fail(String(k)) });`
          }));
        }
      }
    ]
  };
}
