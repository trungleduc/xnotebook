"""Model requests of `xnb agent`, forwarded from the sandboxed browser to one provider.

The agent runs inside the sealed browser and has no network and no API key. Each model
request reaches the host over the CDP bridge; the host checks it, adds the credentials,
sends it to the session's single upstream endpoint and streams the reply back.

Checks (a prompt-injected agent must not get more than a chat endpoint):
  - only the chat endpoint of the provider, with the session's model;
  - nothing that runs on the provider's side (server tools, MCP servers, web search),
    which would be internet access through the provider;
  - max_tokens capped, and optional caps on requests and tokens for the whole session.
"""

from __future__ import annotations

import codecs
import http.client
import json
import os
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional

ANTHROPIC_URL = "https://api.anthropic.com"
OPENAI_URL = "https://api.openai.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_PATHS = ("/v1/messages", "/v1/messages/count_tokens")
OPENAI_PATHS = ("/chat/completions",)
# Beta features pi-ai asks for; anything else is dropped.
ANTHROPIC_BETAS = {
    "fine-grained-tool-streaming-2025-05-14",
    "interleaved-thinking-2025-05-14",
    "server-side-fallback-2026-07-01",
    "mid-conversation-output-config-2026-07-01",
    "thinking-binding-controls-2026-08-01",
    "inline-tools-2026-09-15",
}
OAUTH_BETA = "oauth-2025-04-20"
RESPONSE_HEADERS = ("content-type", "request-id", "x-request-id", "retry-after", "x-should-retry")
MAX_TOKENS_CAP = 128_000
CHUNK = 1 << 14


