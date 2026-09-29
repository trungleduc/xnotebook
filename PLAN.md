# xnb: run notebooks and scripts on xeus wasm kernels inside headless Chromium

## Context
`xnb notebook.ipynb` runs a notebook or source file on the same xeus + emscripten-forge
wasm kernels that JupyterLite uses, and writes back the notebook with outputs.

- **Distribution:** a PyPI package (`pip install xnb`), one small pure-Python wheel. On
  first run it downloads chrome-headless-shell, pinned to a version and verified against
  a sha256.
- **Everything runs inside Chromium:** merging dependency sources, solving (mambajs),
  installing packages into the wasm filesystem, running the kernel, and building the
  output notebook. Python only does four things:
  - launches Chromium;
  - acts as its firewall;
  - runs a **lazy caching package proxy**, the only component that talks to the
    internet;
  - reads and writes files on disk.
- **Isolation:** notebook code runs in a Web Worker with no access to host files,
  processes, or env vars, and **never has network access**. Chromium never talks to the
  internet directly, only to the local proxy. Even that access is cut off before any
  package or user code runs.
- **Dependencies:** an external `environment.yaml`, notebook `metadata.xnb` (same
  schema), PEP 723 blocks in `.py` files, and CLI flags.

A side benefit: the core is a TypeScript web bundle that also runs in any browser, so it
could later be reused in JupyterLite or notebook.link.

## Architecture
```
xnb (Python + Typer CLI)                                Chromium (headless, fresh temp profile)
 ├─ chromium.py  download/verify/extract CfT             ┌─ page http://127.0.0.1:<port>/<token>/  (orchestrator)
 ├─ cdp.py       CDP over --remote-debugging-pipe        │   ├─ merge dep sources
 ├─ firewall.py  Fetch.requestPaused (request stage) ◄───┤   ├─ mambajs solve → lock ──► host (lock cache + expected hashes)
 │     SETUP: allowlisted upstream URL → rewrite to proxy│   ├─ fetch all package bytes (real URLs, rewritten)
 │     RUN:   deny everything                            │   ├─ xnb_seal() ──► phase RUN
 ├─ proxy.py     127.0.0.1:<random>/<token>/...          │   └─ spawn kernel worker, transfer bytes
 │     serves web bundle + lazily cached upstream files  │        └─ Web Worker: xeus kernel + MEMFS
 │     miss → fetch upstream (allowlist) → tee to client + cache
 ├─ cache.py     ~/.cache/xnb/{pkgs,repodata,locks,chromium}
 └─ cli.py / api.py  read inputs, write outputs          └─ results/events → host via Runtime binding
```

### Run phases
1. **Boot.**
   - Python starts the proxy on `127.0.0.1` at a random port, with a random per-run
     token as the first path segment.
   - It launches Chromium over a pipe and enables `Fetch` interception at the request
     stage for `*`.
   - It navigates to `http://127.0.0.1:<port>/<token>/index.html`. Loopback counts as a
     secure context, so there are no mixed-content problems. The web bundle is served
     same-origin by the proxy.
2. **Input.** Python passes a single JSON job to the page: the file contents, the CLI
   options, the env YAML text, the cached lock if there is one, and the contents of any
   mounted files. The page never reads the disk.
3. **Setup** (no package or user code running yet).
   - The page merges the sources and solves with mambajs, which requests repodata from
     the real channel URLs.
   - The firewall sees each allowlisted upstream request and redirects it to the proxy
     with `Fetch.continueRequest({url: "http://127.0.0.1:<port>/<token>/u/<encoded upstream url>"})`.
     The page doesn't see the change.
   - Anything outside the allowlist fails.
   - Once solved, the page sends the lock to the host. The host caches it and passes the
     expected sha256 of every package to the proxy.
   - The page then downloads the packages the same way, through the proxy.
4. **Seal.**
   - The page calls `xnb_seal()`.
   - The host switches the firewall to deny all traffic (the proxy included) and tells
     the proxy to stop contacting upstream.
   - Only then does the host reply.
5. **Run.** The page starts the kernel worker and transfers the package ArrayBuffers to
   it. The worker's `fetch` is shimmed to serve only those preloaded bytes. The worker
   then installs the packages into MEMFS, boots Python, and runs the cells. `.pth`
   files and imports therefore run only after the seal. Outputs stream to the host as
   they're produced, for live printing.
6. **Output.** The page returns the output nbformat JSON. The host writes the notebook
   and the `rw` mounts, then kills Chromium, deletes the temp profile, and stops the
   proxy.

