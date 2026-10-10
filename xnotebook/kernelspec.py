"""Jupyter kernelspecs that launch a sealed xnb kernel with a fixed environment.

Packages cannot change once a kernel is sealed, so each environment gets its own
kernelspec. The spec directory is self-contained: kernel.json keeps the options under
`metadata.xnb`, and the environment file is copied next to it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .mounts import parse_mount

ENV_FILE = "environment.yaml"
NAME_RE = re.compile(r"^[a-z0-9._-]+$", re.IGNORECASE)

# kernelspec name -> (conda package, language); mirrors KERNELS in web/src/deps.ts
KERNELS = {
    "xpython": ("xeus-python", "python"),
    "xr": ("xeus-r", "R"),
    "xlua": ("xeus-lua", "lua"),
    "xjavascript": ("xeus-javascript", "javascript"),
    "xcpp20": ("xeus-cpp", "C++20"),
    "xcpp17": ("xeus-cpp", "C++17"),
    "xcpp23": ("xeus-cpp", "C++23"),
    "xnelson": ("xeus-nelson", "nelson"),
    "xoctave": ("xeus-octave", "octave"),
}


class KernelSpecError(ValueError):
    pass


def resolve_kernel(kernel: Optional[str]) -> str:
    """Kernel name, python3-style alias or package name -> xeus kernelspec name."""
    name = kernel or "xpython"
    if name in ("python", "python3") or name.startswith("python3"):
        name = "xpython"
    if name in KERNELS:
        return name
    for kname, (pkg, _) in KERNELS.items():
        if pkg == name:
            return kname
    raise KernelSpecError(f"unknown kernel {kernel!r}; expected one of {', '.join(KERNELS)}")


def _user_data_dir() -> Path:
    try:
        from jupyter_core.paths import jupyter_data_dir

        return Path(jupyter_data_dir())
    except ImportError:
        pass
    if os.environ.get("JUPYTER_DATA_DIR"):
        return Path(os.environ["JUPYTER_DATA_DIR"]).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Jupyter"
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "jupyter" if appdata else home / ".jupyter" / "data"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else home / ".local" / "share") / "jupyter"


def kernels_dir(prefix: Optional[Path] = None, sys_prefix: bool = False) -> Path:
    if prefix is not None:
        return Path(prefix).expanduser().resolve() / "share" / "jupyter" / "kernels"
    if sys_prefix:
        return Path(sys.prefix) / "share" / "jupyter" / "kernels"
    return _user_data_dir() / "kernels"


def install(
    name: str,
    *,
    env_file: Optional[Path] = None,
    deps: Iterable[str] = (),
    pip: Iterable[str] = (),
    channels: Iterable[str] = (),
    kernel: Optional[str] = None,
    display_name: Optional[str] = None,
    mounts: Iterable[str] = (),
    cwd: Optional[str] = None,
    cell_timeout: Optional[float] = None,
    max_memory_mb: Optional[int] = None,
    strict: bool = False,
    prefix: Optional[Path] = None,
    sys_prefix: bool = False,
) -> Path:
    """Write kernels/<name>/ and return its path. Replaces an earlier xnb spec of that name."""
    if importlib.util.find_spec("zmq") is None:
        raise KernelSpecError("the kernel needs pyzmq: pip install 'xnotebook[kernel]'")
    if not NAME_RE.match(name):
        raise KernelSpecError(f"invalid kernel name {name!r}; use letters, digits, '.', '_' and '-'")
    kname = resolve_kernel(kernel)
    if env_file is not None and not Path(env_file).is_file():
        raise KernelSpecError(f"no such environment file: {env_file}")
    mount_specs = []
    for spec in mounts:
        m = parse_mount(spec)
        mount_specs.append(f"{m.src}:{m.dst}:{m.mode}")

    target = kernels_dir(prefix, sys_prefix) / name
    existing = target / "kernel.json"
    if target.exists():
        try:
            ours = "xnb" in json.loads(existing.read_text()).get("metadata", {})
        except (OSError, ValueError):
            ours = False
        if not ours:
            raise KernelSpecError(f"{target} exists and is not an xnb kernel; remove it first")
        shutil.rmtree(target)
    target.mkdir(parents=True)

    config: Dict[str, Any] = {
        "kernel": kname,
        "deps": list(deps),
        "pip": list(pip),
        "channels": list(channels),
        "mounts": mount_specs,
        "cwd": cwd,
        "cellTimeout": cell_timeout,
        "maxMemoryMb": max_memory_mb,
        "strict": strict,
        "envFile": None,
    }
    if env_file is not None:
        shutil.copyfile(env_file, target / ENV_FILE)
        config["envFile"] = ENV_FILE
    spec = {
        "argv": [sys.executable, "-m", "xnotebook", "kernel", "start", "--spec-dir", str(target), "-f", "{connection_file}"],
        "display_name": display_name or f"{name} (xnb)",
        "language": KERNELS[kname][1],
        "interrupt_mode": "message",
        "metadata": {"xnb": config},
    }
    existing.write_text(json.dumps(spec, indent=1) + "\n", encoding="utf-8")
    return target
