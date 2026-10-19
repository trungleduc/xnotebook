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
- **Network:** none for notebook code (and none for the `xnb agent` worker, whose model
  requests go through the host).
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
xnb mcp [--mount SRC:DST[:ro|rw]]... [--cwd DIR] [-c CHANNEL]... [--cell-timeout S] [--idle-timeout S]
        [--max-sessions N] [--max-memory MB] [--strict] [--offline] [--browser-path PATH] [--cache-dir DIR] [--debug]
xnb agent [-p PROMPT] [-e ENV.yaml] [-d SPEC]... [--pip SPEC]... [--kernel NAME] [--mount SRC:DST[:ro|rw]]... [--cwd DIR]
          [--provider anthropic|openai] [--base-url URL] [--model ID] [--effort LEVEL] [--compat KEY=VALUE]...
          [--max-requests N] [--max-tokens-total N] [--cell-timeout S] [--max-restarts N]
          [--json] [--images-dir DIR] [--save FILE.ipynb] [--system FILE] [--no-images] [--strict] [--debug]
xnb kernel install --name NAME [--display-name TEXT] [-e ENV.yaml] [-d SPEC]... [--pip SPEC]... [-c CHANNEL]...
        [--kernel NAME] [--mount SRC:DST[:ro|rw]]... [--cwd DIR] [--cell-timeout S] [--max-memory MB] [--strict]
        [--user | --sys-prefix | --prefix DIR]
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
$ claude mcp add xnb -- xnb mcp --mount ./data:/data --mount ./xnb-out:/out:rw
```

```json
{"mcpServers": {"xnb": {"command": "xnb", "args": ["mcp", "--mount", "/path/to/data:/data"]}}}
```

Tools:

- `run(code, kernel?, deps?, pip?)`: runs code once in a fresh kernel.
- `session_start(kernel?, deps?, pip?)` → `session_id`, then `session_exec(session_id, code)`:
  a kernel that keeps its state between calls, like a notebook. `session_close` stops it.
- `session_read_file(session_id, path)`: returns a file from the kernel's filesystem to the
  agent, or lists a directory. Text comes back as text, images as images, and other files
  as a binary resource (at most 10 MB).

Outputs come back as text, with plots as images. Errors set `isError`.

- **Packages:** they are fixed when a session starts, because the session is sealed after
  that. Start a new session to add packages.
- **Timeouts:** a cell that times out (`--cell-timeout`, 120 s by default) ends its session.
  Sessions idle for `--idle-timeout` are closed.
- **Resources:** each session is one Chromium process, and `--max-sessions` caps how many
  run at once (4 by default).
- **Mounts:** only the person starting the server chooses them. Files a kernel writes
  under an `:rw` mount are saved to the host after every `run` and `session_exec` call,
  with only changed files sent and the same limits as `xnb run`. So output survives even
  if a later cell times out. The tool result lists the files it saved. The agent is told
  which paths are mounted.

The first `session_start` on a cold cache downloads the browser and the packages. Running
`xnb setup` and one `xnb` run beforehand keeps tool calls fast.

## Agent

`xnb agent` is a chat in the terminal with an agent that runs **inside the sandbox**. Its
loop ([pi](https://www.npmjs.com/package/@earendil-works/pi-agent-core)) runs in a browser
worker next to a sealed kernel. It runs code, reads and edits files there, and restarts the
kernel when a cell kills it.

```console
$ export ANTHROPIC_API_KEY=...
$ xnb agent -d pandas -d matplotlib --mount ./data:/data:rw
$ xnb agent -p "summarize /data/sales.csv" --mount ./data:/data > summary.md
$ xnb agent --provider openai --base-url http://localhost:11434/v1 --model qwen3-coder
```

- **The API key stays on the host.** The agent has no network: each model request goes
  over the DevTools bridge to xnb, which adds the key (`ANTHROPIC_API_KEY` or
  `ANTHROPIC_AUTH_TOKEN`, `OPENAI_API_KEY`) and forwards it to the one endpoint of the
  session. xnb only forwards the chat endpoint with the session's model. It refuses
  anything that runs on the provider's side, such as web search, code execution or MCP
  servers, which would be a way around the seal.
  `--max-requests` and `--max-tokens-total` cap what a session can spend.
- **Providers:** `--provider anthropic` (default model `claude-opus-5-5`) or
  `--provider openai` for any OpenAI-compatible Chat Completions server (OpenAI,
  OpenRouter, vLLM, Ollama, ...). Use `--compat KEY=VALUE` for servers with quirks.
- **Tools:** `execute` (a notebook cell), `read`, `edit` (pi's exact-match edits), `write`
  and `restart_kernel`. There is no shell: the kernel has no processes.
- **Files:** what the agent changes under `:rw` mounts is saved to the host right away and
  shown as a diff. The first time a file is overwritten in a session, its original is copied
  to `~/.cache/xnb/backups/<session>/` (files the agent created are listed there too).
  Writes under `:ro` mounts are refused. Files outside mounts are lost when the kernel
  restarts.
- **Kernel restarts:** a cell that runs past `--cell-timeout` kills the kernel (wasm cannot
  be interrupted). The agent calls `restart_kernel` to get a fresh one with the same
  packages (`--max-restarts`, 10 by default). The restarted kernel is as sealed as the first.
- **Output:** the model's text goes to stdout; tool calls, diffs and progress go to
  stderr. `--json` prints every event as one JSON line, `--images-dir DIR` saves plots,
  and `--save FILE.ipynb` keeps the cells the agent ran.
- **Chat commands:** `/help`, `/reset`, `/restart`, `/mounts`, `/tokens`,
  `/save FILE.ipynb`, `/exit`. Ctrl-C interrupts the agent. A running cell still runs to its
  end.
- `AGENTS.md` in the current directory and `--system FILE` are added to the agent's
  instructions.

Everything the agent sees, including mounted data and cell outputs, is sent to the model
provider. Only mount what you are willing to send.

## Jupyter kernel

`xnb kernel install` adds a kernelspec, so JupyterLab, Notebook, VS Code, nbclient or
papermill can run code in the sandbox. It needs the `kernel` extra (pyzmq):

```console
$ pip install 'xnotebook[kernel]'
$ xnb kernel install --name foo -e environment.yaml --mount ./data:/data
$ jupyter lab                                  # pick "foo (xnb)"
```

- **Packages:** they are fixed per kernelspec, because the kernel is sealed once it
  starts. Install one kernelspec per environment. The environment file is copied into
  the kernelspec, and installing again under the same name replaces it. A kernelspec that
  xnb did not write is never overwritten.
- **Protocol:** Jupyter messages are relayed unchanged to the xeus kernel, so outputs,
  completion, inspection, live widgets and `input()` work as with any kernel.
- **Interrupt:** a wasm kernel cannot be interrupted. An interrupt, a cell that runs past
  `--cell-timeout`, or a crash restarts the kernel instead: the running cell fails with
  `KeyboardInterrupt` (or `TimeoutError`), and the kernel comes back with a fresh state.
- **Mounts:** `:rw` mounts are written back to the host after every cell.

The kernel logs to Jupyter's server log. The first start on a cold cache downloads the
browser and the packages, so run `xnb setup` and one `xnb` run beforehand.

## Cache

The cache lives in `~/.cache/xnb` by default (`$XNB_CACHE_DIR` overrides it):

- `chromium/`: the pinned browser;
- `pkgs/<sha256>`: packages, fetched lazily and immutable;
- `repodata/`: metadata, with ETag and a 1 h TTL;
- `locks/`: solved environments;
- `backups/`: originals of files `xnb agent` overwrote.

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
