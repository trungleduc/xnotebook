"""Token-guarded loopback HTTP server.

Routes (all under /<token>/):
  /<token>/<path>        static web bundle (xnb/_web) and the per-run job.json
  /<token>/u/<quoted>    lazy caching proxy for allowlisted upstream URLs

Package files whose sha256 is known (registered from the lock) are content
addressed in ~/.cache/xnb/pkgs/<sha256>. A miss is streamed from upstream to the
client while being hashed; the last chunk is only released once the hash matches,
so a tampered file never completes in the browser and is never cached.
Everything else (repodata, PyPI JSON) is cached with ETag/Last-Modified and a TTL.
"""

from __future__ import annotations

import hashlib
import http.client
import mimetypes
import os
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Tuple

from .cache import Cache

CHUNK = 1 << 16
PACKAGE_SUFFIXES = (".conda", ".tar.bz2", ".whl")
USER_AGENT = "xnotebook"

# Security headers for the web bundle. Upstream traffic is https:// (rewritten to
# the proxy by the firewall); the kernel worker gets its own, stricter policy.
PAGE_CSP = (
    "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; "
    "connect-src 'self' https:; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
    "worker-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'"
)
WORKER_CSP = (
    "default-src 'none'; script-src 'self' blob: 'unsafe-eval' 'wasm-unsafe-eval'; "
    "worker-src blob:; connect-src 'none'"
)


def origin_of(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}".lower()


