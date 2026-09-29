"""Command line interface (Typer): `xnb <file> [options]`, `xnb setup`, `xnb cache ...`."""

from __future__ import annotations

import json
import os
import shutil
import sys
from enum import Enum
from pathlib import Path
from typing import List, Optional

import typer
from typing_extensions import Annotated

from . import __version__, chromium
from .cache import Cache
from .mounts import MountError

app = typer.Typer(
    name="xnb",
    help="Run notebooks and scripts on sandboxed xeus wasm kernels (emscripten-forge packages) "
    "inside headless Chromium.",
    add_completion=False,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    pretty_exceptions_enable=False,
)

DEPS = "Dependencies"
OUT = "Output"
EXEC = "Execution"
NET = "Cache and browser"


class _Echo:
    """Live terminal echo: scripts print their outputs; notebooks show per-cell progress.

    `quiet` hides progress only; script outputs are hidden only when `script` is False
    (e.g. when the notebook itself goes to stdout).
    """

    def __init__(self, script: bool, quiet: bool, verbose: bool = False) -> None:
        self.script = script
        self.quiet = quiet and not verbose
        self.verbose = verbose
        self.cells = 0

    def __call__(self, event: dict) -> None:
        kind = event.get("kind")
        if kind == "cell_end":
            if self.verbose:
                sys.stderr.write(
                    f"xnb: cell {event.get('cell')} {event.get('status')} in {event.get('seconds', 0):.2f}s "
                    f"({event.get('outputs', 0)} outputs)\n"
                )
                sys.stderr.flush()
            return
        if kind == "cell_start":
            if self.quiet:
                return
            self.cells += 1
            if not self.script or self.verbose:
                first = (event.get("source") or "").strip().splitlines()[:1]
                sys.stderr.write(f"xnb: cell {event.get('cell')}: {first[0][:60] if first else ''}\n")
                sys.stderr.flush()
            return
        if kind != "output" or not self.script or event.get("mode") != "add":
            return
        out = event.get("output", {})
        otype = out.get("output_type")
        if otype == "stream":
            stream = sys.stdout if out.get("name") == "stdout" else sys.stderr
            stream.write(out.get("text", ""))
            stream.flush()
        elif otype in ("execute_result", "display_data"):
            data = out.get("data", {})
            text = data.get("text/plain")
            if isinstance(text, list):
                text = "".join(text)
            if text is not None:
                sys.stdout.write(text + "\n")
            else:
                sys.stdout.write("[" + ", ".join(sorted(data)) + "]\n")
            sys.stdout.flush()
        elif otype == "error":
            sys.stderr.write("\n".join(out.get("traceback") or [f"{out.get('ename')}: {out.get('evalue')}"]) + "\n")
            sys.stderr.flush()


def _default_output(path: Path) -> Path:
    return path.with_name(path.stem + ".out.ipynb")


