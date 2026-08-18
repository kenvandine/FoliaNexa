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

import collections
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8123

# History kept per broadcaster, same size as JVMRunner's own tail
# (runner.py's LOG_TAIL_LINES) — a new /logs/*/stream viewer gets this
# much backfill immediately on connect, not a blank screen.
HISTORY_LINES = 200

# Subscriber queue bound — a slow/stuck HTTP client must never be able to
# block the thread producing log lines (JVMRunner's output-drain thread,
# or whatever logs), so puts are non-blocking and just drop the line for
# that one subscriber if its queue is already full.
SUBSCRIBER_QUEUE_MAXSIZE = 500

# How often a stream write it forced even with no new lines — the only
# way this stdlib server notices a client went away is a failed write
# (BrokenPipeError/ConnectionResetError), so a fully idle world would
# otherwise never detect its viewers disconnected and leak subscriber
# queues forever.
KEEPALIVE_SECONDS = 20.0


class LogBroadcaster:
    """Fan-out for one log stream (console or agent) — a bounded history
    for new-subscriber backfill, plus live delivery to every connected
    HTTP stream via per-subscriber queues. Deliberately stdlib-only
    (threading/queue/collections), matching this module's existing
    constraint of staying light next to the JVM it runs alongside."""

    def __init__(self, maxlen: int = HISTORY_LINES) -> None:
        self._history: collections.deque[str] = collections.deque(maxlen=maxlen)
        self._subscribers: set[queue.Queue[str]] = set()
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        with self._lock:
            self._history.append(line)
            for q in self._subscribers:
                try:
                    q.put_nowait(line)
                except queue.Full:
                    pass  # slow subscriber misses a line rather than blocking the producer

    def subscribe(self) -> tuple[list[str], "queue.Queue[str]"]:
        """Atomic snapshot-and-register — avoids a line arriving between
        reading history and registering the queue (a gap) or the reverse
        (a duplicate at the boundary)."""
        q: queue.Queue[str] = queue.Queue(maxsize=SUBSCRIBER_QUEUE_MAXSIZE)
        with self._lock:
            history = list(self._history)
            self._subscribers.add(q)
        return history, q

    def unsubscribe(self, q: "queue.Queue[str]") -> None:
        with self._lock:
            self._subscribers.discard(q)


class BroadcastLogHandler(logging.Handler):
    """logging.Handler that fans formatted records out to a
    LogBroadcaster — additive next to the existing stdout handler
    (logging.basicConfig in agent.py), so `snap logs folia-nexa-node`
    keeps working unchanged; this just also makes the same lines
    available live over HTTP."""

    def __init__(self, broadcaster: LogBroadcaster) -> None:
        super().__init__()
        self._broadcaster = broadcaster

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._broadcaster.append(self.format(record))
        except Exception:  # noqa: BLE001 - a logging handler must never raise
            pass


@dataclass
class AgentState:
    world_name: str = ""
    phase: str = "starting"  # starting | running | crashed | stopped
    pid: int | None = None
    started_at: float = field(default_factory=time.time)
    last_exit_code: int | None = None
    log_tail: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    console_log: LogBroadcaster = field(default_factory=LogBroadcaster)
    agent_log: LogBroadcaster = field(default_factory=LogBroadcaster)

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

        def _stream_log(self, broadcaster: LogBroadcaster) -> None:
            history, q = broadcaster.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for line in history:
                    self.wfile.write((line + "\n").encode())
                self.wfile.flush()
                while True:
                    try:
                        line = q.get(timeout=KEEPALIVE_SECONDS)
                    except queue.Empty:
                        line = ""  # keepalive — also how a dead client gets noticed below
                    self.wfile.write((line + "\n").encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # client disconnected — fall through to unsubscribe
            finally:
                broadcaster.unsubscribe(q)

        def do_GET(self) -> None:  # noqa: N802 - stdlib method name
            if self.path == "/logs/console/stream":
                self._stream_log(state.console_log)
                return
            if self.path == "/logs/agent/stream":
                self._stream_log(state.agent_log)
                return
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
