"""Minimal Chrome DevTools Protocol client.

Two transports share the same client:
  * PipeConnection (POSIX): --remote-debugging-pipe. Chromium reads commands from
    fd 3 and writes responses/events to fd 4, NUL-terminated JSON documents.
  * WebSocketConnection (Windows, or XNB_CDP_TRANSPORT=ws): --remote-debugging-port=0
    on 127.0.0.1, with a stdlib-only WebSocket client.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
import urllib.parse
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


class Connection:
    """Transport-independent CDP client; subclasses implement _write/_read_messages/_close."""

    def __init__(self) -> None:
        self._send_lock = threading.Lock()
        self._next_id = 0
        self._pending: Dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._handlers: list[EventHandler] = []
        self._closed = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name="xnb-cdp-reader", daemon=True)

    def _start_reader(self) -> None:
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
            self._close()
        except OSError:
            pass

    # -- transport hooks --------------------------------------------------
    def _write(self, data: bytes) -> None:
        raise NotImplementedError

    def _read_messages(self):  # generator of raw JSON bytes
        raise NotImplementedError

    def _close(self) -> None:
        raise NotImplementedError

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
        data = json.dumps(msg, separators=(",", ":")).encode()
        try:
            with self._send_lock:
                self._write(data)
        except OSError as e:
            self._fail_all()
            raise CDPClosed(str(e)) from e
        return msg_id

    def _read_loop(self) -> None:
        try:
            for raw in self._read_messages():
                if raw:
                    self._dispatch(json.loads(raw))
        except (OSError, ValueError):
            pass
        finally:
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


class PipeConnection(Connection):
    """CDP over a pair of OS pipes (write_fd -> chromium fd 3, chromium fd 4 -> read_fd)."""

    def __init__(self, write_fd: int, read_fd: int) -> None:
        self._wfile = os.fdopen(write_fd, "wb", buffering=0)
        self._read_fd = read_fd
        super().__init__()
        self._start_reader()

    def _write(self, data: bytes) -> None:
        self._wfile.write(data + b"\0")

    def _read_messages(self):
        buf = bytearray()
        try:
            while True:
                chunk = os.read(self._read_fd, 1 << 20)
                if not chunk:
                    return
                buf += chunk
                while True:
                    idx = buf.find(b"\0")
                    if idx < 0:
                        break
                    raw = bytes(buf[:idx])
                    del buf[: idx + 1]
                    yield raw
        finally:
            try:
                os.close(self._read_fd)
            except OSError:
                pass

    def _close(self) -> None:
        self._wfile.close()


class WebSocketConnection(Connection):
    """CDP over a WebSocket (RFC 6455 client, text frames, masked as required)."""

    def __init__(self, ws_url: str, timeout: float = 30.0) -> None:
        u = urllib.parse.urlsplit(ws_url)
        if u.scheme != "ws" or u.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise CDPError(f"refusing non-loopback DevTools endpoint: {ws_url}")
        self._sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (f"?{u.query}" if u.query else "")
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise CDPClosed("DevTools closed the connection during the handshake")
            head += chunk
        header, _, rest = head.partition(b"\r\n\r\n")
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise CDPError(f"WebSocket handshake failed: {header[:200]!r}")
        self._rbuf = bytearray(rest)
        self._sock.settimeout(None)
        super().__init__()
        self._start_reader()

    def _write(self, data: bytes) -> None:
        self._sock.sendall(self._frame(0x1, data))

    @staticmethod
    def _frame(opcode: int, data: bytes) -> bytes:
        header = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytearray(data)
        for i in range(n):
            masked[i] ^= mask[i & 3]
        return bytes(header) + bytes(masked)

    def _recv_exact(self, n: int) -> bytes:
        while len(self._rbuf) < n:
            chunk = self._sock.recv(max(1 << 16, n - len(self._rbuf)))
            if not chunk:
                raise OSError("connection closed")
            self._rbuf += chunk
        out = bytes(self._rbuf[:n])
        del self._rbuf[:n]
        return out

    def _read_messages(self):
        message = bytearray()
        while True:
            b0, b1 = self._recv_exact(2)
            opcode = b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                (n,) = struct.unpack("!H", self._recv_exact(2))
            elif n == 127:
                (n,) = struct.unpack("!Q", self._recv_exact(8))
            mask = self._recv_exact(4) if b1 & 0x80 else None
            payload = bytearray(self._recv_exact(n))
            if mask:
                for i in range(n):
                    payload[i] ^= mask[i & 3]
            if opcode == 0x8:  # close
                return
            if opcode == 0x9:  # ping -> pong
                with self._send_lock:
                    self._sock.sendall(self._frame(0xA, bytes(payload)))
                continue
            if opcode in (0x1, 0x2, 0x0):
                message += payload
                if b0 & 0x80:
                    yield bytes(message)
                    message = bytearray()

    def _close(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
