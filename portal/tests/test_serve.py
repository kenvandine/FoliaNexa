from __future__ import annotations

import threading
import urllib.request

import pytest

from folia_portal.serve import build_server


@pytest.fixture
def running_server(tmp_path):
    (tmp_path / "index.html").write_text("<html>hi</html>")
    (tmp_path / "portal.css").write_text("body { color: red; }")

    # port=0 lets the OS pick a free ephemeral port — avoids test flakiness
    # from a hardcoded port already being in use.
    server = build_server("127.0.0.1", 0, str(tmp_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _get(server, path: str) -> tuple[int, bytes]:
    port = server.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_serves_index_html_at_root(running_server):
    status, body = _get(running_server, "/")
    assert status == 200
    assert body == b"<html>hi</html>"


def test_serves_css_with_correct_content_type(running_server):
    port = running_server.server_address[1]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/portal.css", timeout=5) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/css"
        assert resp.read() == b"body { color: red; }"


def test_missing_file_returns_404(running_server):
    status, _ = _get(running_server, "/does-not-exist.html")
    assert status == 404


def test_does_not_escape_static_root(running_server):
    # SimpleHTTPRequestHandler normalizes ".." out of the path itself, so
    # this can never reach outside the directory passed to build_server —
    # confirms that guarantee holds for this server rather than assuming it.
    status, _ = _get(running_server, "/../../../etc/passwd")
    assert status == 404