class LlmError(Exception):
    """A request refused by the host, reported to the agent as an API error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Provider:
    name: str  # "anthropic" | "openai"
    model: str
    base_url: str
    auth: Dict[str, str] = field(default_factory=dict)  # headers added by the host
    max_tokens: int = MAX_TOKENS_CAP

    @property
    def official_openai(self) -> bool:
        return self.name == "openai" and urllib.parse.urlsplit(self.base_url).netloc == "api.openai.com"

    @classmethod
    def from_env(
        cls, name: str, model: str, base_url: Optional[str] = None, env: Mapping[str, str] = os.environ
    ) -> "Provider":
        """Credentials and endpoint from the host environment; they never reach the browser."""
        if name == "anthropic":
            url = base_url or env.get("ANTHROPIC_BASE_URL") or ANTHROPIC_URL
            if env.get("ANTHROPIC_API_KEY"):
                auth = {"x-api-key": env["ANTHROPIC_API_KEY"]}
            elif env.get("ANTHROPIC_AUTH_TOKEN"):
                auth = {"authorization": f"Bearer {env['ANTHROPIC_AUTH_TOKEN']}"}
            else:
                raise LlmError(401, "set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) for --provider anthropic")
        elif name == "openai":
            url = base_url or env.get("OPENAI_BASE_URL") or OPENAI_URL
            auth = {"authorization": f"Bearer {env['OPENAI_API_KEY']}"} if env.get("OPENAI_API_KEY") else {}
            if not auth and urllib.parse.urlsplit(url).netloc == "api.openai.com":
                raise LlmError(401, "set OPENAI_API_KEY for --provider openai")
        else:
            raise LlmError(400, f"unknown provider {name!r}")
        scheme = urllib.parse.urlsplit(url).scheme
        if scheme not in ("http", "https"):
            raise LlmError(400, f"the model endpoint must be http(s): {url}")
        return cls(name, model, url.rstrip("/"), auth)


@dataclass
class Usage:
    requests: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write


def error_body(provider: Provider, message: str) -> bytes:
    if provider.name == "anthropic":
        return json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": message}}).encode()
    return json.dumps({"error": {"type": "invalid_request_error", "message": message}}).encode()


def check_request(provider: Provider, method: str, path: str, body: str) -> bytes:
    """The body to send upstream, or LlmError if the request is not allowed."""
    if method != "POST":
        raise LlmError(405, f"xnb: {method} is not allowed")
    route = path.split("?", 1)[0]
    allowed = ANTHROPIC_PATHS if provider.name == "anthropic" else OPENAI_PATHS
    if route not in allowed:
        raise LlmError(404, f"xnb: {route} is not a model endpoint this session forwards")
    try:
        data = json.loads(body)
    except ValueError:
        raise LlmError(400, "xnb: the request body is not JSON") from None
    if not isinstance(data, dict):
        raise LlmError(400, "xnb: the request body is not a JSON object")
    if data.get("model") != provider.model:
        raise LlmError(400, f"xnb: model {data.get('model')!r} is not this session's model ({provider.model})")
    tools = data.get("tools") or []
    if not isinstance(tools, list):
        raise LlmError(400, "xnb: tools must be a list")
    if provider.name == "anthropic":
        for key in ("mcp_servers", "container"):
            if key in data:
                raise LlmError(400, f"xnb: {key} is not allowed (it would run outside the sandbox)")
        for t in tools:
            kind = t.get("type") if isinstance(t, dict) else None
            if kind not in (None, "custom"):
                raise LlmError(400, f"xnb: server-side tool {kind!r} is not allowed (it would run outside the sandbox)")
        keys = ("max_tokens",)
    else:
        if "web_search_options" in data:
            raise LlmError(400, "xnb: web_search_options is not allowed (it would run outside the sandbox)")
        for t in tools:
            if not isinstance(t, dict) or t.get("type") != "function":
                raise LlmError(400, f"xnb: tool type {t.get('type') if isinstance(t, dict) else t!r} is not allowed")
        keys = ("max_tokens", "max_completion_tokens")
    for key in keys:
        value = data.get(key)
        if isinstance(value, int) and value > provider.max_tokens:
            data[key] = provider.max_tokens
    return json.dumps(data).encode()


def request_headers(provider: Provider, browser: Mapping[str, str]) -> Dict[str, str]:
    """Headers for upstream: the host's credentials plus the few the SDKs need."""
    lower = {k.lower(): v for k, v in browser.items()}
    headers = {"content-type": "application/json", "accept": lower.get("accept", "application/json")}
    if provider.name == "anthropic":
        headers["anthropic-version"] = lower.get("anthropic-version", ANTHROPIC_VERSION)
        betas = [b.strip() for b in lower.get("anthropic-beta", "").split(",") if b.strip() in ANTHROPIC_BETAS]
        if "authorization" in provider.auth:
            betas.append(OAUTH_BETA)
        if betas:
            headers["anthropic-beta"] = ",".join(dict.fromkeys(betas))
    headers.update(provider.auth)
    return headers


class _Sse:
    """Reads token usage out of the streamed reply (Anthropic and OpenAI formats)."""

    def __init__(self) -> None:
        self.buf = ""
        self.input = self.output = self.cache_read = self.cache_write = 0

    def feed(self, text: str) -> None:
        self.buf += text
        *lines, self.buf = self.buf.split("\n")
        for line in lines:
            if not line.startswith("data:"):
                continue
            try:
                data = json.loads(line[5:].strip())
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            usage = (data.get("message") or {}).get("usage") if data.get("type") == "message_start" else data.get("usage")
            if not isinstance(usage, dict):
                continue
            if "prompt_tokens" in usage:  # OpenAI: one usage object at the end
                details = usage.get("prompt_tokens_details") or {}
                self.cache_read = int(details.get("cached_tokens") or 0)
                self.input = int(usage.get("prompt_tokens") or 0) - self.cache_read
                self.output = int(usage.get("completion_tokens") or 0)
                continue
            self.input = max(self.input, int(usage.get("input_tokens") or 0))
            self.cache_read = max(self.cache_read, int(usage.get("cache_read_input_tokens") or 0))
            self.cache_write = max(self.cache_write, int(usage.get("cache_creation_input_tokens") or 0))
            self.output = max(self.output, int(usage.get("output_tokens") or 0))


