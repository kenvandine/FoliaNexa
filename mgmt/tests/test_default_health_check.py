from __future__ import annotations

import http.server
import threading

import pytest

from folia_mgmt.config import Settings
from folia_mgmt.models import World, WorldType
from folia_mgmt.scheduler import default_health_check


class _Handler(http.server.BaseHTTPRequestHandler):
    status_code = 200

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(self.status_code)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def health_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def test_default_health_check_true_on_200(health_server):
    _Handler.status_code = 200
    port = health_server.server_port
    settings = Settings(node_health_port=port, node_health_timeout_seconds=2.0)
    # world.address's port (25565, the Minecraft port) is irrelevant to the
    # health check — only the host is reused; settings.node_health_port is
    # what actually gets dialed.
    world = World(name="w", type=WorldType.lobby, cpu_cores=1, memory_gb=1, address="127.0.0.1:25565")

    assert default_health_check(world, settings) is True


def test_default_health_check_false_on_non_200(health_server):
    _Handler.status_code = 503
    port = health_server.server_port
    settings = Settings(node_health_port=port, node_health_timeout_seconds=2.0)
    world = World(name="w", type=WorldType.lobby, cpu_cores=1, memory_gb=1, address="127.0.0.1:25565")

    assert default_health_check(world, settings) is False


def test_default_health_check_false_with_no_address():
    settings = Settings()
    world = World(name="w", type=WorldType.lobby, cpu_cores=1, memory_gb=1, address=None)
    assert default_health_check(world, settings) is False


def test_default_health_check_false_on_connection_refused():
    settings = Settings(node_health_port=1, node_health_timeout_seconds=1.0)
    world = World(name="w", type=WorldType.lobby, cpu_cores=1, memory_gb=1, address="127.0.0.1:9")
    assert default_health_check(world, settings) is False
