"""On-disk cache layout: ~/.cache/xnb/{chromium,pkgs,repodata,locks,tmp}."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional


def default_cache_dir() -> Path:
    env = os.environ.get("XNB_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "xnb" / "Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "xnb"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "xnb"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Cache:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else default_cache_dir()
        self.pkgs = self.root / "pkgs"
        self.repodata = self.root / "repodata"
        self.locks = self.root / "locks"
        self.chromium = self.root / "chromium"
        self.tmp = self.root / "tmp"

    def ensure(self) -> "Cache":
        for d in (self.pkgs, self.repodata, self.locks, self.chromium, self.tmp):
            d.mkdir(parents=True, exist_ok=True)
        return self

    # -- packages (content addressed, immutable) --------------------------
    def pkg_path(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ValueError(f"invalid sha256: {sha256!r}")
        return self.pkgs / sha256

    def tmp_file(self, suffix: str = "") -> tuple[int, str]:
        return tempfile.mkstemp(dir=self.tmp, suffix=suffix)

    # -- repodata / index metadata (mutable, TTL) -------------------------
    def meta_paths(self, url: str) -> tuple[Path, Path]:
        key = sha256_hex(url.encode())
        return self.repodata / key, self.repodata / (key + ".json")

    def read_meta(self, url: str) -> Optional[dict]:
        body, meta = self.meta_paths(url)
        try:
            info = json.loads(meta.read_text())
        except (OSError, ValueError):
            return None
        if info.get("status") == 200 and not body.exists():
            return None
        return info

    def write_meta(self, url: str, info: dict) -> None:
        _, meta = self.meta_paths(url)
        atomic_write(meta, json.dumps(info).encode(), self.tmp)

    def touch_meta(self, url: str) -> None:
        info = self.read_meta(url)
        if info is not None:
            info["fetched_at"] = time.time()
            self.write_meta(url, info)

    # -- locks ------------------------------------------------------------
    def lock_path(self, lock_id: str) -> Path:
        safe = "".join(c for c in lock_id if c.isalnum() or c in "-_")
        if not safe:
            raise ValueError("empty lock id")
        return self.locks / f"{safe}.json"

    def read_lock(self, lock_id: str) -> Optional[dict]:
        try:
            return json.loads(self.lock_path(lock_id).read_text())
        except (OSError, ValueError):
            return None

    def write_lock(self, lock_id: str, lock: dict) -> None:
        atomic_write(self.lock_path(lock_id), json.dumps(lock, indent=1).encode(), self.tmp)

    # -- maintenance ------------------------------------------------------
    def info(self) -> dict:
        out = {"root": str(self.root)}
        for name in ("chromium", "pkgs", "repodata", "locks"):
            d = getattr(self, name)
            total = 0
            count = 0
            if d.exists():
                for p in d.rglob("*"):
                    if p.is_file():
                        total += p.stat().st_size
                        count += 1
            out[name] = {"files": count, "bytes": total}
        return out


def atomic_write(path: Path, data: bytes, tmp_dir: Optional[Path] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=tmp_dir or path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
