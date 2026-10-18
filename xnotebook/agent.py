"""`xnb agent`: chat in the terminal with an agent that runs inside the sandboxed browser.

The agent loop (pi) runs in a browser worker next to the kernel; see PLAN.md. This process
is the terminal chat and the agent's only way out:

  - chat and control commands go to the page through `__xnbAgent`;
  - model requests come back over the bridge, and `llm.Forwarder` sends them upstream
    with the API key, which never enters the browser;
  - files the kernel changes under rw mounts are written back here, after a backup of the
    original (the first time in the session) and with a diff on the terminal.

Model text goes to stdout; tool blocks, diffs, progress and errors go to stderr.
"""

from __future__ import annotations

import base64
import difflib
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO

from .cache import Cache
from .llm import Forwarder, Provider
from .mounts import Mount, MountError, mount_to_job, write_back
from .session import RunError, Session

DEFAULT_MODELS = {"anthropic": "claude-opus-5-5"}
EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
MAX_AGENTS_MD = 64 << 10
CODE_LINES = 8
OUTPUT_LINES = 5
DIFF_LINES = 20
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

HELP = """\
/help             this help
/reset            clear the conversation (the kernel keeps its state)
/restart          restart the kernel (fresh state; mounts are copied in again)
/mounts           show the mounts
/tokens           show token usage so far
/save FILE.ipynb  save the cells the agent ran as a notebook
/exit             quit (or Ctrl-D)
A line ending with \\ continues on the next line. Ctrl-C interrupts the agent."""


