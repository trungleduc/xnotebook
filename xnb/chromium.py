"""Download, verify and launch a pinned chrome-headless-shell (Chrome for Testing)."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .cache import Cache

CFT = "https://storage.googleapis.com/chrome-for-testing-public"
VERSION = "154.0.8037.57"

# platform -> sha256 of chrome-headless-shell-<platform>.zip for VERSION.
# Regenerate with `python tools/pin_chromium.py <version>`.
PINNED = {
    "linux-arm64": "2213770a541c7ea17c900c631bdb6a3cda97ecf5194a34a575f32e54a8f5ab70",
    "linux64": "5a6979d0ab7cf952ea575d35164e7bdce4872b2ced8f8a215c8f8e8eda00ee09",
    "mac-arm64": "9ba4d8a9732bd7e431ce9009d1bbe1ccf7f8c5de2a9781054f500a9fe898124d",
    "mac-x64": "b3f9551d90c9ff0f4c7c231a4dbf226b3c64faba8ced1ab61a5ef9d430d0e996",
    "win32": "441326f70cc25a32fff518ee92f541849f05a2896494e973826366194186c0da",
    "win64": "bcc91b4d0f83a5457fc6ca7941fd65f2349775523d961be88526c6a66560dc75",
}


class ChromiumError(RuntimeError):
    pass


def cft_platform() -> str:
    machine = platform.machine().lower()
    if sys.platform.startswith("linux"):
        return "linux-arm64" if machine in ("aarch64", "arm64") else "linux64"
    if sys.platform == "darwin":
        return "mac-arm64" if machine == "arm64" else "mac-x64"
    if sys.platform == "win32":
        return "win64" if sys.maxsize > 2**32 else "win32"
    raise ChromiumError(f"unsupported platform: {sys.platform}/{machine}")


def download_url(plat: str, version: str = VERSION) -> str:
    return f"{CFT}/{version}/{plat}/chrome-headless-shell-{plat}.zip"


def _exe_rel(plat: str) -> str:
    name = "chrome-headless-shell.exe" if plat.startswith("win") else "chrome-headless-shell"
    return f"chrome-headless-shell-{plat}/{name}"


def installed_path(cache: Cache, plat: Optional[str] = None) -> Path:
    plat = plat or cft_platform()
    return cache.chromium / VERSION / _exe_rel(plat)


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class _FileLock:
    """Cross-process lock based on O_EXCL lock files (stale after 15 minutes)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> "_FileLock":
        deadline = time.time() + 20 * 60
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 15 * 60:
                        self.path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.time() > deadline:
                    raise ChromiumError(f"timed out waiting for {self.path}")
                time.sleep(0.5)

    def __exit__(self, *exc) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _extract(zip_path: Path, dest: Path) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                raise ChromiumError(f"unsafe path in archive: {info.filename}")
            mode = (info.external_attr >> 16) & 0xFFFF
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(mode):
                link = zf.read(info).decode()
                if os.path.isabs(link) or dest.resolve() not in (target.parent / link).resolve().parents:
                    raise ChromiumError(f"unsafe symlink in archive: {info.filename}")
                os.symlink(link, target)
                continue
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            if mode & 0o111:
                os.chmod(target, 0o755)


def _download(url: str, dest: Path, progress: Callable[[str], None]) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "xnb"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last = 0.0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            now = time.time()
            if total and now - last > 0.5:
                last = now
                progress(f"\rxnb: downloading Chromium {VERSION}: {done * 100 // total}% ({total >> 20} MB)")
        if total:
            progress(f"\rxnb: downloading Chromium {VERSION}: 100% ({total >> 20} MB)\n")


