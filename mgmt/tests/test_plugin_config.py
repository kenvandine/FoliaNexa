from __future__ import annotations

import pytest
from fastapi import HTTPException

from folia_mgmt.models import Host, World, WorldPhase, WorldType
from folia_mgmt.routers.plugin_config import _safe_relative_path

from helpers import auth_header


def _host(name="node-a"):
    return Host(name=name, address="10.0.1.10:8443", capacity_cpu_cores=8, capacity_memory_gb=16)


def _world(name="world-overworld", plugins=None, host_name="node-a", container_name="world-overworld"):
    return World(
        name=name,
        type=WorldType.overworld,
        cpu_cores=1,
        memory_gb=1,
        phase=WorldPhase.running,
        host_name=host_name,
        container_name=container_name,
        address="10.0.1.20:25565",
        plugins=plugins or ["LuckPerms"],
    )


def _files_url(world="world-overworld", plugin="LuckPerms", path=None):
    base = f"/api/v1/worlds/{world}/plugins/{plugin}/files"
    return f"{base}/{path}" if path else base


def test_list_files_merges_live_and_override(client, operator_token, viewer_token, db_session, fake_lxd):
    db_session.add(_host())
    db_session.add(_world())
    db_session.commit()

    fake_lxd.container_files[("node-a", "world-overworld", "/var/snap/folia-nexa-node/common/world/plugins/LuckPerms/config.yml")] = b"live: true"
    fake_lxd.container_files[("node-a", "world-overworld", "/var/snap/folia-nexa-node/common/world/plugins/LuckPerms/lang/en.yml")] = b"hello: world"

    put_resp = client.put(
        _files_url(path="lang/en.yml"), json={"content": "hello: overridden"}, headers=auth_header(operator_token)
    )
    assert put_resp.status_code == 200, put_resp.text
    assert put_resp.json()["pushed_live"] is True

    resp = client.get(_files_url(), headers=auth_header(viewer_token))
    assert resp.status_code == 200, resp.text
    files = {f["path"]: f["source"] for f in resp.json()["files"]}
    assert files == {"config.yml": "live", "lang/en.yml": "both"}


