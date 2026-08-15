"""Local /healthz + /metrics for mgmt to scrape. PLAN.md §9 step 4.

Deliberately stdlib-only (no FastAPI/uvicorn) — this runs alongside a JVM
that already wants the RAM/CPU headroom, so the agent itself should be as
light as possible.

TPS/tick-time/player-count aren't wired up yet: getting real numbers out of
a running Folia server needs an in-game bridge (RCON, or a small plugin
exposing metrics) that doesn't exist yet. Until then /metrics reports what
the agent itself can observe (process liveness, uptime, log tail) rather
than fabricating game-level numbers.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8123


@dataclass
class AgentState:
    world_name: str = ""
    phase: str = "starting"  # starting | running | crashed | stopped
    pid: int | None = None
    started_at: float = field(default_factory=time.time)
    last_exit_code: int | None = None
    log_tail: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "world_name": self.world_name,
                "phase": self.phase,
                "pid": self.pid,
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "last_exit_code": self.last_exit_code,
                "log_tail": list(self.log_tail[-20:]),
            }


def _make_handler(state: AgentState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
            pass  # quiet; mgmt polling every few seconds isn't worth logging

        def _write_json(self, status: int, body: dict) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - stdlib method name
            snapshot = state.snapshot()
            if self.path == "/healthz":
                ok = snapshot["phase"] in ("starting", "running")
                self._write_json(200 if ok else 503, {"status": snapshot["phase"]})
            elif self.path == "/metrics":
                self._write_json(200, snapshot)
            else:
                self._write_json(404, {"error": "not found"})

    return Handler


def start_health_server(state: AgentState, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
