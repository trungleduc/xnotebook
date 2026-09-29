import hashlib
import http.client
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from xnb.cache import Cache
from xnb.proxy import Proxy

PKG = b"conda package bytes " * 5000
PKG_SHA = hashlib.sha256(PKG).hexdigest()
TAMPERED = b"evil" * 20000


class Upstream:
    """A fake channel server on loopback that counts requests."""

    def __init__(self):
        self.hits = {}
        self.etag = '"v1"'
        up = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                up.hits[self.path] = up.hits.get(self.path, 0) + 1
                if self.path == "/ch/noarch/good.conda":
                    return self._send(200, PKG)
                if self.path == "/ch/noarch/bad.conda":
                    return self._send(200, TAMPERED)
                if self.path == "/redirect.conda":
                    self.send_response(302)
                    self.send_header("Location", "/ch/noarch/good.conda")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/ch/noarch/repodata.json":
                    if self.headers.get("If-None-Match") == up.etag:
                        self.send_response(304)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    return self._send(200, b'{"packages": {}}', etag=up.etag)
                return self._send(404, b"nope")

            def _send(self, code, body, etag=None):
                self.send_response(code)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                if etag:
                    self.send_header("ETag", etag)
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream():
    u = Upstream()
    yield u
    u.close()


@pytest.fixture
def web(tmp_path):
    d = tmp_path / "web"
    d.mkdir()
    (d / "index.html").write_text("<html>ok</html>")
    (d / "kernel.worker.js").write_text("// worker")
    (tmp_path / "secret.txt").write_text("secret")
    return d


def make_proxy(tmp_path, web, upstream, **kw):
    p = Proxy(Cache(tmp_path / "cache"), web, allow_origins=[upstream.origin], **kw)
    return p.start()


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read(), r.headers


def test_token_required(tmp_path, web, upstream):
    p = make_proxy(tmp_path, web, upstream)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            get(f"{p.origin}/index.html")
        assert e.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as e:
            get(f"{p.origin}/wrongtoken/index.html")
        assert e.value.code == 403
        status, body, headers = get(p.base + "index.html")
        assert status == 200 and body == b"<html>ok</html>"
        assert "wasm-unsafe-eval" in headers["Content-Security-Policy"]
        _, _, wh = get(p.base + "kernel.worker.js")
        assert "connect-src 'none'" in wh["Content-Security-Policy"]
    finally:
        p.stop()


def test_static_traversal_blocked(tmp_path, web, upstream):
    p = make_proxy(tmp_path, web, upstream)
    try:
        for path in ("../secret.txt", "%2e%2e/secret.txt", "..%2fsecret.txt"):
            with pytest.raises(urllib.error.HTTPError) as e:
                get(p.base + path)
            assert e.value.code in (403, 404)
    finally:
        p.stop()


def test_only_loopback_bind(tmp_path, web, upstream):
    p = make_proxy(tmp_path, web, upstream)
    try:
        assert p._server.server_address[0] == "127.0.0.1"
    finally:
        p.stop()


def test_upstream_allowlist(tmp_path, web, upstream):
    p = make_proxy(tmp_path, web, upstream)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            get(p.upstream_url("https://example.com/x.conda"))
        assert e.value.code == 403
        assert "https://example.com/x.conda" in p.stats.denied
    finally:
        p.stop()


def test_package_lazy_cache(tmp_path, web, upstream):
    p = make_proxy(tmp_path, web, upstream)
    url = upstream.origin + "/ch/noarch/good.conda"
    p.expect(url, PKG_SHA)
    try:
        _, body, _ = get(p.upstream_url(url))
        assert body == PKG
        cached = tmp_path / "cache" / "pkgs" / PKG_SHA
        assert cached.read_bytes() == PKG
        _, body2, _ = get(p.upstream_url(url))
        assert body2 == PKG
        assert upstream.hits["/ch/noarch/good.conda"] == 1
        assert p.stats.package_hits == 1 and p.stats.package_misses == 1
        # A deleted entry is refetched lazily
        cached.unlink()
        get(p.upstream_url(url))
        assert upstream.hits["/ch/noarch/good.conda"] == 2
    finally:
        p.stop()
    assert not list((tmp_path / "cache" / "tmp").iterdir())


def test_same_sha_is_hit_for_other_url(tmp_path, web, upstream):
    p = make_proxy(tmp_path, web, upstream)
    url = upstream.origin + "/ch/noarch/good.conda"
    other = upstream.origin + "/redirect.conda"
    p.expect(url, PKG_SHA)
    p.expect(other, PKG_SHA)
    try:
        get(p.upstream_url(url))
        _, body, _ = get(p.upstream_url(other))
        assert body == PKG
        assert "/redirect.conda" not in upstream.hits
    finally:
        p.stop()