class AgentSession(Session):
    """A browser with a kernel and the agent; `command()` drives it, `on_agent` gets its events."""

    def __init__(
        self,
        job: Dict[str, Any],
        mounts: List[Mount],
        provider: Provider,
        *,
        on_agent: Callable[[dict], None],
        max_requests: Optional[int] = None,
        max_tokens_total: Optional[int] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(job, on_event=lambda e: None, **kwargs)
        self.mounts = mounts
        self.on_agent = on_agent
        self.forwarder = Forwarder(
            provider, self._llm_out, max_requests=max_requests, max_tokens_total=max_tokens_total, log=self._dbg
        )
        self.backup_dir = self.cache.backups / job["agent"]["sessionId"]
        self.backed_up: List[Path] = []
        self.created: List[Path] = []
        self.ready_event: Optional[dict] = None
        self.dead: Optional[str] = None
        self.result: Optional[dict] = None
        self._ready = threading.Event()
        self._finished = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle (called from the terminal thread) -------------------------
    def start(self, timeout: Optional[float] = None) -> "AgentSession":
        self._thread = threading.Thread(target=self._main, name="xnb-agent", daemon=True)
        self._thread.start()
        deadline = None if timeout is None else time.time() + timeout
        while not self._ready.is_set() and not self._finished.is_set():
            if deadline is not None and time.time() > deadline:
                self.kill(f"the agent did not start within {timeout}s")
                break
            self._ready.wait(0.1)
        if not self._ready.is_set():
            self._finished.wait(10)
            raise RunError(self.dead or "the agent failed to start")
        return self

    def command(self, cmd: dict) -> None:
        self._inbox.put(("agent_cmd", cmd))

    def close(self, timeout: float = 30) -> None:
        if self._thread is not None and self._thread.is_alive():
            self.command({"type": "close"})
            self._thread.join(timeout)
            if self._thread.is_alive():
                self.kill("close timed out")
                self._thread.join(10)

    def kill(self, reason: str) -> None:
        self._inbox.put(("crash", reason))

    @property
    def finished(self) -> bool:
        return self._finished.is_set()

    def _main(self) -> None:
        try:
            self.result = self.run()
        except BaseException as e:  # noqa: B036 - reported to the terminal
            self.dead = str(e) or type(e).__name__
        finally:
            self.forwarder.close()
            self._finished.set()
            self.on_agent({"kind": "session_end", "reason": self.dead})

    # -- session loop thread -------------------------------------------------
    def _eval(self, conn, sid: str, expression: str) -> None:
        conn.send("Runtime.evaluate", {"expression": expression, "awaitPromise": False}, sid)

    def _command(self, conn, sid: str, kind: str, data: Any) -> None:
        if kind == "agent_cmd":
            self._eval(conn, sid, f"window.__xnbAgent && window.__xnbAgent({json.dumps(data)})")

    def _llm_out(self, reply: dict) -> None:
        """A part of a model reply, from a forwarder thread (CDP sends are thread-safe)."""
        browser, sid = self.browser, self.page_session
        if browser is None or sid is None or browser.conn.closed:
            return
        self._eval(browser.conn, sid, f"window.__xnbLlm && window.__xnbLlm({json.dumps(reply)})")

    def _page_request(self, conn, sid: str, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "agent":
            event = msg.get("event") or {}
            if event.get("kind") == "agent_ready":
                self.ready_event = event
                self._ready.set()
            self.on_agent(event)
        elif mtype == "llm":
            self.forwarder.start(msg)
        elif mtype == "llm_abort":
            self.forwarder.abort(msg.get("reqId"))
        elif mtype == "writeback":
            self._reply(conn, sid, msg["id"], {"saved": self._writeback(msg.get("mounts") or [])})
        elif mtype == "remount":
            try:
                self._reply(conn, sid, msg["id"], {"mounts": [mount_to_job(m) for m in self.mounts]})
            except (OSError, MountError) as e:
                self._reply(conn, sid, msg["id"], error=f"cannot read the mounts again: {e}")

    # -- write-back with backups and diffs ----------------------------------
    def _writeback(self, outs: List[dict]) -> List[str]:
        saved: List[str] = []
        for out in outs:
            m = next((m for m in self.mounts if m.dst == out.get("dst") and m.mode == "rw"), None)
            if m is None:
                continue
            index = self.mounts.index(m)
            try:
                written = write_back(
                    m, out.get("files") or [], before_write=lambda t, old, new: self._before_write(index, m, t, old, new)
                )
            except (OSError, MountError) as e:
                self.on_agent({"kind": "warning", "message": f"write-back failed: {e}"})
                continue
            saved.extend(display_path(p) for p in written)
        return saved

    def _before_write(self, index: int, m: Mount, target: Path, old: Optional[bytes], new: bytes) -> None:
        root = m.src if m.src.is_dir() else m.src.parent
        base = self.backup_dir / f"{index}-{root.name}"
        if old is None:
            if target not in self.created:
                self.created.append(target)
                self.backup_dir.mkdir(parents=True, exist_ok=True)
                (self.backup_dir / "created.txt").write_text("".join(f"{p}\n" for p in self.created))
        elif target not in self.backed_up and target not in self.created:
            dest = base / target.relative_to(root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(old)
            self.backed_up.append(target)
        self.on_agent(
            {
                "kind": "saved",
                "path": display_path(target),
                "new": old is None,
                "bytes": len(new),
                "diff": text_diff(old, new, target.name),
            }
        )


def display_path(p: Path) -> str:
    try:
        return f"./{p.relative_to(Path.cwd())}"
    except ValueError:
        return str(p)


def text_diff(old: Optional[bytes], new: bytes, name: str) -> Optional[str]:
    """A unified diff of two text files, or None (new file, binary, or not UTF-8)."""
    if old is None or b"\0" in old or b"\0" in new:
        return None
    try:
        a, b = old.decode("utf-8"), new.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), name, name, n=3))


def format_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    return f"{n / 1024:.0f} KB" if n < 1 << 20 else f"{n / (1 << 20):.1f} MB"