def install(
    cache: Cache,
    *,
    from_zip: Optional[Path] = None,
    progress: Callable[[str], None] = lambda m: (sys.stderr.write(m), sys.stderr.flush()),
) -> Path:
    """Ensure the pinned Chromium is installed; return the executable path."""
    plat = cft_platform()
    exe = installed_path(cache, plat)
    if exe.exists():
        return exe
    expected = PINNED.get(plat)
    if expected is None:
        raise ChromiumError(
            f"no pinned Chromium for {plat}; pass --browser-path or set XNB_CHROMIUM_PATH"
        )
    cache.ensure()
    with _FileLock(cache.chromium / f".{VERSION}.lock"):
        if exe.exists():
            return exe
        work = Path(tempfile.mkdtemp(dir=cache.chromium, prefix=f".{VERSION}-"))
        try:
            zip_path = work / "chromium.zip"
            if from_zip is not None:
                shutil.copyfile(from_zip, zip_path)
            else:
                _download(download_url(plat), zip_path, progress)
            got = _hash_file(zip_path)
            if got != expected:
                raise ChromiumError(f"Chromium archive sha256 mismatch: expected {expected}, got {got}")
            out = work / "out"
            out.mkdir()
            _extract(zip_path, out)
            zip_path.unlink()
            final = cache.chromium / VERSION
            try:
                os.replace(out, final)
            except OSError:
                if not exe.exists():
                    raise
        finally:
            shutil.rmtree(work, ignore_errors=True)
    if not exe.exists():
        raise ChromiumError(f"Chromium executable missing after install: {exe}")
    return exe


def resolve(cache: Cache, browser_path: Optional[str] = None) -> Path:
    path = browser_path or os.environ.get("XNB_CHROMIUM_PATH")
    if path:
        p = Path(path).expanduser()
        if not p.exists():
            raise ChromiumError(f"browser not found: {p}")
        return p
    return install(cache)


# -- launching ------------------------------------------------------------

BASE_FLAGS = [
    "--headless",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-gpu",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-sync",
    "--disable-breakpad",
    "--disable-crash-reporter",
    "--disable-domain-reliability",
    "--disable-client-side-phishing-detection",
    "--no-pings",
    "--dns-prefetch-disable",
    "--metrics-recording-only",
    "--mute-audio",
    "--password-store=basic",
    "--use-mock-keychain",
    # Chromium never needs DNS: all real traffic goes to 127.0.0.1 (an IP literal).
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider,"
    "AutofillServerCommunication,CertificateTransparencyComponentUpdater,InterestFeedContentSuggestions",
]

SANDBOX_ERRORS = (
    "No usable sandbox",
    "setuid sandbox",
    "Failed to move to new namespace",
    "zygote_host_impl_linux",
    "credentials.cc",
)


class Browser:
    """A running Chromium process with a CDP pipe connection."""

    def __init__(self, proc: subprocess.Popen, conn, profile: Path, sandboxed: bool) -> None:
        self.proc = proc
        self.conn = conn
        self.profile = profile
        self.sandboxed = sandboxed

    def close(self) -> None:
        try:
            if not self.conn.closed:
                self.conn.send("Browser.close")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self.conn.close()
        shutil.rmtree(self.profile, ignore_errors=True)


def use_pipe() -> bool:
    mode = os.environ.get("XNB_CDP_TRANSPORT", "").lower()
    if mode in ("ws", "websocket"):
        return False
    return os.name == "posix"


def _spawn(exe: Path, flags: List[str], profile: Path) -> Tuple[subprocess.Popen, object]:
    if use_pipe():
        return _spawn_pipe(exe, flags)
    return _spawn_ws(exe, flags, profile)


def _spawn_pipe(exe: Path, flags: List[str]) -> Tuple[subprocess.Popen, object]:
    from .cdp import PipeConnection

    # to_chrome: we write, chromium reads on fd 3. from_chrome: chromium writes on fd 4.
    to_r, to_w = os.pipe()
    from_r, from_w = os.pipe()
    # Hand the pipes over as stdin/stdout of /bin/sh, which moves them to fds 3/4
    # before exec'ing Chromium (no preexec_fn, so this is safe with threads).
    script = 'exec "$0" "$@" 3<&0 4>&1 0</dev/null 1>/dev/null'
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", script, str(exe), "--remote-debugging-pipe", *flags],
            stdin=to_r,
            stdout=from_w,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    finally:
        os.close(to_r)
        os.close(from_w)
    return proc, PipeConnection(to_w, from_r)