def test_redirect_followed_by_proxy(tmp_path, web, upstream):
    p = make_proxy(tmp_path, web, upstream)
    url = upstream.origin + "/redirect.conda"
    p.expect(url, PKG_SHA)
    try:
        status, body, _ = get(p.upstream_url(url))
        assert status == 200 and body == PKG
    finally:
        p.stop()


def test_tampered_package_rejected(tmp_path, web, upstream):
    p = make_proxy(tmp_path, web, upstream)
    url = upstream.origin + "/ch/noarch/bad.conda"
    p.expect(url, PKG_SHA)
    try:
        u = urllib.parse.urlsplit(p.upstream_url(url))
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
        conn.request("GET", u.path)
        resp = conn.getresponse()
        with pytest.raises((http.client.IncompleteRead, ConnectionError)):
            data = resp.read()
            # The final chunk is withheld: the client never gets the complete body.
            assert len(data) < len(TAMPERED)
            raise http.client.IncompleteRead(data)
    finally:
        p.stop()
    assert not (tmp_path / "cache" / "pkgs" / PKG_SHA).exists()
    assert not list((tmp_path / "cache" / "pkgs").iterdir())
    assert not list((tmp_path / "cache" / "tmp").iterdir())


def test_metadata_ttl_etag_offline(tmp_path, web, upstream):
    url = upstream.origin + "/ch/noarch/repodata.json"
    p = make_proxy(tmp_path, web, upstream, ttl=3600)
    try:
        get(p.upstream_url(url))
        get(p.upstream_url(url))
        assert upstream.hits["/ch/noarch/repodata.json"] == 1  # fresh: served from cache
    finally:
        p.stop()
    p = make_proxy(tmp_path, web, upstream, ttl=0)
    try:
        _, body, _ = get(p.upstream_url(url))  # stale: revalidated (304)
        assert body == b'{"packages": {}}'
        assert upstream.hits["/ch/noarch/repodata.json"] == 2
    finally:
        p.stop()
    p = make_proxy(tmp_path, web, upstream, ttl=0, offline=True)
    try:
        _, body, _ = get(p.upstream_url(url))  # offline: stale entry, no upstream
        assert body == b'{"packages": {}}'
        assert upstream.hits["/ch/noarch/repodata.json"] == 2
        with pytest.raises(urllib.error.HTTPError) as e:
            get(p.upstream_url(upstream.origin + "/ch/noarch/other.json"))
        assert e.value.code == 504
    finally:
        p.stop()


def test_404_passthrough_and_negative_cache(tmp_path, web, upstream):
    url = upstream.origin + "/ch/noarch/missing.json"
    p = make_proxy(tmp_path, web, upstream)
    try:
        for _ in range(2):
            with pytest.raises(urllib.error.HTTPError) as e:
                get(p.upstream_url(url))
            assert e.value.code == 404
        assert upstream.hits["/ch/noarch/missing.json"] == 1
    finally:
        p.stop()


def test_sealed_proxy_refuses_everything(tmp_path, web, upstream):
    p = make_proxy(tmp_path, web, upstream)
    url = upstream.origin + "/ch/noarch/good.conda"
    p.expect(url, PKG_SHA)
    get(p.upstream_url(url))
    p.seal()
    try:
        for target in (p.upstream_url(url), p.base + "index.html"):
            with pytest.raises(urllib.error.HTTPError) as e:
                get(target)
            assert e.value.code == 403
        assert p.stats.blocked_after_seal == 2
    finally:
        p.stop()


def test_options_preflight(tmp_path, web, upstream):
    p = make_proxy(tmp_path, web, upstream)
    try:
        req = urllib.request.Request(p.upstream_url(upstream.origin + "/x"), method="OPTIONS")
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 204
            assert r.headers["Access-Control-Allow-Origin"] == "*"
    finally:
        p.stop()


def test_concurrent_cold_downloads(tmp_path, web, upstream):
    p = make_proxy(tmp_path, web, upstream)
    url = upstream.origin + "/ch/noarch/good.conda"
    p.expect(url, PKG_SHA)
    results = []
    try:
        threads = [threading.Thread(target=lambda: results.append(get(p.upstream_url(url))[1])) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        p.stop()
    assert results == [PKG] * 8
    assert (tmp_path / "cache" / "pkgs" / PKG_SHA).read_bytes() == PKG
    assert not list((tmp_path / "cache" / "tmp").iterdir())
