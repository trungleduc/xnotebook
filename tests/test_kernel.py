"""`xnb kernel start`: the ZMQ side, driven by jupyter_client against a fake kernel
(no browser), plus one end-to-end run when Chromium is available."""

import base64
import os
import sys
import threading
import time
import uuid

import pytest

zmq = pytest.importorskip("zmq")
jupyter_client = pytest.importorskip("jupyter_client")

from jupyter_client import BlockingKernelClient  # noqa: E402
from jupyter_client.connect import write_connection_file  # noqa: E402

from xnotebook.kernel import KernelBridge, Wire  # noqa: E402


def kmsg(msg_type, content, parent, channel):
    return {
        "header": {"msg_id": uuid.uuid4().hex, "msg_type": msg_type, "session": "fake", "username": "fake", "date": "", "version": "5.3"},
        "parent_header": parent["header"],
        "metadata": {},
        "content": content,
        "buffers": [],
        "channel": channel,
    }


class FakeBackend:
    """Answers kernel_info and execute; `hang` never finishes, `ask` calls input()."""

    instances = []

    def __init__(self, post):
        self.post = post
        self.killed = None
        self.waiting = None  # execute_request blocked on input()
        FakeBackend.instances.append(self)

    def start(self):
        self.post("ready", {"display_name": "Fake"})

    def kill(self, reason):
        self.killed = reason

    def join(self, timeout):
        pass

    def finish(self, req, text):
        self.post("kmsg", kmsg("stream", {"name": "stdout", "text": text}, req, "iopub"))
        self.post("kmsg", kmsg("execute_reply", {"status": "ok", "execution_count": 1}, req, "shell"))
        self.post("kmsg", kmsg("status", {"execution_state": "idle"}, req, "iopub"))

    def send(self, msg):
        mtype = msg["header"]["msg_type"]
        if mtype == "kernel_info_request":
            content = {"status": "ok", "protocol_version": "5.3", "language_info": {"name": "fake"}}
            self.post("kmsg", kmsg("status", {"execution_state": "busy"}, msg, "iopub"))
            self.post("kmsg", kmsg("kernel_info_reply", content, msg, msg["channel"]))
            self.post("kmsg", kmsg("status", {"execution_state": "idle"}, msg, "iopub"))
        elif mtype == "execute_request":
            code = msg["content"]["code"]
            self.post("kmsg", kmsg("status", {"execution_state": "busy"}, msg, "iopub"))
            if code == "ask":
                self.waiting = msg
                self.post("kmsg", kmsg("input_request", {"prompt": "? ", "password": False}, msg, "stdin"))
            elif code == "bytes":
                out = kmsg("display_data", {"data": {}, "metadata": {}}, msg, "iopub")
                out["buffers"] = [base64.b64encode(b"\x00\x01binary").decode()]
                self.post("kmsg", out)
                self.finish(msg, "")
            elif code != "hang":
                self.finish(msg, f"ran {code}")
        elif mtype == "input_reply" and self.waiting is not None:
            req, self.waiting = self.waiting, None
            self.finish(req, f"got {msg['content']['value']}")


@pytest.fixture
def kernel(tmp_path):
    FakeBackend.instances.clear()
    fname, info = write_connection_file(str(tmp_path / "kernel.json"), ip="127.0.0.1", key=b"secret")
    bridge = KernelBridge(info, FakeBackend, cell_timeout=1.5, log=lambda m: None)
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("code", bridge.serve()), daemon=True)
    thread.start()
    kc = BlockingKernelClient(connection_file=fname)
    kc.load_connection_file()
    kc.start_channels()
    kc.wait_for_ready(timeout=10)
    yield kc, bridge, thread, result
    kc.stop_channels()
    if thread.is_alive():
        bridge.stopping = True
        thread.join(5)


def run(kc, code, **kw):
    outputs = []
    reply = kc.execute_interactive(code, output_hook=outputs.append, timeout=10, **kw)
    return reply["content"], outputs