class Chat:
    """The terminal side: reads input, sends commands, renders the agent's events."""

    def __init__(
        self,
        out: TextIO = sys.stdout,
        err: TextIO = sys.stderr,
        *,
        json_mode: bool = False,
        images_dir: Optional[Path] = None,
        color: Optional[bool] = None,
    ) -> None:
        self.out = out
        self.err = err
        self.json_mode = json_mode
        self.images_dir = images_dir
        tty = err.isatty() if hasattr(err, "isatty") else False
        self.color = (tty and not os.environ.get("NO_COLOR")) if color is None else color
        self.spin = tty and not json_mode
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.session: Optional[AgentSession] = None
        self.text_open = False  # stdout is in the middle of a line of model text
        self.tool: Optional[str] = None  # tool running now
        self.pending_saved: List[dict] = []
        self.turn_ok = True
        self.turn_tools = 0
        self.turn_t0 = 0.0
        self.turn_usage = (0, 0)
        self.images = 0
        self.spinner_since: Optional[float] = None
        self.spinner_shown = False

    # -- output helpers ----------------------------------------------------
    def style(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def dim(self, text: str) -> str:
        return self.style(text, "2")

    def say(self, line: str = "") -> None:
        self.stop_spinner()
        self.end_text()
        self.err.write(line + "\n")
        self.err.flush()

    def end_text(self) -> None:
        if self.text_open:
            self.out.write("\n")
            self.out.flush()
            self.text_open = False

    def block(self, lines: List[str], limit: int, code: Optional[str] = None) -> None:
        for line in lines[:limit]:
            self.say("  " + self.dim("│ ") + (self.style(line, code) if code else self.dim(line)))
        if len(lines) > limit:
            self.say("  " + self.dim(f"│ … {len(lines) - limit} more lines"))

    def diff(self, patch: str) -> None:
        lines = [
            ln
            for ln in patch.splitlines()
            if not ln.startswith(("---", "+++", "Index:", "====")) and ln != "\\ No newline at end of file"
        ]
        shown = lines[:DIFF_LINES]
        for line in shown:
            code = "32" if line.startswith("+") else "31" if line.startswith("-") else "36" if line.startswith("@@") else None
            self.say("  " + self.dim("│ ") + (self.style(line, code) if code else line))
        if len(lines) > DIFF_LINES:
            self.say("  " + self.dim(f"│ … {len(lines) - DIFF_LINES} more lines"))

    # -- spinner -------------------------------------------------------------
    def start_spinner(self) -> None:
        if self.spinner_since is None:
            self.spinner_since = time.time()

    def stop_spinner(self) -> None:
        self.spinner_since = None
        if self.spinner_shown:
            self.err.write("\r\x1b[K")
            self.err.flush()
            self.spinner_shown = False

    def tick(self) -> None:
        if not self.spin or self.spinner_since is None or self.text_open:
            return
        elapsed = time.time() - self.spinner_since
        if elapsed < 0.3:
            return
        frame = SPINNER[int(elapsed * 10) % len(SPINNER)]
        label = "running" if self.tool else "thinking"
        self.err.write(f"\r{self.dim(f'{frame} {label}… {elapsed:.0f}s')}\x1b[K")
        self.err.flush()
        self.spinner_shown = True

    # -- events ----------------------------------------------------------------
    def on_agent(self, event: dict) -> None:
        """From the session thread."""
        self.events.put(event)

    def header(self, ready: dict, config: dict, mounts: List[Mount], extra: List[str]) -> None:
        spec = ready.get("spec") or {}
        packages = ready.get("packages") or []
        pk = ", ".join(packages[:6]) + (f" (+{len(packages) - 6} more)" if len(packages) > 6 else "")
        effort = config.get("effort") or ("medium" if config["provider"] == "anthropic" else None)
        title = f"xnb agent · {config['model']} ({config['provider']})" + (f" · effort {effort}" if effort else "")
        self.say()
        self.say(" " + self.style(title, "1"))
        self.say(f" {self.dim('kernel ')}   {spec.get('name', '?')} · {pk or 'no extra packages'}")
        if mounts:
            for i, m in enumerate(mounts):
                label = "mounts " if i == 0 else "       "
                self.say(f" {self.dim(label)}   {m.dst} ← {display_path(m.src)} ({m.mode})")
        else:
            self.say(f" {self.dim('mounts ')}   none")
        self.say(f" {self.dim('sealed ')}   the kernel has no network; model calls go through the host")
        for line in extra:
            self.say(f" {self.dim('       ')}   {line}")
        self.say(" " + self.dim("/help for commands · Ctrl-C interrupts · Ctrl-D quits"))
        self.say()

    def render(self, ev: dict) -> None:
        if self.json_mode:
            if ev.get("kind") not in ("notebook",):
                self.out.write(json.dumps(ev, ensure_ascii=False) + "\n")
                self.out.flush()
            self._track(ev)
            return
        kind = ev.get("kind")
        if kind == "text":
            delta = ev.get("delta") or ""
            if delta:
                self.stop_spinner()
                self.out.write(delta)
                self.out.flush()
                self.text_open = not delta.endswith("\n")
        elif kind == "thinking":
            self.start_spinner()
        elif kind == "tool_start":
            self.tool_start(ev)
        elif kind == "tool_end":
            self.tool_end(ev)
        elif kind == "saved":
            if self.tool is not None:
                self.pending_saved.append(ev)
            else:
                self.saved(ev, show_diff=True)
        elif kind == "message":
            self.message(ev)
        elif kind == "error":
            self.turn_ok = False
            self.say(self.style(f"xnb: {ev.get('message')}", "31"))
        elif kind == "warning":
            self.say(self.style(f"xnb: {ev.get('message')}", "33"))
        elif kind == "kernel_dead" and self.tool is None:
            # During a tool call its result already says so.
            self.say(self.style(f"✗ the kernel died: {ev.get('reason')}", "31"))
        elif kind == "turn_end":
            self.turn_end()
        elif kind == "session_end":
            if ev.get("reason"):
                self.say(self.style(f"xnb: the session ended: {ev.get('reason')}", "31"))
        self._track(ev)

    def _track(self, ev: dict) -> None:
        kind = ev.get("kind")
        if kind == "tool_start":
            self.tool = ev.get("name")
            self.turn_tools += 1
        elif kind == "tool_end":
            self.tool = None
        elif kind == "message" and ev.get("stopReason") in ("error", "length"):
            self.turn_ok = False
        elif kind == "error":
            self.turn_ok = False

    def tool_start(self, ev: dict) -> None:
        name = ev.get("name", "?")
        args = ev.get("args") or {}
        if name == "execute":
            self.say(self.style("● execute", "1"))
            self.block(str(args.get("code", "")).rstrip("\n").splitlines(), CODE_LINES)
        elif name in ("read", "edit", "write"):
            self.say(self.style(f"● {name}", "1") + f" {args.get('path', '')}")
        elif name == "restart_kernel":
            self.say(self.style("● restart_kernel", "1"))
        else:
            self.say(self.style(f"● {name}", "1") + " " + json.dumps(args)[:120])
        self.start_spinner()

    def tool_end(self, ev: dict) -> None:
        name = ev.get("name")
        details = ev.get("details") or {}
        text = ev.get("text") or ""
        seconds = ev.get("seconds") or 0
        images = ev.get("images") or []
        mark = self.style("✗", "31") if ev.get("isError") else self.style("✓", "32")
        extra: List[str] = []
        if images:
            extra.append(f"{len(images)} plot{'s' if len(images) > 1 else ''}")
        if name == "execute":
            if ev.get("isError"):
                lines = [ln for ln in text.splitlines() if ln.strip()]
                self.say(f"  {mark} {seconds:.1f}s" + (f" · {lines[-1][:120]}" if lines else ""))
                self.block(lines[-4:-1], 3, "31")
            else:
                self.say(f"  {mark} {seconds:.1f}s" + "".join(f" · {e}" for e in extra))
                lines = [ln for ln in text.splitlines() if not ln.startswith("saved to the host:")]
                if lines and lines != ["(no output)"]:
                    self.block(lines, OUTPUT_LINES)
        elif name == "edit" and not ev.get("isError"):
            m = re.match(r"Successfully replaced (\d+) block", text)
            n = int(m.group(1)) if m else 1
            self.say(f"  {mark} {n} block{'s' if n != 1 else ''} replaced")
            if details.get("patch"):
                self.diff(details["patch"])
        elif name == "write" and not ev.get("isError"):
            self.say(f"  {mark} wrote {format_size(int(details.get('bytes') or 0))}")
        elif name == "read" and not ev.get("isError"):
            self.say(f"  {mark} {len(text.splitlines())} lines" if not images else f"  {mark} image")
        else:
            self.say(f"  {mark} {text.splitlines()[0][:160] if text else ''}")
        self.save_images(images)
        for saved in self.pending_saved:
            self.saved(saved, show_diff=name == "execute")
        self.pending_saved.clear()
        self.start_spinner()

    def saved(self, ev: dict, show_diff: bool) -> None:
        note = f"new, {format_size(int(ev.get('bytes') or 0))}" if ev.get("new") else None
        self.say("  " + self.style("↳ saved", "36") + f" {ev.get('path')}" + (f" ({note})" if note else ""))
        if show_diff and ev.get("diff"):
            self.diff(ev["diff"])

    def save_images(self, images: List[dict]) -> None:
        if not self.images_dir:
            return
        for img in images:
            ext = "jpg" if img.get("mimeType") == "image/jpeg" else "png"
            self.images += 1
            path = self.images_dir / f"plot-{self.images}.{ext}"
            try:
                self.images_dir.mkdir(parents=True, exist_ok=True)
                path.write_bytes(base64.b64decode(img.get("data") or ""))
            except (OSError, ValueError) as e:
                self.say(self.style(f"xnb: cannot save a plot: {e}", "33"))
                continue
            self.say("  " + self.style("↳ plot saved", "36") + f" {display_path(path.resolve())}")

    def message(self, ev: dict) -> None:
        reason = ev.get("stopReason")
        if reason == "error":
            self.say(self.style(f"✗ model error: {ev.get('error') or 'unknown error'}", "31"))
        elif reason == "length":
            self.say(self.style("(the reply was cut off: it reached the output token limit)", "33"))

    def turn_end(self) -> None:
        self.stop_spinner()
        self.end_text()
        u = self.session.forwarder.usage if self.session else None
        parts = [f"{self.turn_tools} tool{'s' if self.turn_tools != 1 else ''}"]
        if u is not None:
            inp = u.input + u.cache_read + u.cache_write - self.turn_usage[0]
            parts += [f"{format_tokens(inp)} in", f"{format_tokens(u.output - self.turn_usage[1])} out"]
        parts.append(f"{time.time() - self.turn_t0:.0f}s")
        line = f"── {' · '.join(parts)} "
        self.say()
        self.say(self.dim(line + "─" * max(0, 60 - len(line))))
        self.say()

    # -- turns and commands -------------------------------------------------
    def begin_turn(self) -> None:
        self.turn_ok = True
        self.turn_tools = 0
        self.turn_t0 = time.time()
        u = self.session.forwarder.usage if self.session else None
        self.turn_usage = (u.input + u.cache_read + u.cache_write, u.output) if u else (0, 0)
        self.start_spinner()

    def wait(self, kinds: tuple, timeout: Optional[float] = None) -> Optional[dict]:
        """Render events until one of `kinds` arrives (or the session ends); Ctrl-C aborts."""
        assert self.session is not None
        deadline = None if timeout is None else time.time() + timeout
        interrupted = False
        while True:
            try:
                try:
                    ev = self.events.get(timeout=0.1)
                except queue.Empty:
                    if self.session.finished and self.events.empty():
                        return None
                    if deadline is not None and time.time() > deadline:
                        return None
                    self.tick()
                    continue
                self.render(ev)
                if ev.get("kind") in kinds or ev.get("kind") == "session_end":
                    return ev
            except KeyboardInterrupt:
                if interrupted:
                    raise
                interrupted = True
                self.turn_ok = False
                self.session.command({"type": "abort"})
                if self.tool == "execute":
                    self.say(self.style("^C interrupted; the cell is still running (wasm can't be interrupted). Ctrl-C again to quit.", "33"))
                else:
                    self.say(self.style("^C interrupted", "33"))

    def turn(self, text: str) -> bool:
        assert self.session is not None
        self.begin_turn()
        self.session.command({"type": "chat", "text": text})
        ev = self.wait(("turn_end",))
        return ev is not None and ev.get("kind") == "turn_end" and self.turn_ok

    def slash(self, line: str, mounts: List[Mount]) -> bool:
        """Run a /command; False to quit."""
        assert self.session is not None
        cmd, _, arg = line.partition(" ")
        if cmd in ("/exit", "/quit"):
            return False
        if cmd == "/help":
            self.say(HELP)
        elif cmd == "/reset":
            self.session.command({"type": "reset"})
            ev = self.wait(("reset", "error"), timeout=30)
            if ev and ev.get("kind") == "reset":
                self.say(self.dim("conversation cleared"))
        elif cmd == "/restart":
            self.session.command({"type": "restart"})
            ev = self.wait(("restart_result",), timeout=600)
            if ev and ev.get("kind") == "restart_result":
                mark = self.style("✓", "32") if ev.get("ok") else self.style("✗", "31")
                self.say(f"{mark} {ev.get('message')}")
        elif cmd == "/mounts":
            for m in mounts:
                self.say(f"{m.dst} ← {m.src} ({m.mode})")
            if not mounts:
                self.say("no mounts")
        elif cmd == "/tokens":
            u = self.session.forwarder.usage
            self.say(
                f"{u.requests} model requests · {format_tokens(u.input)} in · {format_tokens(u.cache_read)} cache read · "
                f"{format_tokens(u.cache_write)} cache write · {format_tokens(u.output)} out"
            )
        elif cmd == "/save":
            if not arg.strip():
                self.say("usage: /save FILE.ipynb")
                return True
            self.session.command({"type": "notebook"})
            ev = self.wait(("notebook",), timeout=60)
            if ev and ev.get("notebook"):
                target = Path(arg.strip()).expanduser()
                target.write_text(json.dumps(ev["notebook"], indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
                self.say(f"xnb: wrote {target}")
        else:
            self.say(f"unknown command {cmd}; /help lists them")
        return True

    def read_line(self) -> str:
        line = input("› ")
        while line.endswith("\\"):
            line = line[:-1] + "\n" + input("… ")
        return line

    def repl(self, mounts: List[Mount]) -> None:
        while True:
            try:
                line = self.read_line()
            except KeyboardInterrupt:
                self.err.write("\n")
                continue
            except EOFError:
                self.err.write("\n")
                return
            if not line.strip():
                continue
            if line.startswith("/") and not line.startswith("//"):
                if not self.slash(line.strip(), mounts):
                    return
                continue
            self.turn(line)
            if self.session is not None and self.session.finished:
                return


def build_agent_job(
    *,
    provider: Provider,
    effort: Optional[str],
    max_output_tokens: Optional[int],
    compat: Dict[str, Any],
    images: bool,
    system: Optional[str],
    max_restarts: int,
    **job_options: Any,
) -> "tuple[dict, List[Mount]]":
    from .api import build_job

    filename = job_options.pop("filename", "agent.py")
    job, mounts = build_job(content="", filename=filename, **job_options)
    job.update(
        {
            "allowErrors": True,
            "widgetState": False,
            "stdin": [],  # input() fails instead of waiting for an answer
            "agent": {
                "provider": provider.name,
                "model": provider.model,
                "openaiOfficial": provider.official_openai,
                "effort": effort,
                "maxTokens": max_output_tokens or (64_000 if provider.name == "anthropic" else None),
                "compat": compat,
                "images": images,
                "system": system,
                "maxRestarts": max_restarts,
                "sessionId": uuid.uuid4().hex,
            },
        }
    )
    return job, mounts


def project_instructions(path: Path) -> Optional[str]:
    """AGENTS.md from the directory xnb was started in, if there is one."""
    try:
        if path.is_file() and path.stat().st_size <= MAX_AGENTS_MD:
            return f"Project instructions ({path.name}):\n\n{path.read_text(encoding='utf-8')}"
    except (OSError, UnicodeDecodeError):
        pass
    return None


def run_agent(
    job: dict,
    mounts: List[Mount],
    provider: Provider,
    *,
    prompt: Optional[str] = None,
    json_mode: bool = False,
    images_dir: Optional[Path] = None,
    save: Optional[Path] = None,
    max_requests: Optional[int] = None,
    max_tokens_total: Optional[int] = None,
    header_extra: Optional[List[str]] = None,
    cache: Optional[Cache] = None,
    history: bool = True,
    **session_options: Any,
) -> int:
    """Start the agent, chat (or run one prompt), and shut down; returns the exit code."""
    chat = Chat(json_mode=json_mode, images_dir=images_dir)
    session = AgentSession(
        job,
        mounts,
        provider,
        on_agent=chat.on_agent,
        max_requests=max_requests,
        max_tokens_total=max_tokens_total,
        cache=cache,
        **session_options,
    )
    chat.session = session
    try:
        session.start(timeout=900)
    except KeyboardInterrupt:
        session.kill("interrupted")
        session.close(10)
        return 130
    except RunError as e:
        sys.stderr.write(f"xnb: {e}\n")
        return 2
    code = 0
    history_file = session.cache.root / "agent_history"
    readline = _readline() if history and prompt is None and sys.stdin.isatty() else None
    if readline is not None:
        try:
            readline.read_history_file(str(history_file))
        except OSError:
            pass
    try:
        if prompt is None and not json_mode:
            chat.header(session.ready_event or {}, job["agent"], mounts, header_extra or [])
        # Events that arrived before the header (agent_ready) are rendered now.
        while not chat.events.empty():
            chat.render(chat.events.get())
        if prompt is not None:
            code = 0 if chat.turn(prompt) else 1
        else:
            chat.repl(mounts)
        if save is not None and not session.finished:
            session.command({"type": "notebook"})
            ev = chat.wait(("notebook",), timeout=60)
            if ev and ev.get("notebook"):
                save.write_text(json.dumps(ev["notebook"], indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
                chat.say(f"xnb: wrote {save}")
    except KeyboardInterrupt:
        chat.say()
        session.kill("interrupted")
        code = 130
    finally:
        if readline is not None:
            try:
                history_file.parent.mkdir(parents=True, exist_ok=True)
                readline.set_history_length(1000)
                readline.write_history_file(str(history_file))
            except OSError:
                pass
        session.close()
        summary(chat, session)
    if session.dead and code == 0:
        code = 1
    return code


def summary(chat: Chat, session: AgentSession) -> None:
    u = session.forwarder.usage
    if u.requests:
        chat.say(
            chat.dim(
                f"xnb: session: {u.requests} model requests · "
                f"{format_tokens(u.input + u.cache_read + u.cache_write)} in · {format_tokens(u.output)} out"
            )
        )
    if session.backed_up or session.created:
        parts = []
        if session.backed_up:
            n = len(session.backed_up)
            parts.append(f"originals of {n} changed file{'s' if n > 1 else ''} saved in {session.backup_dir}")
        if session.created:
            n = len(session.created)
            parts.append(f"{n} file{'s' if n > 1 else ''} created (listed in {session.backup_dir / 'created.txt'})")
        chat.say(chat.dim("xnb: " + "; ".join(parts)))


def _readline() -> Any:
    try:
        import readline
    except ImportError:  # Windows
        return None
    return readline