### Lazy caching proxy (`proxy.py`)
- A stdlib `ThreadingHTTPServer` bound to `127.0.0.1` only, at a random port. It
  accepts only `GET`/`HEAD`, and only requests that carry the per-run token. Other local
  processes can't use it as an open proxy or poison its cache. It also answers
  `OPTIONS` preflights, without the token, with permissive CORS headers and no body.
- **Routes:**
  - `/<token>/…`: the web bundle shipped in the wheel (`xnb/_web/`). It's served with
    `Content-Security-Policy: default-src 'self'; script-src 'self' 'wasm-unsafe-eval'`,
    so all traffic is same-origin and all upstream traffic goes through the rewrite.
  - `/<token>/u/<upstream-url>`: the caching proxy. The upstream host must be on the
    allowlist (the channel hosts from the merged env, plus `pypi.org` and
    `files.pythonhosted.org` when there are pip deps). Anything else gets a 403.
- **Package files** (`.conda`, `.tar.bz2`, `.whl`) are immutable:
  - **Hit:** served from `~/.cache/xnb/pkgs/<sha256>` through `sendfile`, at native
    speed.
  - **Miss (lazy):**
    - stream from upstream while writing to `<cache>/tmp/<random>` and hashing;
    - each chunk goes to Chromium as soon as it arrives, so the first run isn't slowed
      down;
    - at the end, compare the hash with the one expected from the lock;
    - on a match, rename the file atomically into place, which is safe with parallel
      xnb runs;
    - on a mismatch, delete the temp file and drop the connection, so the page sees an
      error and the file is never cached.
  - A URL→sha256 index lets later runs find cached files from the lock alone.
- **Repodata / PyPI JSON:**
  - cached in `~/.cache/xnb/repodata/<sha256(url)>` along with ETag and
    Last-Modified, and revalidated with `If-None-Match` once a TTL passes (default 1 h,
    `--refresh` forces it);
  - `--offline` serves stale entries and never contacts upstream;
  - lock cache hits skip repodata entirely.
- **Responses** carry `Access-Control-Allow-Origin: *`, the upstream `Content-Type`, and
  `Content-Length` when known. The proxy follows upstream redirects itself and returns
  only the final 200, so Chromium never sees a cross-origin redirect.
- **After the seal:** upstream access is switched off in the proxy, as well as blocked in
  the firewall.

### Firewall (`firewall.py`, `Fetch.requestPaused`, request stage only)
- Only small control messages go over the pipe. No response bodies ever cross CDP.
- **Before the seal:**
  - requests to the proxy origin continue as they are;
  - allowlisted upstream URLs are rewritten to the proxy;
  - everything else fails with `BlockedByClient`.
- **After the seal:** every request fails.
- Backup layers:
  - `--host-resolver-rules="MAP * ~NOTFOUND"`: Chromium itself never needs DNS, since
    all real traffic goes to an IP literal;
  - `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`;
  - `--disable-background-networking`, `--disable-component-update`, `--no-pings`,
    `--dns-prefetch-disable`.

### Chromium management (`chromium.py`)
- `PINNED = {platform: (version, url, sha256)}`, recorded at release time from Chrome
  for Testing.
- **First run:** download with progress, verify, and unzip to
  `~/.cache/xnb/chromium/<version>/`. A file lock covers concurrent first runs, and an
  atomic rename means a half-extracted copy is never used.
- **Commands and options:**
  - `xnb setup` downloads Chromium explicitly;
  - `xnb setup --from <zip>` installs from a local file, still hash-checked;
  - `--browser-path` / `XNB_CHROMIUM_PATH` uses your own Chromium.
- **Missing libraries on Linux:** run `ldd` and print the exact
  `apt`/`dnf`/`apk` command.
- **Sandbox:** launch with the sandbox first. If Chromium reports "No usable sandbox",
  retry with `--no-sandbox` and print a warning. `--strict` refuses to fall back.

### CDP transport (`cdp.py`)
- **POSIX:** `subprocess.Popen(..., pass_fds=...)` with `--remote-debugging-pipe`
  (fds 3/4, null-delimited JSON), read by a reader thread.
- **Windows (v1):** `--remote-debugging-port=0` on 127.0.0.1, read from
  `DevToolsActivePort`, over a stdlib WebSocket client. A documented weaker spot; the
  pipe through `_winapi` comes later.
- Domains used: `Target`, `Page`, `Runtime` (`addBinding`, `evaluate`), `Fetch`,
  `Inspector`.

## Reused building blocks (inside the web bundle)
- `@emscripten-forge/mambajs`: `create({yml, platform})` / `solve()` / `pipInstall`.
  Reference: `WORK/qsai/notebook-link-public-env/src/tool.ts`.