def _spawn_ws(exe: Path, flags: List[str], profile: Path) -> Tuple[subprocess.Popen, object]:
    """--remote-debugging-port=0 on loopback; the port is read from DevToolsActivePort."""
    from .cdp import CDPClosed, WebSocketConnection

    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [str(exe), "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1", *flags],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        **kwargs,
    )
    port_file = profile / "DevToolsActivePort"
    deadline = time.time() + 30
    while True:
        if proc.poll() is not None:
            break
        try:
            lines = port_file.read_text().splitlines()
            if len(lines) >= 2 and lines[0].strip().isdigit():
                url = f"ws://127.0.0.1:{int(lines[0])}{lines[1].strip()}"
                return proc, WebSocketConnection(url)
        except OSError:
            pass
        if time.time() > deadline:
            break
        time.sleep(0.05)

    class _Dead:
        closed = True

        def call(self, *a, **k):
            raise CDPClosed("Chromium exited before DevTools was ready")

        def send(self, *a, **k):
            raise CDPClosed("Chromium exited before DevTools was ready")

        def close(self) -> None:
            pass

    return proc, _Dead()


def _missing_libs_hint(exe: Path) -> str:
    try:
        out = subprocess.run(["ldd", str(exe)], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    missing = sorted({line.split()[0] for line in out.splitlines() if "not found" in line})
    if not missing:
        return ""
    hint = "missing shared libraries: " + ", ".join(missing)
    if shutil.which("apt-get"):
        hint += (
            "\n  try: sudo apt-get install -y libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 "
            "libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2"
        )
    elif shutil.which("dnf"):
        hint += "\n  try: sudo dnf install -y nss atk at-spi2-atk cups-libs libxkbcommon libXcomposite libXdamage libXrandr mesa-libgbm pango alsa-lib"
    elif shutil.which("apk"):
        hint += "\n  try: apk add nss atk at-spi2-atk cups-libs libxkbcommon libxcomposite libxdamage libxrandr mesa-gbm pango alsa-lib"
    return hint


def launch(
    exe: Path,
    *,
    strict: bool = False,
    max_memory_mb: Optional[int] = None,
    log: Callable[[str], None] = lambda m: sys.stderr.write(m + "\n"),
) -> Browser:
    """Launch Chromium, preferring the OS sandbox and falling back to --no-sandbox."""
    from .cdp import CDPError

    attempts = [True]
    if not strict:
        attempts.append(False)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # Chromium refuses to sandbox as root.
        if strict:
            raise ChromiumError("refusing to run Chromium without sandbox as root (--strict)")
        attempts = [False]
    last_err = ""
    for sandbox in attempts:
        profile = Path(tempfile.mkdtemp(prefix="xnb-profile-"))
        flags = [*BASE_FLAGS, f"--user-data-dir={profile}"]
        if not sandbox:
            flags.append("--no-sandbox")
        if max_memory_mb:
            flags.append(f"--js-flags=--max-old-space-size={int(max_memory_mb)}")
        flags.append("about:blank")
        proc, conn = _spawn(exe, flags, profile)
        try:
            conn.call("Browser.getVersion", timeout=30)  # type: ignore[attr-defined]
            if not sandbox:
                log("xnb: warning: Chromium OS sandbox unavailable; running with --no-sandbox "
                    "(use --strict to refuse)")
            # Drain stderr in the background so the pipe never fills up.
            _drain(proc)
            return Browser(proc, conn, profile, sandbox)
        except CDPError:
            try:
                proc.kill()
            except OSError:
                pass
            err = b""
            try:
                err = proc.communicate(timeout=5)[1] or b""
            except subprocess.SubprocessError:
                pass
            last_err = err.decode(errors="replace")
            shutil.rmtree(profile, ignore_errors=True)
            if "error while loading shared libraries" in last_err:
                raise ChromiumError(last_err.strip() + "\n" + _missing_libs_hint(exe))
            if sandbox and any(s in last_err for s in SANDBOX_ERRORS):
                continue
            if sandbox and not strict:
                continue
            break
    raise ChromiumError("failed to start Chromium:\n" + last_err.strip()[-4000:])


def _drain(proc: subprocess.Popen) -> None:
    import threading

    def run() -> None:
        stream = proc.stderr
        if stream is None:
            return
        for line in iter(stream.readline, b""):
            if os.environ.get("XNB_DEBUG_BROWSER"):
                sys.stderr.write("[chromium] " + line.decode(errors="replace"))

    threading.Thread(target=run, name="xnb-chromium-stderr", daemon=True).start()
