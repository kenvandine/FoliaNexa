from __future__ import annotations

import http.server
import json
import threading

import pytest

from folia_mgmt.config import Settings
from folia_mgmt.models import World, WorldType
from folia_mgmt.scheduler import _record_restore_outcome


class _Handler(http.server.BaseHTTPRequestHandler):
    body: dict = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        payload = json.dumps(self.body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def metrics_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def test_records_a_failed_restore(metrics_server):
    # POST /{name}/backups/{id}/restore only confirms the tarball was
    # pushed and the container told to restart — this is the mechanism
    # that surfaces the *actual* extraction outcome (node/src/folia_node/
    # agent.py's _apply_pending_restore, reported via AgentState.snapshot's
    # last_restore_at/last_restore_error) back onto the World row.
    _Handler.body = {"last_restore_at": 1234567890.0, "last_restore_error": "tarball is corrupt"}
    settings = Settings(node_health_port=metrics_server.server_port, node_health_timeout_seconds=2.0)
    world = World(name="w", type=WorldType.overworld, cpu_cores=1, memory_gb=1)

    _record_restore_outcome(world, "127.0.0.1", settings)

    assert world.last_restore_error == "tarball is corrupt"
    assert world.last_restore_confirmed_at is not None


def test_records_a_successful_restore_as_no_error(metrics_server):
    _Handler.body = {"last_restore_at": 1234567890.0, "last_restore_error": None}
    settings = Settings(node_health_port=metrics_server.server_port, node_health_timeout_seconds=2.0)
    world = World(name="w", type=WorldType.overworld, cpu_cores=1, memory_gb=1, last_restore_error="stale error")

    _record_restore_outcome(world, "127.0.0.1", settings)

    assert world.last_restore_error is None
    assert world.last_restore_confirmed_at is not None


def test_leaves_fields_untouched_when_no_restore_happened(metrics_server):
    # last_restore_at absent/null means this boot didn't apply a restore
    # at all (the normal case for every ordinary placement/crash-restart
    # finalize_provisioning also runs through) — nothing to record.
    _Handler.body = {"last_restore_at": None, "last_restore_error": None}
    settings = Settings(node_health_port=metrics_server.server_port, node_health_timeout_seconds=2.0)
    world = World(name="w", type=WorldType.overworld, cpu_cores=1, memory_gb=1, last_restore_error="stale error")

    _record_restore_outcome(world, "127.0.0.1", settings)

    assert world.last_restore_error == "stale error"
    assert world.last_restore_confirmed_at is None


def test_swallows_connection_failure():
    settings = Settings(node_health_port=1, node_health_timeout_seconds=1.0)
    world = World(name="w", type=WorldType.overworld, cpu_cores=1, memory_gb=1)

    _record_restore_outcome(world, "127.0.0.1", settings)  # must not raise

    assert world.last_restore_error is None
    assert world.last_restore_confirmed_at is None
