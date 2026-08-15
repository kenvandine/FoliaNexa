from __future__ import annotations

import json
import urllib.request

import pytest

from folia_node.health import AgentState, start_health_server


@pytest.fixture
def server():
    state = AgentState(world_name="world-nether")
    srv = start_health_server(state, port=0)  # OS-assigned free port
    yield srv, state
    srv.shutdown()


def _get(port: int, path: str):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_healthz_ok_while_starting(server):
    srv, _state = server
    status, body = _get(srv.server_port, "/healthz")
    assert status == 200
    assert body["status"] == "starting"


def test_healthz_reflects_crashed_phase(server):
    srv, state = server
    with state.lock:
        state.phase = "crashed"
    status, body = _get(srv.server_port, "/healthz")
    assert status == 503
    assert body["status"] == "crashed"


def test_metrics_reports_world_and_pid(server):
    srv, state = server
    with state.lock:
        state.pid = 12345
        state.phase = "running"
    status, body = _get(srv.server_port, "/metrics")
    assert status == 200
    assert body["world_name"] == "world-nether"
    assert body["pid"] == 12345
    assert body["phase"] == "running"


def test_unknown_path_404s(server):
    srv, _state = server
    status, _body = _get(srv.server_port, "/nope")
    assert status == 404