class Forwarder:
    """Runs the agent's model requests on threads; `emit` sends reply parts to the page."""

    def __init__(
        self,
        provider: Provider,
        emit: Callable[[dict], None],
        *,
        max_requests: Optional[int] = None,
        max_tokens_total: Optional[int] = None,
        timeout: float = 600,
        log: Callable[[str], None] = lambda m: None,
    ) -> None:
        self.provider = provider
        self.emit = emit
        self.max_requests = max_requests
        self.max_tokens_total = max_tokens_total
        self.timeout = timeout
        self.log = log
        self.usage = Usage()
        self._lock = threading.Lock()
        self._active: Dict[Any, http.client.HTTPConnection] = {}
        self._aborted: set = set()

    def start(self, msg: dict) -> None:
        threading.Thread(target=self._run, args=(msg,), name="xnb-llm", daemon=True).start()

    def abort(self, req_id: Any) -> None:
        with self._lock:
            self._aborted.add(req_id)
            conn = self._active.pop(req_id, None)
        if conn is not None:
            self._close(conn)

    def close(self) -> None:
        with self._lock:
            conns = list(self._active.values())
            self._active.clear()
        for conn in conns:
            self._close(conn)

    @staticmethod
    def _close(conn: http.client.HTTPConnection) -> None:
        try:
            if conn.sock is not None:
                conn.sock.shutdown(2)
        except OSError:
            pass
        conn.close()

    def _limit(self) -> Optional[str]:
        u = self.usage
        if self.max_requests and u.requests >= self.max_requests:
            return f"xnb: the session reached --max-requests {self.max_requests}"
        if self.max_tokens_total and u.total >= self.max_tokens_total:
            return f"xnb: the session reached --max-tokens-total {self.max_tokens_total}"
        return None

    def _refuse(self, req_id: Any, status: int, message: str) -> None:
        self.log(f"xnb: refused a model request: {message}")
        self.emit({"reqId": req_id, "head": {"status": status, "headers": {"content-type": "application/json"}}})
        self.emit({"reqId": req_id, "chunk": error_body(self.provider, message).decode()})
        self.emit({"reqId": req_id, "end": True})

    def _run(self, msg: dict) -> None:
        req_id = msg.get("reqId")
        p = self.provider
        try:
            body = check_request(p, str(msg.get("method", "")), str(msg.get("path", "")), str(msg.get("body", "")))
        except LlmError as e:
            return self._refuse(req_id, e.status, str(e))
        with self._lock:
            limit = self._limit()
            if limit is None:
                self.usage.requests += 1
        if limit is not None:
            return self._refuse(req_id, 400, limit)
        target = urllib.parse.urlsplit(p.base_url + str(msg.get("path")))
        cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        conn = cls(target.hostname or "", target.port, timeout=self.timeout)
        with self._lock:
            if req_id in self._aborted:
                self._aborted.discard(req_id)
                return
            self._active[req_id] = conn
        sse = _Sse()
        try:
            path = target.path + (f"?{target.query}" if target.query else "")
            conn.request("POST", path, body, request_headers(p, msg.get("headers") or {}))
            resp = conn.getresponse()
            headers = {k.lower(): v for k, v in resp.getheaders() if k.lower() in RESPONSE_HEADERS}
            self.emit({"reqId": req_id, "head": {"status": resp.status, "headers": headers}})
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            while True:
                data = resp.read1(CHUNK)
                if not data:
                    break
                text = decoder.decode(data)
                sse.feed(text)
                self.emit({"reqId": req_id, "chunk": text})
            tail = decoder.decode(b"", final=True)
            if tail:
                self.emit({"reqId": req_id, "chunk": tail})
            self.emit({"reqId": req_id, "end": True})
        except (OSError, http.client.HTTPException) as e:
            with self._lock:
                aborted = req_id in self._aborted
            if not aborted:
                self.log(f"xnb: model request failed: {e}")
                self.emit({"reqId": req_id, "error": f"model request failed: {e}"})
        finally:
            with self._lock:
                self._active.pop(req_id, None)
                self._aborted.discard(req_id)
                u = self.usage
                u.input += sse.input
                u.output += sse.output
                u.cache_read += sse.cache_read
                u.cache_write += sse.cache_write
            conn.close()