- `@emscripten-forge/mambajs-core`: `parseEnvYml`, `computeLockId`,
  `computePackageUrl`, `installPackagesToEmscriptenFS`, `loadSharedLibs`,
  `bootstrapPython`, `waitRunDependencies`, `getPythonVersion`, `untarCondaPackage`.
- The kernel boot sequence from `@jupyterlite/xeus` 5.x `worker.ts`. Fetch the current
  source, because the local `WORK/xeus` checkout is from January 2025. It includes the
  `toplevel_promise` handling for top-level `await`.

## Repository layout
```
pyproject.toml            hatchling; build hook runs `npm run build` in web/ and copies dist → xnb/_web/
xnb/
  __init__.py             public API: run(path | nb dict, **options) -> nb dict
  __main__.py, cli.py     argparse CLI
  chromium.py             pinned download / verify / extract / launch flags / sandbox fallback
  cdp.py                  pipe + (Windows) websocket transport
  firewall.py             phases, allowlist, URL rewrite to proxy
  proxy.py                token-guarded loopback server: web bundle + lazy caching upstream proxy
  cache.py                cache dir layout, atomic writes, url→sha256 index, TTL metadata
  session.py              drives a run: boot → job → events → result; timeouts; crash handling
  mounts.py               read mounts into the job; safe write-back for rw mounts
  _web/                   built bundle (generated, shipped in wheel, ~3 MB)
web/                      TypeScript
  src/orchestrator.ts     job handling, phases, seal, event relay
  src/deps.ts             source merge (env file, metadata.xnb, PEP 723, CLI), kernel mapping
  src/solve.ts            mambajs solve + package prefetch + sha256 verify
  src/kernel.worker.ts    xeus boot from preloaded bytes, MEMFS mounts, stdin replies
  src/executor.ts         iopub → nbformat outputs
  src/formats.ts          ipynb in/out, `# %%` script splitting
tests/                    pytest (host) + vitest (web)
```
The Python package's only runtime dependency is **Typer** (CLI); everything else is stdlib.

## CLI
`xnb <file> [-o out|-] [--inplace] [-e env.yaml] [-d spec]... [--pip spec]...
[-c channel]... [--kernel xpython|xr|xlua|...] [--lock f] [--lock-out f]
[--mount src:dst[:ro|rw]]... [--stdin f] [--timeout s] [--cell-timeout s]
[--max-memory MB] [--allow-errors] [--offline] [--refresh] [--strict]
[--browser-path p] [-q]`, plus `xnb setup [--from zip]` and
`xnb cache {info,clean,prune}`.

## Dependency merge (web/src/deps.ts)
- Sources are applied in the order env file → `metadata.xnb` / PEP 723 → CLI, and a
  later source wins.
- Channels are a union that keeps order. Specs are deduplicated by package name.
- PEP 723 `dependencies` go to pip, and `[tool.xnb]` holds conda deps and channels.
- The kernel package comes from `--kernel`, then `kernelspec.name`, then the file
  extension (`xpython→xeus-python`, `xr→xeus-r`, `xlua→xeus-lua`,
  `xjavascript→xeus-javascript`).
- Default channels: `https://prefix.dev/emscripten-forge-4x`, `conda-forge`.
- The lock cache key is `computeLockId(mergedYaml)`, stored in `~/.cache/xnb/locks/`.

## Behaviors (v1)
- **Outputs:** `stream` messages are merged; `display_data`, `execute_result`, `error`,
  `update_display_data` (by display_id), and `clear_output(wait)` are handled;
  `execution_count` and `language_info` are recorded.
- **Errors and timeouts:** execution stops at the first error unless `--allow-errors`
  is given. A cell timeout terminates the worker, and the remaining cells are marked as
  not run.
- **stdin:** `input()` reads lines from `--stdin`, or raises an error that says no
  stdin is available.
- **Widgets and comms:** comm messages are ignored. Widget view outputs are kept, but
  widget state isn't saved.
- **pip:** only pure-Python wheels. Anything else fails at solve time and names the
  package.
- **Mounts:** `ro` files are copied into MEMFS. `rw` files are also written back, only
  as regular files under `dst`, with no symlinks, no `..`, and size caps.
- **Resources:** `--max-memory` sets `--js-flags=--max-old-space-size`; wasm memory is
  capped at 4 GB; timeouts apply per cell and for the whole run; Chromium runs with
  `--disable-gpu`.