def test_wire_roundtrip():
    wire = Wire("k")
    msg = {"header": {"msg_id": "1"}, "parent_header": {}, "metadata": {}, "content": {"a": 1}, "buffers": [base64.b64encode(b"xy").decode()]}
    frames = wire.serialize(msg, [b"id"])
    idents, back = wire.parse(frames)
    assert idents == [b"id"] and back == msg
    frames[-2] = b'{"a": 2}'
    with pytest.raises(ValueError):
        wire.parse(frames)


def test_execute_and_info(kernel):
    kc = kernel[0]
    assert kc.kernel_info(reply=True)["content"]["language_info"]["name"] == "fake"
    reply, outputs = run(kc, "x")
    assert reply["status"] == "ok"
    assert [o["content"]["text"] for o in outputs if o["msg_type"] == "stream"] == ["ran x"]


def test_buffers(kernel):
    _, outputs = run(kernel[0], "bytes")
    data = [o for o in outputs if o["msg_type"] == "display_data"]
    assert data and bytes(data[0]["buffers"][0]) == b"\x00\x01binary"


def test_input(kernel):
    kc = kernel[0]
    prompts = []

    def answer(msg):
        prompts.append(msg["content"]["prompt"])
        kc.input("42")

    reply, outputs = run(kc, "ask", allow_stdin=True, stdin_hook=answer)
    assert prompts == ["? "]
    assert [o["content"]["text"] for o in outputs if o["msg_type"] == "stream"] == ["got 42"]


def test_interrupt_restarts(kernel):
    kc = kernel[0]
    msg_id = kc.execute("hang")
    time.sleep(0.3)
    kc.control_channel.send(kc.session.msg("interrupt_request", {}))
    assert kc.get_control_msg(timeout=5)["content"]["status"] == "ok"
    reply = kc.get_shell_msg(timeout=5)
    assert reply["parent_header"]["msg_id"] == msg_id
    assert reply["content"]["status"] == "error" and reply["content"]["ename"] == "KeyboardInterrupt"
    assert run(kc, "y")[0]["status"] == "ok"
    assert len(FakeBackend.instances) == 2 and FakeBackend.instances[0].killed


def test_cell_timeout_restarts(kernel):
    kc = kernel[0]
    msg_id = kc.execute("hang")
    reply = kc.get_shell_msg(timeout=10)
    assert reply["parent_header"]["msg_id"] == msg_id
    assert reply["content"]["ename"] == "TimeoutError"
    assert run(kc, "y")[0]["status"] == "ok"
    assert len(FakeBackend.instances) == 2


def test_shutdown(kernel):
    kc, _, thread, result = kernel
    kc.shutdown()
    assert kc.get_control_msg(timeout=5)["msg_type"] == "shutdown_reply"
    thread.join(10)
    assert not thread.is_alive() and result["code"] == 0


def _have_browser() -> bool:
    from xnotebook import chromium
    from xnotebook.cache import Cache
    from xnotebook.session import WEB_ROOT

    if not (WEB_ROOT / "index.html").exists():
        return False
    if os.environ.get("XNB_CHROMIUM_PATH"):
        return True
    try:
        return chromium.installed_path(Cache()).exists()
    except chromium.ChromiumError:
        return False


@pytest.mark.skipif(not _have_browser(), reason="Chromium or web bundle not available")
def test_browser_kernel(tmp_path, monkeypatch):
    from jupyter_client.manager import start_new_kernel

    from xnotebook.cli import main

    assert main(["kernel", "install", "--name", "xnb-e2e", "--prefix", str(tmp_path)]) == 0
    monkeypatch.setenv("JUPYTER_PATH", str(tmp_path / "share" / "jupyter"))
    km, kc = start_new_kernel(kernel_name="xnb-e2e", startup_timeout=600)
    try:
        assert km.kernel_spec.argv[0] == sys.executable
        reply, outputs = run(kc, "print('hi'); 6 * 7")
        assert reply["status"] == "ok"
        assert any(o["msg_type"] == "execute_result" and o["content"]["data"]["text/plain"] == "42" for o in outputs)
        reply, outputs = run(kc, "print(input('? '))", allow_stdin=True, stdin_hook=lambda m: kc.input("abc"))
        assert "abc" in "".join(o["content"].get("text", "") for o in outputs if o["msg_type"] == "stream")
    finally:
        kc.stop_channels()
        km.shutdown_kernel(now=False)
