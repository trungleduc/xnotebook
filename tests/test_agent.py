"""`xnb agent`: the host-side model forwarder, write-back with backups, the CLI, and
end-to-end runs in headless Chromium against a scripted model server."""

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fake_llm import FakeLLM, text, tool_call
from xnotebook import cdp, chromium
from xnotebook.agent import AgentSession, Chat, build_agent_job, text_diff
from xnotebook.cache import Cache
from xnotebook.cli import main
from xnotebook.llm import (
    Forwarder,
    LlmError,
    Provider,
    _Sse,
    check_request,
    request_headers,
)
from xnotebook.mounts import parse_mount
from xnotebook.session import WEB_ROOT

ANTHROPIC = Provider("anthropic", "claude-opus-5-5", "https://api.anthropic.com", {"x-api-key": "sk-secret"})
OPENAI = Provider("openai", "gpt-x", "http://127.0.0.1:1/v1", {})


def body(**kw):
    return json.dumps({"model": "claude-opus-5-5", "max_tokens": 100, "messages": [], **kw})


# -- request checks -------------------------------------------------------------
def test_check_request_allows_the_chat_endpoint():
    out = json.loads(check_request(ANTHROPIC, "POST", "/v1/messages?beta=true", body(tools=[{"name": "execute", "input_schema": {}}])))
    assert out["model"] == "claude-opus-5-5"


@pytest.mark.parametrize(
    "method,path,payload,message",
    [
        ("GET", "/v1/messages", body(), "GET is not allowed"),
        ("POST", "/v1/files", body(), "not a model endpoint"),
        ("POST", "/v1/messages", body(model="claude-fable-5-1"), "not this session's model"),
        ("POST", "/v1/messages", body(tools=[{"type": "web_search_20260209", "name": "web_search"}]), "server-side tool"),
        ("POST", "/v1/messages", body(mcp_servers=[{"type": "url", "url": "https://x"}]), "mcp_servers is not allowed"),
        ("POST", "/v1/messages", "not json", "not JSON"),
    ],
)
def test_check_request_refuses(method, path, payload, message):
    with pytest.raises(LlmError, match=message):
        check_request(ANTHROPIC, method, path, payload)


def test_check_request_caps_max_tokens():
    out = json.loads(check_request(ANTHROPIC, "POST", "/v1/messages", body(max_tokens=10_000_000)))
    assert out["max_tokens"] == ANTHROPIC.max_tokens


def test_check_request_openai():
    ok = json.dumps({"model": "gpt-x", "messages": [], "tools": [{"type": "function", "function": {"name": "f"}}], "max_completion_tokens": 10**9})
    assert json.loads(check_request(OPENAI, "POST", "/chat/completions", ok))["max_completion_tokens"] == OPENAI.max_tokens
    with pytest.raises(LlmError, match="web_search_options"):
        check_request(OPENAI, "POST", "/chat/completions", json.dumps({"model": "gpt-x", "web_search_options": {}}))
    with pytest.raises(LlmError, match="tool type"):
        check_request(OPENAI, "POST", "/chat/completions", json.dumps({"model": "gpt-x", "tools": [{"type": "web_search"}]}))
    with pytest.raises(LlmError, match="not a model endpoint"):
        check_request(OPENAI, "POST", "/v1/messages", json.dumps({"model": "gpt-x"}))


def test_request_headers_keep_only_known_betas_and_add_the_key():
    h = request_headers(ANTHROPIC, {"x-api-key": "xnb", "Anthropic-Beta": "interleaved-thinking-2025-05-14,files-api-2025-04-14", "x-stainless-os": "Linux"})
    assert h["x-api-key"] == "sk-secret"
    assert h["anthropic-beta"] == "interleaved-thinking-2025-05-14"
    assert "x-stainless-os" not in h
    oauth = Provider("anthropic", "m", "https://api.anthropic.com", {"authorization": "Bearer tok"})
    assert "oauth-2025-04-20" in request_headers(oauth, {})["anthropic-beta"]


