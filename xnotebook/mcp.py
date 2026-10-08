"""`xnb mcp`: a Model Context Protocol server over stdio.

JSON-RPC 2.0, one message per line on stdin/stdout. Hand-rolled on purpose: tools only,
stdio only, no dependencies. stdout carries protocol messages only; logs go to stderr.

Tools run code on the same sandboxed kernels as `xnb run`: either one-shot (`run`) or in
an `InteractiveSession` that keeps its state between calls (`session_*`). Host files and
other server-wide settings come from the command line, never from the model.
"""

from __future__ import annotations

import itertools
import json
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Set, Tuple

from . import __version__

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_TEXT = 20_000
MAX_IMAGES = 8
IMAGE_TYPES = ("image/png", "image/jpeg")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

SANDBOX = (
    "Code runs in a WebAssembly kernel inside a sandboxed browser: no network, no subprocesses, "
    "no host files except the read-only mounts the server was started with. Packages are "
    "emscripten-forge conda packages (`deps`, e.g. numpy, pandas, matplotlib) and pure-Python "
    "pip wheels (`pip`), installed when the kernel starts."
)
INSTRUCTIONS = (
    f"{SANDBOX} Use `run` for one-off snippets and `session_start` / `session_exec` when state "
    "should persist between calls. A session's packages are fixed once it starts: start a new "
    "session to add packages. A cell that times out ends its session."
)

_KERNEL = {
    "type": "string",
    "description": "Kernel: xpython (Python, default), xr (R), xlua (Lua), xjavascript (JavaScript).",
    "default": "xpython",
}
_DEPS = {
    "type": "array",
    "items": {"type": "string"},
    "description": "emscripten-forge conda packages, e.g. [\"numpy\", \"pandas>=2\"].",
}
_PIP = {"type": "array", "items": {"type": "string"}, "description": "Pure-Python pip packages."}
_CODE = {"type": "string", "description": "Code to execute."}

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "run",
        "description": "Run code once in a fresh sandboxed kernel and return its outputs (text, "
        f"plots as images, errors). Nothing persists after the call. {SANDBOX}",
        "inputSchema": {
            "type": "object",
            "properties": {"code": _CODE, "kernel": _KERNEL, "deps": _DEPS, "pip": _PIP},
            "required": ["code"],
        },
    },
    {
        "name": "session_start",
        "description": "Start a sandboxed kernel that keeps variables between `session_exec` calls; "
        f"returns its session_id. Packages are fixed for the session's lifetime. {SANDBOX}",
        "inputSchema": {
            "type": "object",
            "properties": {"kernel": _KERNEL, "deps": _DEPS, "pip": _PIP},
        },
    },
    {
        "name": "session_exec",
        "description": "Execute code in a session (like a notebook cell) and return its outputs. "
        "A cell that times out ends the session.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}, "code": _CODE},
            "required": ["session_id", "code"],
        },
    },
    {
        "name": "session_close",
        "description": "Stop a session and free its kernel.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    },
]


