from __future__ import annotations

import httpx

from folia_node.devlxd import WorldAssignment
from folia_node.staging import ensure_staged, sync_world_config

JAR_BYTES = b"fake-jar-bytes"
PLUGIN_BYTES = b"fake-plugin-bytes"
DATAPACK_BYTES = b"fake-datapack-bytes"


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("folia.jar"):
        return httpx.Response(200, content=JAR_BYTES)
    if url.endswith("server-properties-manifest"):
        return httpx.Response(200, json={"allow-flight": "true"})
    if url.endswith("datapacks-manifest.json"):
        return httpx.Response(200, json=[{"name": "Matcha", "url": "https://artifacts.internal/datapacks/matcha.zip"}])
    if url.endswith("manifest.json"):
        return httpx.Response(200, json=[{"name": "HuskClaims", "url": "https://artifacts.internal/plugins/huskclaims.jar"}])
    if url.endswith("huskclaims.jar"):
        return httpx.Response(200, content=PLUGIN_BYTES)
    if url.endswith("matcha.zip"):
        return httpx.Response(200, content=DATAPACK_BYTES)
    return httpx.Response(404)


def _assignment(**overrides) -> WorldAssignment:
    base = dict(
        world_name="world-nether",
        world_type="nether",
        jar_url="https://artifacts.internal/folia/1.21.4/folia.jar",
        plugins_manifest_url="https://artifacts.internal/folia/manifests/manifest.json",
    )
    base.update(overrides)
    return WorldAssignment(**base)


def test_ensure_staged_downloads_jar_and_accepts_eula(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    jar_path = ensure_staged(tmp_path, _assignment(), client=client)

    assert jar_path.read_bytes() == JAR_BYTES
    assert (tmp_path / "eula.txt").read_text() == "eula=true\n"
    assert (tmp_path / ".staged").exists()
    # plugins/datapacks/server.properties are sync_world_config's job now
    assert not (tmp_path / "plugins").exists()
    assert not (tmp_path / "server.properties").exists()


def test_ensure_staged_skips_redownload_when_marker_present(tmp_path):
    call_count = 0

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return _handler(request)

    client = httpx.Client(transport=httpx.MockTransport(counting_handler))
    ensure_staged(tmp_path, _assignment(), client=client)
    assert call_count > 0

    client2 = httpx.Client(transport=httpx.MockTransport(counting_handler))
    call_count = 0
    ensure_staged(tmp_path, _assignment(), client=client2)
    assert call_count == 0


def test_sync_world_config_downloads_plugins(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    sync_world_config(tmp_path, _assignment(), client=client)
    assert (tmp_path / "plugins" / "HuskClaims.jar").read_bytes() == PLUGIN_BYTES


def test_sync_world_config_without_plugins_manifest(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    sync_world_config(tmp_path, _assignment(plugins_manifest_url=None), client=client)
    assert not (tmp_path / "plugins").exists()


def test_sync_world_config_removes_plugin_no_longer_in_manifest(tmp_path):
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "OldPlugin.jar").write_bytes(b"stale")
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    sync_world_config(tmp_path, _assignment(), client=client)
    assert not (tmp_path / "plugins" / "OldPlugin.jar").exists()
    assert (tmp_path / "plugins" / "HuskClaims.jar").read_bytes() == PLUGIN_BYTES


def test_sync_world_config_does_not_redownload_existing_plugin(tmp_path):
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "HuskClaims.jar").write_bytes(b"already-here")

    def failing_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("huskclaims.jar"):
            raise AssertionError("should not re-download an already-staged plugin")
        return _handler(request)

    client = httpx.Client(transport=httpx.MockTransport(failing_handler))
    sync_world_config(tmp_path, _assignment(), client=client)
    assert (tmp_path / "plugins" / "HuskClaims.jar").read_bytes() == b"already-here"


def test_sync_world_config_downloads_datapacks_into_level_datapacks_dir(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    sync_world_config(
        tmp_path,
        _assignment(datapacks_manifest_url="https://artifacts.internal/folia/manifests/datapacks-manifest.json"),
        client=client,
    )
    assert (tmp_path / "world" / "datapacks" / "Matcha.zip").read_bytes() == DATAPACK_BYTES


def test_sync_world_config_without_datapacks_manifest(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    sync_world_config(tmp_path, _assignment(datapacks_manifest_url=None), client=client)
    assert not (tmp_path / "world" / "datapacks").exists()


def test_sync_world_config_writes_operator_properties_plus_required_baseline(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    sync_world_config(
        tmp_path,
        _assignment(
            plugins_manifest_url=None,
            server_properties_url="https://artifacts.internal/api/v1/worlds/world-nether/server-properties-manifest",
        ),
        client=client,
    )
    content = (tmp_path / "server.properties").read_text()
    assert "allow-flight=true" in content
    assert "online-mode=false" in content


def test_sync_world_config_preserves_paper_managed_keys_not_touched_by_overrides(tmp_path):
    # Simulates a world that's already booted at least once — Paper writes
    # many keys of its own (difficulty, motd, view-distance, ...) that
    # this codebase never templates and must not clobber on a later sync.
    (tmp_path / "server.properties").write_text(
        "online-mode=false\ndifficulty=normal\nmotd=A Minecraft Server\nview-distance=10\n"
    )
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    sync_world_config(
        tmp_path,
        _assignment(
            plugins_manifest_url=None,
            server_properties_url="https://artifacts.internal/api/v1/worlds/world-nether/server-properties-manifest",
        ),
        client=client,
    )
    content = (tmp_path / "server.properties").read_text()
    assert "difficulty=normal" in content
    assert "motd=A Minecraft Server" in content
    assert "view-distance=10" in content
    assert "allow-flight=true" in content  # the new override from _handler
    assert "online-mode=false" in content


def test_sync_world_config_without_server_properties_url_still_writes_required_baseline(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    sync_world_config(tmp_path, _assignment(plugins_manifest_url=None), client=client)
    assert (tmp_path / "server.properties").read_text() == "online-mode=false\n"


def test_sync_world_config_keeps_existing_properties_on_fetch_failure(tmp_path):
    (tmp_path / "server.properties").write_text("online-mode=false\nallow-flight=true\n")

    def failing_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("server-properties-manifest"):
            return httpx.Response(500)
        return _handler(request)

    client = httpx.Client(transport=httpx.MockTransport(failing_handler))
    sync_world_config(
        tmp_path,
        _assignment(
            plugins_manifest_url=None,
            server_properties_url="https://artifacts.internal/api/v1/worlds/world-nether/server-properties-manifest",
        ),
        client=client,
    )
    assert (tmp_path / "server.properties").read_text() == "online-mode=false\nallow-flight=true\n"


def test_sync_world_config_writes_baseline_on_fetch_failure_with_no_existing_file(tmp_path):
    def failing_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("server-properties-manifest"):
            return httpx.Response(500)
        return _handler(request)

    client = httpx.Client(transport=httpx.MockTransport(failing_handler))
    sync_world_config(
        tmp_path,
        _assignment(
            plugins_manifest_url=None,
            server_properties_url="https://artifacts.internal/api/v1/worlds/world-nether/server-properties-manifest",
        ),
        client=client,
    )
    assert (tmp_path / "server.properties").read_text() == "online-mode=false\n"
