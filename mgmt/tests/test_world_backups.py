"""world_backups.py — file-level world+plugin backup transfer. Uses a
real, ephemeral local http.server (same house style as portal/'s own
tests, per CLAUDE.md) rather than mocking httpx, since httpx.stream()
(the module-level convenience function world_backups.py calls) doesn't
accept an injectable transport in the version pinned here.
"""

from __future__ import annotations

import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from folia_mgmt.config import Settings
from folia_mgmt.models import World, WorldType
from folia_mgmt.world_backups import (
    BackupTransferError,
    backup_file_path,
    delete_backup_file,
    fetch_and_store_backup,
)


def _valid_tar_gz_bytes() -> bytes:
    import io

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        content = b"fake level.dat"
        info = tarfile.TarInfo(name="world/level.dat")
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class _FakeNodeServer:
    """A real local HTTP server standing in for folia-nexa-node's health
    server, just the one /backup route this module talks to."""

    def __init__(self, handler_fn):
        self._handler_fn = handler_fn
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer._handler_fn(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)


def _world(address: str | None) -> World:
    return World(
        name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1, address=address,
    )


def test_fetch_and_store_backup_writes_valid_tar_gz(tmp_path):
    content = _valid_tar_gz_bytes()

    def handler(req):
        req.send_response(200)
        req.send_header("Content-Type", "application/gzip")
        req.end_headers()
        req.wfile.write(content)

    server = _FakeNodeServer(handler)
    try:
        settings = Settings(state_dir=tmp_path, node_health_port=server.port)
        world = _world(f"127.0.0.1:{server.port}")
        size = fetch_and_store_backup(settings, world, "auto-123")
    finally:
        server.close()

    assert size == len(content)
    path = backup_file_path(settings, "world-a", "auto-123")
    assert path.read_bytes() == content
    with tarfile.open(path, "r:gz") as tf:
        assert tf.getnames() == ["world/level.dat"]


def test_fetch_and_store_backup_raises_on_non_200(tmp_path):
    def handler(req):
        req.send_response(500)
        req.end_headers()

    server = _FakeNodeServer(handler)
    try:
        settings = Settings(state_dir=tmp_path, node_health_port=server.port)
        world = _world(f"127.0.0.1:{server.port}")
        with pytest.raises(BackupTransferError, match="HTTP 500"):
            fetch_and_store_backup(settings, world, "auto-123")
    finally:
        server.close()

    assert not backup_file_path(settings, "world-a", "auto-123").exists()


def test_fetch_and_store_backup_raises_on_corrupt_stream(tmp_path):
    def handler(req):
        req.send_response(200)
        req.send_header("Content-Type", "application/gzip")
        req.end_headers()
        req.wfile.write(b"not actually a gzip tarball")

    server = _FakeNodeServer(handler)
    try:
        settings = Settings(state_dir=tmp_path, node_health_port=server.port)
        world = _world(f"127.0.0.1:{server.port}")
        with pytest.raises(BackupTransferError, match="isn't a valid tar.gz"):
            fetch_and_store_backup(settings, world, "auto-123")
    finally:
        server.close()

    # A truncated/corrupt download must not be left looking like a good backup.
    assert not backup_file_path(settings, "world-a", "auto-123").exists()


def test_fetch_and_store_backup_raises_when_connection_refused(tmp_path):
    settings = Settings(state_dir=tmp_path, node_health_port=1)  # nothing listening
    world = _world("127.0.0.1:1")
    with pytest.raises(BackupTransferError):
        fetch_and_store_backup(settings, world, "auto-123")


def test_fetch_and_store_backup_raises_when_world_has_no_address(tmp_path):
    settings = Settings(state_dir=tmp_path)
    world = _world(None)
    with pytest.raises(BackupTransferError, match="no known address"):
        fetch_and_store_backup(settings, world, "auto-123")


def test_delete_backup_file_removes_an_existing_file(tmp_path):
    settings = Settings(state_dir=tmp_path)
    path = backup_file_path(settings, "world-a", "auto-123")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")

    delete_backup_file(settings, "world-a", "auto-123")

    assert not path.exists()


def test_delete_backup_file_is_a_noop_for_a_missing_file(tmp_path):
    settings = Settings(state_dir=tmp_path)
    delete_backup_file(settings, "world-a", "does-not-exist")  # must not raise