class ProtocolError(Exception):
    """A JSON-RPC error response (bad params, unknown method, ...)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class ToolError(Exception):
    """A failed tool call: reported to the model as a result with isError."""


@dataclass
class Config:
    mounts: List[str] = field(default_factory=list)
    cwd: Optional[str] = None
    channels: List[str] = field(default_factory=list)
    cell_timeout: Optional[float] = 120
    idle_timeout: Optional[float] = 900
    start_timeout: float = 600
    max_sessions: int = 4
    max_memory_mb: Optional[int] = None
    strict: bool = False
    offline: bool = False
    cache_dir: Optional[Path] = None
    browser_path: Optional[str] = None
    debug: bool = False


def _log(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def default_factory(config: Config) -> Callable[[str, List[str], List[str]], Any]:
    """Build an InteractiveSession (not started) for kernel + packages."""
    from .api import build_job
    from .cache import Cache
    from .session import InteractiveSession

    def make(kernel: str, deps: List[str], pip: List[str]) -> Any:
        job, _ = build_job(
            content="",
            filename="session.py",
            kernel=kernel,
            deps=deps,
            pip=pip,
            channels=config.channels,
            mounts=config.mounts,
            cwd=config.cwd,
            cell_timeout=config.cell_timeout,
        )
        return InteractiveSession(
            job,
            cache=Cache(config.cache_dir) if config.cache_dir else None,
            browser_path=config.browser_path,
            strict=config.strict,
            offline=config.offline,
            max_memory_mb=config.max_memory_mb,
            log=_log,
            debug=config.debug,
        )

    return make


def _text(value: Any) -> str:
    return "".join(value) if isinstance(value, list) else str(value)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head, tail = limit * 3 // 4, limit // 4
    return f"{text[:head]}\n... [{len(text) - head - tail} characters truncated] ...\n{text[-tail:]}"


def report_to_content(report: Optional[dict], max_text: int = MAX_TEXT) -> Tuple[List[dict], bool]:
    """Turn a cell report (nbformat outputs + status) into MCP content items."""
    report = report or {}
    items: List[dict] = []
    texts: List[str] = []  # text since the last image, merged into one item
    images = 0

    def flush() -> None:
        if texts:
            items.append({"type": "text", "text": "".join(texts)})
            texts.clear()

    for out in report.get("outputs") or []:
        otype = out.get("output_type")
        if otype == "stream":
            texts.append(_text(out.get("text", "")))
        elif otype in ("execute_result", "display_data"):
            data = out.get("data") or {}
            mime = next((m for m in IMAGE_TYPES if m in data), None)
            if mime is not None and images < MAX_IMAGES:
                flush()
                items.append({"type": "image", "data": re.sub(r"\s", "", _text(data[mime])), "mimeType": mime})
                images += 1
            elif mime is not None:
                texts.append(f"[{mime} omitted: more than {MAX_IMAGES} images]\n")
            elif "text/plain" in data:
                texts.append(_text(data["text/plain"]).rstrip("\n") + "\n")
            elif data:
                texts.append(f"[{', '.join(sorted(data))}]\n")
        elif otype == "error":
            tb = [ANSI.sub("", _text(t)) for t in out.get("traceback") or []]
            texts.append(("\n".join(tb) if tb else f"{out.get('ename')}: {out.get('evalue')}") + "\n")
    status = report.get("status", "ok")
    if status in ("timeout", "dead"):
        texts.append(f"session ended: {report.get('error') or status}; start a new session to continue\n")
    flush()
    budget = max_text
    for item in items:
        if item["type"] == "text":
            item["text"] = _truncate(item["text"], max(budget, 200))
            budget -= len(item["text"])
    if not items:
        items.append({"type": "text", "text": "(no output)"})
    return items, status != "ok"


class _Managed:
    def __init__(self, session: Any, kernel: str) -> None:
        self.session = session
        self.kernel = kernel
        self.last_used = time.time()
        self.busy = 0


class Server:
    def __init__(
        self,
        config: Optional[Config] = None,
        out: Optional[BinaryIO] = None,
        factory: Optional[Callable[[str, List[str], List[str]], Any]] = None,
    ) -> None:
        self.config = config or Config()
        self.out = out
        self.factory = factory or default_factory(self.config)
        self.sessions: Dict[str, _Managed] = {}
        self._oneshots: Set[Any] = set()  # kernels of `run` calls in flight
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._slots = 0  # sessions running or starting (one browser each)
        self._stop = threading.Event()

    # -- transport ----------------------------------------------------------
    def send(self, msg: dict) -> None:
        assert self.out is not None
        data = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._write_lock:
            self.out.write(data)
            self.out.flush()

    def serve(self, stdin: BinaryIO) -> None:
        """Read requests until EOF; tool calls run on their own threads."""
        reaper = threading.Thread(target=self._reap, name="xnb-mcp-reaper", daemon=True)
        reaper.start()
        try:
            for line in stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError as e:
                    self.send(_error(None, -32700, f"parse error: {e}"))
                    continue
                if isinstance(msg, dict) and msg.get("method") == "tools/call" and "id" in msg:
                    threading.Thread(target=self._respond, args=(msg,), daemon=True).start()
                else:
                    self._respond(msg)
        finally:
            self.shutdown()

    def _respond(self, msg: Any) -> None:
        reply = self.handle(msg)
        if reply is not None:
            self.send(reply)

    # -- dispatch -----------------------------------------------------------
    def handle(self, msg: Any) -> Optional[dict]:
        """One JSON-RPC message in, the response out (None for notifications)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
        method = msg.get("method")
        if not isinstance(method, str):
            return None  # a response to something we never sent
        is_request = "id" in msg
        params = msg.get("params") or {}
        try:
            if not isinstance(params, dict):
                raise ProtocolError(-32602, "params must be an object")
            if method == "initialize":
                result: Any = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self._call(params)
            elif not is_request:
                return None  # notifications/initialized, notifications/cancelled, ...
            else:
                raise ProtocolError(-32601, f"method not found: {method}")
        except ProtocolError as e:
            return _error(msg.get("id"), e.code, str(e)) if is_request else None
        except Exception as e:  # never let one request take the server down
            _log(f"xnb mcp: internal error in {method}: {e!r}")
            return _error(msg.get("id"), -32603, f"internal error: {e}") if is_request else None
        return {"jsonrpc": "2.0", "id": msg["id"], "result": result} if is_request else None

    def _initialize(self, params: dict) -> dict:
        asked = params.get("protocolVersion")
        return {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "xnb", "version": __version__},
            "instructions": INSTRUCTIONS,
        }

    def _call(self, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise ProtocolError(-32602, "arguments must be an object")
        tool = getattr(self, f"_tool_{name}", None) if name in {t["name"] for t in TOOLS} else None
        if tool is None:
            raise ProtocolError(-32602, f"unknown tool: {name}")
        try:
            content, is_error = tool(args)
        except ToolError as e:
            content, is_error = [{"type": "text", "text": str(e)}], True
        return {"content": content, "isError": is_error}

    # -- tools --------------------------------------------------------------
    def _tool_run(self, args: dict) -> Tuple[List[dict], bool]:
        code = _arg_str(args, "code")
        session = self._start(args)
        with self._lock:
            self._oneshots.add(session)
        try:
            return report_to_content(self._execute(session, code))
        finally:
            with self._lock:
                owned = session in self._oneshots
                self._oneshots.discard(session)
            if owned:
                self._stop_session(session)

    def _tool_session_start(self, args: dict) -> Tuple[List[dict], bool]:
        kernel = _arg_str(args, "kernel", "xpython")
        session = self._start(args)
        with self._lock:
            sid = f"s{next(self._ids)}"
            self.sessions[sid] = _Managed(session, kernel)
        spec = getattr(session, "spec", None) or {}
        lines = [f"session_id: {sid}", f"kernel: {spec.get('display_name') or kernel}"]
        packages = _arg_list(args, "deps") + [f"pip:{p}" for p in _arg_list(args, "pip")]
        if packages:
            lines.append("packages: " + ", ".join(packages))
        return [{"type": "text", "text": "\n".join(lines)}], False

    def _tool_session_exec(self, args: dict) -> Tuple[List[dict], bool]:
        sid = _arg_str(args, "session_id")
        code = _arg_str(args, "code")
        with self._lock:
            managed = self.sessions.get(sid)
            if managed is not None:
                managed.busy += 1
        if managed is None:
            raise ToolError(f"no session {sid!r} (closed, or ended after being idle); start a new one")
        try:
            report = self._execute(managed.session, code)
        finally:
            with self._lock:
                managed.busy -= 1
                managed.last_used = time.time()
        if not managed.session.alive:
            self._drop(sid)
        return report_to_content(report)

    def _tool_session_close(self, args: dict) -> Tuple[List[dict], bool]:
        sid = _arg_str(args, "session_id")
        if not self._drop(sid):
            raise ToolError(f"no session {sid!r}")
        return [{"type": "text", "text": f"closed {sid}"}], False

    # -- sessions -----------------------------------------------------------
    def _start(self, args: dict) -> Any:
        kernel = _arg_str(args, "kernel", "xpython")
        deps = _arg_list(args, "deps")
        pip = _arg_list(args, "pip")
        with self._lock:
            if self._slots >= self.config.max_sessions:
                raise ToolError(
                    f"too many kernels running (max {self.config.max_sessions}); close a session first"
                )
            self._slots += 1
        try:
            session = self.factory(kernel, deps, pip)
            session.start(timeout=self.config.start_timeout)
            return session
        except Exception as e:
            with self._lock:
                self._slots -= 1
            raise ToolError(f"kernel failed to start: {e}") from None

    def _execute(self, session: Any, code: str) -> dict:
        from .session import RunError

        timeout = self.config.cell_timeout + 30 if self.config.cell_timeout else None
        try:
            return session.execute(code, timeout=timeout)
        except RunError as e:
            return {"status": "dead", "error": str(e).replace("session ended: ", ""), "outputs": []}

    def _stop_session(self, session: Any) -> None:
        try:
            session.close()
        finally:
            with self._lock:
                self._slots -= 1

    def _drop(self, sid: str) -> bool:
        with self._lock:
            managed = self.sessions.pop(sid, None)
        if managed is None:
            return False
        self._stop_session(managed.session)
        return True

    def _reap(self) -> None:
        idle = self.config.idle_timeout
        while not self._stop.wait(min(30.0, idle) if idle else 30.0):
            now = time.time()
            with self._lock:
                stale = [
                    sid
                    for sid, m in self.sessions.items()
                    if m.busy == 0 and (not m.session.alive or (idle and now - m.last_used > idle))
                ]
            for sid in stale:
                _log(f"xnb mcp: closing idle session {sid}")
                self._drop(sid)

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            sids = list(self.sessions)
            oneshots = list(self._oneshots)
            self._oneshots.clear()
        for sid in sids:
            self._drop(sid)
        for session in oneshots:
            self._stop_session(session)


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _arg_str(args: dict, name: str, default: Optional[str] = None) -> str:
    value = args.get(name, default)
    if not isinstance(value, str):
        raise ProtocolError(-32602, f"`{name}` must be a string")
    return value


def _arg_list(args: dict, name: str) -> List[str]:
    value = args.get(name) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ProtocolError(-32602, f"`{name}` must be a list of strings")
    return value


def serve_stdio(config: Config) -> None:
    """Serve on this process's stdin/stdout until EOF or SIGTERM."""
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # a stray print() must never corrupt the protocol stream
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = Server(config, out=out)
    _log(f"xnb mcp {__version__}: serving on stdio")
    try:
        server.serve(sys.stdin.buffer)
    except KeyboardInterrupt:
        pass
