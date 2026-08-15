from __future__ import annotations

import json

import httpx
import pytest

from folia_bot.mgmt_client import MgmtClient


@pytest.fixture
def mock_http(monkeypatch):
    """Patches httpx.AsyncClient the same way test_cli.py patches the sync
    one — routes every client mgmt_client.py constructs through a per-test
    handler instead of a real socket."""
    real_client_cls = httpx.AsyncClient

    def install(handler):
        def factory(*args, **kwargs):
            kwargs.pop("transport", None)
            return real_client_cls(*args, transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)

    return install


async def test_get_worlds_sends_bearer_token(mock_http):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers["Authorization"]
        captured["path"] = request.url.path
        return httpx.Response(200, json=[{"name": "world-overworld"}])

    mock_http(handler)
    client = MgmtClient("https://mgmt.example:8443", "tok-abc")
    worlds = await client.get_worlds()

    assert captured["auth"] == "Bearer tok-abc"
    assert captured["path"] == "/api/v1/worlds"
    assert worlds == [{"name": "world-overworld"}]


async def test_get_worlds_raises_on_error_status(mock_http):
    mock_http(lambda r: httpx.Response(401, json={"detail": "invalid token"}))
    client = MgmtClient("https://mgmt.example:8443", "bad-token")
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_worlds()


async def test_create_access_request_sends_expected_body(mock_http):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "pending"})

    mock_http(handler)
    client = MgmtClient("https://mgmt.example:8443", "tok-abc")
    result = await client.create_access_request(
        discord_user_id="123",
        discord_username="somebody#0",
        minecraft_username="Steve",
        auto_approve=True,
    )

    assert captured["body"] == {
        "discord_user_id": "123",
        "discord_username": "somebody#0",
        "minecraft_username": "Steve",
        "minecraft_uuid": None,
        "auto_approve": True,
    }
    assert result == {"status": "pending"}


async def test_create_access_request_sends_bedrock_uuid_when_given(mock_http):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "pending"})

    mock_http(handler)
    client = MgmtClient("https://mgmt.example:8443", "tok-abc")
    await client.create_access_request(
        discord_user_id="123",
        discord_username="somebody#0",
        minecraft_username="SomeBedrockGamertag",
        minecraft_uuid="00000000-0000-0000-0009-0009000000f4",
        auto_approve=False,
    )

    assert captured["body"]["minecraft_uuid"] == "00000000-0000-0000-0009-0009000000f4"


async def test_base_url_trailing_slash_is_stripped(mock_http):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=[])

    mock_http(handler)
    client = MgmtClient("https://mgmt.example:8443/", "tok")
    await client.get_worlds()
    assert captured["url"] == "https://mgmt.example:8443/api/v1/worlds"
