"""`xnb mcp` protocol and tools, with a fake session (no browser needed)."""

import io
import json
import subprocess
import sys

import pytest

from xnotebook.cli import main
from xnotebook.mcp import TOOLS, Config, Server, report_to_content
from xnotebook.session import RunError


class FakeSession:
    """Evaluates Python expressions; `boom` fails the cell, `hang` ends the session."""

    started = []

    def __init__(self, kernel, deps, pip):
        self.kernel, self.deps, self.pip = kernel, deps, pip
        self.ns = {}
        self.dead = None
        self.closed = False
        self.spec = {"display_name": f"Fake {kernel}"}

    def start(self, timeout=None):
        if self.kernel == "broken":
            raise RunError("no such kernel")
        FakeSession.started.append(self)
        return self

    @property
    def alive(self):
        return self.dead is None and not self.closed

    def execute(self, code, timeout=None):
        if self.dead:
            raise RunError(f"session ended: {self.dead}")
        if code == "hang":
            self.dead = "cell 0 timed out after 1s"
            return {"status": "timeout", "error": self.dead, "outputs": []}
        if code == "boom":
            return {
                "status": "error",
                "error": "ValueError: boom",
                "outputs": [{"output_type": "error", "ename": "ValueError", "evalue": "boom", "traceback": ["\x1b[31mValueError\x1b[0m: boom"]}],
            }
        if "=" in code:
            exec(code, self.ns)
            return {"status": "ok", "outputs": []}
        value = eval(code, self.ns)
        return {"status": "ok", "outputs": [{"output_type": "execute_result", "data": {"text/plain": repr(value)}}]}

    def close(self, timeout=30):
        self.closed = True


@pytest.fixture
def server():
    FakeSession.started = []
    s = Server(Config(max_sessions=2, idle_timeout=None), factory=FakeSession)
    yield s
    s.shutdown()


def rpc(server, method, params=None, id=1):
    msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if id is not None:
        msg["id"] = id
    return server.handle(msg)


def call(server, name, **args):
    reply = rpc(server, "tools/call", {"name": name, "arguments": args})
    assert "result" in reply, reply
    return reply["result"]


def text(result):
    return "".join(c["text"] for c in result["content"] if c["type"] == "text")


def test_initialize_negotiates_version(server):
    res = rpc(server, "initialize", {"protocolVersion": "2025-03-26"})["result"]
    assert res["protocolVersion"] == "2025-03-26"
    assert res["capabilities"] == {"tools": {}}
    assert res["serverInfo"]["name"] == "xnb"
    assert "no network" in res["instructions"]
    newer = rpc(server, "initialize", {"protocolVersion": "2099-01-01"})["result"]
    assert newer["protocolVersion"] == "2025-06-18"


def test_tools_list(server):
    tools = rpc(server, "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["run", "session_start", "session_exec", "session_close"]
    for t in tools:
        assert t["inputSchema"]["type"] == "object"
        assert t["description"]


def test_protocol_errors(server):
    assert rpc(server, "nope")["error"]["code"] == -32601
    assert rpc(server, "notifications/initialized", id=None) is None
    assert rpc(server, "ping")["result"] == {}
    assert rpc(server, "tools/call", {"name": "rm_rf"})["error"]["code"] == -32602
    assert rpc(server, "tools/call", {"name": "run", "arguments": {"code": 1}})["error"]["code"] == -32602
    assert rpc(server, "tools/call", {"name": "run", "arguments": {"code": "1", "deps": "numpy"}})["error"]["code"] == -32602
    assert server.handle({"id": 1, "method": "ping"})["error"]["code"] == -32600
    assert server.handle({"jsonrpc": "2.0", "id": 5, "result": {}}) is None


def test_session_keeps_state(server):
    res = call(server, "session_start", deps=["numpy"], pip=["six"])
    assert not res["isError"]
    assert "session_id: s1" in text(res)
    assert "Fake xpython" in text(res)
    assert "packages: numpy, pip:six" in text(res)
    assert FakeSession.started[0].deps == ["numpy"]
    assert not call(server, "session_exec", session_id="s1", code="x = 41")["isError"]
    assert text(call(server, "session_exec", session_id="s1", code="x + 1")) == "42\n"
    failed = call(server, "session_exec", session_id="s1", code="boom")
    assert failed["isError"]
    assert text(failed) == "ValueError: boom\n"  # ANSI codes stripped
    assert text(call(server, "session_exec", session_id="s1", code="x")) == "41\n"
    assert text(call(server, "session_close", session_id="s1")) == "closed s1"
    assert FakeSession.started[0].closed
    gone = call(server, "session_exec", session_id="s1", code="x")
    assert gone["isError"] and "no session" in text(gone)


def test_timeout_ends_session(server):
    call(server, "session_start")
    res = call(server, "session_exec", session_id="s1", code="hang")
    assert res["isError"]
    assert "session ended" in text(res)
    assert "s1" not in server.sessions
    assert FakeSession.started[0].closed


def test_run_is_one_shot(server):
    res = call(server, "run", code="6 * 7", kernel="xr")
    assert text(res) == "42\n"
    assert FakeSession.started[0].kernel == "xr"
    assert FakeSession.started[0].closed
    assert server.sessions == {}


def test_max_sessions(server):
    call(server, "session_start")
    call(server, "session_start")
    res = call(server, "session_start")
    assert res["isError"] and "too many kernels" in text(res)
    call(server, "session_close", session_id="s1")
    assert not call(server, "session_start")["isError"]


def test_start_failure_frees_slot(server):
    for _ in range(3):
        res = call(server, "session_start", kernel="broken")
        assert res["isError"] and "no such kernel" in text(res)
    assert not call(server, "session_start")["isError"]


def test_report_to_content():
    content, is_error = report_to_content(
        {
            "status": "ok",
            "outputs": [
                {"output_type": "stream", "name": "stdout", "text": "a\n"},
                {"output_type": "stream", "name": "stderr", "text": "warn\n"},
                {"output_type": "display_data", "data": {"image/png": "iVBO\nRw==", "text/plain": "<Figure>"}},
                {"output_type": "display_data", "data": {"text/html": "<b>x</b>"}},
                {"output_type": "execute_result", "data": {"text/plain": ["1", "2"]}},
            ],
        }
    )
    assert not is_error
    assert content == [
        {"type": "text", "text": "a\nwarn\n"},
        {"type": "image", "data": "iVBORw==", "mimeType": "image/png"},
        {"type": "text", "text": "[text/html]\n12\n"},
    ]
    assert report_to_content({"status": "ok", "outputs": []}) == ([{"type": "text", "text": "(no output)"}], False)
    big, _ = report_to_content({"outputs": [{"output_type": "stream", "text": "x" * 50_000}]}, max_text=1000)
    assert len(big[0]["text"]) < 1100
    assert "characters truncated" in big[0]["text"]


def test_serve_writes_json_lines_only():
    out = io.BytesIO()
    server = Server(Config(idle_timeout=None), out=out, factory=FakeSession)
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in lines) + b"not json\n")
    server.serve(stdin)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, None]
    assert replies[2]["error"]["code"] == -32700


def test_stdio_subprocess():
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "xnotebook", "mcp"],
        input="".join(json.dumps(m) + "\n" for m in msgs),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2]
    assert len(replies[1]["result"]["tools"]) == len(TOOLS)
    assert "serving on stdio" in proc.stderr


def test_cli_refuses_rw_mount(tmp_path, capsys):
    assert main(["mcp", "--mount", f"{tmp_path}:/data:rw"]) == 2
    assert "read-only" in capsys.readouterr().err