@app.command("run", no_args_is_help=True)
def run_command(
    file: Annotated[Path, typer.Argument(help="Notebook (.ipynb) or script (.py, .R, .lua, .js, ...).")],
    output: Annotated[Optional[str], typer.Option("-o", "--output", help="Output notebook path, or - for stdout.", rich_help_panel=OUT)] = None,
    inplace: Annotated[bool, typer.Option("--inplace", help="Overwrite the input notebook.", rich_help_panel=OUT)] = False,
    quiet: Annotated[bool, typer.Option("-q", "--quiet", help="No progress messages (script outputs are still printed).", rich_help_panel=OUT)] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Show every step with timings (packages, kernel boot, cells, network stats).", rich_help_panel=OUT)] = False,
    env_file: Annotated[Optional[Path], typer.Option("-e", "--env", help="environment.yaml.", rich_help_panel=DEPS)] = None,
    deps: Annotated[Optional[List[str]], typer.Option("-d", "--dep", help="Conda spec (repeatable).", rich_help_panel=DEPS)] = None,
    pip: Annotated[Optional[List[str]], typer.Option("--pip", help="Pip spec (repeatable).", rich_help_panel=DEPS)] = None,
    channels: Annotated[Optional[List[str]], typer.Option("-c", "--channel", help="Channel (repeatable).", rich_help_panel=DEPS)] = None,
    kernel: Annotated[Optional[str], typer.Option("--kernel", help="Kernel name (xpython, xr, xlua, xjavascript, ...) or package.", rich_help_panel=DEPS)] = None,
    lock: Annotated[Optional[Path], typer.Option("--lock", help="Use this lock file instead of solving.", rich_help_panel=DEPS)] = None,
    lock_out: Annotated[Optional[Path], typer.Option("--lock-out", help="Write the resolved lock here.", rich_help_panel=DEPS)] = None,
    mounts: Annotated[Optional[List[str]], typer.Option("--mount", metavar="SRC:DST[:ro|rw]", help="Expose host files to the kernel (copied in; rw writes back after the run).", rich_help_panel=EXEC)] = None,
    cwd: Annotated[Optional[str], typer.Option("--cwd", help="Kernel working directory (default /home/xnb).", rich_help_panel=EXEC)] = None,
    stdin: Annotated[Optional[Path], typer.Option("--stdin", help="File whose lines answer input() calls.", rich_help_panel=EXEC)] = None,
    timeout: Annotated[Optional[float], typer.Option("--timeout", help="Whole-run timeout in seconds.", rich_help_panel=EXEC)] = None,
    cell_timeout: Annotated[Optional[float], typer.Option("--cell-timeout", help="Per-cell timeout in seconds.", rich_help_panel=EXEC)] = None,
    max_memory: Annotated[Optional[int], typer.Option("--max-memory", metavar="MB", help="JS heap limit for the browser.", rich_help_panel=EXEC)] = None,
    allow_errors: Annotated[bool, typer.Option("--allow-errors", help="Keep executing after a failing cell.", rich_help_panel=EXEC)] = False,
    widget_state: Annotated[bool, typer.Option("--widget-state/--no-widget-state", help="Save ipywidgets state in the notebook metadata.", rich_help_panel=OUT)] = True,
    offline: Annotated[bool, typer.Option("--offline", help="Never contact upstream; use the cache only.", rich_help_panel=NET)] = False,
    refresh: Annotated[bool, typer.Option("--refresh", help="Re-solve and revalidate cached metadata.", rich_help_panel=NET)] = False,
    strict: Annotated[bool, typer.Option("--strict", help="Refuse to run Chromium without its OS sandbox.", rich_help_panel=NET)] = False,
    browser_path: Annotated[Optional[str], typer.Option("--browser-path", help="Use this Chromium/chrome-headless-shell binary.", rich_help_panel=NET)] = None,
    cache_dir: Annotated[Optional[Path], typer.Option("--cache-dir", help="Cache directory (default ~/.cache/xnb or $XNB_CACHE_DIR).", rich_help_panel=NET)] = None,
    debug: Annotated[bool, typer.Option("--debug", help="Verbose logs (firewall, proxy, page console).", rich_help_panel=NET)] = False,
) -> None:
    """Execute FILE and write the notebook with outputs (default: <name>.out.ipynb for notebooks)."""
    from .api import build_job
    from .mounts import write_back
    from .session import RunError, Session

    src = file
    if not src.exists():
        _fail(f"no such file: {src}", 2)
    is_nb = src.suffix.lower() == ".ipynb"
    if inplace and not is_nb:
        _fail("--inplace only applies to notebooks", 2)
    try:
        job, mount_objs = build_job(
            src,
            env_file=env_file,
            deps=deps or [],
            pip=pip or [],
            channels=channels or [],
            kernel=kernel,
            lock=lock,
            stdin=stdin,
            mounts=mounts or [],
            allow_errors=allow_errors,
            cell_timeout=cell_timeout,
            cwd=cwd,
            widget_state=widget_state,
        )
    except (OSError, ValueError, MountError) as e:
        _fail(str(e), 2)
    echo = _Echo(script=not is_nb and output != "-", quiet=quiet, verbose=verbose)

    def log(m: str) -> None:
        if not quiet or debug:
            sys.stderr.write(m + "\n")
            sys.stderr.flush()

    session = Session(
        job,
        cache=Cache(cache_dir) if cache_dir else None,
        browser_path=browser_path,
        strict=strict,
        offline=offline,
        refresh=refresh,
        timeout=timeout,
        max_memory_mb=max_memory,
        lock_out=lock_out,
        on_event=echo,
        log=log,
        debug=debug,
        verbose=verbose,
        quiet=quiet,
    )
    try:
        result = session.run()
    except (RunError, chromium.ChromiumError) as e:
        _fail(str(e), 2)
    except KeyboardInterrupt:
        raise typer.Exit(130)
    if debug:
        sys.stderr.write(f"xnb: stats {json.dumps(session.stats)[:2000]}\n")

    by_dst = {m.dst: m for m in mount_objs}
    for out in result.get("mounts") or []:
        m = by_dst.get(out["dst"])
        if m is not None and m.mode == "rw":
            try:
                for p in write_back(m, out["files"]):
                    log(f"xnb: wrote {p}")
            except MountError as e:
                sys.stderr.write(f"xnb: {e}\n")

    nb_json = json.dumps(result["notebook"], indent=1, ensure_ascii=False) + "\n"
    target: Optional[Path] = None
    if output == "-":
        sys.stdout.write(nb_json)
    elif output:
        target = Path(output)
    elif inplace:
        target = src
    elif is_nb:
        target = _default_output(src)
    if target is not None:
        tmp = target.with_name(f".{target.name}.xnb-tmp")
        tmp.write_text(nb_json, encoding="utf-8")
        os.replace(tmp, target)
        if not quiet:
            sys.stderr.write(f"xnb: wrote {target}\n")
    if result.get("status") != "ok":
        if not quiet:
            sys.stderr.write(f"xnb: {result.get('error') or result.get('status')}\n")
        raise typer.Exit(1)