def test_provider_from_env():
    p = Provider.from_env("anthropic", "m", env={"ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "http://127.0.0.1:9/"})
    assert p.auth == {"x-api-key": "k"} and p.base_url == "http://127.0.0.1:9"
    with pytest.raises(LlmError, match="ANTHROPIC_API_KEY"):
        Provider.from_env("anthropic", "m", env={})
    with pytest.raises(LlmError, match="OPENAI_API_KEY"):
        Provider.from_env("openai", "m", env={})
    local = Provider.from_env("openai", "m", "http://localhost:11434/v1", env={})
    assert local.auth == {} and not local.official_openai
    assert Provider.from_env("openai", "m", env={"OPENAI_API_KEY": "k"}).official_openai


def test_sse_usage():
    a = _Sse()
    a.feed('data: {"type":"message_start","message":{"usage":{"input_tokens":10,"cache_read_input_tokens":5,"output_tokens":1}}}\n')
    a.feed('data: {"type":"message_delta","usage":{"output_tokens":7}}\n')
    assert (a.input, a.cache_read, a.output) == (10, 5, 7)
    o = _Sse()
    o.feed('data: {"choices":[],"usage":{"prompt_tokens":30,"completion_tokens":4,"prompt_tokens_details":{"cached_tokens":10}}}\ndata: [DONE]\n')
    assert (o.input, o.cache_read, o.output) == (20, 10, 4)


def _forward(fwd, msg, timeout=10):
    """Run one request through a Forwarder; return the reply parts."""
    parts = []
    done = threading.Event()

    def emit(p):
        parts.append(p)
        if p.get("end") or p.get("error"):
            done.set()

    fwd.emit = emit
    fwd.start(msg)
    assert done.wait(timeout)
    return parts


def test_forwarder_streams_and_counts_usage():
    with FakeLLM([text("hi")]) as fake:
        p = Provider("anthropic", "claude-opus-5-5", fake.url, {"x-api-key": "sk-secret"})
        fwd = Forwarder(p, lambda r: None)
        parts = _forward(fwd, {"reqId": 1, "method": "POST", "path": "/v1/messages", "headers": {"x-api-key": "xnb"}, "body": body()})
        assert parts[0]["head"]["status"] == 200
        assert "hi" in "".join(x.get("chunk", "") for x in parts)
        assert fake.requests[0]["headers"]["x-api-key"] == "sk-secret"
        assert (fwd.usage.requests, fwd.usage.input, fwd.usage.output) == (1, 100, 20)


def test_forwarder_refuses_and_limits():
    with FakeLLM([text("a"), text("b")]) as fake:
        p = Provider("anthropic", "claude-opus-5-5", fake.url, {"x-api-key": "k"})
        fwd = Forwarder(p, lambda r: None, max_requests=1)
        msg = {"reqId": 1, "method": "POST", "path": "/v1/messages", "headers": {}, "body": body()}
        bad = _forward(fwd, {**msg, "body": body(model="other")})
        assert bad[0]["head"]["status"] == 400 and "not this session's model" in bad[1]["chunk"]
        assert _forward(fwd, msg)[0]["head"]["status"] == 200
        limited = _forward(fwd, {**msg, "reqId": 2})
        assert "--max-requests 1" in limited[1]["chunk"]
        assert len(fake.requests) == 1


# -- write-back, backups and diffs ------------------------------------------------
def test_text_diff():
    assert text_diff(None, b"x", "f") is None
    assert text_diff(b"\0", b"x", "f") is None
    d = text_diff(b"a\nb\n", b"a\nc\n", "f.py")
    assert "-b" in d and "+c" in d


def test_writeback_backs_up_originals_once_and_lists_created(tmp_path):
    src = tmp_path / "data"
    src.mkdir()
    (src / "a.txt").write_text("original\n")
    m = parse_mount(f"{src}:/data:rw")
    provider = Provider("anthropic", "m", "http://127.0.0.1:1", {})
    job, _ = build_agent_job(provider=provider, effort=None, max_output_tokens=None, compat={}, images=True, system=None, max_restarts=1)
    events = []
    s = AgentSession(job, [m], provider, on_agent=events.append, cache=Cache(tmp_path / "cache"), log=lambda m: None)

    def files(**contents):
        import base64

        return [{"dst": "/data", "files": [{"path": f"/data/{k}", "data": base64.b64encode(v.encode()).decode()} for k, v in contents.items()]}]

    s._writeback(files(**{"a.txt": "first change\n", "new.txt": "n\n"}))
    s._writeback(files(**{"a.txt": "second change\n", "new.txt": "n2\n"}))
    assert (src / "a.txt").read_text() == "second change\n"
    backup = s.backup_dir / f"0-{src.name}" / "a.txt"
    assert backup.read_text() == "original\n"  # the file as it was before the session
    assert not (s.backup_dir / f"0-{src.name}" / "new.txt").exists()
    assert (s.backup_dir / "created.txt").read_text().strip() == str(src / "new.txt")
    saved = [e for e in events if e["kind"] == "saved"]
    assert saved[0]["diff"] and "+first change" in saved[0]["diff"]


# -- CLI ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "args,message",
    [
        (["agent", "--provider", "openai"], "--model is required"),
        (["agent", "--effort", "huge"], "--effort must be one of"),
        (["agent", "--compat", "nokey"], "--compat expects KEY=VALUE"),
    ],
)
def test_cli_agent_usage_errors(args, message, capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert main(args) == 2
    assert message in capsys.readouterr().err


def test_cli_agent_needs_a_key(capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert main(["agent", "-p", "hi"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


# -- end to end in Chromium ---------------------------------------------------------
def _have_browser() -> bool:
    if not (WEB_ROOT / "agent.worker.js").exists():
        return False
    if os.environ.get("XNB_CHROMIUM_PATH"):
        return True
    try:
        return chromium.installed_path(Cache()).exists()
    except chromium.ChromiumError:
        return False


browser = pytest.mark.skipif(not _have_browser(), reason="Chromium or web bundle not available")


@pytest.fixture
def cdp_payloads(monkeypatch):
    """Every CDP message the host sends to the browser."""
    sent = []
    send, call = cdp.PipeConnection.send, cdp.PipeConnection.call

    def spy_send(self, method, params=None, session_id=None):
        sent.append(json.dumps(params or {}))
        return send(self, method, params, session_id)

    def spy_call(self, method, params=None, **kw):
        sent.append(json.dumps(params or {}))
        return call(self, method, params, **kw)

    monkeypatch.setattr(cdp.PipeConnection, "send", spy_send)
    monkeypatch.setattr(cdp.PipeConnection, "call", spy_call)
    return sent


def _session(provider, mounts=(), cell_timeout=60, max_restarts=3):
    job, mount_objs = build_agent_job(
        provider=provider, effort=None, max_output_tokens=None, compat={}, images=True, system=None,
        max_restarts=max_restarts, mounts=list(mounts), cell_timeout=cell_timeout,
    )
    chat = Chat(out=io.StringIO(), err=io.StringIO(), color=False)
    session = AgentSession(job, mount_objs, provider, on_agent=chat.on_agent, log=lambda m: None, quiet=True)
    chat.session = session
    return job, chat, session


def _run(provider, prompt, **kw):
    job, chat, session = _session(provider, **kw)
    session.start(timeout=600)
    try:
        ok = chat.turn(prompt)
    finally:
        session.close()
    return ok, job, chat, session


@browser
def test_agent_runs_tools_in_the_sealed_kernel(cdp_payloads):
    script = [tool_call("execute", {"code": "print(6*7)"}), text("The answer is 42.")]
    with FakeLLM(script) as fake:
        provider = Provider("anthropic", "claude-opus-5-5", fake.url, {"x-api-key": "sk-never-in-the-browser"})
        ok, job, chat, session = _run(provider, "what is 6*7?")
    assert ok
    assert "42" in fake.tool_results(1)[-1]
    assert chat.out.getvalue().strip() == "The answer is 42."  # only model text on stdout
    assert "● execute" in chat.err.getvalue()
    assert session.stats["requests_after_seal"] == 0
    assert "sk-never-in-the-browser" not in json.dumps(job)
    assert not any("sk-never-in-the-browser" in p for p in cdp_payloads)
    assert fake.requests[0]["headers"]["x-api-key"] == "sk-never-in-the-browser"


@browser
def test_agent_openai_compatible():
    script = [tool_call("execute", {"code": "print(sum(range(5)))"}), text("10")]
    with FakeLLM(script, provider="openai") as fake:
        provider = Provider("openai", "local-model", fake.url + "/v1", {})
        ok, *_ = _run(provider, "sum")
    assert ok
    assert fake.requests[0]["path"] == "/v1/chat/completions"
    first = fake.requests[0]["body"]
    assert first["messages"][0]["role"] == "system"  # not "developer" for non-OpenAI servers
    assert all(t["type"] == "function" for t in first["tools"])
    assert fake.tool_results(1)[-1] == "10\n"


@browser
def test_agent_restarts_a_dead_kernel_that_stays_sealed():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    srv.settimeout(0.2)
    port = srv.getsockname()[1]
    probe = (
        "import js, time\n"
        "out = [str(js.self.crossOriginIsolated)]\n"
        "try:\n"
        "    x = js.XMLHttpRequest.new(); x.open('GET', js.self.location.origin + '/', False); x.send(); out.append('xhr-ok')\n"
        "except Exception:\n"
        "    out.append('xhr-blocked')\n"
        f"ws = js.WebSocket.new('ws://127.0.0.1:{port}/')\n"
        "t = time.time()\n"
        "while time.time() - t < 1: pass\n"
        "out.append(f'ws-state-{ws.readyState}')\n"
        "print(' '.join(out), 'marker' in dir())\n"
    )
    script = [
        tool_call("execute", {"code": "marker = 1\nwhile True: pass"}),
        tool_call("execute", {"code": "print(marker)"}),
        tool_call("restart_kernel", {}),
        tool_call("execute", {"code": probe}),
        text("ok"),
    ]
    with FakeLLM(script) as fake:
        provider = Provider("anthropic", "claude-opus-5-5", fake.url, {"x-api-key": "k"})
        ok, *_ = _run(provider, "go", cell_timeout=3)
    hits = 0
    try:
        while True:
            srv.accept()[0].close()
            hits += 1
    except OSError:
        pass
    srv.close()
    results = [fake.tool_results(i)[-1] for i in range(1, 5)]
    assert "timed out" in results[0]
    assert "call restart_kernel" in results[1]
    assert "fresh kernel started" in results[2]
    # The restarted kernel runs in a nested worker: still isolated, still without network.
    assert results[3].strip() == "True xhr-blocked ws-state-3 False"
    assert hits == 0


@browser
def test_agent_edits_files_and_respects_mount_modes(tmp_path):
    rw = tmp_path / "rw"
    ro = tmp_path / "ro"
    rw.mkdir()
    ro.mkdir()
    (rw / "clean.py").write_text("a = 1\nb = 2\n")
    (ro / "keep.txt").write_text("keep\n")
    script = [
        tool_call("edit", {"path": "/data/clean.py", "edits": [{"oldText": "b = 2", "newText": "b = 3"}]}),
        tool_call("write", {"path": "/data/sub/new.txt", "content": "hello\n"}),
        tool_call("write", {"path": "/ro/keep.txt", "content": "changed"}),
        tool_call("edit", {"path": "/ro/keep.txt", "edits": [{"oldText": "keep", "newText": "x"}]}),
        tool_call("read", {"path": "/data/clean.py"}),
        tool_call("execute", {"code": "open('/data/clean.py', 'a').write('c = 4\\n')"}),
        text("done"),
    ]
    with FakeLLM(script) as fake:
        provider = Provider("anthropic", "claude-opus-5-5", fake.url, {"x-api-key": "k"})
        ok, job, chat, session = _run(provider, "go", mounts=[f"{rw}:/data:rw", f"{ro}:/ro"])
    assert ok
    results = [fake.tool_results(i)[-1] for i in range(1, 7)]
    assert "Saved to the host" in results[0]
    assert (rw / "clean.py").read_text() == "a = 1\nb = 3\nc = 4\n"
    assert (rw / "sub" / "new.txt").read_text() == "hello\n"
    assert "read-only mount" in results[2] and "read-only mount" in results[3]
    assert (ro / "keep.txt").read_text() == "keep\n"
    assert results[4].startswith("a = 1\nb = 3")
    err = chat.err.getvalue()
    assert "-b = 2" in err and "+b = 3" in err and "+c = 4" in err
    assert (session.backup_dir / f"0-{rw.name}" / "clean.py").read_text() == "a = 1\nb = 2\n"
    assert str(rw / "sub" / "new.txt") in (session.backup_dir / "created.txt").read_text()


@browser
def test_cli_agent_prompt(tmp_path):
    with FakeLLM([text("hello from the sandbox")]) as fake:
        env = {**os.environ, "ANTHROPIC_BASE_URL": fake.url, "ANTHROPIC_API_KEY": "k"}
        r = subprocess.run(
            [sys.executable, "-m", "xnotebook", "agent", "-p", "hi", "--save", str(tmp_path / "s.ipynb")],
            env=env, capture_output=True, text=True, timeout=600, cwd=tmp_path,
        )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "hello from the sandbox"
    assert json.loads((tmp_path / "s.ipynb").read_text())["nbformat"] == 4


@browser
def test_interrupt_keeps_the_history_valid():
    script = [tool_call("execute", {"code": "import time\ntime.sleep(2)\nprint('slept')"}), text("unused"), text("second turn")]
    with FakeLLM(script) as fake:
        provider = Provider("anthropic", "claude-opus-5-5", fake.url, {"x-api-key": "k"})
        _, chat, session = _session(provider)
        session.start(timeout=600)
        try:
            chat.begin_turn()
            session.command({"type": "chat", "text": "sleep"})
            assert chat.wait(("tool_start",), timeout=60)
            session.command({"type": "abort"})
            assert chat.wait(("turn_end",), timeout=60)
            assert len(fake.requests) == 1  # no model call after the interrupt
            assert chat.turn("next")
        finally:
            session.close()
    msgs = fake.requests[1]["body"]["messages"]
    uses = [c["id"] for m in msgs if m["role"] == "assistant" for c in m["content"] if c.get("type") == "tool_use"]
    results = [c["tool_use_id"] for m in msgs if m["role"] == "user" and isinstance(m["content"], list) for c in m["content"] if c.get("type") == "tool_result"]
    assert uses == results == ["toolu_1"]
    assert "slept" in fake.tool_results(1)[0]
    # pi sets the effort with an effort-only system message; the turn itself ends with the user.
    assert [m for m in msgs if m["role"] != "system"][-1]["role"] == "user"
