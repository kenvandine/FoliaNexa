from __future__ import annotations

import json
import queue
import tarfile
import urllib.error
import urllib.request
from io import BytesIO

import pytest

from folia_node.health import AgentState, LogBroadcaster, start_health_server


@pytest.fixture
def server():
    state = AgentState(world_name="world-nether")
    srv = start_health_server(state, port=0)  # OS-assigned free port
    yield srv, state
    srv.shutdown()


BACKUP_SECRET = "test-backup-secret"


@pytest.fixture
def server_with_world_dir(tmp_path):
    state = AgentState(world_name="world-nether", world_dir=tmp_path, backup_shared_secret=BACKUP_SECRET)
    srv = start_health_server(state, port=0)
    yield srv, state, tmp_path
    srv.shutdown()


def _get_bytes(port: int, path: str, headers: dict[str, str] | None = None) -> bytes:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read()


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


def _auth_headers(secret: str = BACKUP_SECRET) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def test_backup_archive_includes_world_and_plugins_dirs(server_with_world_dir):
    srv, _state, world_dir = server_with_world_dir
    (world_dir / "world").mkdir()
    (world_dir / "world" / "level.dat").write_bytes(b"fake level data")
    (world_dir / "plugins").mkdir()
    (world_dir / "plugins" / "SomePlugin.jar").write_bytes(b"fake jar bytes")

    raw = _get_bytes(srv.server_port, "/backup", headers=_auth_headers())

    with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as tf:
        names = set(tf.getnames())
    assert "world/level.dat" in names
    assert "plugins/SomePlugin.jar" in names


def test_backup_archive_skips_missing_directories(server_with_world_dir):
    # A world that hasn't generated a save yet (or has no plugins) must
    # still get a valid, if empty, archive back — not an error.
    srv, _state, _world_dir = server_with_world_dir

    raw = _get_bytes(srv.server_port, "/backup", headers=_auth_headers())

    with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as tf:
        assert tf.getnames() == []


def test_backup_archive_401s_without_authorization_header(server_with_world_dir):
    srv, _state, _world_dir = server_with_world_dir
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _get_bytes(srv.server_port, "/backup")
    assert exc_info.value.code == 401


def test_backup_archive_401s_with_wrong_secret(server_with_world_dir):
    srv, _state, _world_dir = server_with_world_dir
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _get_bytes(srv.server_port, "/backup", headers=_auth_headers("wrong-secret"))
    assert exc_info.value.code == 401


def test_backup_archive_401s_when_no_secret_configured(server):
    # A world whose container hasn't picked up the config key yet (e.g.
    # not restarted since mgmt started setting it) must fail closed, not
    # silently serve unauthenticated.
    srv, _state = server
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _get_bytes(srv.server_port, "/backup", headers=_auth_headers())
    assert exc_info.value.code == 401


# -- LogBroadcaster (pure unit, no HTTP) -------------------------------------


def test_log_broadcaster_subscribe_backfills_existing_history():
    b = LogBroadcaster()
    b.append("line 1")
    b.append("line 2")
    history, _q = b.subscribe()
    assert history == ["line 1", "line 2"]


def test_log_broadcaster_delivers_new_lines_to_subscribers():
    b = LogBroadcaster()
    _history, q = b.subscribe()
    b.append("live line")
    assert q.get(timeout=1) == "live line"


def test_log_broadcaster_unsubscribe_stops_delivery():
    b = LogBroadcaster()
    _history, q = b.subscribe()
    b.unsubscribe(q)
    b.append("should not arrive")
    with pytest.raises(queue.Empty):
        q.get(timeout=0.2)


def test_log_broadcaster_history_caps_at_maxlen():
    b = LogBroadcaster(maxlen=3)
    for i in range(5):
        b.append(str(i))
    history, _q = b.subscribe()
    assert history == ["2", "3", "4"]


def test_log_broadcaster_drops_line_for_full_subscriber_without_blocking():
    b = LogBroadcaster()
    _history, q = b.subscribe()
    # Fill the subscriber's queue past its bound — append must not raise
    # or block even though this one subscriber can never keep up.
    for i in range(600):
        b.append(str(i))
    assert q.qsize() <= 500


# -- /logs/*/stream over real HTTP -------------------------------------------


def _open_stream(port: int, path: str, timeout: float = 5.0):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    return urllib.request.urlopen(req, timeout=timeout)


def test_console_log_stream_backfills_history_then_live(server):
    srv, state = server
    state.console_log.append("boot line")
    resp = _open_stream(srv.server_port, "/logs/console/stream")
    try:
        assert resp.readline().decode().strip() == "boot line"
        state.console_log.append("live line")
        assert resp.readline().decode().strip() == "live line"
    finally:
        resp.close()


def test_agent_log_stream_is_independent_of_console_log_stream(server):
    srv, state = server
    state.agent_log.append("agent boot line")
    resp = _open_stream(srv.server_port, "/logs/agent/stream")
    try:
        assert resp.readline().decode().strip() == "agent boot line"
        state.console_log.append("should not appear on agent stream")
        state.agent_log.append("agent live line")
        # The next readable line on the agent stream must be the agent
        # line, not whatever landed on the (separate) console broadcaster.
        assert resp.readline().decode().strip() == "agent live line"
    finally:
        resp.close()
