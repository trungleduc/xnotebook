# xnotebook

Run Jupyter notebooks and scripts on **xeus wasm kernels** with **emscripten-forge**
packages, sandboxed inside headless Chromium.

```console
$ pip install xnotebook
$ xnb analysis.ipynb                 # writes analysis.out.ipynb
$ xnb script.py -d numpy             # prints outputs as they come
```

The command is `xnb` (`xnotebook` works too), and the Python module is `xnotebook`.

The first run downloads a pinned, sha256-verified `chrome-headless-shell`
(about 120 MB) into the cache. No conda, Node or system browser is needed.

## Dependencies

Three sources are merged, and a later source wins for the same package:

1. an environment file: `-e environment.yaml`
2. the input file itself:
   - notebook metadata `metadata.xnb`, which uses the environment.yaml schema:
     ```json
     "metadata": {"xnb": {"channels": ["conda-forge"], "dependencies": ["numpy", {"pip": ["six"]}]}}
     ```
   - or, in a `.py` script, a [PEP 723](https://peps.python.org/pep-0723/) block, where
     `dependencies` are pip packages and `[tool.xnb]` holds conda packages and channels:
     ```python
     # /// script
     # dependencies = ["six"]
     # [tool.xnb]
     # dependencies = ["numpy"]
     # ///
     ```
3. the CLI: `-d numpy -d "pandas>=2" --pip six -c conda-forge`

Channels default to `https://prefix.dev/emscripten-forge-4x` and `conda-forge`. The
kernel comes from `--kernel`, then the notebook's `kernelspec`, then the file extension:

| kernel | package | extension |
| --- | --- | --- |
| `xpython` | xeus-python | `.py` |
| `xr` | xeus-r | `.R` |
| `xlua` | xeus-lua | `.lua` |
| `xjavascript` | xeus-javascript | `.js` |

Solved environments are cached as locks. `--lock-out lock.json` saves the lock, and
`--lock lock.json` reuses it without solving.

## Widgets

ipywidgets state is saved in `metadata.widgets`
(`application/vnd.jupyter.widget-state+json`), like `jupyter nbconvert --execute` does.
JupyterLab, nbviewer and Voila then render the widgets with the values they had at the
end of the run. Closed widgets are dropped. Use `--no-widget-state` to turn this off.
See `demo/widgets_demo.ipynb`.

Scripts are split into cells at `# %%` markers (percent format). The comment prefix
follows the language, so Lua uses `-- %%`.

## Security model

Notebook code runs in a Web Worker inside Chromium:

- **Files:** the kernel sees only an in-memory filesystem, so the host disk is out of
  reach. `--mount src:/dst` copies files in. `:rw` mounts are written back after the
  run, and only as regular files under `src`: no symlinks, no `..`, with size caps.
- **Network:** none for notebook code.
  - Packages are fetched *before* any package or user code runs. Everything goes through
    a local caching proxy that verifies each file against the lock's sha256.
  - Then the run is **sealed**. The host firewall (CDP `Fetch` interception) denies every
    request, the proxy refuses everything, and the kernel worker also has
    `connect-src 'none'`.
  - Chromium's own DNS is disabled.
- **Processes and environment:** not available from a browser worker.
- **OS sandbox:**
  - Chromium's OS sandbox is used when the system allows it. Otherwise xnb warns and
    runs with `--no-sandbox`, and `--strict` refuses to run at all.
  - On Windows (and with `XNB_CDP_TRANSPORT=ws`), DevTools uses a loopback WebSocket
    port instead of a pipe.

## CLI

```
xnb FILE [-o OUT|-] [--inplace] [-q]
         [-e ENV.yaml] [-d SPEC]... [--pip SPEC]... [-c CHANNEL]... [--kernel NAME]
         [--lock FILE] [--lock-out FILE]
         [--mount SRC:DST[:ro|rw]]... [--cwd DIR] [--stdin FILE]
         [--timeout S] [--cell-timeout S] [--max-memory MB] [--allow-errors] [--no-widget-state] [-v]
         [--offline] [--refresh] [--strict] [--browser-path PATH] [--cache-dir DIR] [--debug]
xnb setup [--from chrome-headless-shell.zip]
xnb cache {info,clean,prune}
xnb mcp [--mount SRC:DST[:ro]]... [--cwd DIR] [-c CHANNEL]... [--cell-timeout S] [--idle-timeout S]
        [--max-sessions N] [--max-memory MB] [--strict] [--offline] [--browser-path PATH] [--cache-dir DIR] [--debug]
```

Exit codes: 0 on success, 1 when a cell fails or times out, 2 for usage or setup
errors.

## Python API

```python
import xnotebook

nb = xnotebook.run("analysis.ipynb", deps=["numpy"], cell_timeout=60)   # returns the executed nbformat dict
res = xnotebook.run(nb_dict, allow_errors=True, return_result=True)     # includes status, failedCell, stats
```

## MCP server

`xnb mcp` serves the same sandbox to MCP clients (Claude Code, Claude Desktop, ...) as a
code interpreter. It speaks stdio only and never opens a port.

```console
$ claude mcp add xnb -- xnb mcp --mount ./data:/data
```

```json
{"mcpServers": {"xnb": {"command": "xnb", "args": ["mcp", "--mount", "/path/to/data:/data"]}}}
```

Tools:

- `run(code, kernel?, deps?, pip?)`: runs code once in a fresh kernel.
- `session_start(kernel?, deps?, pip?)` → `session_id`, then `session_exec(session_id, code)`:
  a kernel that keeps its state between calls, like a notebook. `session_close` stops it.

Outputs come back as text, with plots as images. Errors set `isError`.

- **Packages:** they are fixed when a session starts, because the session is sealed after
  that. Start a new session to add packages.
- **Timeouts:** a cell that times out (`--cell-timeout`, 120 s by default) ends its session.
  Sessions idle for `--idle-timeout` are closed.
- **Resources:** each session is one Chromium process, and `--max-sessions` caps how many
  run at once (4 by default).
- **Mounts:** only the person starting the server chooses them, and they are read-only.

The first `session_start` on a cold cache downloads the browser and the packages. Running
`xnb setup` and one `xnb` run beforehand keeps tool calls fast.

## Cache

The cache lives in `~/.cache/xnb` by default (`$XNB_CACHE_DIR` overrides it):

- `chromium/`: the pinned browser;
- `pkgs/<sha256>`: packages, fetched lazily and immutable;
- `repodata/`: metadata, with ETag and a 1 h TTL;
- `locks/`: solved environments.

`--offline` uses only the cache, and `--refresh` re-solves and revalidates.

## Development

```console
$ cd web && npm ci && npm run build && npm test   # bundle -> xnotebook/_web
$ pip install -e ".[test]"
$ pytest                                         # browser tests are skipped without Chromium
$ python tools/pin_chromium.py 154.0.8037.57       # hashes for chromium.PINNED
```

## Known limitations

- **Widgets:** the final ipywidgets state is saved as a static snapshot. Python callbacks
  don't run when the saved notebook is opened.
- **pip:** pure-Python wheels only.
- **xeus-lua:** each top-level line is evaluated on its own, so `local` variables don't
  persist across lines. Use globals.
