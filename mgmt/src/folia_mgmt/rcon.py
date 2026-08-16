"""Minimal Source RCON client, just enough to run one command against a
world's Paper/Folia server and get its response. PLAN.md §2/§9 — the
"live command" counterpart to PATCH /worlds/{name}'s restart-to-apply
config edits (server.properties changes need a restart; things like
/gamerule, /difficulty, /whitelist, /op don't).

Deliberately hand-rolled rather than a dependency: the protocol is ~20
lines of framing (see https://wiki.vg/RCON) and Minecraft's server
implementation is simpler than full Source RCON (no multi-packet
response splitting, no empty ack packet after auth) — a real library
would bring compatibility surface this doesn't need.
"""

from __future__ import annotations

import socket
import struct

SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0

_REQUEST_ID = 1


class RconError(RuntimeError):
    pass


def _send_packet(sock: socket.socket, request_id: int, packet_type: int, body: str) -> None:
    payload = struct.pack("<ii", request_id, packet_type) + body.encode("utf-8") + b"\x00\x00"
    sock.sendall(struct.pack("<i", len(payload)) + payload)


def _read_packet(sock: socket.socket) -> tuple[int, int, str]:
    length_bytes = _recv_exact(sock, 4)
    (length,) = struct.unpack("<i", length_bytes)
    body = _recv_exact(sock, length)
    request_id, packet_type = struct.unpack("<ii", body[:8])
    text = body[8:-2].decode("utf-8", errors="replace")  # strip the two trailing null bytes
    return request_id, packet_type, text


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise RconError("connection closed unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def execute_rcon_command(host: str, port: int, password: str, command: str, timeout: float = 10.0) -> str:
    """Connects, authenticates, runs one command, and disconnects — no
    connection pooling/reuse, this is an occasional operator action, not a
    hot path."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            _send_packet(sock, _REQUEST_ID, SERVERDATA_AUTH, password)
            auth_id, _auth_type, _ = _read_packet(sock)
            if auth_id == -1:
                raise RconError("RCON authentication failed (wrong password)")

            _send_packet(sock, _REQUEST_ID + 1, SERVERDATA_EXECCOMMAND, command)
            _, _, response = _read_packet(sock)
            return response
    except OSError as exc:
        raise RconError(f"could not reach RCON at {host}:{port}: {exc}") from exc
