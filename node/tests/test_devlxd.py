from __future__ import annotations

import httpx
import pytest

from folia_node.devlxd import DevLXDClient, DevLXDError

CONFIG_KEYS = [
    "/1.0/config/user.folia.world-name",
    "/1.0/config/user.folia.world-type",
    "/1.0/config/user.folia.jar-url",
    "/1.0/config/user.folia.plugins-manifest-url",
    "/1.0/config/user.folia.datapacks-manifest-url",
]

VALUES = {
    "/1.0/config/user.folia.world-name": "world-nether",
    "/1.0/config/user.folia.world-type": "nether",
    "/1.0/config/user.folia.jar-url": "https://artifacts.internal/folia/1.21.4/folia.jar",
    "/1.0/config/user.folia.plugins-manifest-url": "https://artifacts.internal/folia/manifests/world-nether.json",
    "/1.0/config/user.folia.datapacks-manifest-url": "https://artifacts.internal/folia/manifests/world-nether-datapacks.json",
}


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/1.0/config":
        return httpx.Response(200, json=CONFIG_KEYS)
    if request.url.path in VALUES:
        return httpx.Response(200, text=VALUES[request.url.path])
    return httpx.Response(404)


def _client() -> DevLXDClient:
    return DevLXDClient(transport=httpx.MockTransport(_handler))


def test_get_config_returns_all_user_keys():
    config = _client().get_config()
    assert config["user.folia.world-name"] == "world-nether"
    assert config["user.folia.jar-url"].endswith("folia.jar")


def test_get_world_assignment_parses_expected_fields():
    assignment = _client().get_world_assignment()
    assert assignment.world_name == "world-nether"
    assert assignment.world_type == "nether"
    assert assignment.jar_url.endswith("folia.jar")
    assert assignment.plugins_manifest_url.endswith("world-nether.json")
    assert assignment.datapacks_manifest_url.endswith("world-nether-datapacks.json")


def test_missing_required_key_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/1.0/config":
            return httpx.Response(200, json=["/1.0/config/user.folia.world-name"])
        return httpx.Response(200, text="world-nether")

    client = DevLXDClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DevLXDError):
        client.get_world_assignment()


def test_config_endpoint_failure_raises():
    client = DevLXDClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(DevLXDError):
        client.get_config()