def test_get_file_prefers_override_over_live(client, operator_token, viewer_token, db_session, fake_lxd):
    db_session.add(_host())
    db_session.add(_world())
    db_session.commit()
    fake_lxd.container_files[("node-a", "world-overworld", "/var/snap/folia-nexa-node/common/world/plugins/LuckPerms/config.yml")] = b"live: true"

    client.put(_files_url(path="config.yml"), json={"content": "override: true"}, headers=auth_header(operator_token))

    resp = client.get(_files_url(path="config.yml"), headers=auth_header(viewer_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "override"
    assert body["content"] == "override: true"
    assert body["updated_by"] == "op"


def test_get_file_falls_back_to_live(client, viewer_token, db_session, fake_lxd):
    db_session.add(_host())
    db_session.add(_world())
    db_session.commit()
    fake_lxd.container_files[("node-a", "world-overworld", "/var/snap/folia-nexa-node/common/world/plugins/LuckPerms/config.yml")] = b"live: true"

    resp = client.get(_files_url(path="config.yml"), headers=auth_header(viewer_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "live"
    assert body["content"] == "live: true"


def test_get_file_detects_binary(client, viewer_token, db_session, fake_lxd):
    db_session.add(_host())
    db_session.add(_world())
    db_session.commit()
    fake_lxd.container_files[("node-a", "world-overworld", "/var/snap/folia-nexa-node/common/world/plugins/LuckPerms/data.bin")] = b"\xff\xfe\x00\x01"

    resp = client.get(_files_url(path="data.bin"), headers=auth_header(viewer_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_binary"] is True
    assert body["content"] is None


def test_get_file_404_when_neither_exists(client, viewer_token, db_session):
    db_session.add(_host())
    db_session.add(_world())
    db_session.commit()

    resp = client.get(_files_url(path="config.yml"), headers=auth_header(viewer_token))
    assert resp.status_code == 404


def test_put_file_rejects_undeclared_plugin(client, operator_token, db_session):
    db_session.add(_host())
    db_session.add(_world(plugins=["Spark"]))
    db_session.commit()

    resp = client.put(
        _files_url(plugin="LuckPerms", path="config.yml"),
        json={"content": "x"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 404


def test_put_file_not_pushed_live_when_unplaced(client, operator_token, db_session):
    world = _world(host_name=None, container_name=None)
    world.phase = WorldPhase.pending
    db_session.add(world)
    db_session.commit()

    resp = client.put(_files_url(path="config.yml"), json={"content": "x"}, headers=auth_header(operator_token))
    assert resp.status_code == 200
    assert resp.json()["pushed_live"] is False


def test_delete_reverts_override(client, operator_token, viewer_token, db_session, fake_lxd):
    db_session.add(_host())
    db_session.add(_world())
    db_session.commit()
    live_path = "/var/snap/folia-nexa-node/common/world/plugins/LuckPerms/config.yml"
    fake_lxd.container_files[("node-a", "world-overworld", live_path)] = b"live: true"
    # Simulate the live push failing so the container's own copy stays
    # untouched — proving DELETE reverts to the live file, not to whatever
    # was (or wasn't) last pushed, which is the actual contract being tested.
    fake_lxd.fail_push_for.add(live_path)
    put_resp = client.put(_files_url(path="config.yml"), json={"content": "override: true"}, headers=auth_header(operator_token))
    assert put_resp.json()["pushed_live"] is False

    resp = client.delete(_files_url(path="config.yml"), headers=auth_header(operator_token))
    assert resp.status_code == 200

    after = client.get(_files_url(path="config.yml"), headers=auth_header(viewer_token))
    assert after.json()["source"] == "live"
    assert after.json()["content"] == "live: true"


def test_delete_404_when_no_override(client, operator_token, db_session):
    db_session.add(_host())
    db_session.add(_world())
    db_session.commit()

    resp = client.delete(_files_url(path="config.yml"), headers=auth_header(operator_token))
    assert resp.status_code == 404


def test_write_endpoints_require_operator(client, viewer_token, db_session):
    db_session.add(_host())
    db_session.add(_world())
    db_session.commit()

    put_resp = client.put(_files_url(path="config.yml"), json={"content": "x"}, headers=auth_header(viewer_token))
    assert put_resp.status_code == 403

    delete_resp = client.delete(_files_url(path="config.yml"), headers=auth_header(viewer_token))
    assert delete_resp.status_code == 403


# -- path traversal (_safe_relative_path) ------------------------------------


def test_safe_relative_path_rejects_parent_traversal():
    with pytest.raises(HTTPException) as exc:
        _safe_relative_path("../../../etc/shadow")
    assert exc.value.status_code == 400


def test_safe_relative_path_rejects_absolute_path():
    with pytest.raises(HTTPException) as exc:
        _safe_relative_path("/etc/shadow")
    assert exc.value.status_code == 400


def test_safe_relative_path_rejects_bare_dotdot():
    with pytest.raises(HTTPException) as exc:
        _safe_relative_path("..")
    assert exc.value.status_code == 400


def test_safe_relative_path_rejects_current_dir():
    with pytest.raises(HTTPException) as exc:
        _safe_relative_path(".")
    assert exc.value.status_code == 400


def test_safe_relative_path_allows_nested_path():
    assert _safe_relative_path("lang/en.yml") == "lang/en.yml"


def test_safe_relative_path_collapses_internal_dotdot_within_bounds():
    # "a/b/../c.yml" never escapes the plugin's own folder — normalize it,
    # don't reject it.
    assert _safe_relative_path("a/b/../c.yml") == "a/c.yml"


def test_get_file_rejects_path_traversal_over_http(client, viewer_token, db_session):
    db_session.add(_host())
    db_session.add(_world())
    db_session.commit()

    resp = client.get(
        "/api/v1/worlds/world-overworld/plugins/LuckPerms/files/..%2F..%2F..%2Fetc%2Fshadow",
        headers=auth_header(viewer_token),
    )
    assert resp.status_code == 400


def test_put_file_rejects_path_traversal_over_http(client, operator_token, db_session):
    db_session.add(_host())
    db_session.add(_world())
    db_session.commit()

    resp = client.put(
        "/api/v1/worlds/world-overworld/plugins/LuckPerms/files/..%2F..%2Fescape.yml",
        json={"content": "pwned"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 400
