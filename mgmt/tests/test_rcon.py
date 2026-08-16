from __future__ import annotations

import socket
import struct
import threading

import pytest

from folia_mgmt.rcon import RconError, execute_rcon_command

SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_RESPONSE_VALUE = 0


def _recv_exact(sock, n):
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_packet(sock):
    (length,) = struct.unpack("<i", _recv_exact(sock, 4))
    body = _recv_exact(sock, length)
    request_id, packet_type = struct.unpack("<ii", body[:8])
    text = body[8:-2].decode("utf-8")
    return request_id, packet_type, text


def _send_packet(sock, request_id, packet_type, body):
    payload = struct.pack("<ii", request_id, packet_type) + body.encode("utf-8") + b"\x00\x00"
    sock.sendall(struct.pack("<i", len(payload)) + payload)


class FakeMinecraftRconServer:
    """A real TCP server speaking the actual (simplified, Minecraft-style —
    no empty ack packet, no multi-packet splitting) RCON wire protocol, so
    execute_rcon_command is tested against genuine framing rather than a
    mocked socket."""

    def __init__(self, password: str, command_responses: dict[str, str] | None = None):
        self.password = password
        self.command_responses = command_responses or {}
        self.received_commands: list[str] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve_one, daemon=True)
        self._thread.start()

    def _serve_one(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            request_id, packet_type, password = _read_packet(conn)
            assert packet_type == SERVERDATA_AUTH
            if password != self.password:
                _send_packet(conn, -1, SERVERDATA_AUTH_RESPONSE, "")
                return
            _send_packet(conn, request_id, SERVERDATA_AUTH_RESPONSE, "")

            cmd_id, _packet_type, command = _read_packet(conn)
            self.received_commands.append(command)
            response = self.command_responses.get(command, f"ran: {command}")
            _send_packet(conn, cmd_id, SERVERDATA_RESPONSE_VALUE, response)

    def close(self):
        self._sock.close()


@pytest.fixture
def fake_rcon_server():
    servers = []

    def make(password="secret", command_responses=None):
        server = FakeMinecraftRconServer(password, command_responses)
        servers.append(server)
        return server

    yield make
    for s in servers:
        s.close()


def test_execute_rcon_command_returns_response(fake_rcon_server):
    server = fake_rcon_server(password="secret", command_responses={"gamerule doDaylightCycle false": "Game rule doDaylightCycle is now set to: false"})
    response = execute_rcon_command("127.0.0.1", server.port, "secret", "gamerule doDaylightCycle false")
    assert response == "Game rule doDaylightCycle is now set to: false"
    assert server.received_commands == ["gamerule doDaylightCycle false"]


def test_execute_rcon_command_wrong_password_raises(fake_rcon_server):
    server = fake_rcon_server(password="secret")
    with pytest.raises(RconError, match="authentication failed"):
        execute_rcon_command("127.0.0.1", server.port, "wrong", "list")


def test_execute_rcon_command_unreachable_host_raises():
    with pytest.raises(RconError, match="could not reach RCON"):
        execute_rcon_command("127.0.0.1", 1, "secret", "list", timeout=1.0)
