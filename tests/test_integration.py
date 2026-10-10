"""End-to-end runs in headless Chromium (network needed on a cold cache).

Skipped unless Chromium is available (installed in the xnb cache, or XNB_CHROMIUM_PATH).
Set XNB_CACHE_DIR to share a warm cache between runs.
"""

import json
import os
import textwrap

import pytest

import xnotebook
from xnotebook import chromium
from xnotebook.cache import Cache
from xnotebook.session import WEB_ROOT


def _have_browser() -> bool:
    if not (WEB_ROOT / "index.html").exists():
        return False
    if os.environ.get("XNB_CHROMIUM_PATH"):
        return True
    try:
        return chromium.installed_path(Cache()).exists()
    except chromium.ChromiumError:
        return False


pytestmark = pytest.mark.skipif(not _have_browser(), reason="Chromium or web bundle not available")


def nb(*cells, **metadata):
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {"kernelspec": {"name": "xpython", "display_name": "Python"}, **metadata},
        "cells": [
            {"cell_type": "code", "source": textwrap.dedent(c), "metadata": {}, "outputs": [], "execution_count": None}
            for c in cells
        ],
    }


def texts(cell):
    out = []
    for o in cell["outputs"]:
        if o["output_type"] == "stream":
            out.append(o["text"])
        elif o["output_type"] in ("execute_result", "display_data"):
            out.append(o["data"].get("text/plain", ""))
        elif o["output_type"] == "error":
            out.append(f"{o['ename']}: {o['evalue']}")
    return "".join(out)


def test_basic_notebook(tmp_path):
    stdin = tmp_path / "stdin.txt"
    stdin.write_text("World\n")
    result = xnotebook.run(
        nb(
            "print('hello'); 1 + 1",
            "import numpy as np\nint(np.arange(3).sum())",
            "from IPython.display import display, HTML\nh = display(HTML('<b>v1</b>'), display_id=True)\nh.update(HTML('<b>v2</b>'))",
            "import asyncio\nawait asyncio.sleep(0.05)\nprint('awaited')",
            "print('hi', input())",
            "1/0",
            "print('not reached')",
            xnb={"dependencies": ["numpy"]},
        ),
        stdin=str(stdin),
        return_result=True,
    )
    out = result["notebook"]
    cells = out["cells"]
    assert texts(cells[0]) == "hello\n2"
    assert texts(cells[1]) == "3"
    assert cells[2]["outputs"][0]["data"]["text/html"] == "<b>v2</b>"
    assert texts(cells[3]) == "awaited\n"
    assert texts(cells[4]) == "hi World\n"
    assert cells[5]["outputs"][0]["ename"] == "ZeroDivisionError"
    assert cells[6]["outputs"] == [] and cells[6]["execution_count"] is None
    assert result["status"] == "error" and result["failedCell"] == 5
    assert out["metadata"]["language_info"]["name"] == "python"
    assert result["stats"]["requests_after_seal"] == 0
    try:
        import nbformat

        nbformat.validate(nbformat.from_dict(out))
    except ImportError:
        pass


def test_escape_attempts_blocked():
    probe = """
        import js
        r = js.eval('''(async () => {
          const real = WorkerGlobalScope.prototype.fetch;
          const out = [];
          const proxied = location.href.replace('kernel.worker.js', 'u/' + encodeURIComponent('https://prefix.dev/'));
          for (const u of ['https://example.com/', proxied, location.href]) {
            try { await real.call(self, u); out.push('ESCAPED ' + u); } catch (e) { out.push('blocked'); }
          }
          try { const x = new XMLHttpRequest(); x.open('GET', 'https://example.com', false); x.send(); out.push('ESCAPED xhr'); }
          catch (e) { out.push('blocked'); }
          try { importScripts('https://example.com/x.js'); out.push('ESCAPED importScripts'); } catch (e) { out.push('blocked'); }
          const w = new Worker(URL.createObjectURL(new Blob(["fetch('https://example.com').then(()=>postMessage('ESCAPED nested'),()=>postMessage('blocked'))"])));
          out.push(await new Promise(res => { w.onmessage = e => res(e.data); w.onerror = () => res('blocked'); }));
          const ws = new WebSocket('wss://example.com');
          out.push(await new Promise(res => { ws.onopen = () => res('ESCAPED ws'); ws.onerror = () => res('blocked'); ws.onclose = () => res('blocked'); }));
          out.push(typeof __xnbSend === 'undefined' ? 'blocked' : 'ESCAPED binding');
          return out.join(',');
        })()''')
        print(await r)
    """
    fs = """
        import os
        try:
            open('/etc/passwd').read(); print('ESCAPED passwd')
        except OSError:
            print('blocked')
        print('HOME' in os.environ and os.environ['HOME'] != os.path.expanduser('~root'))
    """
    result = xnotebook.run(nb(probe, fs), allow_errors=True, return_result=True)
    cells = result["notebook"]["cells"]
    assert "ESCAPED" not in texts(cells[0]), texts(cells[0])
    assert set(texts(cells[0]).strip().split(",")) == {"blocked"}
    assert texts(cells[1]).startswith("blocked")
    assert result["stats"]["requests_after_seal"] == 0
    assert result["stats"]["proxy_blocked_after_seal"] == 0


