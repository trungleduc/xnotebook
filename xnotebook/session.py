"""Drive one run: boot Chromium -> hand the job to the page -> relay events -> result.

`InteractiveSession` keeps the kernel up and feeds it one cell at a time.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from collections import deque
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional, Tuple

from . import chromium
from .cache import Cache
from .cdp import CDPError
from .firewall import Firewall
from .proxy import Proxy, origin_of

WEB_ROOT = Path(__file__).parent / "_web"
PIP_ORIGINS = ("https://pypi.org", "https://files.pythonhosted.org")
# conda <-> PyPI name mapping that mambajs loads after every solve.
ALLOWED_PREFIXES = ("https://raw.githubusercontent.com/prefix-dev/parselmouth/main/files/",)
# Hosts a channel host implies (rattler fetches sharded repodata from a sibling host).
RELATED_ORIGINS = {
    "https://prefix.dev": ("https://repo.prefix.dev", "https://shards.prefix.dev"),
    "https://repo.prefix.dev": ("https://prefix.dev", "https://shards.prefix.dev"),
}


class RunError(RuntimeError):
    pass


class Session:
    def __init__(
        self,
        job: Dict[str, Any],
        *,
        cache: Optional[Cache] = None,
        browser_path: Optional[str] = None,
        strict: bool = False,
        offline: bool = False,
        refresh: bool = False,
        timeout: Optional[float] = None,
        max_memory_mb: Optional[int] = None,
        lock_out: Optional[Path] = None,
        on_event: Callable[[dict], None] = lambda e: None,
        log: Callable[[str], None] = lambda m: sys.stderr.write(m + "\n"),
        debug: bool = False,
        verbose: bool = False,
        quiet: bool = False,
    ) -> None:
        self.job = job
        self.cache = (cache or Cache()).ensure()
        self.browser_path = browser_path
        self.strict = strict
        self.offline = offline
        self.refresh = refresh
        self.timeout = timeout
        self.max_memory_mb = max_memory_mb
        self.lock_out = lock_out
        self.on_event = on_event
        self.log = log
        self.debug = debug or bool(os.environ.get("XNB_DEBUG"))
        self.verbose = verbose or self.debug
        self.quiet = quiet and not self.verbose
        self._t0 = time.time()
        self._inbox: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.page_session: Optional[str] = None
        self._page_target: Optional[str] = None
        self._attached = threading.Event()
        self._targets: Dict[str, str] = {}
        self._targets_lock = threading.Lock()
        self.proxy: Optional[Proxy] = None
        self.firewall: Optional[Firewall] = None
        self.browser: Any = None
        self.stats: Dict[str, Any] = {}

    def _dbg(self, msg: str) -> None:
        if self.debug:
            self.log(msg)

    def progress(self, msg: str, verbose: bool = False) -> None:
        """Phase messages (unless quiet); detailed steps only with --verbose, timestamped."""
        if verbose and not self.verbose:
            return
        if self.quiet:
            return
        if self.verbose:
            self.log(f"xnb [{time.time() - self._t0:6.2f}s] {msg}")
        else:
            self.log(f"xnb: {msg}")

    # -- main entry -------------------------------------------------------
    def run(self) -> dict:
        try:
            conn, sid = self._start()
            return self._loop(conn, sid)
        finally:
            self._teardown()

    def _start(self) -> Tuple[Any, str]:
        """Proxy, browser, firewall, page: everything up to the page taking over."""
        if not (WEB_ROOT / "index.html").exists():
            raise RunError(f"web bundle missing at {WEB_ROOT}; build it with `npm run build` in web/")
        self._t0 = time.time()
        exe = chromium.resolve(self.cache, self.browser_path)
        self.progress(f"browser: {exe}", verbose=True)
        proxy = Proxy(self.cache, WEB_ROOT, offline=self.offline, refresh=self.refresh, log=self._dbg)
        proxy.allow_prefixes.update(ALLOWED_PREFIXES)
        proxy.add_file("job.json", json.dumps(self.job).encode())
        proxy.start()
        self.proxy = proxy
        self.progress(f"package proxy on {proxy.origin} (cache {self.cache.root})", verbose=True)
        self.browser = chromium.launch(exe, strict=self.strict, max_memory_mb=self.max_memory_mb, log=self.log)
        conn = self.browser.conn
        self.progress(
            "browser started" + ("" if self.browser.sandboxed else " without OS sandbox"), verbose=True
        )
        fw = Firewall(conn, proxy, log=self._dbg)
        self.firewall = fw
        fw.on_attach(self._on_attach)
        conn.on_event(self._on_event)
        fw.install()
        target = conn.call("Target.createTarget", {"url": "about:blank"})
        self._page_target = target["targetId"]
        deadline = time.time() + 30
        while self.page_session is None:
            with self._targets_lock:
                self.page_session = self._targets.get(self._page_target)
            if self.page_session is None:
                if time.time() > deadline:
                    raise RunError("failed to attach to the page target")
                self._attached.wait(0.05)
                self._attached.clear()
        sid = self.page_session
        conn.call("Runtime.enable", session_id=sid)
        conn.call("Runtime.addBinding", {"name": "__xnbSend"}, session_id=sid)
        conn.call("Inspector.enable", session_id=sid)
        self.progress("firewall installed; loading the runner page", verbose=True)
        nav = conn.call("Page.navigate", {"url": proxy.base + "index.html"}, session_id=sid)
        if nav.get("errorText"):
            raise RunError(f"failed to load the xnb page: {nav['errorText']}")
        return conn, sid

    def _teardown(self) -> None:
        proxy = self.proxy
        if proxy is None:
            return
        if self.firewall is not None:
            self.stats["requests_after_seal"] = self.firewall.requests_after_seal
            self.stats["blocked"] = list(self.firewall.blocked)
        self.stats["upstream_fetches"] = proxy.stats.upstream_fetches
        self.stats["package_hits"] = proxy.stats.package_hits
        self.stats["package_misses"] = proxy.stats.package_misses
        self.stats["proxy_blocked_after_seal"] = proxy.stats.blocked_after_seal
        st = self.stats
        self.progress(
            f"network: {st['upstream_fetches']} upstream fetches, {st['package_hits']} cached / "
            f"{st['package_misses']} downloaded packages, {st.get('requests_after_seal', 0)} requests after seal",
            verbose=True,
        )
        if self.browser is not None:
            self.browser.close()
        proxy.stop()

    # -- CDP events (reader thread: never block here) ----------------------
    def _on_attach(self, sid: str, info: dict) -> None:
        with self._targets_lock:
            self._targets[info.get("targetId", "")] = sid
            self._attached.set()

    def _on_event(self, method: str, params: dict, sid: Optional[str]) -> None:
        if sid is not None and sid != self.page_session:
            if method == "Runtime.consoleAPICalled" or method == "Runtime.exceptionThrown":
                self._inbox.put(("console", params))
            return
        if method == "Runtime.bindingCalled" and params.get("name") == "__xnbSend":
            self._inbox.put(("msg", params.get("payload", "")))
        elif method in ("Inspector.targetCrashed",):
            self._inbox.put(("crash", "page crashed (out of memory?)"))
        elif method == "Target.detachedFromTarget" and params.get("sessionId") == self.page_session:
            self._inbox.put(("crash", "page detached"))
        elif method in ("Runtime.consoleAPICalled", "Runtime.exceptionThrown"):
            self._inbox.put(("console", params))

    # -- main loop --------------------------------------------------------
    def _reply(self, conn, sid: str, msg_id: Any, value: Any = None, error: Optional[str] = None) -> None:
        payload = json.dumps({"id": msg_id, "value": value, "error": error})
        conn.call(
            "Runtime.evaluate",
            {"expression": f"window.__xnbReply({payload})", "awaitPromise": False},
            session_id=sid,
        )

    def _loop(self, conn, sid: str) -> dict:
        deadline = time.time() + self.timeout if self.timeout else None
        while True:
            wait = None if deadline is None else max(0.0, deadline - time.time())
            if wait is not None and wait <= 0:
                raise RunError(f"run timed out after {self.timeout}s")
            try:
                kind, data = self._inbox.get(timeout=min(wait, 1.0) if wait is not None else 1.0)
            except queue.Empty:
                if conn.closed:
                    raise RunError("browser exited unexpectedly")
                continue
            if kind == "crash":
                raise RunError(data)
            if kind == "console":
                self._console(data)
                continue
            if kind in ("exec", "close"):
                self._command(conn, sid, kind, data)
                continue
            msg = json.loads(data)
            mtype = msg.get("type")
            if mtype == "log":
                self.log(msg.get("message", ""))
            elif mtype == "progress":
                self.progress(msg.get("message", ""), verbose=msg.get("level") == "verbose")
            elif mtype == "debug":
                self._dbg(msg.get("message", ""))
            elif mtype == "env":
                self._reply(conn, sid, msg["id"], self._handle_env(msg))
            elif mtype == "lock":
                self._handle_lock(msg)
                self._reply(conn, sid, msg["id"], True)
            elif mtype == "seal":
                assert self.firewall is not None
                self.firewall.seal()
                self.progress(f"seal: firewall now denies all requests ({self.firewall.rewritten} proxied before)", verbose=True)
                self._reply(conn, sid, msg["id"], True)
            elif mtype == "event":
                self.on_event(msg.get("event", {}))
            elif mtype == "next":
                self._next(conn, sid, msg)
            elif mtype == "done":
                return msg["result"]
            elif mtype == "error":
                message = msg.get("message", "unknown error")
                if self.offline and "504" in message:
                    message += "\n(--offline: this environment is not fully cached yet; run once without --offline)"
                raise RunError(message)

    # -- interactive hooks (see InteractiveSession) -------------------------
    def _next(self, conn, sid: str, msg: dict) -> None:
        raise RunError("the page asked for a cell, but this is not an interactive session")

    def _command(self, conn, sid: str, kind: str, data: Any) -> None:
        pass

    def _console(self, params: dict) -> None:
        if not self.debug:
            return
        if "exceptionDetails" in params:
            d = params["exceptionDetails"]
            text = d.get("exception", {}).get("description") or d.get("text")
            self.log(f"[page exception] {text}")
            return
        args = params.get("args", [])
        text = " ".join(str(a.get("value", a.get("description", ""))) for a in args)
        self.log(f"[console.{params.get('type')}] {text}")

    def _handle_env(self, msg: dict) -> dict:
        """The page reports the merged environment; open the upstream allowlist for it."""
        assert self.proxy is not None
        origins = set()
        for url in msg.get("channelUrls", []):
            if url.startswith("https://") or url.startswith("http://127.0.0.1:"):
                origin = origin_of(url)
                origins.add(origin)
                origins.update(RELATED_ORIGINS.get(origin, ()))
        if msg.get("hasPip"):
            origins.update(PIP_ORIGINS)
        self.proxy.allow_origins |= origins
        self._dbg(f"xnb: upstream allowlist: {sorted(self.proxy.allow_origins)}")
        lock = None
        lock_id = msg.get("lockId")
        if lock_id and not self.refresh:
            lock = self.cache.read_lock(lock_id)
        return {"lock": lock}

    def _handle_lock(self, msg: dict) -> None:
        assert self.proxy is not None
        lock = msg["lock"]
        if msg.get("lockId") and msg.get("solved"):
            self.cache.write_lock(msg["lockId"], lock)
        if self.lock_out is not None:
            self.lock_out.write_text(json.dumps(lock, indent=1))
        for f in msg.get("files", []):
            if f.get("sha256"):
                self.proxy.expect(f["url"], f["sha256"], f.get("size"))


class InteractiveSession(Session):
    """A kernel that stays up and runs cells one at a time.

    The page asks for each command with a `next` request (carrying the result of the
    previous one); the session loop runs on a background thread and answers it with code
    from `execute()`, a path from `read_file()`, or `close`.
    """

    def __init__(self, job: Dict[str, Any], **kwargs: Any) -> None:
        job = {**job, "interactive": True, "allowErrors": True, "widgetState": False}
        if job.get("stdin") is None:
            job["stdin"] = []  # input() fails instead of waiting for an answer
        super().__init__(job, **kwargs)
        self._ready = threading.Event()
        self._finished = threading.Event()
        self._exec_lock = threading.Lock()
        self._next_id: Any = None  # the page is waiting for code under this request id
        self._queued: Deque[Tuple[dict, Future]] = deque()
        self._current: Optional[Future] = None
        self._closing = False
        self._thread: Optional[threading.Thread] = None
        self.result: Optional[dict] = None
        self.dead: Optional[str] = None
        self.spec: Optional[dict] = None  # kernelspec of the running kernel
        on_event = self.on_event

        def record(event: dict) -> None:
            if event.get("kind") == "kernel_ready":
                self.spec = event.get("spec")
            on_event(event)

        self.on_event = record

    def start(self, timeout: Optional[float] = None) -> "InteractiveSession":
        """Boot browser and kernel; return once the kernel is ready for the first cell."""
        self._thread = threading.Thread(target=self._main, name="xnb-session", daemon=True)
        self._thread.start()
        deadline = None if timeout is None else time.time() + timeout
        while not self._ready.is_set() and not self._finished.is_set():
            if deadline is not None and time.time() > deadline:
                self.kill(f"session did not start within {timeout}s")
                break
            self._ready.wait(0.1)
        if not self._ready.is_set():
            self._finished.wait(10)
            raise RunError(self.dead or "session failed to start")
        return self

    def execute(self, code: str, timeout: Optional[float] = None) -> dict:
        """Run one cell; return its report: cell, status, error, executionCount, outputs,
        and `mounts` (files under rw mounts that changed during the cell)."""
        return self._send({"code": code}, timeout)

    def read_file(self, path: str, max_bytes: int, timeout: Optional[float] = 60) -> dict:
        """A file of the kernel filesystem: {path, size, data (base64)}, {path, entries} for a
        directory, or {error}."""
        return self._send({"read": path, "maxBytes": max_bytes}, timeout)

    def _send(self, command: dict, timeout: Optional[float]) -> dict:
        with self._exec_lock:
            if self.dead is not None:
                raise RunError(f"session ended: {self.dead}")
            fut: Future = Future()
            self._inbox.put(("exec", (command, fut)))
            try:
                return fut.result(timeout)
            except FutureTimeout:
                self.kill(f"no reply within {timeout}s")
                raise RunError(f"session ended: no reply within {timeout}s") from None

    def close(self, timeout: float = 30) -> Optional[dict]:
        """Stop the kernel and the browser; return the run result (the session as a notebook)."""
        if self._thread is not None and self._thread.is_alive():
            self._inbox.put(("close", None))
            self._thread.join(timeout)
            if self._thread.is_alive():
                self.kill("close timed out")
                self._thread.join(10)
        return self.result

    def kill(self, reason: str) -> None:
        """End the session now; the loop raises and tears the browser down."""
        self._inbox.put(("crash", reason))

    @property
    def alive(self) -> bool:
        return self._ready.is_set() and self.dead is None and not self._finished.is_set()

    # -- session thread -----------------------------------------------------
    def _main(self) -> None:
        try:
            self.result = self.run()
            self.dead = self.result.get("error") or "session closed"
            last = self.result.get("last")
            if self._current is not None and last is not None:
                self._current.set_result(last)
                self._current = None
        except BaseException as e:  # noqa: B036 - reported to every waiter
            self.dead = str(e) or type(e).__name__
        finally:
            for fut in [self._current, *(f for _, f in self._queued)]:
                if fut is not None and not fut.done():
                    fut.set_exception(RunError(f"session ended: {self.dead}"))
            self._current = None
            self._queued.clear()
            self._finished.set()

    def _next(self, conn, sid: str, msg: dict) -> None:
        if self._current is not None:
            self._current.set_result(msg.get("result"))
            self._current = None
        self._next_id = msg["id"]
        self._ready.set()
        self._dispatch(conn, sid)

    def _command(self, conn, sid: str, kind: str, data: Any) -> None:
        if kind == "close":
            self._closing = True
        else:
            self._queued.append(data)
        self._dispatch(conn, sid)

    def _dispatch(self, conn, sid: str) -> None:
        if self._next_id is None:
            return
        if self._closing:
            self._reply(conn, sid, self._next_id, {"close": True})
        elif self._queued:
            command, fut = self._queued.popleft()
            self._current = fut
            self._reply(conn, sid, self._next_id, command)
        else:
            return
        self._next_id = None
