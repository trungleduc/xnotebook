"""`xnb kernel start`: a Jupyter kernel process backed by a sealed xnb kernel.

Jupyter talks to this process over ZMQ (the kernel wire protocol). Messages are relayed
as they are to the xeus kernel in the browser page, and the kernel's messages come back
the same way, so comms (widgets), completion and inspection work unchanged.

The wasm kernel cannot be interrupted. An interrupt, a cell timeout or a crash restarts
it: running and queued cells get an error reply, other requests are sent again to the
new kernel, and the frontend sees the kernel come back with a fresh state.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import queue
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import zmq

from .mounts import Mount, MountError, write_back
from .session import Session

DELIM = b"<IDS|MSG>"
WAKE = "inproc://xnb-wake"


class Wire:
    """Signing and (de)serialization of kernel protocol messages; buffers travel as base64."""

    def __init__(self, key: str, scheme: str = "hmac-sha256") -> None:
        self.key = key.encode()
        if not scheme.startswith("hmac-"):
            raise ValueError(f"unsupported signature scheme {scheme!r}")
        self.digest = getattr(hashlib, scheme[5:])

    def sign(self, parts: List[bytes]) -> bytes:
        if not self.key:
            return b""
        h = hmac.new(self.key, digestmod=self.digest)
        for p in parts:
            h.update(p)
        return h.hexdigest().encode()

    def parse(self, frames: List[bytes]) -> "tuple[List[bytes], dict]":
        i = frames.index(DELIM)
        signature, parts, buffers = frames[i + 1], frames[i + 2:i + 6], frames[i + 6:]
        if len(parts) != 4:
            raise ValueError("incomplete message")
        if self.key and not hmac.compare_digest(signature, self.sign(parts)):
            raise ValueError("invalid signature")
        header, parent, metadata, content = (json.loads(p) for p in parts)
        msg = {
            "header": header,
            "parent_header": parent,
            "metadata": metadata,
            "content": content,
            "buffers": [base64.b64encode(b).decode() for b in buffers],
        }
        return frames[:i], msg

    def serialize(self, msg: dict, idents: List[bytes] = ()) -> List[bytes]:
        keys = ("header", "parent_header", "metadata", "content")
        parts = [json.dumps(msg.get(k) or {}, default=str).encode() for k in keys]
        buffers = [base64.b64decode(b) for b in msg.get("buffers") or []]
        return [*idents, DELIM, self.sign(parts), *parts, *buffers]


class BridgeSession(Session):
    """A browser kernel in bridge mode; reports to `post(kind, data)` from its own thread:
    ("ready", spec), ("kmsg", message), ("mounts", mounts), ("dead", reason)."""

    def __init__(self, job: Dict[str, Any], post: Callable[..., None], **kwargs: Any) -> None:
        job = {**job, "bridge": True, "allowErrors": True, "widgetState": False, "stdin": None}
        super().__init__(job, **kwargs)
        self.post = post
        self._thread: Optional[threading.Thread] = None
        on_event = self.on_event

        def record(event: dict) -> None:
            if event.get("kind") == "kernel_ready":
                post("ready", event.get("spec"))
            on_event(event)

        self.on_event = record

    def start(self) -> None:
        self._thread = threading.Thread(target=self._main, name="xnb-kernel", daemon=True)
        self._thread.start()

    def send(self, msg: dict) -> None:
        self._inbox.put(("kmsg", msg))

    def kill(self, reason: str) -> None:
        self._inbox.put(("crash", reason))

    def join(self, timeout: float) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _main(self) -> None:
        try:
            result = self.run()
            reason = result.get("error") or "kernel exited"
        except BaseException as e:  # noqa: B036 - reported to the bridge
            reason = str(e) or type(e).__name__
        self.post("dead", reason)

    def _command(self, conn, sid: str, kind: str, data: Any) -> None:
        if kind == "kmsg":
            conn.call(
                "Runtime.evaluate",
                {"expression": f"window.__xnbKernel({json.dumps(data)})", "awaitPromise": False},
                session_id=sid,
            )

    def _page_message(self, msg: dict) -> None:
        self.post(msg["type"], msg.get("msg") if msg["type"] == "kmsg" else msg.get("mounts"))


@dataclass
class Request:
    """A frontend request waiting for its reply."""

    channel: str
    idents: List[bytes]
    msg: dict
    started: Optional[float] = None  # execute_request: when the kernel went busy on it

    @property
    def msg_type(self) -> str:
        return self.msg["header"]["msg_type"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class KernelBridge:
    """Serve one Jupyter connection file, relaying to a backend from `backend_factory(post)`
    (a BridgeSession in production) with start(), send(msg), kill(reason), join(timeout)."""

    def __init__(
        self,
        connection: dict,
        backend_factory: Callable[[Callable[..., None]], Any],
        *,
        mounts: List[Mount] = (),
        cell_timeout: Optional[float] = None,
        log: Callable[[str], None] = lambda m: sys.stderr.write(m + "\n"),
    ) -> None:
        self.wire = Wire(connection.get("key", ""), connection.get("signature_scheme", "hmac-sha256"))
        self.backend_factory = backend_factory
        self.mounts = {m.dst: m for m in mounts}
        self.cell_timeout = cell_timeout
        self.log = log
        self.session_id = uuid.uuid4().hex
        self.ctx = zmq.Context()
        self._events: "queue.Queue[tuple]" = queue.Queue()
        self.wake = self.ctx.socket(zmq.PULL)
        self.wake.bind(WAKE)
        # One socket for every backend thread; the lock serializes its use.
        self._waker = self.ctx.socket(zmq.PUSH)
        self._waker.connect(WAKE)
        self._waker_lock = threading.Lock()

        def bind(kind: int, port_key: str) -> zmq.Socket:
            sock = self.ctx.socket(kind)
            sock.linger = 1000
            transport, ip, port = connection.get("transport", "tcp"), connection["ip"], connection[port_key]
            sock.bind(f"tcp://{ip}:{port}" if transport == "tcp" else f"ipc://{ip}-{port}")
            return sock

        self.shell = bind(zmq.ROUTER, "shell_port")
        self.control = bind(zmq.ROUTER, "control_port")
        self.stdin = bind(zmq.ROUTER, "stdin_port")
        self.iopub = bind(zmq.PUB, "iopub_port")
        self._hb_stop = threading.Event()
        self._hb = threading.Thread(target=self._heartbeat, args=(bind(zmq.REP, "hb_port"),), name="xnb-hb", daemon=True)
        self._hb.start()

        self.backend: Any = None
        self.generation = 0
        self.ready = False
        self.started_once = False
        self.backlog: List[dict] = []
        self.pending: Dict[str, Request] = {}  # msg_id -> request, in arrival order
        self.execution_count = 0
        self.stopping = False
        self.exit_code = 0

    # -- threads --------------------------------------------------------------
    def _heartbeat(self, sock: zmq.Socket) -> None:
        try:
            while not self._hb_stop.is_set():
                if sock.poll(200):
                    sock.send_multipart(sock.recv_multipart())
        finally:
            sock.close(0)

    def _post(self, generation: int, kind: str, data: Any) -> None:
        """Called from backend threads; the main loop picks the event up."""
        self._events.put((generation, kind, data))
        with self._waker_lock:
            if self._waker.closed:
                return
            try:
                self._waker.send(b"", zmq.NOBLOCK)
            except zmq.Again:
                pass  # the loop is already awake

    # -- main loop ------------------------------------------------------------
    def serve(self) -> int:
        poller = zmq.Poller()
        for sock in (self.shell, self.control, self.stdin, self.wake):
            poller.register(sock, zmq.POLLIN)
        channels = {self.shell: "shell", self.control: "control", self.stdin: "stdin"}
        self._boot()
        try:
            while not self.stopping:
                for sock, _ in poller.poll(self._poll_timeout()):
                    if sock is self.wake:
                        while self.wake.poll(0):
                            self.wake.recv()
                    else:
                        self._from_frontend(channels[sock], sock.recv_multipart())
                self._drain()
                self._check_timeout()
        finally:
            self.close()
        return self.exit_code

    def close(self) -> None:
        if self.backend is not None:
            self.backend.kill("kernel shut down")
            self.backend.join(10)
            self.backend = None
        self._hb_stop.set()
        self._hb.join(2)
        with self._waker_lock:
            self._waker.close(0)
        for sock in (self.wake, self.shell, self.control, self.stdin, self.iopub):
            sock.close()
        self.ctx.term()

    def _poll_timeout(self) -> int:
        if not self.cell_timeout:
            return 1000
        starts = [r.started for r in self.pending.values() if r.started is not None]
        if not starts:
            return 1000
        left = min(starts) + self.cell_timeout - time.time()
        return max(10, min(1000, int(left * 1000)))

    def _boot(self) -> None:
        self.generation += 1
        gen = self.generation
        self.ready = False
        self.backend = self.backend_factory(lambda kind, data: self._post(gen, kind, data))
        self.backend.start()

    # -- frontend -> kernel -----------------------------------------------------
    def _from_frontend(self, channel: str, frames: List[bytes]) -> None:
        try:
            idents, msg = self.wire.parse(frames)
        except (ValueError, KeyError) as e:
            self.log(f"xnb: dropped a {channel} message: {e}")
            return
        mtype = msg["header"].get("msg_type", "")
        if channel == "control" and mtype == "shutdown_request":
            restart = bool(msg["content"].get("restart"))
            self._send(self.control, "shutdown_reply", {"status": "ok", "restart": restart}, msg, idents)
            self.stopping = True
            return
        if channel == "control" and mtype == "interrupt_request":
            self._send(self.control, "interrupt_reply", {"status": "ok"}, msg, idents)
            if any(r.msg_type == "execute_request" for r in self.pending.values()):
                self._restart("KeyboardInterrupt", "xnb kernels cannot be interrupted; the kernel was restarted")
            return
        if channel == "stdin":
            if self.ready:
                self.backend.send({**msg, "channel": "stdin"})
            return
        msg["channel"] = channel
        if mtype.endswith("_request"):
            self.pending[msg["header"]["msg_id"]] = Request(channel, idents, msg)
        self._forward(msg)

    def _forward(self, msg: dict) -> None:
        if self.ready:
            self.backend.send(msg)
        else:
            self.backlog.append(msg)

    # -- kernel -> frontend -----------------------------------------------------
    def _drain(self) -> None:
        while True:
            try:
                gen, kind, data = self._events.get_nowait()
            except queue.Empty:
                return
            if gen != self.generation:
                continue  # from a kernel that was replaced
            if kind == "ready":
                self._on_ready(data)
            elif kind == "kmsg":
                self._from_kernel(data)
            elif kind == "mounts":
                self._write_back(data)
            elif kind == "dead":
                self._on_dead(data)

    def _on_ready(self, spec: Any) -> None:
        self.ready = True
        self.started_once = True
        self.log(f"xnb: kernel ready ({(spec or {}).get('display_name', '?')})")
        self._publish("status", {"execution_state": "idle"}, None)
        backlog, self.backlog = self.backlog, []
        for msg in backlog:
            self.backend.send(msg)

    def _on_dead(self, reason: str) -> None:
        self.ready = False
        if self.stopping:
            return
        self.backend = None
        if not self.started_once:
            self.log(f"xnb: the kernel failed to start: {reason}")
            self.exit_code = 1
            self.stopping = True
            return
        self.log(f"xnb: the kernel died ({reason}); restarting it")
        self._restart("KernelDied", f"the kernel died ({reason}) and was restarted")

    def _from_kernel(self, msg: dict) -> None:
        header = msg.get("header") or {}
        mtype = header.get("msg_type", "")
        content = msg.get("content") or {}
        parent_id = (msg.get("parent_header") or {}).get("msg_id")
        req = self.pending.get(parent_id) if parent_id else None
        if "execution_count" in content and isinstance(content["execution_count"], int):
            self.execution_count = max(self.execution_count, content["execution_count"])
        if mtype == "input_request":
            if req is None or not req.msg["content"].get("allow_stdin", True):
                reply = self._message("input_reply", {"value": "", "status": "error"}, msg)
                self.backend.send({**reply, "channel": "stdin"})
            else:
                self.stdin.send_multipart(self.wire.serialize(msg, req.idents))
            return
        if mtype.endswith("_reply") and msg.get("channel") != "iopub":
            if req is None:
                return  # nobody to route it to
            sock = self.control if req.channel == "control" else self.shell
            sock.send_multipart(self.wire.serialize(msg, req.idents))
            del self.pending[parent_id]
            return
        if mtype == "status" and req is not None and req.msg_type == "execute_request":
            if content.get("execution_state") == "busy":
                req.started = time.time()
        self.iopub.send_multipart([mtype.encode(), *self.wire.serialize(msg)])

    def _write_back(self, mounts: List[dict]) -> None:
        for out in mounts or []:
            m = self.mounts.get(out.get("dst"))
            if m is None or m.mode != "rw":
                continue
            try:
                for p in write_back(m, out.get("files") or []):
                    self.log(f"xnb: wrote {p}")
            except MountError as e:
                self.log(f"xnb: {e}")

    # -- restart ----------------------------------------------------------------
    def _check_timeout(self) -> None:
        if not self.cell_timeout:
            return
        for req in self.pending.values():
            if req.started is not None and time.time() - req.started > self.cell_timeout:
                self._restart("TimeoutError", f"cell execution timed out after {self.cell_timeout:g}s; the kernel was restarted")
                return

    def _restart(self, ename: str, evalue: str) -> None:
        """Fail running and queued cells, then replace the kernel; other requests are
        sent again to the new one."""
        resend: List[dict] = []
        for req in list(self.pending.values()):
            if req.msg_type != "execute_request":
                resend.append(req.msg)
                continue
            error = {"ename": ename, "evalue": evalue, "traceback": [f"{ename}: {evalue}"]}
            self._publish("error", error, req.msg)
            content = {"status": "error", "execution_count": self.execution_count, **error}
            self._send(self.shell, "execute_reply", content, req.msg, req.idents)
            self._publish("status", {"execution_state": "idle"}, req.msg)
            del self.pending[req.msg["header"]["msg_id"]]
        # Messages without replies (comms) belong to the old kernel; requests are re-sent.
        self.backlog = resend
        if self.backend is not None:
            self.backend.kill(evalue)
            self.backend = None
        self.execution_count = 0
        self._publish("status", {"execution_state": "starting"}, None)
        self._boot()

    # -- sending ------------------------------------------------------------------
    def _message(self, msg_type: str, content: dict, parent: Optional[dict]) -> dict:
        return {
            "header": {
                "msg_id": uuid.uuid4().hex,
                "msg_type": msg_type,
                "username": "xnb",
                "session": self.session_id,
                "date": _now(),
                "version": "5.3",
            },
            "parent_header": (parent or {}).get("header") or {},
            "metadata": {},
            "content": content,
            "buffers": [],
        }

    def _send(self, sock: zmq.Socket, msg_type: str, content: dict, parent: dict, idents: List[bytes]) -> None:
        sock.send_multipart(self.wire.serialize(self._message(msg_type, content, parent), idents))

    def _publish(self, msg_type: str, content: dict, parent: Optional[dict]) -> None:
        self.iopub.send_multipart([msg_type.encode(), *self.wire.serialize(self._message(msg_type, content, parent))])


def start(spec_dir: Path, connection_file: Path, **session_kwargs: Any) -> int:
    """Run the kernel described by an `xnb kernel install` spec until Jupyter shuts it down."""
    from .api import build_job
    from .cache import Cache

    spec = json.loads((spec_dir / "kernel.json").read_text())
    config = spec.get("metadata", {}).get("xnb", {})
    env_file = spec_dir / config["envFile"] if config.get("envFile") else None
    job, mounts = build_job(
        content="",
        filename="kernel",
        env_file=env_file,
        deps=config.get("deps") or [],
        pip=config.get("pip") or [],
        channels=config.get("channels") or [],
        kernel=config.get("kernel"),
        mounts=config.get("mounts") or [],
        cwd=config.get("cwd"),
    )
    cache_dir = session_kwargs.pop("cache_dir", None)
    kwargs = {
        "cache": Cache(cache_dir) if cache_dir else None,
        "strict": bool(config.get("strict")),
        "max_memory_mb": config.get("maxMemoryMb"),
        **session_kwargs,
    }
    connection = json.loads(connection_file.read_text())
    bridge = KernelBridge(
        connection,
        lambda post: BridgeSession(job, post, **kwargs),
        mounts=mounts,
        cell_timeout=config.get("cellTimeout"),
    )

    def terminate(signum: int, frame: Any) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    return bridge.serve()