def test_script_pep723_mounts_and_lock(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "in.txt").write_text("input")
    out = tmp_path / "out"
    out.mkdir()
    script = tmp_path / "s.py"
    script.write_text(
        textwrap.dedent(
            """\
            # /// script
            # dependencies = ["six"]
            # ///
            # %%
            import six
            print(six.__name__, open('/data/in.txt').read())
            # %%
            open('/data/in.txt', 'w').write('changed')
            open('/out/result.txt', 'w').write('from kernel')
            """
        )
    )
    lock_out = tmp_path / "lock.json"
    result = xnotebook.run(
        script,
        mounts=[f"{data}:/data", f"{out}:/out:rw"],
        lock_out=lock_out,
        return_result=True,
    )
    cells = result["notebook"]["cells"]
    assert texts(cells[0]) == "six input\n"
    assert (data / "in.txt").read_text() == "input"  # ro: untouched
    assert (out / "result.txt").read_text() == "from kernel"
    lock = json.loads(lock_out.read_text())
    assert any(p.startswith("six-") for p in lock["pipPackages"])
    # Reuse the lock offline: everything comes from the cache
    again = xnotebook.run(script, lock=lock_out, offline=True, mounts=[f"{data}:/data", f"{out}:/out:rw"], return_result=True)
    assert again["status"] == "ok"
    assert again["stats"]["upstream_fetches"] == 0


def test_cell_timeout(tmp_path):
    result = xnotebook.run(nb("print('a')", "while True: pass", "print('b')"), cell_timeout=3, return_result=True)
    assert result["status"] == "timeout" and result["failedCell"] == 1
    assert result["notebook"]["cells"][1]["outputs"][-1]["ename"] == "TimeoutError"
    assert result["notebook"]["cells"][2]["outputs"] == []


def test_lua_kernel(tmp_path):
    f = tmp_path / "hello.lua"
    f.write_text('print("lua", 1 + 2)\n')
    result = xnotebook.run(f, return_result=True)
    assert texts(result["notebook"]["cells"][0]) == "lua 3\n"
    assert result["notebook"]["metadata"]["kernelspec"]["name"] == "xlua"


def test_widget_state_saved():
    import pathlib

    result = xnotebook.run(
        nb(
            "import ipywidgets as w\ns = w.IntSlider(value=3)\ns",
            "s.value = 7\ntmp = w.Checkbox()\ndisplay(tmp)\ntmp.close()",
            "w.Image(value=b'\\x89PNG\\r\\n', format='png')",
            xnb={"dependencies": ["ipywidgets"]},
        ),
        return_result=True,
    )
    out = result["notebook"]
    doc = out["metadata"]["widgets"]["application/vnd.jupyter.widget-state+json"]
    views = [
        o["data"]["application/vnd.jupyter.widget-view+json"]["model_id"]
        for c in out["cells"]
        for o in c["outputs"]
        if "application/vnd.jupyter.widget-view+json" in o.get("data", {})
    ]
    slider, checkbox, image = views
    assert doc["state"][slider]["state"]["value"] == 7
    assert checkbox not in doc["state"]
    assert doc["state"][image]["buffers"][0]["path"] == ["value"]
    try:
        import jsonschema
    except ImportError:
        return
    schema = json.loads((pathlib.Path(__file__).parent / "widget_state.schema.json").read_text())
    jsonschema.validate(doc, schema)


def test_widget_state_disabled():
    result = xnotebook.run(
        nb("import ipywidgets as w\nw.IntSlider()", xnb={"dependencies": ["ipywidgets"]}),
        widget_state=False,
        return_result=True,
    )
    assert "widgets" not in result["notebook"]["metadata"]