class _NoRedirectToBadScheme(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if urllib.parse.urlsplit(newurl).scheme not in ("https", "http"):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ProxyStats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.upstream_fetches = 0
        self.package_hits = 0
        self.package_misses = 0
        self.blocked_after_seal = 0
        self.denied: list[str] = []

    def bump(self, name: str, n: int = 1) -> None:
        with self.lock:
            setattr(self, name, getattr(self, name) + n)


class Proxy:
    def __init__(
        self,
        cache: Cache,
        web_root: Path,
        *,
        allow_origins: Iterable[str] = (),
        offline: bool = False,
        refresh: bool = False,
        ttl: float = 3600.0,
        timeout: float = 60.0,
        log: Callable[[str], None] = lambda m: None,
    ) -> None:
        self.cache = cache.ensure()
        self.web_root = Path(web_root).resolve()
        self.token = secrets.token_urlsafe(18)
        self.allow_origins = {o.lower().rstrip("/") for o in allow_origins}
        self.allow_prefixes: set = set()  # exact URL prefixes, for single files on shared hosts
        self.offline = offline
        self.refresh = refresh
        self.ttl = ttl
        self.timeout = timeout
        self.log = log
        self.files: Dict[str, Tuple[bytes, str]] = {}  # extra in-memory files (job.json)
        self.expected: Dict[str, Tuple[str, Optional[int]]] = {}  # url -> (sha256, size)
        self.sealed = False
        self.stats = ProxyStats()
        self._opener = urllib.request.build_opener(_NoRedirectToBadScheme())
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> "Proxy":
        proxy = self

        class Handler(_Handler):
            ctx = proxy

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="xnb-proxy", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def base(self) -> str:
        return f"{self.origin}/{self.token}/"

    def upstream_url(self, url: str) -> str:
        """The proxy URL that serves `url`."""
        return f"{self.base}u/{urllib.parse.quote(url, safe='')}"

    def is_allowed(self, url: str) -> bool:
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ("https", "http") or p.username or p.password:
            return False
        if origin_of(url) in self.allow_origins:
            return True
        return any(url.startswith(p) for p in self.allow_prefixes)

    def add_file(self, name: str, data: bytes, content_type: str = "application/json") -> None:
        self.files[name] = (data, content_type)

    def expect(self, url: str, sha256: str, size: Optional[int] = None) -> None:
        self.expected[url] = (sha256.lower(), size)

    def seal(self) -> None:
        self.sealed = True


class _Handler(BaseHTTPRequestHandler):
    ctx: Proxy
    protocol_version = "HTTP/1.1"
    server_version = "xnb"
    sys_version = ""

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    # -- helpers ----------------------------------------------------------
    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "600")

    def _simple(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8") -> None:
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _route(self) -> Optional[str]:
        """Return the path after /<token>/ or None if the token is missing/wrong."""
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        prefix = f"/{self.ctx.token}/"
        if not path.startswith(prefix):
            return None
        return path[len(prefix):]

    # -- verbs ------------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        self._simple(405, b"method not allowed")

    do_PUT = do_DELETE = do_PATCH = do_POST

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        ctx = self.ctx
        rest = self._route()
        if rest is None:
            return self._simple(403, b"forbidden")
        if ctx.sealed:
            ctx.stats.bump("blocked_after_seal")
            return self._simple(403, b"sealed")
        if rest.startswith("u/"):
            raw = self.path[len(f"/{ctx.token}/u/"):]
            url = urllib.parse.unquote(raw)
            if not ctx.is_allowed(url):
                with ctx.stats.lock:
                    ctx.stats.denied.append(url)
                ctx.log(f"proxy: denied {url}")
                return self._simple(403, b"upstream not allowed")
            try:
                return self._upstream(url)
            except (ConnectionError, socket.timeout, BrokenPipeError):
                self.close_connection = True
                return None
        return self._static(rest)

    # -- static bundle ----------------------------------------------------
    def _static(self, rest: str) -> None:
        ctx = self.ctx
        name = urllib.parse.unquote(rest) or "index.html"
        if name in ctx.files:
            data, ctype = ctx.files[name]
            return self._simple(200, data, ctype)
        target = (ctx.web_root / name).resolve()
        if ctx.web_root not in target.parents or not target.is_file():
            return self._simple(404, b"not found")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".wasm":
            ctype = "application/wasm"
        elif target.suffix in (".js", ".mjs"):
            ctype = "text/javascript"
        size = target.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", WORKER_CSP if "worker" in target.name else PAGE_CSP)
        self.end_headers()
        if self.command != "HEAD":
            with open(target, "rb") as f:
                self._send_file(f, size)

    def _send_file(self, f, size: int) -> None:
        try:
            self.connection.sendfile(f, 0, size)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            f.seek(0)
            shutil.copyfileobj(f, self.wfile, CHUNK)

    # -- upstream ---------------------------------------------------------
    def _upstream(self, url: str) -> None:
        ctx = self.ctx
        expected = ctx.expected.get(url)
        if expected is not None:
            return self._package(url, *expected)
        path = urllib.parse.urlsplit(url).path
        if path.endswith(PACKAGE_SUFFIXES):
            # A package we have no hash for (e.g. probed during a pip solve): pass through.
            return self._passthrough(url)
        return self._metadata(url)

    def _open(self, url: str, headers: Optional[dict] = None):
        ctx = self.ctx
        ctx.stats.bump("upstream_fetches")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        return ctx._opener.open(req, timeout=ctx.timeout)

    def _start_stream(self, status: int, ctype: str, length: Optional[int]) -> bool:
        """Send headers; return True when the body is chunk-encoded."""
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        chunked = length is None
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(length))
        self.end_headers()
        return chunked

    def _write(self, data: bytes, chunked: bool) -> None:
        if not data:
            return
        if chunked:
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        else:
            self.wfile.write(data)

    def _end(self, chunked: bool) -> None:
        if chunked:
            self.wfile.write(b"0\r\n\r\n")

    def _upstream_error(self, e: Exception, url: str) -> None:
        if isinstance(e, urllib.error.HTTPError):
            body = b""
            try:
                body = e.read()
            except Exception:  # noqa: BLE001
                pass
            return self._simple(e.code, body, e.headers.get("Content-Type", "text/plain"))
        self.ctx.log(f"proxy: upstream error for {url}: {e}")
        return self._simple(502, f"upstream error: {e}".encode())

    def _package(self, url: str, sha256: str, size: Optional[int]) -> None:
        ctx = self.ctx
        path = ctx.cache.pkg_path(sha256)
        ctype = "application/octet-stream"
        if path.is_file():
            ctx.stats.bump("package_hits")
            fsize = path.stat().st_size
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(fsize))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                with open(path, "rb") as f:
                    self._send_file(f, fsize)
            return None
        if ctx.offline:
            return self._simple(504, f"offline and not cached: {url}".encode())
        ctx.stats.bump("package_misses")
        try:
            resp = self._open(url)
        except Exception as e:  # noqa: BLE001
            return self._upstream_error(e, url)
        fd, tmp = ctx.cache.tmp_file(".part")
        ok = False
        try:
            with resp, os.fdopen(fd, "wb") as out:
                length = resp.headers.get("Content-Length")
                length_i = int(length) if length and length.isdigit() else None
                chunked = self._start_stream(200, ctype, length_i)
                h = hashlib.sha256()
                held = b""  # the last chunk is held back until the hash is verified
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
                    out.write(chunk)
                    if held:
                        self._write(held, chunked)
                    held = chunk
                if h.hexdigest() != sha256:
                    ctx.log(f"proxy: sha256 mismatch for {url}; dropping")
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return None
                out.flush()
                os.fsync(out.fileno())
                ok = True
            os.replace(tmp, path)
            self._write(held, chunked)
            self._end(chunked)
        finally:
            if not ok:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return None

    def _passthrough(self, url: str) -> None:
        if self.ctx.offline:
            return self._simple(504, f"offline: {url}".encode())
        try:
            resp = self._open(url)
        except Exception as e:  # noqa: BLE001
            return self._upstream_error(e, url)
        with resp:
            length = resp.headers.get("Content-Length")
            chunked = self._start_stream(
                200,
                resp.headers.get("Content-Type", "application/octet-stream"),
                int(length) if length and length.isdigit() else None,
            )
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                self._write(chunk, chunked)
            self._end(chunked)
        return None

    def _metadata(self, url: str) -> None:
        ctx = self.ctx
        cache = ctx.cache
        info = cache.read_meta(url)
        body_path, _ = cache.meta_paths(url)
        fresh = info is not None and (time.time() - info.get("fetched_at", 0)) < ctx.ttl and not ctx.refresh
        if info is not None and (fresh or ctx.offline):
            return self._serve_meta(info, body_path)
        if ctx.offline:
            return self._simple(504, f"offline and not cached: {url}".encode())
        headers = {}
        if info is not None and info.get("status") == 200:
            if info.get("etag"):
                headers["If-None-Match"] = info["etag"]
            if info.get("last_modified"):
                headers["If-Modified-Since"] = info["last_modified"]
        try:
            resp = self._open(url, headers)
        except urllib.error.HTTPError as e:
            if e.code == 304 and info is not None:
                cache.touch_meta(url)
                return self._serve_meta(info, body_path)
            if e.code in (404, 410):
                cache.write_meta(url, {"url": url, "status": e.code, "fetched_at": time.time()})
            return self._upstream_error(e, url)
        except Exception as e:  # noqa: BLE001
            if info is not None:  # upstream down: serve stale
                return self._serve_meta(info, body_path)
            return self._upstream_error(e, url)
        fd, tmp = cache.tmp_file(".meta")
        ok = False
        try:
            with resp, os.fdopen(fd, "wb") as out:
                ctype = resp.headers.get("Content-Type", "application/octet-stream")
                length = resp.headers.get("Content-Length")
                length_i = int(length) if length and length.isdigit() else None
                chunked = self._start_stream(200, ctype, length_i)
                held = b""  # released only once the cache entry is committed
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    if held:
                        self._write(held, chunked)
                    held = chunk
                ok = True
            os.replace(tmp, body_path)
            cache.write_meta(
                url,
                {
                    "url": url,
                    "status": 200,
                    "content_type": ctype,
                    "etag": resp.headers.get("ETag"),
                    "last_modified": resp.headers.get("Last-Modified"),
                    "fetched_at": time.time(),
                },
            )
            self._write(held, chunked)
            self._end(chunked)
        finally:
            if not ok:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return None

    def _serve_meta(self, info: dict, body_path: Path) -> None:
        if info.get("status") != 200:
            return self._simple(int(info.get("status", 404)), b"not found (cached)")
        size = body_path.stat().st_size
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", info.get("content_type") or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            with open(body_path, "rb") as f:
                self._send_file(f, size)
        return None


def _silence_broken_pipes() -> None:
    # ThreadingHTTPServer prints tracebacks for clients that disconnect mid-stream.
    import socketserver

    orig = socketserver.BaseServer.handle_error

    def handle_error(self, request, client_address):  # type: ignore[no-untyped-def]
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, BrokenPipeError, socket.timeout, http.client.HTTPException)):
            return
        orig(self, request, client_address)

    socketserver.BaseServer.handle_error = handle_error  # type: ignore[method-assign]


_silence_broken_pipes()