## Spike findings (2026-09-29)
- Risk 1 is resolved: `Fetch.continueRequest({url})` to the proxy works for the page's cross-origin fetches (rattler, packages).
- Rattler also fetches sharded repodata from `shards.prefix.dev`, and mambajs loads the parselmouth mapping from `raw.githubusercontent.com` at module load. The allowlist derives sibling hosts per channel; the mapping is allowed by exact URL prefix from the start.
- `--host-resolver-rules` needs `EXCLUDE 127.0.0.1`.
- The kernel worker's CSP (`connect-src 'none'`) already blocks real fetch/XHR/WebSocket/importScripts, including from nested blob workers. The firewall counter showed 0 requests after the seal.
- A warm run of xeus-python + numpy takes about 7 s with 0 upstream fetches (cold time not measured yet).

## Open risks (resolve in the spike)
1. **How `Fetch.continueRequest` with a changed `url` behaves.** CORS is not a concern,
   because the proxy writes every response header itself. Private Network Access is
   also fine, because the page is on loopback already. What remains to confirm is that
   the page receives the loopback response as the original `https://…` URL, as the CDP
   docs promise. The proxy follows upstream redirects itself, so Chromium never sees
   one. Fallback: give mambajs proxy channel URLs directly, and rewrite the lock back to
   upstream URLs before caching it so it stays portable.
2. **Sandbox behavior** on Ubuntu 24.04 (the AppArmor userns restriction), in Docker,
   and on WSL2. Detect the failure and fall back cleanly.
3. **The Windows WebSocket transport.** It's weaker isolation and needs its own tests.

## Implementation order
1. **Spike:** `chromium.py` + `cdp.py` (pipe) + `proxy.py` + a minimal firewall. The web
   side uses a hard-coded env (xeus-python + numpy), solves through the proxy, seals,
   boots the worker from preloaded bytes, and runs `print(1+1)`. Resolve risk 1.
2. Proxy caching in full (hash checks, atomic writes, TTL/ETag, `--offline`) and the
   lock cache.
3. The dependency merge, the executor, ipynb in/out, and the `xnb.run()` API.
4. Hardening: sandbox policy, resolver rules, mounts, limits, and escape tests.
5. Script formats with live streaming, and `xnb setup` / `xnb cache`.
6. The Windows transport, packaging (hatch build hook), docs, and CI wheels.

## Verification
- `pip install -e . && xnb examples/basic.ipynb`: the outputs should include print, a
  pandas DataFrame, a matplotlib PNG, `update_display_data`, top-level `await`, and an
  error cell. Validate in tests with `nbformat.validate`.
- **Lazy cache:**
  - On the first run, the files in `pkgs/` match the lock's hashes.
  - On the second run, the proxy logs zero upstream fetches for packages.
  - `--offline` succeeds.
  - A deleted cache entry is re-fetched lazily and only that one.
- **Integrity:** a proxy test fixture serves a tampered upstream file. The page gets an
  error, and nothing is written to `pkgs/`.
- **Concurrency:** two `xnb` runs at once with a cold cache both succeed, and no partial
  files are left behind.
- **Proxy guard:** a request without the token, or to a host outside the allowlist,
  gets a 403. The proxy isn't reachable on anything other than 127.0.0.1.
- **Escape tests.** Each must fail inside the kernel, and the run must still finish:
  - `open('/etc/passwd')`;
  - `js.fetch(location.origin + '/<token>/u/https://prefix.dev/')` after the seal;
  - `js.fetch('https://example.com')`;
  - a WebSocket or `RTCPeerConnection`;
  - `importScripts('https://x')`;
  - an `rw` mount escaping through `../` or a symlink.

  A host-side counter must show zero requests after the seal.
- **Seal ordering:** a test wheel with a `.pth` file that tries `fetch` at startup must
  be blocked.
- **Dependency sources:** `-e`, `metadata.xnb`, PEP 723, and the CLI flags. Check the
  precedence with `--lock-out`.
- **Clean `python:3.12-slim` container:** Chromium is downloaded and verified, missing
  libraries produce the `apt` hint, and `xnb setup --from` works offline.
- A non-Python kernel: `xnb hello.lua --kernel xlua`.

## Status (2026-09-29)
- Done and tested on Linux (WSL2): steps 1–5. That covers the proxy, lock cache, dependency merge, executor, API, mounts, timeouts, offline mode, the Typer CLI, and `setup`/`cache`.
  - Tests: 30 pytest (proxy, mounts, CLI, browser integration including escape tests) and 9 web unit tests.
- Chromium hashes are pinned for all 6 CfT platforms (`tools/pin_chromium.py`).
- The WebSocket CDP transport (Windows) is implemented. It has only been tested on Linux via `XNB_CDP_TRANSPORT=ws`, not on Windows or macOS.
- The wheel (3.3 MB, web bundle included via the hatch hook) installs and runs from a clean venv.
- Not done: CI wheels, a Docker/`python:3.12-slim` check, and a test of the firewall layer on its own (CSP currently blocks escapes first).