def test_interactive_session():
    from xnotebook.api import build_job
    from xnotebook.session import InteractiveSession, RunError

    job, _ = build_job(content="", filename="session.py", kernel="xpython", cell_timeout=5)
    session = InteractiveSession(job, quiet=True).start(timeout=300)
    try:
        assert session.spec["language"] == "python"
        assert session.execute("x = 41\nprint('hi')")["outputs"] == [
            {"output_type": "stream", "name": "stdout", "text": "hi\n"}
        ]
        report = session.execute("x + 1")
        assert report["status"] == "ok"
        assert report["outputs"][0]["data"]["text/plain"] == "42"
        failed = session.execute("1/0")
        assert failed["status"] == "error"
        assert failed["error"].startswith("ZeroDivisionError")
        assert session.execute("x")["outputs"][0]["data"]["text/plain"] == "41"  # still alive
        blocked = session.execute("import urllib.request\nurllib.request.urlopen('https://example.com')")
        assert blocked["status"] == "error"
        timed_out = session.execute("while True: pass")
        assert timed_out["status"] == "timeout"
        assert not session.alive
        with pytest.raises(RunError, match="session ended"):
            session.execute("1")
    finally:
        result = session.close()
    assert len(result["notebook"]["cells"]) == 6
    assert result["status"] == "timeout"


def test_mcp_server_tools():
    from xnotebook.mcp import Config, Server

    server = Server(Config(cell_timeout=30, idle_timeout=None))
    try:
        def call(name, **args):
            return server.handle(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
            )["result"]

        started = call("session_start", deps=["matplotlib"])
        assert not started["isError"], started
        sid = started["content"][0]["text"].split()[1]
        call("session_exec", session_id=sid, code="import matplotlib.pyplot as plt\nxs = list(range(5))")
        plot = call("session_exec", session_id=sid, code="plt.plot(xs, xs); plt.show()")
        assert [c["type"] for c in plot["content"]] == ["image"]
        assert plot["content"][0]["mimeType"] == "image/png"
        assert call("session_exec", session_id=sid, code="sum(xs)")["content"] == [{"type": "text", "text": "10\n"}]
        assert call("session_close", session_id=sid)["content"][0]["text"] == f"closed {sid}"
        once = call("run", code="print(6 * 7)")
        assert once["content"] == [{"type": "text", "text": "42\n"}]
        assert server.sessions == {}
    finally:
        server.shutdown()


def test_session_files(tmp_path):
    from xnotebook.mcp import Config, Server

    out = tmp_path / "out"
    out.mkdir()
    (out / "kept.txt").write_text("from the host")
    server = Server(Config(mounts=[f"{out}:/out:rw"], cell_timeout=30, idle_timeout=None))
    try:
        def call(name, **args):
            return server.handle(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
            )["result"]

        sid = call("session_start", deps=["matplotlib"])["content"][0]["text"].split()[1]
        first = call("session_exec", session_id=sid, code="open('/out/a.txt', 'w').write('one')")
        assert (out / "a.txt").read_text() == "one"
        assert first["content"][-1]["text"] == f"saved to the host: {out / 'a.txt'}"
        # Only files changed by the cell come back: kept.txt and a.txt are not resent.
        second = call("session_exec", session_id=sid, code="open('/out/b.txt', 'w').write('two')")
        assert second["content"][-1]["text"] == f"saved to the host: {out / 'b.txt'}"
        call("session_exec", session_id=sid, code="open('/out/a.txt', 'w').write('ONE')")
        assert (out / "a.txt").read_text() == "ONE"

        call(
            "session_exec",
            session_id=sid,
            code="import matplotlib.pyplot as plt\nplt.plot([1, 2]); plt.savefig('plot.png')\n"
            "open('notes.txt', 'w').write('hello')",
        )
        assert call("session_read_file", session_id=sid, path="notes.txt")["content"] == [{"type": "text", "text": "hello"}]
        image = call("session_read_file", session_id=sid, path="/home/xnb/plot.png")["content"][0]
        assert image["type"] == "image" and image["mimeType"] == "image/png"
        listing = call("session_read_file", session_id=sid, path=".")["content"][0]["text"]
        assert listing.startswith("/home/xnb:") and "notes.txt (5 bytes)" in listing
        assert call("session_read_file", session_id=sid, path="/nope")["isError"]
        assert not (out / "plot.png").exists()  # only rw mounts reach the host
    finally:
        server.shutdown()


def test_shared_array_buffer():
    result = xnotebook.run(
        nb(
            "import pyjs\npyjs.js.crossOriginIsolated",
            "b = pyjs.js.SharedArrayBuffer.new(8)\na = pyjs.js.Int32Array.new(b)\npyjs.js.Atomics.add(a, 0, 7)\nint(a[0])",
            "import urllib.request\nurllib.request.urlopen('https://example.com')",
        ),
        allow_errors=True,
        return_result=True,
    )
    cells = result["notebook"]["cells"]
    assert texts(cells[0]) == "True"
    assert texts(cells[1]) == "7"
    assert cells[2]["outputs"][-1]["output_type"] == "error"  # still sealed
