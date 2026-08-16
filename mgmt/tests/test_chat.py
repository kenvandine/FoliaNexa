from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from helpers import auth_header


class _FakeDiscordWebhookServer:
    """A real local HTTP server standing in for Discord's webhook
    endpoint, so report_chat's outbound delivery is tested against a
    genuine HTTP round trip rather than a mocked httpx.post."""

    def __init__(self):
        received = self.received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                received.append(body)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/webhook"

    def close(self):
        self._server.shutdown()


@pytest.fixture
def fake_webhook():
    server = _FakeDiscordWebhookServer()
    yield server
    server.close()


def test_get_chat_config_requires_operator(client, viewer_token):
    resp = client.get("/api/v1/chat/config", headers=auth_header(viewer_token))
    assert resp.status_code == 403


def test_chat_config_defaults_empty(client, operator_token):
    resp = client.get("/api/v1/chat/config", headers=auth_header(operator_token))
    assert resp.status_code == 200
    assert resp.json() == {"server_wide_webhook_url": None, "world_webhook_urls": {}, "inbound_channels": {}}


def test_update_and_read_back_chat_config(client, operator_token):
    resp = client.put(
        "/api/v1/chat/config",
        json={
            "server_wide_webhook_url": "https://discord.com/api/webhooks/1/aaa",
            "world_webhook_urls": {"world-lobby": "https://discord.com/api/webhooks/2/bbb"},
            "inbound_channels": {"111": "world-lobby", "222": "*"},
        },
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    resp = client.get("/api/v1/chat/config", headers=auth_header(operator_token))
    assert resp.json() == {
        "server_wide_webhook_url": "https://discord.com/api/webhooks/1/aaa",
        "world_webhook_urls": {"world-lobby": "https://discord.com/api/webhooks/2/bbb"},
        "inbound_channels": {"111": "world-lobby", "222": "*"},
    }


def test_report_chat_delivers_to_both_world_and_server_wide_webhooks(client, operator_token, viewer_token, fake_webhook):
    client.put(
        "/api/v1/chat/config",
        json={
            "server_wide_webhook_url": fake_webhook.url,
            "world_webhook_urls": {"world-lobby": fake_webhook.url},
            "inbound_channels": {},
        },
        headers=auth_header(operator_token),
    )
    resp = client.post(
        "/api/v1/chat/report",
        json={"world": "world-lobby", "player": "Steve", "message": "hello"},
        headers=auth_header(viewer_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"delivered": 2}
    assert len(fake_webhook.received) == 2
    assert fake_webhook.received[0]["content"] == "hello"
    assert "Steve" in fake_webhook.received[0]["username"]


def test_report_chat_delivers_only_to_server_wide_when_world_has_no_webhook(
    client, operator_token, viewer_token, fake_webhook
):
    client.put(
        "/api/v1/chat/config",
        json={"server_wide_webhook_url": fake_webhook.url, "world_webhook_urls": {}, "inbound_channels": {}},
        headers=auth_header(operator_token),
    )
    resp = client.post(
        "/api/v1/chat/report",
        json={"world": "world-minigame", "player": "Alex", "message": "hi"},
        headers=auth_header(viewer_token),
    )
    assert resp.json() == {"delivered": 1}


def test_report_chat_with_no_config_delivers_nothing(client, viewer_token):
    resp = client.post(
        "/api/v1/chat/report",
        json={"world": "world-lobby", "player": "Steve", "message": "hello"},
        headers=auth_header(viewer_token),
    )
    assert resp.status_code == 200
    assert resp.json() == {"delivered": 0}


def test_relay_chat_queues_for_mapped_world_channel(client, operator_token, viewer_token):
    client.put(
        "/api/v1/chat/config",
        json={"world_webhook_urls": {}, "inbound_channels": {"111": "world-lobby"}},
        headers=auth_header(operator_token),
    )
    resp = client.post(
        "/api/v1/chat/relay",
        json={"channel_id": "111", "discord_username": "someone", "message": "hey"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200
    assert resp.json() == {"queued": True}

    pending = client.get("/api/v1/chat/pending", headers=auth_header(viewer_token))
    assert pending.json() == [{"world": "world-lobby", "author": "someone", "message": "hey"}]


def test_relay_chat_wildcard_channel_queues_with_null_world(client, operator_token, viewer_token):
    client.put(
        "/api/v1/chat/config",
        json={"world_webhook_urls": {}, "inbound_channels": {"222": "*"}},
        headers=auth_header(operator_token),
    )
    client.post(
        "/api/v1/chat/relay",
        json={"channel_id": "222", "discord_username": "someone", "message": "hey all"},
        headers=auth_header(operator_token),
    )
    pending = client.get("/api/v1/chat/pending", headers=auth_header(viewer_token))
    assert pending.json() == [{"world": None, "author": "someone", "message": "hey all"}]


def test_relay_chat_unconfigured_channel_is_not_queued(client, operator_token, viewer_token):
    resp = client.post(
        "/api/v1/chat/relay",
        json={"channel_id": "999", "discord_username": "someone", "message": "hey"},
        headers=auth_header(operator_token),
    )
    assert resp.json() == {"queued": False}
    pending = client.get("/api/v1/chat/pending", headers=auth_header(viewer_token))
    assert pending.json() == []


def test_pending_chat_drains_the_queue(client, operator_token, viewer_token):
    client.put(
        "/api/v1/chat/config",
        json={"world_webhook_urls": {}, "inbound_channels": {"111": "world-lobby"}},
        headers=auth_header(operator_token),
    )
    client.post(
        "/api/v1/chat/relay",
        json={"channel_id": "111", "discord_username": "someone", "message": "hey"},
        headers=auth_header(operator_token),
    )
    first = client.get("/api/v1/chat/pending", headers=auth_header(viewer_token))
    assert len(first.json()) == 1
    second = client.get("/api/v1/chat/pending", headers=auth_header(viewer_token))
    assert second.json() == []


def test_relay_chat_requires_operator(client, viewer_token):
    resp = client.post(
        "/api/v1/chat/relay",
        json={"channel_id": "111", "discord_username": "someone", "message": "hey"},
        headers=auth_header(viewer_token),
    )
    assert resp.status_code == 403
