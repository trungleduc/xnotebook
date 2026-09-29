"""Minimal Chrome DevTools Protocol client over --remote-debugging-pipe.

Chromium reads commands from fd 3 and writes responses/events to fd 4, each
message being a JSON document terminated by a NUL byte.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, Dict, Optional


class CDPError(RuntimeError):
    pass


class CDPClosed(CDPError):
    pass


EventHandler = Callable[[str, Dict[str, Any], Optional[str]], None]


class _Pending:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: Optional[dict] = None


class PipeConnection:
    """CDP over a pair of OS pipes (write_fd -> chromium fd 3, chromium fd 4 -> read_fd)."""

    def __init__(self, write_fd: int, read_fd: int) -> None:
        self._wfile = os.fdopen(write_fd, "wb", buffering=0)
        self._read_fd = read_fd
        self._send_lock = threading.Lock()
        self._next_id = 0
        self._pending: Dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._handlers: list[EventHandler] = []
        self._closed = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name="xnb-cdp-reader", daemon=True)
        self._reader.start()

    # -- public API ------------------------------------------------------
    def on_event(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def send(self, method: str, params: Optional[dict] = None, session_id: Optional[str] = None) -> int:
        """Send a command without waiting for its result."""
        return self._send(method, params, session_id, None)

    def call(
        self,
        method: str,
        params: Optional[dict] = None,
        session_id: Optional[str] = None,
        timeout: Optional[float] = 60.0,
    ) -> Any:
        """Send a command and wait for its result."""
        pending = _Pending()
        self._send(method, params, session_id, pending)
        if not pending.event.wait(timeout):
            raise CDPError(f"CDP call {method} timed out")
        if pending.error is not None:
            if pending.error.get("code") == "closed":
                raise CDPClosed("browser connection closed")
            raise CDPError(f"{method}: {pending.error.get('message')} {pending.error.get('data', '')}".strip())
        return pending.result

    def close(self) -> None:
        try:
            self._wfile.close()
        except OSError:
            pass

    # -- internals -------------------------------------------------------
    def _send(self, method: str, params: Optional[dict], session_id: Optional[str], pending: Optional[_Pending]) -> int:
        if self._closed.is_set():
            raise CDPClosed("browser connection closed")
        with self._pending_lock:
            self._next_id += 1
            msg_id = self._next_id
            if pending is not None:
                self._pending[msg_id] = pending
        msg: Dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        data = json.dumps(msg, separators=(",", ":")).encode() + b"\0"
        try:
            with self._send_lock:
                self._wfile.write(data)
        except OSError as e:
            self._fail_all()
            raise CDPClosed(str(e)) from e
        return msg_id

    def _read_loop(self) -> None:
        buf = bytearray()
        try:
            while True:
                chunk = os.read(self._read_fd, 1 << 20)
                if not chunk:
                    break
                buf += chunk
                while True:
                    idx = buf.find(b"\0")
                    if idx < 0:
                        break
                    raw = bytes(buf[:idx])
                    del buf[: idx + 1]
                    if raw:
                        self._dispatch(json.loads(raw))
        except OSError:
            pass
        finally:
            try:
                os.close(self._read_fd)
            except OSError:
                pass
            self._fail_all()

    def _dispatch(self, msg: dict) -> None:
        if "id" in msg:
            with self._pending_lock:
                pending = self._pending.pop(msg["id"], None)
            if pending is not None:
                if "error" in msg:
                    pending.error = msg["error"]
                else:
                    pending.result = msg.get("result", {})
                pending.event.set()
            return
        method = msg.get("method")
        if method:
            params = msg.get("params", {})
            session_id = msg.get("sessionId")
            for handler in list(self._handlers):
                try:
                    handler(method, params, session_id)
                except Exception:  # noqa: BLE001 - never kill the reader thread
                    import traceback

                    traceback.print_exc()

    def _fail_all(self) -> None:
        self._closed.set()
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for p in pending:
            p.error = {"code": "closed", "message": "browser connection closed"}
            p.event.set()
