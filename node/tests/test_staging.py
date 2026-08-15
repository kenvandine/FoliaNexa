from __future__ import annotations

import httpx

from folia_node.devlxd import WorldAssignment
from folia_node.staging import ensure_staged

JAR_BYTES = b"fake-jar-bytes"
PLUGIN_BYTES = b"fake-plugin-bytes"
DATAPACK_BYTES = b"fake-datapack-bytes"


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("folia.jar"):
        return httpx.Response(200, content=JAR_BYTES)
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


def test_ensure_staged_downloads_jar_and_plugins(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    jar_path = ensure_staged(tmp_path, _assignment(), client=client)

    assert jar_path.read_bytes() == JAR_BYTES
    assert (tmp_path / "plugins" / "HuskClaims.jar").read_bytes() == PLUGIN_BYTES
    assert (tmp_path / ".staged").exists()


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


def test_ensure_staged_without_plugins_manifest(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    jar_path = ensure_staged(tmp_path, _assignment(plugins_manifest_url=None), client=client)
    assert jar_path.read_bytes() == JAR_BYTES
    assert not (tmp_path / "plugins").exists()


def test_ensure_staged_downloads_datapacks_into_level_datapacks_dir(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    ensure_staged(
        tmp_path,
        _assignment(datapacks_manifest_url="https://artifacts.internal/folia/manifests/datapacks-manifest.json"),
        client=client,
    )
    assert (tmp_path / "world" / "datapacks" / "Matcha.zip").read_bytes() == DATAPACK_BYTES


def test_ensure_staged_without_datapacks_manifest(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    jar_path = ensure_staged(tmp_path, _assignment(datapacks_manifest_url=None), client=client)
    assert jar_path.read_bytes() == JAR_BYTES
    assert not (tmp_path / "world" / "datapacks").exists()