@app.command("setup")
def setup_command(
    from_zip: Annotated[Optional[Path], typer.Option("--from", help="Install from a local chrome-headless-shell zip (still hash-checked).")] = None,
    cache_dir: Annotated[Optional[Path], typer.Option("--cache-dir", help="Cache directory.")] = None,
) -> None:
    """Download and verify the pinned Chromium."""
    cache = Cache(cache_dir) if cache_dir else Cache()
    try:
        exe = chromium.install(cache.ensure(), from_zip=from_zip)
    except chromium.ChromiumError as e:
        _fail(str(e), 2)
    typer.echo(exe)


class CacheAction(str, Enum):
    info = "info"
    clean = "clean"
    prune = "prune"


@app.command("cache")
def cache_command(
    action: Annotated[CacheAction, typer.Argument(help="info: sizes; clean: remove packages and metadata; prune: drop metadata and locks.")],
    all_: Annotated[bool, typer.Option("--all", help="With clean: also remove Chromium.")] = False,
    cache_dir: Annotated[Optional[Path], typer.Option("--cache-dir", help="Cache directory.")] = None,
) -> None:
    """Inspect or clean the xnb cache."""
    cache = Cache(cache_dir) if cache_dir else Cache()
    if action == CacheAction.info:
        typer.echo(json.dumps(cache.info(), indent=1))
        return
    targets = [cache.repodata, cache.locks, cache.tmp]
    if action == CacheAction.clean:
        targets.append(cache.pkgs)
        if all_:
            targets.append(cache.chromium)
    for t in targets:
        shutil.rmtree(t, ignore_errors=True)
        typer.echo(f"removed {t}")


def _version(value: bool) -> None:
    if value:
        typer.echo(f"xnb {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[Optional[bool], typer.Option("--version", callback=_version, is_eager=True, help="Show the version.")] = None,
) -> None:
    """Run notebooks and scripts on sandboxed xeus wasm kernels inside headless Chromium.

    `xnb FILE [OPTIONS]` is short for `xnb run FILE [OPTIONS]`.
    """


def _fail(message: str, code: int) -> "typer.Exit":
    sys.stderr.write(f"xnb: {message}\n")
    raise typer.Exit(code)


COMMANDS = {"run", "setup", "cache"}


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `xnb notebook.ipynb ...` -> `xnb run notebook.ipynb ...`
    if argv and argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version"):
        argv = ["run", *argv]
    # Standalone mode lets Typer print usage errors itself; every outcome ends in
    # SystemExit, so we never need to import click (newer Typer vendors it).
    try:
        app(args=argv, prog_name="xnb", standalone_mode=True)
    except SystemExit as e:
        if e.code is None:
            return 0
        if isinstance(e.code, int):
            return e.code
        sys.stderr.write(f"{e.code}\n")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
