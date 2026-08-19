from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from helpers import auth_header
from sqlmodel import select

from folia_mgmt.models import World


class _FakeNodeLogServer:
    """A real local HTTP server standing in for folia-nexa-node's own
    /logs/{console,agent}/stream (health.py's LogBroadcaster) — same
    "genuine HTTP round trip, not a mocked httpx call" convention as
    test_chat.py's _FakeDiscordWebhookServer."""

    def __init__(self, body_by_path: dict[str, bytes], status_by_path: dict[str, int] | None = None):
        received_paths = self.received_paths = []
        status_by_path = status_by_path or {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received_paths.append(self.path)
                body = body_by_path.get(self.path, b"")
                self.send_response(status_by_path.get(self.path, 200))
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._server.shutdown()


def _create_and_place_world(client, operator_token, db_session, name, address):
    client.post(
        "/api/v1/worlds",
        json={"name": name, "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    world = db_session.exec(select(World).where(World.name == name)).one()
    world.address = address
    db_session.add(world)
    db_session.commit()


def test_console_log_proxies_node_stream(client, operator_token, viewer_token, db_session, monkeypatch):
    server = _FakeNodeLogServer({"/logs/console/stream": b"boot line\nlive line\n"})
    try:
        monkeypatch.setenv("FOLIA_MGMT_NODE_HEALTH_PORT", str(server.port))
        _create_and_place_world(client, operator_token, db_session, "world-overworld", "127.0.0.1:25565")
        resp = client.get("/api/v1/worlds/world-overworld/logs/console", headers=auth_header(viewer_token))
    finally:
        server.close()
    assert resp.status_code == 200, resp.text
    assert resp.text == "boot line\nlive line\n"
    assert server.received_paths == ["/logs/console/stream"]


def test_agent_log_proxies_a_different_node_path(client, operator_token, viewer_token, db_session, monkeypatch):
    server = _FakeNodeLogServer({"/logs/agent/stream": b"staging world 'x'\n"})
    try:
        monkeypatch.setenv("FOLIA_MGMT_NODE_HEALTH_PORT", str(server.port))
        _create_and_place_world(client, operator_token, db_session, "world-overworld", "127.0.0.1:25565")
        resp = client.get("/api/v1/worlds/world-overworld/logs/agent", headers=auth_header(viewer_token))
    finally:
        server.close()
    assert resp.status_code == 200, resp.text
    assert resp.text == "staging world 'x'\n"
    assert server.received_paths == ["/logs/agent/stream"]


def test_logs_require_placed_world(client, operator_token, viewer_token):
    client.post(
        "/api/v1/worlds",
        json={"name": "world-stuck", "type": "overworld", "cpu_cores": 999, "memory_gb": 999},
        headers=auth_header(operator_token),
    )
    resp = client.get("/api/v1/worlds/world-stuck/logs/console", headers=auth_header(viewer_token))
    assert resp.status_code == 409


def test_logs_404_for_unknown_world(client, viewer_token):
    resp = client.get("/api/v1/worlds/no-such-world/logs/console", headers=auth_header(viewer_token))
    assert resp.status_code == 404


def test_logs_surfaces_node_error_instead_of_passthrough(
    client, operator_token, viewer_token, db_session, monkeypatch
):
    # A node agent that predates the /logs endpoint (or otherwise errors)
    # must not have its error body silently forwarded as a 200 — that
    # renders in the log panel indistinguishable from real log content.
    server = _FakeNodeLogServer(
        {"/logs/console/stream": b'{"error": "not found"}'},
        status_by_path={"/logs/console/stream": 404},
    )
    try:
        monkeypatch.setenv("FOLIA_MGMT_NODE_HEALTH_PORT", str(server.port))
        _create_and_place_world(client, operator_token, db_session, "world-overworld", "127.0.0.1:25565")
        resp = client.get("/api/v1/worlds/world-overworld/logs/console", headers=auth_header(viewer_token))
    finally:
        server.close()
    assert resp.status_code == 502, resp.text
    assert "404" in resp.text


def test_logs_viewer_role_is_sufficient(client, operator_token, viewer_token, db_session, monkeypatch):
    # Unlike /rcon (an operator action), logs are read-only diagnostics —
    # the viewer role must be enough, no operator/admin escalation needed.
    server = _FakeNodeLogServer({"/logs/console/stream": b"ok\n"})
    try:
        monkeypatch.setenv("FOLIA_MGMT_NODE_HEALTH_PORT", str(server.port))
        _create_and_place_world(client, operator_token, db_session, "world-overworld", "127.0.0.1:25565")
        resp = client.get("/api/v1/worlds/world-overworld/logs/console", headers=auth_header(viewer_token))
    finally:
        server.close()
    assert resp.status_code == 200, resp.text
