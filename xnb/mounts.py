"""Host files exposed to the kernel: copied into MEMFS, written back only for rw mounts."""

from __future__ import annotations

import base64
import os
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

MAX_MOUNT_BYTES = 512 << 20
MAX_WRITEBACK_FILE = 256 << 20
MAX_WRITEBACK_TOTAL = 1 << 30


class MountError(ValueError):
    pass


@dataclass
class Mount:
    src: Path
    dst: str
    mode: str  # "ro" | "rw"


def parse_mount(spec: str) -> Mount:
    """`src:dst[:ro|rw]` (dst must be absolute inside the kernel)."""
    mode = "ro"
    parts = spec.rsplit(":", 1)
    if len(parts) == 2 and parts[1] in ("ro", "rw"):
        spec, mode = parts[0], parts[1]
    # src may contain ':' on Windows (C:\...), dst is the part after the last ':'
    idx = spec.rfind(":")
    if idx <= 0:
        raise MountError(f"invalid --mount {spec!r}; expected src:dst[:ro|rw]")
    src, dst = spec[:idx], spec[idx + 1:]
    if not dst.startswith("/"):
        raise MountError(f"mount destination must be absolute: {dst!r}")
    dst = posixpath.normpath(dst)
    if dst == "/" or dst.split("/")[1] in ("bin", "lib", "share", "include", "etc", "dev", "proc", "tmp"):
        raise MountError(f"refusing to mount over {dst!r}")
    p = Path(src).expanduser()
    if not p.exists():
        raise MountError(f"mount source does not exist: {src}")
    return Mount(p.resolve(), dst, mode)


def mount_to_job(m: Mount) -> dict:
    files: List[dict] = []
    dirs: List[str] = []
    total = 0

    def add(path: Path, dst: str) -> None:
        nonlocal total
        size = path.stat().st_size
        total += size
        if total > MAX_MOUNT_BYTES:
            raise MountError(f"mount {m.src} is larger than {MAX_MOUNT_BYTES >> 20} MB")
        files.append({"path": dst, "data": base64.b64encode(path.read_bytes()).decode()})

    if m.src.is_file():
        add(m.src, m.dst)
    else:
        dirs.append(m.dst)
        for root, dnames, fnames in os.walk(m.src, followlinks=False):
            rel = Path(root).relative_to(m.src)
            base = posixpath.join(m.dst, *rel.parts) if rel.parts else m.dst
            for d in dnames:
                if not (Path(root) / d).is_symlink():
                    dirs.append(posixpath.join(base, d))
            for f in fnames:
                fp = Path(root) / f
                if fp.is_symlink() or not fp.is_file():
                    continue
                add(fp, posixpath.join(base, f))
    return {"dst": m.dst, "mode": m.mode, "files": files, "dirs": dirs}


def write_back(m: Mount, files: List[Dict[str, str]]) -> List[Path]:
    """Write files reported by the kernel for an rw mount, confined to the mount source."""
    written: List[Path] = []
    total = 0
    root = m.src if m.src.is_dir() else m.src.parent
    root_real = Path(os.path.realpath(root))
    for f in files:
        kpath = posixpath.normpath(f["path"])
        if m.src.is_file():
            if kpath != m.dst:
                continue
            target = m.src
        else:
            if not (kpath == m.dst or kpath.startswith(m.dst.rstrip("/") + "/")):
                continue
            rel = posixpath.relpath(kpath, m.dst)
            parts = rel.split("/")
            if rel == "." or any(p in ("", ".", "..") for p in parts):
                continue
            target = root.joinpath(*parts)
        data = base64.b64decode(f["data"])
        if len(data) > MAX_WRITEBACK_FILE:
            raise MountError(f"refusing to write back {kpath}: file too large")
        total += len(data)
        if total > MAX_WRITEBACK_TOTAL:
            raise MountError("refusing to write back: total size too large")
        # No symlinks anywhere on the way, and the final path must stay under the root.
        parent = target.parent
        check = parent
        while check != root and root in check.parents:
            if check.is_symlink():
                raise MountError(f"refusing to write through symlink {check}")
            check = check.parent
        parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise MountError(f"refusing to overwrite symlink {target}")
        real_parent = Path(os.path.realpath(parent))
        if real_parent != root_real and root_real not in real_parent.parents:
            raise MountError(f"refusing to write outside the mount: {target}")
        if target.exists() and not target.is_file():
            continue
        if target.exists() and target.read_bytes() == data:
            continue
        tmp = target.with_name(f".{target.name}.xnb-tmp")
        with open(tmp, "wb") as out:
            out.write(data)
        os.replace(tmp, target)
        written.append(target)
    return written
