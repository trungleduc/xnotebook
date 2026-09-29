"""Python API: build a job, run it in a Session, return the executed notebook."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

from .cache import Cache
from .mounts import Mount, mount_to_job, parse_mount, write_back
from .session import RunError, Session


class CellError(RuntimeError):
    """Raised by run(..., raise_on_error=True) when a cell fails."""

    def __init__(self, message: str, notebook: dict, cell: Optional[int]) -> None:
        super().__init__(message)
        self.notebook = notebook
        self.cell = cell


def build_job(
    source: Union[str, Path, dict],
    *,
    filename: Optional[str] = None,
    env_file: Optional[Union[str, Path]] = None,
    deps: Iterable[str] = (),
    pip: Iterable[str] = (),
    channels: Iterable[str] = (),
    kernel: Optional[str] = None,
    lock: Optional[Union[str, Path, dict]] = None,
    stdin: Optional[Union[str, Path, List[str]]] = None,
    mounts: Iterable[Union[str, Mount]] = (),
    allow_errors: bool = False,
    cell_timeout: Optional[float] = None,
    cwd: Optional[str] = None,
    widget_state: bool = True,
) -> tuple[dict, List[Mount]]:
    if isinstance(source, dict):
        content = json.dumps(source)
        path = filename or "notebook.ipynb"
    else:
        p = Path(source)
        content = p.read_text(encoding="utf-8")
        path = filename or p.name
    fmt = "ipynb" if path.lower().endswith(".ipynb") else "script"
    lock_obj = None
    if isinstance(lock, dict):
        lock_obj = lock
    elif lock is not None:
        lock_obj = json.loads(Path(lock).read_text())
    stdin_lines = None
    if isinstance(stdin, list):
        stdin_lines = [str(s) for s in stdin]
    elif stdin is not None:
        stdin_lines = Path(stdin).read_text().splitlines()
    mount_objs = [m if isinstance(m, Mount) else parse_mount(m) for m in mounts]
    job: Dict[str, Any] = {
        "path": path,
        "format": fmt,
        "content": content,
        "envYaml": Path(env_file).read_text() if env_file else None,
        "deps": list(deps),
        "pip": list(pip),
        "channels": list(channels),
        "kernel": kernel,
        "lock": lock_obj,
        "stdin": stdin_lines,
        "mounts": [mount_to_job(m) for m in mount_objs],
        "allowErrors": allow_errors,
        "cellTimeout": cell_timeout,
        "cwd": cwd or "/home/xnb",
        "widgetState": widget_state,
    }
    return job, mount_objs


def run(
    source: Union[str, Path, dict],
    *,
    raise_on_error: bool = False,
    timeout: Optional[float] = None,
    offline: bool = False,
    refresh: bool = False,
    strict: bool = False,
    browser_path: Optional[str] = None,
    max_memory_mb: Optional[int] = None,
    lock_out: Optional[Union[str, Path]] = None,
    cache_dir: Optional[Union[str, Path]] = None,
    on_event: Callable[[dict], None] = lambda e: None,
    log: Optional[Callable[[str], None]] = None,
    return_result: bool = False,
    verbose: bool = False,
    **job_options: Any,
) -> dict:
    """Execute a notebook (path or nbformat dict) or a script; return the executed notebook.

    Keyword options mirror the CLI: env_file, deps, pip, channels, kernel, lock,
    stdin, mounts, allow_errors, cell_timeout, cwd, widget_state.
    """
    job, mounts = build_job(source, **job_options)
    kwargs: Dict[str, Any] = {}
    if log is not None:
        kwargs["log"] = log
    session = Session(
        job,
        cache=Cache(Path(cache_dir)) if cache_dir else None,
        browser_path=browser_path,
        strict=strict,
        offline=offline,
        refresh=refresh,
        timeout=timeout,
        max_memory_mb=max_memory_mb,
        lock_out=Path(lock_out) if lock_out else None,
        on_event=on_event,
        verbose=verbose,
        quiet=log is None and not verbose,
        **kwargs,
    )
    result = session.run()
    result["stats"] = session.stats
    by_dst = {m.dst: m for m in mounts}
    for out in result.get("mounts") or []:
        m = by_dst.get(out["dst"])
        if m is not None and m.mode == "rw":
            write_back(m, out["files"])
    if raise_on_error and result.get("status") != "ok":
        raise CellError(result.get("error") or "execution failed", result["notebook"], result.get("failedCell"))
    return result if return_result else result["notebook"]


__all__ = ["run", "build_job", "CellError", "RunError"]
