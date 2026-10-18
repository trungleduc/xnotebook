"""A scripted model server for `xnb agent` tests: Anthropic Messages and OpenAI Chat
Completions, streamed as SSE. Each request gets the next scripted reply; every request is
recorded (path, headers, body) for the tests to check."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


def tool_call(name: str, args: Dict[str, Any]) -> dict:
    return {"tool": name, "args": args}


def text(t: str) -> dict:
    return {"text": t}


def _anthropic_sse(reply: dict, n: int) -> bytes:
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": f"msg_{n}", "type": "message", "role": "assistant", "model": reply.get("model", "claude-opus-5-5"),
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 1}}}),
    ]
    if "tool" in reply:
        events += [
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {
                "type": "tool_use", "id": f"toolu_{n}", "name": reply["tool"], "input": {}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps(reply["args"])}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ]
        stop = "tool_use"
    else:
        events += [
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": reply["text"]}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ]
        stop = "end_turn"
    events += [
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 20}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(f"event: {e}\ndata: {json.dumps(d)}\n\n".encode() for e, d in events)


def _openai_sse(reply: dict, n: int, model: str) -> bytes:
    base = {"id": f"chatcmpl-{n}", "object": "chat.completion.chunk", "created": 0, "model": model}
    chunks: List[dict] = [{**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}]
    if "tool" in reply:
        chunks.append({**base, "choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "id": f"call_{n}", "type": "function",
            "function": {"name": reply["tool"], "arguments": json.dumps(reply["args"])}}]}, "finish_reason": None}]})
        finish = "tool_calls"
    else:
        chunks.append({**base, "choices": [{"index": 0, "delta": {"content": reply["text"]}, "finish_reason": None}]})
        finish = "stop"
    chunks.append({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    chunks.append({**base, "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


class FakeLLM:
    def __init__(self, script: List[dict], provider: str = "anthropic") -> None:
        self.script = list(script)
        self.provider = provider
        self.requests: List[dict] = []
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                with fake.lock:
                    n = len(fake.requests) + 1
                    fake.requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(body or b"{}")})
                    reply = fake.script.pop(0) if fake.script else text("(script exhausted)")
                if fake.provider == "anthropic":
                    data = _anthropic_sse(reply, n)
                else:
                    data = _openai_sse(reply, n, fake.requests[-1]["body"].get("model", ""))
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "FakeLLM":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def tool_results(self, index: int) -> List[str]:
        """The text of every tool result sent in request `index` (both formats)."""
        msgs = self.requests[index]["body"].get("messages", [])
        raw: List[Any] = []
        for m in msgs:
            if m.get("role") == "tool":
                raw.append(m.get("content"))
            if isinstance(m.get("content"), list):
                raw += [c.get("content") for c in m["content"] if isinstance(c, dict) and c.get("type") == "tool_result"]
        out = []
        for r in raw:
            if isinstance(r, list):
                r = "".join(c.get("text", "") for c in r if isinstance(c, dict))
            out.append(r if isinstance(r, str) else json.dumps(r))
        return out

    def last_message(self, index: int) -> Optional[dict]:
        msgs = self.requests[index]["body"].get("messages", [])
        return msgs[-1] if msgs else None
